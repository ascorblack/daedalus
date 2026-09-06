"""Voice notes → text through any OpenAI-compatible transcription endpoint."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from daedalus.config import AsrConfig

logger = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    pass


async def transcribe(path: Path, config: AsrConfig, *, client: httpx.AsyncClient | None = None) -> str:
    """Return the transcript of an audio file, or raise :class:`TranscriptionError`."""
    if not config.url:
        raise TranscriptionError("no speech-to-text endpoint is configured (settings → asr.url)")
    url = config.url.rstrip("/") + "/audio/transcriptions"
    headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    data = {"model": config.model, "response_format": "json"}
    if config.language:
        data["language"] = config.language
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15.0))
    try:
        with path.open("rb") as fh:
            response = await client.post(url, headers=headers, data=data, files={"file": (path.name, fh, "application/octet-stream")})
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"transcription request failed: {type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code >= 400:
        raise TranscriptionError(f"transcription endpoint answered HTTP {response.status_code}")
    try:
        text = str(response.json().get("text") or "").strip()
    except ValueError as exc:
        raise TranscriptionError("transcription endpoint returned no JSON") from exc
    if not text:
        raise TranscriptionError("empty transcript")
    return text


__all__ = ["TranscriptionError", "transcribe"]
