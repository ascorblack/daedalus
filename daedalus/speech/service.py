"""The local recogniser as the rest of the application sees it.

One object on the application, holding the models directory and the loaded engine, with the three
questions anyone asks it: is a local model in use, what are the words in this file, and give me a
stream to talk into. Everything that decides *whether* to use a local model decides it here, so the
precedence — local model, then the configured endpoint, then the browser's own recognition — is
written once and is the same on the voice page, in the composer and in Telegram.

Audio arrives in whatever the platform felt like sending: a Telegram voice note is Opus in an Ogg
container, the site's recorder sends WebM, the voice page's stream sends raw PCM. The models read
none of those. ``decode_file`` is the one place that converts, and it says plainly when the converter
is not installed rather than failing somewhere deeper as an empty transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import wave
from pathlib import Path

from daedalus.config import RuntimeConfig, SttConfig
from daedalus.speech import catalog
from daedalus.speech.engine import CACHE, SAMPLE_RATE, Engine, SpeechError, StreamSession
from daedalus.speech.models import Downloads

logger = logging.getLogger(__name__)

CONVERTER = "ffmpeg"
"""What turns a voice note into samples. Present in the runtime image; on a native install it comes
from the machine, and the doctor says so when it does not."""

CONVERT_TIMEOUT = 120.0
"""A conversion that has not finished by now is not going to; the file is longer than anything spoken."""


class LocalSpeech:
    """The local speech models: which is chosen, whether it is here, and what it hears.

    Held by the application for its lifetime. Loading is lazy — an installation that never selects a
    model never imports the engine — and the loaded model is dropped whenever the selection changes,
    so the process holds one at a time.
    """

    def __init__(self, state_dir: Path, config: RuntimeConfig) -> None:
        self.downloads = Downloads(state_dir / "models" / "stt")
        self.config = config

    # -- what is in use ---------------------------------------------------------------------

    @property
    def settings(self) -> SttConfig:
        return self.config.stt

    def selected(self) -> catalog.SpeechModel | None:
        """The chosen model, or None. An id that is no longer in the catalog counts as no choice."""
        chosen = self.settings.local_model
        if not chosen:
            return None
        try:
            return catalog.get(chosen)
        except KeyError:
            logger.warning("configured local speech model %r is not in the catalog; ignoring it", chosen)
            return None

    def active(self) -> catalog.SpeechModel | None:
        """The chosen model if it is actually installed — the only case in which anything local happens."""
        model = self.selected()
        return model if model is not None and self.downloads.is_installed(model.id) else None

    def available(self) -> bool:
        """Whether an utterance would be recognised here rather than sent anywhere."""
        return self.active() is not None

    def language(self) -> str:
        """The language the model is loaded for; empty where it chooses for itself."""
        value = (self.settings.local_language or "").strip().lower()
        return "" if value in ("", "auto") else value.split("-")[0]

    # -- using it ---------------------------------------------------------------------------

    async def engine(self) -> Engine:
        """The loaded model, loading it first if this is the first utterance since it was chosen."""
        model = self.active()
        if model is None:
            raise SpeechError("no local speech model is installed and selected")
        return await CACHE.get(
            model,
            self.downloads.directory(model.id),
            threads=self.settings.local_threads,
            language=self.language(),
        )

    async def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        """Raw samples as words."""
        return await (await self.engine()).transcribe(pcm16, sample_rate)

    async def transcribe_file(self, path: Path) -> str:
        """A recording in any container the converter reads, as words."""
        pcm, rate = await decode_file(path)
        if not pcm:
            raise SpeechError("the recording held no audio")
        return await self.transcribe(pcm, rate)

    async def session(self) -> StreamSession:
        """A stream to feed while the operator talks."""
        return (await self.engine()).session()

    def forget(self) -> None:
        """Drop the loaded model: the selection changed, or the files were deleted underneath it."""
        CACHE.drop()

    # -- what the page and the doctor show ----------------------------------------------------

    def state(self) -> dict[str, object]:
        """One line about local recognition, for the voice page's recogniser chip and ``/api/stt``."""
        model = self.selected()
        installed = model is not None and self.downloads.is_installed(model.id)
        return {
            "model": model.id if model else "",
            "label": model.label if model else "",
            "installed": installed,
            "active": installed,
            "streaming": bool(model and model.streaming),
            "language": self.settings.local_language,
            "loaded": CACHE.loaded(),
            "converter": converter_present(),
        }


def converter_present() -> bool:
    """Whether the thing that turns a voice note into samples is on this machine."""
    return shutil.which(CONVERTER) is not None


async def decode_file(path: Path) -> tuple[bytes, int]:
    """A recording as 16-bit mono samples, and their rate.

    A plain WAV is read here; anything else — Opus, WebM, m4a — goes through the converter, which is
    the only external program this feature needs. The engine resamples, so the converter is asked for
    the model's rate directly and the two never disagree.
    """
    with contextlib.suppress(OSError, wave.Error, EOFError):
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() == 2 and handle.getnchannels() == 1:
                return handle.readframes(handle.getnframes()), handle.getframerate()
    if not converter_present():
        raise SpeechError(
            f"this recording is not a plain WAV and {CONVERTER} is not installed, so it cannot be converted; "
            f"install {CONVERTER} or use a transcription endpoint instead"
        )
    process = await asyncio.create_subprocess_exec(
        CONVERTER, "-nostdin", "-loglevel", "error", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=CONVERT_TIMEOUT)
    except TimeoutError:
        process.kill()
        raise SpeechError("converting the recording took too long") from None
    if process.returncode:
        raise SpeechError(f"the recording could not be converted: {err.decode(errors='replace').strip()[:200]}")
    return out, SAMPLE_RATE


async def transcribe_recording(speech: LocalSpeech, config: RuntimeConfig, manager: object, path: Path) -> str:
    """The words in a recording, by the precedence the whole application shares.

    The local model first where one is installed and selected, and the configured endpoint otherwise.
    A local model that fails is not silently replaced by the endpoint: the operator chose it, an
    endpoint costs money and may not be configured at all, and a failure that hides itself behind a
    fallback is a failure nobody fixes. The message says which half refused.

    Raises ``TranscriptionError`` either way, so every call site keeps the one exception it already
    catches.
    """
    from daedalus.transport.telegram.voice import TranscriptionError, effective_asr, transcribe  # Lazy: the transport imports this module

    if speech.available():
        try:
            words = await speech.transcribe_file(path)
        except SpeechError as exc:
            raise TranscriptionError(f"the local speech model could not transcribe this: {exc}") from exc
        if not words:
            raise TranscriptionError("the local speech model heard nothing in this recording")
        return words
    return await transcribe(path, effective_asr(config.asr, manager))


def recogniser_available(speech: LocalSpeech, config: RuntimeConfig) -> bool:
    """Whether a recording can be turned into words at all, by either route."""
    from daedalus.transport.telegram.voice import asr_configured  # Lazy: the transport imports this module

    return speech.available() or asr_configured(config.asr)


__all__ = [
    "CONVERTER",
    "LocalSpeech",
    "converter_present",
    "decode_file",
    "recogniser_available",
    "transcribe_recording",
]
