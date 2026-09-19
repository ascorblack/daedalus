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
import io
import logging
import shutil
import wave
from pathlib import Path

from daedalus.config import RuntimeConfig, SttConfig
from daedalus.speech import catalog
from daedalus.speech.engine import CACHE, SAMPLE_RATE, Engine, SpeechError, StreamSession
from daedalus.speech.models import Downloads

logger = logging.getLogger(__name__)

OPUS_DECODER = "opusdec"
"""What turns a Telegram voice note into samples. One and a quarter megabytes from opus-tools, which
is the whole reason it is preferred over ffmpeg here: ffmpeg brings four hundred and fifty megabytes
of video codecs into the image to decode a mono voice clip."""

CONVERTER = "ffmpeg"
"""The general case, for a recording in something other than WAV or Ogg Opus. Not in the runtime
image — see above — so this is what a native installation that happens to have it gets, and the
message below is what an installation without it gets."""

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

    def warm(self) -> dict[str, object]:
        """Start loading the chosen model now, and answer with where that got to.

        A model is half a gigabyte of weights and takes seconds to become a recogniser, and until this
        existed that cost was paid by the first thing the operator said: they tapped the microphone,
        talked, and the words arrived long afterwards or not at all. So the load is started at the two
        moments it is free — when the model is chosen, and when the voice page opens — and the page is
        told to wait rather than left to discover it.

        Returns at once. Nothing is warmed where there is no model, or no wheel to read one with;
        the state says so and the page falls back to whatever else can listen.
        """
        model = self.active()
        if model is None or not engine_present():
            return CACHE.state().as_json()
        return CACHE.warm(
            model,
            self.downloads.directory(model.id),
            threads=self.settings.local_threads,
            language=self.language(),
        ).as_json()

    def load_state(self) -> dict[str, object]:
        """Where the resident model is in its loading — ``idle``, ``loading``, ``ready`` or ``error``."""
        return CACHE.state().as_json()

    # -- what the page and the doctor show ----------------------------------------------------

    def state(self) -> dict[str, object]:
        """One line about local recognition, for the voice page's recogniser chip and ``/api/stt``.

        ``installed`` and ``active`` are different questions and the page needs both. The archive can
        be on the disk while the wheel that reads it is not — a native installation whose
        ``uv sync --extra speech`` failed is exactly that — and ``active`` is what the voice page uses
        to put the local model in front of the browser's own recogniser. Answering it on the archive
        alone takes a working browser recogniser away and replaces it with a stream that 503s.
        """
        model = self.selected()
        installed = model is not None and self.downloads.is_installed(model.id)
        return {
            "model": model.id if model else "",
            "label": model.label if model else "",
            "installed": installed,
            "active": installed and engine_present(),
            "engine_installed": engine_present(),
            "streaming": bool(model and model.streaming),
            "language": self.settings.local_language,
            "loaded": CACHE.loaded(),
            "load": CACHE.state().as_json(),
            "decoders": decoders(),
        }


_engine_present: bool | None = None


def engine_present() -> bool:
    """Whether the wheel that runs a model is importable here.

    Cached, because ``/api/voice`` is polled and an import probe on every poll is not free. Nothing
    invalidates it deliberately: installing the extra into a running process does not make it
    importable anyway — the installer restarts — and ``forget_engine`` exists for the tests that
    install a stub.
    """
    global _engine_present
    if _engine_present is None:
        try:
            import sherpa_onnx  # noqa: F401  # Lazy: the engine is an optional extra, and this asks whether it is here
        except ImportError:
            _engine_present = False
        else:
            _engine_present = True
    return _engine_present


def forget_engine() -> None:
    """Ask the import question again next time."""
    global _engine_present
    _engine_present = None


def converter_present() -> bool:
    """Whether a general converter is here. The Opus decoder is asked for separately; see :func:`decoders`."""
    return shutil.which(CONVERTER) is not None


def decoders() -> dict[str, bool]:
    """Which decoders this machine has, for the doctor line and the settings page."""
    return {"opus": shutil.which(OPUS_DECODER) is not None, "any": converter_present()}


def can_decode_recordings() -> bool:
    """Whether a voice note or a browser recording can be turned into samples at all."""
    found = decoders()
    return found["opus"] or found["any"]


def is_ogg(path: Path) -> bool:
    """Whether the file is an Ogg stream, by its magic bytes rather than by its name.

    Telegram sends a voice note as ``.oga``, the site sometimes as ``.ogg``, and an ``audio`` message
    can arrive with no useful extension at all, so the first four bytes are the only reliable answer.
    """
    with contextlib.suppress(OSError):
        with path.open("rb") as handle:
            return handle.read(4) == b"OggS"
    return False


async def _run(program: str, *args: str, feed: bytes | None = None) -> bytes:
    """One decoder, with its output collected. Raises :class:`SpeechError` with what it complained about."""
    process = await asyncio.create_subprocess_exec(
        program, *args,
        stdin=asyncio.subprocess.PIPE if feed is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(feed), timeout=CONVERT_TIMEOUT)
    except TimeoutError:
        process.kill()
        raise SpeechError("decoding the recording took too long") from None
    if process.returncode:
        raise SpeechError(f"the recording could not be decoded: {err.decode(errors='replace').strip()[:200]}")
    return out


def _wav_body(blob: bytes) -> tuple[bytes, int]:
    """The samples inside a WAV in memory, and their rate.

    Mono only, which is what every decoder here is asked for: opusdec is given ``--force-wav`` on a
    voice note, which is mono to begin with, and ffmpeg is given ``-ac 1``. A stereo WAV arriving
    would mean one of them was invoked wrongly, and the honest answer to that is the error below.
    """
    with contextlib.suppress(OSError, wave.Error, EOFError):
        with wave.open(io.BytesIO(blob), "rb") as handle:
            if handle.getsampwidth() == 2 and handle.getnchannels() == 1:
                return handle.readframes(handle.getnframes()), handle.getframerate()
    raise SpeechError("the decoder did not produce readable audio")


async def decode_file(path: Path) -> tuple[bytes, int]:
    """A recording as 16-bit mono samples, and their rate.

    Three routes, cheapest first. A plain WAV is read here with nothing shelled out to. An Ogg Opus
    stream — which is what a Telegram voice note always is, and what most browsers record — goes
    through opusdec, a megabyte of decoder that does exactly this one job. Anything else needs a
    general converter, and the runtime image deliberately does not carry one: ffmpeg would add four
    hundred and fifty megabytes of video codecs to decode a voice clip, so an installation that wants
    it installs it, and one that does not gets a message saying so rather than a mysterious silence.
    """
    with contextlib.suppress(OSError, wave.Error, EOFError):
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() == 2 and handle.getnchannels() == 1:
                return handle.readframes(handle.getnframes()), handle.getframerate()
    if is_ogg(path) and shutil.which(OPUS_DECODER):
        # opusdec writes a WAV to stdout; it resamples itself, so the rate asked for is the rate that
        # comes back and the engine has nothing left to do.
        blob = await _run(OPUS_DECODER, "--quiet", "--force-wav", "--rate", str(SAMPLE_RATE), str(path), "-")
        return _wav_body(blob)
    if converter_present():
        out = await _run(
            CONVERTER, "-nostdin", "-loglevel", "error", "-i", str(path),
            "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
        )
        return out, SAMPLE_RATE
    raise SpeechError(
        "this recording is not a plain WAV and nothing here can decode it: install opus-tools for "
        "voice notes, or ffmpeg for everything else, or use a transcription endpoint instead"
    )


async def transcribe_recording(speech: LocalSpeech, config: RuntimeConfig, manager: object, path: Path) -> str:
    """The words in a recording: Voice Notes endpoint first, local model only if none is set.

    The composer microphone and a Telegram voice note are the Voice Notes setting (an
    ``/audio/transcriptions`` endpoint). The local catalog model is the voice page's live listener;
    using it here stole those notes from the endpoint the operator picked. It remains the fallback
    so a machine with a zipformer and no key still has a microphone. A failure on the chosen route
    is not hidden behind the other.

    Raises ``TranscriptionError`` either way, so every call site keeps the one exception it already
    catches.
    """
    from daedalus.transport.telegram.voice import (  # Lazy: the transport imports this module
        TranscriptionError,
        asr_configured,
        effective_asr,
        transcribe,
    )

    if asr_configured(config.asr):
        return await transcribe(path, effective_asr(config.asr, manager))
    if speech.available():
        try:
            words = await speech.transcribe_file(path)
        except SpeechError as exc:
            raise TranscriptionError(f"the local speech model could not transcribe this: {exc}") from exc
        if not words:
            raise TranscriptionError("the local speech model heard nothing in this recording")
        return words
    raise TranscriptionError("no speech-to-text endpoint is configured")


def recogniser_available(speech: LocalSpeech, config: RuntimeConfig) -> bool:
    """Whether a recording can be turned into words at all, by either route."""
    from daedalus.transport.telegram.voice import asr_configured  # Lazy: the transport imports this module

    return speech.available() or asr_configured(config.asr)


__all__ = [
    "CONVERTER",
    "OPUS_DECODER",
    "can_decode_recordings",
    "decoders",
    "LocalSpeech",
    "converter_present",
    "decode_file",
    "engine_present",
    "forget_engine",
    "recogniser_available",
    "transcribe_recording",
]
