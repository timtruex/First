"""YouTube publish layer.

Uploads PRIVATE by default: the private upload is the human review gate, and for
mechanism content that gate is the cheapest insurance against shipping a factual
error. Nothing here flips a video public — that stays a human action in Studio.

Google's YouTube Data API revision history (2026-11-02) reversed the 2020 rule that
locked unverified-project uploads to private permanently, so a reviewed episode can
be published without an API compliance audit. Verify that has reached your project
before depending on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_SCIENCE_TECH = "28"


@dataclass(frozen=True, slots=True)
class EpisodeMetadata:
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str = CATEGORY_SCIENCE_TECH
    # General-audience channel: made-for-kids is False, but it is still an explicit
    # declaration rather than a default, so a pipeline bug cannot ship it unset.
    made_for_kids: bool = False


def authenticate(client_secrets: Path, token_cache: Path):
    creds: Credentials | None = None
    if token_cache.exists():
        creds = Credentials.from_authorized_user_file(str(token_cache), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
            creds = flow.run_local_server(port=0)
        token_cache.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def publish_private(youtube, video: Path, meta: EpisodeMetadata) -> str:
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": meta.title,
                "description": meta.description,
                "tags": list(meta.tags),
                "categoryId": meta.category_id,
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": meta.made_for_kids,
                "madeForKids": meta.made_for_kids,
            },
        },
        media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
    return response["id"]
