"""ElevenLabs text-to-speech with character-level timestamps.

One request per scene. Scene-local audio means a single line can be re-voiced without
re-rendering the rest, and scene durations come from real audio rather than an estimate.
previous_text / next_text are sent so prosody still flows across the cut points.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from pathlib import Path

import httpx

from ..config import Config
from ..models import VoiceScene, Word
from .timing import words_from_alignment

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class VoiceError(RuntimeError):
    pass


def synthesize_scenes(cfg: Config, narrations: list[str], out_dir: Path) -> list[VoiceScene]:
    """Voice every scene in order, reusing one HTTP connection."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[VoiceScene] = []
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        for i, text in enumerate(narrations):
            scene = _synthesize_one(
                cfg,
                client,
                index=i,
                text=text,
                previous_text=narrations[i - 1] if i > 0 else None,
                next_text=narrations[i + 1] if i + 1 < len(narrations) else None,
                out_dir=out_dir,
            )
            scenes.append(scene)
            log.info("voiced scene %d/%d (%.2fs)", i + 1, len(narrations), scene.duration)
    return scenes


def _synthesize_one(
    cfg: Config,
    client: httpx.Client,
    *,
    index: int,
    text: str,
    previous_text: str | None,
    next_text: str | None,
    out_dir: Path,
) -> VoiceScene:
    audio_path = out_dir / f"scene_{index:02d}.mp3"
    timing_path = out_dir / f"scene_{index:02d}.words.json"

    if audio_path.exists() and timing_path.exists():
        cached = json.loads(timing_path.read_text())
        if cached.get("text") == text:
            words = [Word(**w) for w in cached["words"]]
            return VoiceScene(index, audio_path, cached["duration"], words)

    voice = cfg.voice
    url = f"{voice.base_url}/v1/text-to-speech/{voice.voice_id}/with-timestamps"
    body = {
        "text": text,
        "model_id": voice.model_id,
        "voice_settings": {
            "stability": voice.stability,
            "similarity_boost": voice.similarity_boost,
            "style": voice.style,
            "use_speaker_boost": True,
            "speed": voice.speed,
        },
    }
    if previous_text:
        body["previous_text"] = previous_text
    if next_text:
        body["next_text"] = next_text

    payload = _post_with_retry(
        client,
        url,
        params={"output_format": voice.output_format},
        headers={
            "xi-api-key": voice.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body=body,
    )

    audio_b64 = payload.get("audio_base64")
    if not audio_b64:
        raise VoiceError(f"scene {index}: response carried no audio")
    audio_path.write_bytes(base64.b64decode(audio_b64))

    alignment = payload.get("alignment") or payload.get("normalized_alignment")
    if not alignment:
        raise VoiceError(f"scene {index}: response carried no alignment")
    words = words_from_alignment(
        alignment["characters"],
        alignment["character_start_times_seconds"],
        alignment["character_end_times_seconds"],
    )

    duration = max(
        probe_duration(cfg, audio_path),
        words[-1].end if words else 0.0,
    )
    if duration <= 0:
        raise VoiceError(f"scene {index}: could not determine audio duration")

    timing_path.write_text(
        json.dumps(
            {
                "text": text,
                "duration": duration,
                "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
            },
            indent=2,
        )
    )
    return VoiceScene(index, audio_path, duration, words)


def _post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict,
    headers: dict,
    json_body: dict,
) -> dict:
    delay = 2.0
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.post(url, params=params, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            last_error = f"transport: {exc}"
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                raise VoiceError("ElevenLabs rejected the API key (401)")
            if resp.status_code == 422:
                raise VoiceError(f"ElevenLabs rejected the request (422): {resp.text[:400]}")
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in RETRY_STATUSES:
                raise VoiceError(last_error)
            if retry_after := resp.headers.get("retry-after"):
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        if attempt < MAX_ATTEMPTS:
            log.warning("ElevenLabs attempt %d failed (%s); retrying in %.0fs", attempt, last_error, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    raise VoiceError(f"ElevenLabs failed after {MAX_ATTEMPTS} attempts: {last_error}")


def probe_duration(cfg: Config, path: Path) -> float:
    try:
        out = subprocess.run(
            [
                cfg.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0
