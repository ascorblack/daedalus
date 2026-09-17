"""The local voice as the rest of the application sees it.

One object on the application, holding the voices directory and the loaded synthesiser, with the
questions anyone asks it: is a voice of ours in use, read this aloud, and what should the page be
told. Everything that decides *whether* to speak here decides it in :meth:`LocalTts.available`, so the
precedence — a downloaded voice, then the configured endpoint, then the browser's own synthesiser — is
written once and is the same wherever speech comes out.

The audio leaves as a complete clip rather than a stream. The page fetches it into a blob before it
plays anything, so streaming the response would buy nothing there; what does buy something is that
the clip is built a sentence at a time (:meth:`TtsEngine.stream`), which keeps the memory to one
sentence and lets a cancelled request stop at the next boundary instead of at the end.

Ogg Opus where ``opusenc`` is present — it is, in the runtime image, for the recognition side's sake —
and WAV otherwise. A sentence is about two hundred kilobytes as WAV and twenty as Opus, which matters
on a phone and not at all on a desktop, so the encoder is used when it is there and never required.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from daedalus.config import RuntimeConfig, TtsConfig
from daedalus.speech import tts_catalog as catalog
from daedalus.speech.models import DownloadError, Downloads
from daedalus.speech.tts_catalog import TtsVoice
from daedalus.speech.tts_engine import CACHE, TtsEngine, TtsError, resolve, wav

logger = logging.getLogger(__name__)

ENCODER = "opusenc"
"""The Opus encoder, from opus-tools — the same 1.3 MB package the recognition side needs ``opusdec``
from, so nothing new enters the image for this."""

ENCODE_TIMEOUT = 60.0
"""An encode that has not finished by now is not going to; the input is one answer read aloud."""

OPUS_BITRATE = "32"
"""Kilobits per second. Speech from a synthesiser is clean mono and does not need more; this is about a
tenth of the WAV and still above what anyone can hear the difference of on a phone speaker."""

WAV_TYPE = "audio/wav"
OPUS_TYPE = "audio/ogg"


def check_voice(directory: Path, voice: TtsVoice) -> None:
    """Whether an unpacked archive is a loadable voice, as the download manager asks it.

    Imported through :func:`resolve` rather than loading the model: this runs at the end of every
    download, including on an installation whose engine wheel is not installed yet.
    """
    try:
        resolve(directory, voice.kind)
    except TtsError as exc:
        raise DownloadError(str(exc)) from exc


class LocalTts:
    """The local voices: which is chosen, whether it is here, and what it sounds like.

    Held by the application for its lifetime. Loading is lazy — an installation that never chooses a
    voice never imports the engine — and the loaded voice is dropped whenever the choice changes, so
    the process holds one at a time.
    """

    def __init__(self, state_dir: Path, config: RuntimeConfig) -> None:
        self.downloads = Downloads(state_dir / "models" / "tts", lookup=catalog.get, resolver=check_voice)
        self.config = config
        self._state = "idle"
        """``idle``, ``loading``, ``ready`` or ``error`` — what the page's chip says. Held here rather
        than derived, because "the voice is loading" is a fact about this second and the cache can only
        answer whether a load has already finished."""
        self._error = ""

    # -- what is in use ---------------------------------------------------------------------

    @property
    def settings(self) -> TtsConfig:
        return self.config.voice.tts

    def selected(self) -> TtsVoice | None:
        """The chosen voice, or None. An id that is no longer in the catalog counts as no choice."""
        chosen = self.settings.local_voice
        if not chosen:
            return None
        try:
            return catalog.get(chosen)
        except KeyError:
            logger.warning("configured local voice %r is not in the catalog; ignoring it", chosen)
            return None

    def active(self) -> TtsVoice | None:
        """The chosen voice if it is actually installed — the only case in which anything local speaks."""
        voice = self.selected()
        return voice if voice is not None and self.downloads.is_installed(voice.id) else None

    def available(self) -> bool:
        """Whether an answer would be spoken here rather than sent anywhere."""
        return self.active() is not None

    def speaker(self) -> str:
        return (self.settings.local_speaker or "").strip()

    def speed(self) -> float:
        return self.settings.local_speed

    # -- using it ---------------------------------------------------------------------------

    async def engine(self) -> TtsEngine:
        """The loaded voice, loading it first if this is the first sentence since it was chosen."""
        voice = self.active()
        if voice is None:
            raise TtsError("no voice is installed and selected")
        if CACHE.loaded() != voice.id:
            self._state, self._error = "loading", ""
        try:
            engine = await CACHE.get(voice, self.downloads.directory(voice.id), threads=self.settings.local_threads)
        except Exception as exc:
            self._state, self._error = "error", str(exc)
            raise
        self._state, self._error = "ready", ""
        return engine

    async def warm(self) -> None:
        """Load the voice now, so the first thing said is not also the first thing loaded.

        Called when a voice is chosen. A failure is recorded and not raised: choosing a voice should
        report what went wrong on the page, not fail the request that chose it.
        """
        try:
            await self.engine()
        except Exception as exc:  # noqa: BLE001 - the state carries the reason; the caller carries on
            logger.warning("the local voice could not be loaded: %s", exc)

    async def speak(self, text: str) -> tuple[bytes, int]:
        """A piece of text as samples and their rate, in the chosen voice."""
        engine = await self.engine()
        return await engine.speak(text, speaker=self.speaker(), speed=self.speed())

    async def audio(self, text: str) -> tuple[bytes, str]:
        """The same, as a file a browser plays, and its media type."""
        pcm, rate = await self.speak(text)
        if not pcm:
            raise TtsError("there was nothing to say")
        return await encode(pcm, rate)

    async def sample(self, voice_id: str) -> tuple[bytes, str]:
        """One short phrase in a voice's own language, so it can be heard before it is chosen.

        Loads the voice being sampled, which means sampling a second voice drops the first — that is
        the same single-resident rule everything else here follows, and hearing two voices at once is
        not something anybody asks for.
        """
        voice = catalog.get(voice_id)
        if not self.downloads.is_installed(voice.id):
            raise TtsError(f"{voice.label} is not downloaded yet")
        engine = await CACHE.get(voice, self.downloads.directory(voice.id), threads=self.settings.local_threads)
        speaker = self.speaker() if voice.id == self.settings.local_voice else ""
        pcm, rate = await engine.speak(voice.sample(), speaker=speaker, speed=self.speed())
        if not pcm:
            raise TtsError(f"{voice.label} produced no audio for its own sample")
        return await encode(pcm, rate)

    def forget(self) -> None:
        """Drop the loaded voice: the choice changed, or the files were deleted underneath it."""
        CACHE.drop()
        self._state, self._error = "idle", ""

    # -- what the page and the doctor show ----------------------------------------------------

    def state(self) -> dict[str, object]:
        """One line about local speech, for the voice page and for ``/api/tts``.

        ``installed`` and ``active`` are different questions and the page needs both: the archive can
        be on the disk while the wheel that reads it is not, which is what a native installation whose
        ``uv sync --extra speech`` failed looks like.
        """
        from daedalus.speech.service import engine_present  # Lazy: one import probe, cached, shared with recognition

        voice = self.selected()
        installed = voice is not None and self.downloads.is_installed(voice.id)
        engine = engine_present()
        error = self._error
        if voice is None and self.settings.local_voice:
            # A configured id the catalog no longer has. Nothing local speaks, but "idle" would say
            # the operator never chose one, which is the opposite of what happened.
            phase = "error"
            error = f"{self.settings.local_voice} is not in the catalog any more; choose a voice again"
        elif voice is None:
            phase = "idle"
        elif installed and engine:
            phase = self._state
        else:
            phase = "error"
            error = error or ("the speech engine is not installed" if installed else f"{voice.label} was never downloaded")
        return {
            "voice": voice.id if voice else "",
            "label": voice.label if voice else "",
            "language": voice.language if voice else "",
            "speaker": self.speaker(),
            "speed": self.speed(),
            "threads": self.settings.local_threads,
            "installed": installed,
            "active": installed and engine,
            "engine_installed": engine,
            "state": phase,
            "error": error,
            "loaded": CACHE.loaded(),
            "encoder": encoder_present(),
        }


def encoder_present() -> bool:
    """Whether Opus can be produced here. False only means bigger clips, never no clips."""
    return shutil.which(ENCODER) is not None


async def encode(pcm16: bytes, rate: int) -> tuple[bytes, str]:
    """Samples as a playable file: Ogg Opus where the encoder is here, WAV where it is not.

    An encoder that is present and fails still produces audio — the WAV is already in hand — because a
    silent voice page is a much worse outcome than a clip ten times larger than it needed to be.
    """
    if not encoder_present():
        return wav(pcm16, rate), WAV_TYPE
    try:
        process = await asyncio.create_subprocess_exec(
            ENCODER, "--quiet", "--raw", "--raw-bits", "16", "--raw-rate", str(rate),
            "--raw-chan", "1", "--bitrate", OPUS_BITRATE, "-", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(pcm16), timeout=ENCODE_TIMEOUT)
        if process.returncode or not out:
            logger.warning("opusenc refused the audio (%s); sending WAV instead", err.decode(errors="replace").strip()[:200])
            return wav(pcm16, rate), WAV_TYPE
        return out, OPUS_TYPE
    except (TimeoutError, OSError) as exc:
        logger.warning("opusenc could not be run (%s); sending WAV instead", exc)
        return wav(pcm16, rate), WAV_TYPE


__all__ = [
    "ENCODER",
    "OPUS_TYPE",
    "WAV_TYPE",
    "LocalTts",
    "check_voice",
    "encode",
    "encoder_present",
]
