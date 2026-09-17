"""Loading a downloaded voice and turning words into audio.

Everything in here is CPU-bound and blocking, and none of it may run on the event loop: a voice takes
a second or two to load and a paragraph takes about as long again to speak. The public surface is
therefore async and every call crosses into a worker thread, while the loaded voice is cached —
loading Piper for each sentence would cost more than synthesising it.

Two ways out, because there are two things the answer is wanted for. :meth:`TtsEngine.speak` renders
a whole piece of text and hands back samples. :meth:`TtsEngine.stream` renders it a sentence at a
time and yields each as it is finished, so the first words are audible while the rest is still being
made; on a voice that runs four times faster than speech that is the difference between waiting for
one sentence and waiting for a paragraph.

**Phonemisation ships with the model.** Every Piper archive in the catalog carries its own copy of
``espeak-ng-data`` — about nineteen megabytes of it — and Kokoro and KittenTTS carry the same. So a
Russian voice expands "17" to "семнадцатое" and reads a Latin word inside a Russian sentence in
Russian phonemes with nothing installed in the image and no Python fallback anywhere. The one voice
here that does not work that way, Mandarin, ships a lexicon and three rule FSTs instead, and
:func:`resolve` tells the two apart by what is in the directory rather than by anything in the
catalog — the same rule the recognition side follows for the same reason.

Files are found by shape, not by name (see :mod:`daedalus.speech.tts_catalog`). :func:`resolve` is the
whole of that knowledge, and it is deliberately small.
"""

from __future__ import annotations

import array
import asyncio
import logging
import re
import struct
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.speech.tts_catalog import TtsVoice

logger = logging.getLogger(__name__)

MIN_SPEED = 0.5
MAX_SPEED = 2.0
"""What ``speed`` may be. Below a half is a recording slowed to unintelligibility and above two is a
sound rather than speech; both ends are what a stuck slider or a bad client sends, not a preference."""

MAX_TEXT_CHARS = 4000
"""One synthesis request. Longer than this is a document being read aloud, which is not what a spoken
conversation is, and it is also a minute of audio held in memory at once."""

SENTENCE_END = re.compile(r"(?<=[.!?…。！？])\s+|\n{2,}")
"""Where one spoken chunk may end. Deliberately blunt: a wrong split costs a breath in the wrong place
and a missed one costs nothing at all, so there is no abbreviation table here to get wrong in two
languages."""

MIN_SENTENCE_CHARS = 12
"""A fragment shorter than this is joined to the next rather than synthesised alone. "Yes." on its own
is a separate model call, a separate audio clip and an audible gap, for three characters of speech."""

MAX_SENTENCE_CHARS = 400
"""A "sentence" longer than this is split on the nearest comma or space. Something without full stops
— a list, a pasted line — would otherwise be one long synthesis and defeat the whole streaming path."""

OPENING_CHARS = 60
"""How much of the first chunk is worth waiting for before any sound comes out.

Everything after the first chunk is made while the one before it is being played, so its length costs
the listener nothing; the first one is the whole of the silence between asking and hearing. On a voice
that renders at a quarter of real time an opening of sixty characters is about half a second, and a
sentence of ninety is about a second — which is the difference between an answer that begins and a
page that appears to have missed the question. The cut is only ever taken at a clause break that is
already in the text: a fragment ending nowhere in particular is read as if it ended, and that is worse
than the wait."""

RULE_FSTS = ("phone.fst", "date.fst", "number.fst")
"""The text-normalisation rules a lexicon-phonemised voice ships, in the order sherpa wants them: how a
character is pronounced, then dates, then numbers."""


class TtsError(RuntimeError):
    """Anything that stops audio coming out: the wheel, the files, or the voice itself."""


def require_sherpa() -> Any:
    """The engine module, or a refusal that says how to get it."""
    try:
        import sherpa_onnx  # Lazy: the engine is an optional extra, absent until the operator downloads a voice
    except ImportError as exc:  # pragma: no cover - exercised only where the extra is absent
        raise TtsError(
            "speaking here needs the speech extra: run `uv sync --extra speech` in the runtime, "
            "or configure a speech endpoint instead (Settings → Voice)"
        ) from exc
    return sherpa_onnx


# -- what a voice is made of ------------------------------------------------------------------


@dataclass(frozen=True)
class Files:
    """The files one voice is loaded from, found inside an unpacked archive."""

    model: Path
    tokens: Path
    data_dir: Path | None = None
    """``espeak-ng-data``, where the voice phonemises through espeak-ng — which is all but one of them."""
    lexicon: Path | None = None
    """``lexicon.txt``, where it does not."""
    voices: Path | None = None
    """``voices.bin``: the style vectors of a multi-speaker Kokoro or KittenTTS archive."""
    rule_fsts: tuple[Path, ...] = ()


def _model_file(directory: Path) -> Path:
    """The one ONNX in the directory, preferring the quantised build where an archive ships both."""
    found = sorted(directory.glob("*.onnx"))
    if not found:
        raise TtsError(f"{directory.name} has no model file — the download is incomplete")
    quantised = [p for p in found if ".int8." in p.name or ".quant." in p.name]
    return (quantised or found)[0]


def resolve(directory: Path, kind: str) -> Files:
    """What to load a voice from, discovered inside the unpacked archive.

    This is also the integrity check a download gets after it is unpacked: an archive that arrived
    short raises here rather than at the first sentence somebody asks to hear.
    """
    model = _model_file(directory)
    tokens = directory / "tokens.txt"
    if not tokens.is_file():
        raise TtsError(f"{directory.name} has no tokens file — the download is incomplete")
    espeak = directory / "espeak-ng-data"
    lexicon = directory / "lexicon.txt"
    if kind in ("kokoro", "kitten"):
        voices = directory / "voices.bin"
        if not voices.is_file():
            raise TtsError(f"{directory.name} has no voices.bin — the download is incomplete")
        if not espeak.is_dir():
            raise TtsError(f"{directory.name} has no espeak-ng-data — the download is incomplete")
        return Files(model=model, tokens=tokens, data_dir=espeak, voices=voices)
    if kind != "vits":
        raise TtsError(f"{directory.name} is a {kind} voice, which this engine does not load")
    if espeak.is_dir():
        return Files(model=model, tokens=tokens, data_dir=espeak)
    if lexicon.is_file():
        rules = tuple(directory / name for name in RULE_FSTS if (directory / name).is_file())
        return Files(model=model, tokens=tokens, lexicon=lexicon, rule_fsts=rules)
    raise TtsError(
        f"{directory.name} has neither espeak-ng-data nor a lexicon, so nothing here can turn text "
        "into phonemes for it — the download is incomplete"
    )


def _config(voice: TtsVoice, files: Files, threads: int) -> Any:
    """sherpa's configuration for one voice. The only place that knows the three families apart."""
    sherpa = require_sherpa()
    model = sherpa.OfflineTtsModelConfig(num_threads=max(1, threads), provider="cpu")
    if voice.kind == "kokoro":
        model.kokoro = sherpa.OfflineTtsKokoroModelConfig(
            model=str(files.model), voices=str(files.voices), tokens=str(files.tokens),
            data_dir=str(files.data_dir),
        )
    elif voice.kind == "kitten":
        model.kitten = sherpa.OfflineTtsKittenModelConfig(
            model=str(files.model), voices=str(files.voices), tokens=str(files.tokens),
            data_dir=str(files.data_dir),
        )
    elif files.data_dir is not None:
        model.vits = sherpa.OfflineTtsVitsModelConfig(
            model=str(files.model), tokens=str(files.tokens), data_dir=str(files.data_dir),
        )
    else:
        model.vits = sherpa.OfflineTtsVitsModelConfig(
            model=str(files.model), tokens=str(files.tokens), lexicon=str(files.lexicon),
        )
    return sherpa.OfflineTtsConfig(
        model=model,
        rule_fsts=",".join(str(p) for p in files.rule_fsts),
        # One sentence per call. The splitting is done here instead, because this engine has to know
        # where the boundaries are anyway to stream, and two different ideas of where a sentence ends
        # would mean audio arriving in chunks nobody asked for.
        max_num_sentences=1,
    )


# -- the text ---------------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,4}[-*+]\s+", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_SPACE = re.compile(r"[ \t]+")


def spoken(text: str) -> str:
    """Text as it should be read aloud: no markdown, no code blocks, no addresses.

    The concierge already writes in spoken sentences, so this is a guard rather than a rewriter — what
    it removes is the punctuation a synthesiser would otherwise pronounce or stumble over. A fenced
    block becomes the phrase "code block" because reading a diff aloud is worse than saying there was
    one, and a link becomes its own text because a URL read character by character is unbearable.
    """
    body = _FENCE.sub(" code block. ", text)
    body = _INLINE_CODE.sub(r"\1", body)
    body = _LINK.sub(r"\1", body)
    body = _BARE_URL.sub("a link", body)
    body = _HEADING.sub("", body)
    body = _BULLET.sub("", body)
    body = _EMPHASIS.sub("", body)
    body = _SPACE.sub(" ", body)
    return "\n".join(line.strip() for line in body.splitlines()).strip()


def _cut_long(part: str) -> list[str]:
    """Break something with no full stop in it on a comma, then on a space, then not at all."""
    if len(part) <= MAX_SENTENCE_CHARS:
        return [part]
    out: list[str] = []
    rest = part
    while len(rest) > MAX_SENTENCE_CHARS:
        window = rest[:MAX_SENTENCE_CHARS]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" — "))
        if cut < MIN_SENTENCE_CHARS:
            cut = window.rfind(" ")
        if cut < MIN_SENTENCE_CHARS:
            cut = MAX_SENTENCE_CHARS
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def _open_sooner(first: str) -> list[str]:
    """The opening chunk, cut at a clause break if that makes the first sound arrive sooner.

    One cut, at the last comma, semicolon or dash that leaves a fragment worth speaking and still
    inside :data:`OPENING_CHARS`. Where the sentence has no such break — and many short ones do not —
    it is left whole rather than broken somewhere a reader would hear as a mistake.
    """
    if len(first) <= OPENING_CHARS:
        return [first]
    window = first[:OPENING_CHARS]
    cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" — "), window.rfind(": "))
    if cut < MIN_SENTENCE_CHARS:
        return [first]
    head, tail = first[:cut + 1].strip(), first[cut + 1:].strip()
    return [head, tail] if tail else [first]


def sentences(text: str) -> list[str]:
    """The text as the chunks it will be spoken in, in order.

    A short fragment is carried into the next chunk rather than spoken alone: every chunk is a model
    call and a clip boundary, and "Yes." as its own clip is an audible stutter for three characters.
    The first chunk is the exception and is made *shorter* where the text allows it, because it is the
    only one the listener waits through in silence.
    """
    parts: list[str] = []
    for raw in SENTENCE_END.split(text):
        piece = raw.strip()
        if piece:
            parts.extend(_cut_long(piece))
    out: list[str] = []
    for piece in parts:
        if out and len(out[-1]) < MIN_SENTENCE_CHARS:
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return _open_sooner(out[0]) + out[1:] if out else out


# -- audio ------------------------------------------------------------------------------------


def to_pcm16(samples: Any) -> bytes:
    """sherpa's floats as the 16-bit samples everything downstream speaks in.

    Clamped rather than scaled: a synthesiser occasionally puts a sample just past full scale, and
    normalising the whole clip to it would make one loud consonant quieten the sentence around it.
    """
    out = array.array("h", (max(-32768, min(32767, int(value * 32767))) for value in samples))
    if struct.pack("=h", 1) != b"\x01\x00":  # pragma: no cover - no big-endian machine runs this
        out.byteswap()
    return out.tobytes()


def wav(pcm16: bytes, rate: int) -> bytes:
    """The samples as a WAV file, header and all. Mono, 16-bit, at the voice's own rate."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm16)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16
    ) + b"data" + struct.pack("<I", len(pcm16))
    return header + pcm16


class TtsEngine:
    """A loaded voice, shared by everything that wants audio out of words.

    One synthesiser serves every caller and the lock is what makes that safe: sherpa's is not
    re-entrant, so requests are serialised. They are short — a sentence at a time — so queueing behind
    one costs less than holding a second copy of the model.
    """

    def __init__(self, voice: TtsVoice, directory: Path, *, threads: int = 2) -> None:
        self.voice = voice
        self.directory = directory
        self.threads = threads
        self.lock = threading.Lock()
        sherpa = require_sherpa()
        self.files = resolve(directory, voice.kind)
        self.tts = sherpa.OfflineTts(_config(voice, self.files, threads))

    @property
    def sample_rate(self) -> int:
        """What it actually synthesises at, read back from the loaded model rather than the catalog."""
        return int(self.tts.sample_rate)

    @property
    def num_speakers(self) -> int:
        return int(self.tts.num_speakers)

    def speaker_id(self, speaker: str | int) -> int:
        """A speaker as the model numbers it, from a name, an index, or nothing.

        Out of range becomes the first speaker rather than an error: a voice the operator picked before
        the catalog changed under them should still talk.
        """
        if isinstance(speaker, int):
            return speaker if 0 <= speaker < self.num_speakers else 0
        name = (speaker or "").strip()
        if not name:
            return 0
        if name in self.voice.speakers:
            index = self.voice.speakers.index(name)
            return index if index < self.num_speakers else 0
        if name.isdigit():
            index = int(name)
            return index if index < self.num_speakers else 0
        return 0

    def render(self, text: str, *, speaker: str | int = "", speed: float = 1.0) -> bytes:
        """One chunk of text as samples. Blocking; callers hold the lock."""
        body = text.strip()
        if not body:
            return b""
        rate = max(MIN_SPEED, min(MAX_SPEED, speed))
        audio = self.tts.generate(body, sid=self.speaker_id(speaker), speed=rate)
        return to_pcm16(audio.samples)

    def pieces(self, body: str, *, speaker: str | int = "", speed: float = 1.0) -> Iterator[bytes]:
        """The text as samples, a sentence at a time, in order. Blocking; holds the lock per sentence.

        ``body`` is text :func:`spoken` has already been through — the caller has it in hand and
        applying it twice is work for nothing and a trap the moment a rule in there stops being
        idempotent.

        The lock is taken and released per sentence rather than held across the whole text, so a long
        answer being read aloud does not block the sample the operator just asked to hear.
        """
        for piece in sentences(body):
            with self.lock:
                chunk = self.render(piece, speaker=speaker, speed=speed)
            if chunk:
                yield chunk

    async def speak(self, text: str, *, speaker: str | int = "", speed: float = 1.0) -> tuple[bytes, int]:
        """A whole piece of text as samples and the rate they are at."""
        body = spoken(text)
        if len(body) > MAX_TEXT_CHARS:
            raise TtsError(f"this is {len(body)} characters to read aloud; {MAX_TEXT_CHARS} is the most at once")

        def run() -> bytes:
            return b"".join(self.pieces(body, speaker=speaker, speed=speed))

        return await asyncio.to_thread(run), self.sample_rate

    async def stream(
        self,
        text: str,
        *,
        speaker: str | int = "",
        speed: float = 1.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> AsyncIterator[bytes]:
        """The same audio, yielded a sentence at a time as each is finished.

        Each sentence is synthesised on a worker thread and handed over before the next is begun, so
        the caller has the first words after one sentence's work rather than after the whole text's.

        ``cancelled`` is asked at every sentence boundary, and the generator being closed — which is
        what a client disconnecting does to it — stops it at the same place. Either way the work that
        is abandoned is one sentence's, and the lock is never held across the gap: a request nobody is
        listening to any more cannot keep the next one waiting.
        """
        body = spoken(text)
        if len(body) > MAX_TEXT_CHARS:
            raise TtsError(f"this is {len(body)} characters to read aloud; {MAX_TEXT_CHARS} is the most at once")
        for piece in sentences(body):
            if cancelled is not None and cancelled():
                return

            def run(part: str = piece) -> bytes:
                with self.lock:
                    return self.render(part, speaker=speaker, speed=speed)

            chunk = await asyncio.to_thread(run)
            if cancelled is not None and cancelled():
                return
            if chunk:
                yield chunk


class TtsCache:
    """The one loaded voice, kept between sentences and swapped when the operator picks another.

    Loading is slow and happens under a lock, so two sentences arriving together load once and both
    wait. Choosing another voice drops the old one; there is never more than one resident.
    """

    def __init__(self) -> None:
        self._engine: TtsEngine | None = None
        self._key: tuple[str, int] | None = None
        self._lock = asyncio.Lock()
        self._fields = threading.Lock()
        self._generation = 0
        """Bumped by every :meth:`drop`. A load that finishes after one publishes nothing."""

    async def get(self, voice: TtsVoice, directory: Path, *, threads: int) -> TtsEngine:
        key = (voice.id, threads)
        async with self._lock:
            with self._fields:
                if self._engine is not None and self._key == key:
                    return self._engine
                self._engine = None
                self._key = None
                mine = self._generation
            logger.warning("loading local voice %s (%d threads)", voice.id, threads)
            engine = await asyncio.to_thread(TtsEngine, voice, directory, threads=threads)
            with self._fields:
                if self._generation != mine:
                    # The voice was deleted or swapped while this was loading. The caller still gets
                    # what it asked for — the request is already in flight and a half-spoken answer
                    # helps nobody — but nothing resident is left behind pointing at a voice the
                    # operator has since said they do not want.
                    logger.warning("local voice %s finished loading after it was dropped; not keeping it", voice.id)
                    return engine
                self._engine, self._key = engine, key
            return engine

    def loaded(self) -> str:
        """The id of the resident voice, or empty. For the doctor line and the settings page."""
        with self._fields:
            return self._engine.voice.id if self._engine is not None else ""

    def drop(self) -> None:
        """Let go of the voice — the operator deleted it or chose another.

        Synchronous, because it is called from ``save_config`` and from the delete endpoint, neither of
        which should have to be async for this. A drop cannot wait for a load it arrives in the middle
        of — the whole point of it is to be answerable now — so it marks the generation instead, and a
        loader that comes back into a newer generation hands its engine to its own caller and leaves
        the cache empty rather than writing over the drop.
        """
        with self._fields:
            self._generation += 1
            self._engine = None
            self._key = None


CACHE = TtsCache()

__all__ = [
    "CACHE",
    "MAX_SPEED",
    "MAX_TEXT_CHARS",
    "MIN_SPEED",
    "OPENING_CHARS",
    "Files",
    "TtsCache",
    "TtsEngine",
    "TtsError",
    "require_sherpa",
    "resolve",
    "sentences",
    "spoken",
    "to_pcm16",
    "wav",
]
