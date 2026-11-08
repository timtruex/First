"""Upload finished cuts to YouTube as private drafts.

The point is to keep the file off the phone. Rendering, uploading and the 50MB Telegram
ceiling all stay on the Mac; the phone opens YouTube Studio, checks the title and
thumbnail, and publishes. Nothing is ever made public from here - privacy_status is
always sent explicitly, because since November 2026 omitting it makes an upload public
by default, and an unreviewed affiliate video going live unattended is the one outcome
worth engineering against.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, ConfigError
from ..publish.metadata import VideoMetadata

log = logging.getLogger(__name__)

# youtube.upload covers videos.insert and thumbnails.set.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRY_STATUSES = {500, 502, 503, 504}
MAX_ATTEMPTS = 5
CHUNK_BYTES = 8 * 1024 * 1024  # resumable chunks, so a dropped connection resumes
PRIVACY = ("private", "unlisted", "public")


class YouTubeError(RuntimeError):
    pass


@dataclass
class Upload:
    video_id: str
    url: str
    privacy: str
    title: str


def _require_libs():
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.http import MediaFileUpload  # noqa: F401
    except ImportError as exc:
        raise YouTubeError(
            "the YouTube client libraries are not installed. Run:\n"
            '    ./.venv/bin/pip install -e ".[youtube]"'
        ) from exc


def authorize(cfg: Config, *, interactive: bool = False):
    """Load stored credentials, refreshing or running the consent flow as needed."""
    _require_libs()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(cfg.youtube.get("token_path", "~/.config/amzvid/youtube-token.json")).expanduser()
    secrets_path = Path(cfg.youtube.get("client_secrets", "")).expanduser()

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, KeyError) as exc:
            log.warning("stored YouTube token is unreadable (%s); re-authorizing", exc)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(token_path, creds)
            return creds
        except Exception as exc:
            log.warning("refreshing the YouTube token failed (%s); re-authorizing", exc)

    if not interactive:
        raise YouTubeError(
            "no usable YouTube credentials. Run this once, on the Mac, with a browser:\n"
            "    ./.venv/bin/amzvid youtube-auth"
        )

    if not secrets_path.exists():
        raise ConfigError(
            f"[youtube] client_secrets points at {secrets_path}, which does not exist. "
            "Download the OAuth client JSON from Google Cloud Console and put it there."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save(token_path, creds)
    return creds


def _save(path: Path, creds) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    path.chmod(0o600)  # it is a long-lived credential for the channel
    log.info("saved YouTube credentials to %s", path)


def upload_video(
    cfg: Config,
    path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    thumbnail: Path | None = None,
) -> Upload:
    _require_libs()
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    privacy = str(cfg.youtube.get("privacy_status", "private")).strip().lower()
    if privacy not in PRIVACY:
        raise ConfigError(f"[youtube] privacy_status must be one of {', '.join(PRIVACY)}")

    creds = authorize(cfg)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": str(cfg.youtube.get("category_id", "22")),  # 22 = People & Blogs
        },
        "status": {
            # Always explicit. Omitting this publishes the video.
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(cfg.youtube.get("made_for_kids", False)),
        },
    }

    media = MediaFileUpload(str(path), chunksize=CHUNK_BYTES, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = _resumable(request, path.name)
    video_id = response.get("id")
    if not video_id:
        raise YouTubeError(f"upload of {path.name} returned no video id: {response}")

    got = response.get("status", {}).get("privacyStatus", "unknown")
    if got != privacy:
        log.warning("%s uploaded as %r, not the requested %r", path.name, got, privacy)

    if thumbnail and thumbnail.exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
            ).execute()
        except HttpError as exc:
            # A custom thumbnail needs a verified channel; the video is still fine.
            log.warning("thumbnail for %s rejected: %s", video_id, exc)

    return Upload(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        privacy=got,
        title=title,
    )


def _resumable(request, label: str) -> dict:
    """Drive a resumable upload, retrying the transient failures."""
    from googleapiclient.errors import HttpError

    response = None
    attempt = 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if progress:
                log.info("uploading %s: %d%%", label, int(progress.progress() * 100))
        except HttpError as exc:
            if exc.resp.status not in RETRY_STATUSES:
                raise YouTubeError(f"YouTube rejected {label}: {exc}") from exc
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise YouTubeError(f"gave up uploading {label} after {attempt} attempts") from exc
            delay = min(60.0, (2 ** attempt) + random.random())
            log.warning("%s: HTTP %s, retrying in %.0fs", label, exc.resp.status, delay)
            time.sleep(delay)
        except OSError as exc:
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise YouTubeError(f"upload of {label} failed: {exc}") from exc
            time.sleep(min(60.0, 2 ** attempt))
    return response


def upload_all(cfg: Config, deliverables, meta: VideoMetadata, out_dir: Path) -> list[Upload]:
    """Upload the long cut and its Shorts, each with its own title and description."""
    from .metadata import shorts_description

    thumbnail = out_dir / "thumbnail.jpg"
    uploads: list[Upload] = []

    for item in deliverables:
        if item.kind == "long":
            title, description, tags = meta.title, meta.description, meta.tags
            thumb = thumbnail if thumbnail.exists() else None
        else:
            title = item.title or meta.title
            description = (out_dir / f"{item.path.stem.split('_')[-1]}.description.txt")
            description = description.read_text() if description.exists() else meta.description
            tags, thumb = meta.tags, None

        log.info("uploading %s to YouTube", item.path.name)
        uploads.append(
            upload_video(
                cfg, item.path, title=title, description=description, tags=tags, thumbnail=thumb
            )
        )
    return uploads
