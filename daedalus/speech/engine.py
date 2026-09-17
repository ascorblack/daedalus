"""Loading a downloaded model and turning audio into words.

Everything in here is CPU-bound and blocking, and none of it may run on the event loop: a model takes
a second or two to load and a long voice note takes longer than that to decode. The public surface is
therefore async and every call crosses into a worker thread, while the recognisers themselves are
cached — loading Parakeet on each utterance would cost more than decoding it.

Two ways in, because there are two ways the operator speaks. :meth:`Engine.transcribe` takes a finished
recording (a Telegram voice note, one cut utterance from the voice page) and answers with its words.
:meth:`Engine.session` opens a stream that is fed as the operator talks and answers with a partial
after every chunk. A model that can stream does so natively and reports its own endpoints; a model
that cannot is given the same interface anyway — the audio is buffered and decoded when the talking
stops — so the page above never has to ask which kind it got.

Model files are found by shape, not by name (see :mod:`daedalus.speech.catalog`). ``resolve`` is the
whole of that knowledge, and it is deliberately small.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.speech.catalog import SpeechModel

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
"""What the models want. Anything else is resampled on the way in."""

MIN_RATE = 8_000
MAX_RATE = 48_000
"""The band a declared capture rate must fall in. A browser gives 16 000, 44 100 or 48 000; the wide
ends are there for a telephone codec and for nothing else. See :func:`clamp_rate` for why a bound
matters more here than the values do."""

CHUNK_SECONDS = 0.1
"""How much audio a streaming decode step is given. Smaller is not faster — it is only more overhead."""

BATCH_SILENCE_SECONDS = 0.8
"""How long a non-streaming model waits for quiet before it decides the utterance is over."""

BATCH_MAX_SECONDS = 30.0
"""A non-streaming model decodes at least this often even if the operator never pauses."""

SILENCE_RMS = 380
"""Below this, in 16-bit sample units, counts as quiet. About 1% of full scale."""

MIN_UTTERANCE_SECONDS = 0.3
"""Shorter than this is a cough, not a sentence, and is not sent to the model."""

MAX_BUFFER_BYTES = 2 * 2 * 60 * SAMPLE_RATE
"""A hard ceiling on what one batch session holds: a minute of samples at twice the models' rate.

Expressed in bytes rather than seconds on purpose. Every other limit here divides by the declared
sample rate, and the rate comes from the browser, so a rate that is wrong or hostile makes all of them
unreachable at once. This one cannot be moved by anything the client says."""


class SpeechError(RuntimeError):
    """Anything that stops words coming out: the wheel, the files, or the model itself."""


def require_sherpa() -> Any:
    """The engine module, or a refusal that says how to get it."""
    try:
        import sherpa_onnx  # Lazy: the engine is an optional extra, absent until the operator downloads a model
    except ImportError as exc:  # pragma: no cover - exercised only where the extra is absent
        raise SpeechError(
            "local speech recognition needs the speech extra: run `uv sync --extra speech` in the runtime, "
            "or use a transcription endpoint instead (Settings → Tools → Voice notes)"
        ) from exc
    return sherpa_onnx


def _pick(directory: Path, *stems: str) -> Path:
    """The one ONNX file in ``directory`` whose name carries one of ``stems``, preferring the quantised build.

    An archive ships either a single precision or both; where both are there the int8 one is what the
    catalog's size and speed numbers describe, so it is what gets loaded.
    """
    files = sorted(directory.glob("*.onnx"))
    # Exact stem first: "cached_decode" is a substring of "uncached_decode", and the two are different
    # graphs in the same directory. Substring is the fallback for the families that prefix the file
    # name with the model's own ("small-encoder.int8.onnx"), where nothing matches exactly.
    found = [p for p in files if p.name.split(".")[0] in stems] or [p for p in files if any(stem in p.name for stem in stems)]
    if not found:
        raise SpeechError(f"{directory.name} has no {stems[0]} model file — the download is incomplete")
    quantised = [p for p in found if ".int8." in p.name or ".quant." in p.name]
    return (quantised or found)[0]


def _tokens(directory: Path) -> Path:
    found = sorted(directory.glob("*tokens.txt"))
    if not found:
        raise SpeechError(f"{directory.name} has no tokens file — the download is incomplete")
    return found[0]


def resolve(directory: Path, kind: str) -> dict[str, Path]:
    """The files one kind of model is loaded from, found inside an unpacked archive.

    The zoo names them differently per family — ``encoder.onnx`` here, ``tiny-encoder.int8.onnx``
    there — so nothing is hard-coded; each file is recognised by the word in its name that says what
    it does. This is also the integrity check a download without a published checksum gets: an
    archive that unpacked short raises here rather than at the first utterance.
    """
    if kind in ("transducer", "nemo_transducer"):
        return {
            "encoder": _pick(directory, "encoder"),
            "decoder": _pick(directory, "decoder"),
            "joiner": _pick(directory, "joiner"),
            "tokens": _tokens(directory),
        }
    if kind == "whisper":
        return {"encoder": _pick(directory, "encoder"), "decoder": _pick(directory, "decoder"), "tokens": _tokens(directory)}
    if kind == "moonshine":
        return {
            "preprocessor": _pick(directory, "preprocess"),
            "encoder": _pick(directory, "encode"),
            "uncached_decoder": _pick(directory, "uncached_decode"),
            "cached_decoder": _pick(directory, "cached_decode"),
            "tokens": _tokens(directory),
        }
    if kind in ("ctc", "t_one_ctc", "sense_voice"):
        return {"model": _pick(directory, "model"), "tokens": _tokens(directory)}
    raise SpeechError(f"unknown model kind {kind!r}")


def _build(model: SpeechModel, directory: Path, threads: int, language: str) -> Any:
    """The loaded recogniser. Blocking, and slow enough to be worth caching."""
    sherpa = require_sherpa()
    files = {name: str(path) for name, path in resolve(directory, model.kind).items()}
    if model.kind == "transducer":
        # Streaming models keep their own endpoint detector, which knows the model's frame rate and
        # beats a level threshold on the raw samples. The rules are sherpa's defaults, tightened: an
        # utterance ends after a second and a half of quiet, or two thirds of a second after words.
        return sherpa.OnlineRecognizer.from_transducer(
            num_threads=threads,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=1.5,
            rule2_min_trailing_silence=0.7,
            rule3_min_utterance_length=25.0,
            **files,
        )
    if model.kind == "t_one_ctc":
        return sherpa.OnlineRecognizer.from_t_one_ctc(
            num_threads=threads,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=1.5,
            rule2_min_trailing_silence=0.7,
            rule3_min_utterance_length=25.0,
            **files,
        )
    if model.kind == "nemo_transducer":
        return sherpa.OfflineRecognizer.from_transducer(num_threads=threads, model_type="nemo_transducer", **files)
    if model.kind == "whisper":
        # Whisper is told the language rather than left to detect it: detection costs a pass over the
        # audio and gets it wrong on a short utterance, which is most of what is said to this.
        return sherpa.OfflineRecognizer.from_whisper(num_threads=threads, language=language or "en", **files)
    if model.kind == "moonshine":
        return sherpa.OfflineRecognizer.from_moonshine(num_threads=threads, **files)
    if model.kind == "sense_voice":
        return sherpa.OfflineRecognizer.from_sense_voice(num_threads=threads, language=language, use_itn=True, **files)
    if model.kind == "ctc":
        return sherpa.OfflineRecognizer.from_nemo_ctc(num_threads=threads, **files)
    raise SpeechError(f"unknown model kind {model.kind!r}")


def to_float(pcm16: bytes) -> list[float]:
    """Signed 16-bit little-endian samples as the floats the engine reads.

    The list is what sherpa's binding accepts without numpy, which is not a dependency here and is not
    worth becoming one for a division.
    """
    count = len(pcm16) // 2
    return [sample / 32768.0 for sample in struct.unpack_from(f"<{count}h", pcm16, 0)]


def rms(pcm16: bytes) -> int:
    """Loudness of a chunk in sample units, for the silence test. Zero for an empty chunk.

    Written out rather than taken from ``audioop``, which Python 3.13 removed: this feature would
    otherwise be the one thing holding the interpreter back.
    """
    if len(pcm16) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return int(math.sqrt(sum(value * value for value in samples) / len(samples)))


@dataclass
class Partial:
    """What a stream says after a chunk: the words so far, and whether the utterance has ended."""

    text: str
    final: bool = False


class StreamSession:
    """One continuous stretch of talking, fed a chunk at a time.

    A streaming model decodes each chunk and reports its own endpoints. A batch model cannot, so it is
    given a level-based one: audio accumulates, and the moment it goes quiet the buffer is decoded and
    the words come back as a final. Either way the caller sees the same two methods and the same
    :class:`Partial`, which is the point — the page above chose a model, not an architecture.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._streaming = engine.model.streaming
        self._stream = engine.recognizer.create_stream() if self._streaming else None
        self._buffer = bytearray()
        self._rate = 0
        self._quiet = 0.0
        self._spoken = 0.0
        self._said = ""

    async def feed(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> Partial:
        """Push one chunk in and get back what can be said about the talking so far.

        The rate is carried rather than converted: sherpa resamples on the way in, and resampling
        twice is only a worse copy of the audio. It is fixed at the first chunk and a later chunk that
        declares a different one is fed at the first — a stream that changed rate mid-sentence would
        have to be re-opened anyway, and quietly relabelling the audio is how a model ends up hearing
        a sentence at three times its speed.
        """
        self._rate = self._rate or clamp_rate(sample_rate)
        return await asyncio.to_thread(self._feed, pcm16)

    async def finish(self) -> Partial:
        """No more audio is coming: flush whatever is held and answer with the last words."""
        return await asyncio.to_thread(self._finish)

    # -- the blocking half, always on a worker thread -------------------------------------------

    def _feed(self, audio: bytes) -> Partial:
        with self._engine.lock:
            if self._streaming:
                return self._feed_streaming(audio)
            return self._feed_batch(audio)

    def _feed_streaming(self, audio: bytes) -> Partial:
        recognizer, stream = self._engine.recognizer, self._stream
        stream.accept_waveform(self._rate, to_float(audio))
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = str(recognizer.get_result(stream)).strip()
        if recognizer.is_endpoint(stream):
            recognizer.reset(stream)
            return Partial(text=text, final=bool(text))
        return Partial(text=text)

    def _feed_batch(self, audio: bytes) -> Partial:
        seconds = len(audio) / 2 / self._rate
        loud = rms(audio) > SILENCE_RMS
        if loud:
            self._quiet = 0.0
            self._spoken += seconds
        elif self._spoken:
            self._quiet += seconds
        if loud or self._spoken:
            self._buffer += audio
        held = len(self._buffer) / 2 / self._rate
        long_enough = self._spoken >= MIN_UTTERANCE_SECONDS
        if long_enough and (self._quiet >= BATCH_SILENCE_SECONDS or held >= BATCH_MAX_SECONDS):
            return Partial(text=self._decode_buffer(), final=True)
        if not long_enough and (held >= BATCH_MAX_SECONDS or len(self._buffer) >= MAX_BUFFER_BYTES):
            # A cough opened the buffer and no sentence followed it. Accumulation is gated on
            # ``_spoken``, and ``_spoken`` cannot grow through silence, so without this the session
            # buffers every quiet chunk for as long as the page keeps feeding it and never emits
            # another final: the operator talks and nothing reaches the agent. Throw the blip away
            # and be an idle session again.
            self._reset()
            return Partial(text="")
        if len(self._buffer) >= MAX_BUFFER_BYTES:
            # Real speech, but more of it than a batch model should be holding. Decode what there is
            # rather than grow: the ceiling is in bytes so that no declared sample rate can lift it.
            return Partial(text=self._decode_buffer(), final=True)
        # There is nothing honest to show between utterances: a batch model has no words until it has
        # run, and inventing an ellipsis here would make the page look like it heard something.
        return Partial(text=self._said if self._quiet else "")

    def _reset(self) -> None:
        """Forget what is held without decoding it."""
        self._buffer = bytearray()
        self._quiet = self._spoken = 0.0

    def _decode_buffer(self) -> str:
        audio = bytes(self._buffer)
        self._reset()
        self._said = self._engine.decode(audio, self._rate)
        return self._said

    def _finish(self) -> Partial:
        with self._engine.lock:
            if not self._streaming:
                if self._spoken < MIN_UTTERANCE_SECONDS:
                    return Partial(text="", final=True)
                return Partial(text=self._decode_buffer(), final=True)
            recognizer, stream = self._engine.recognizer, self._stream
            stream.input_finished()
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            text = str(recognizer.get_result(stream)).strip()
            recognizer.reset(stream)
            return Partial(text=text, final=True)


class Engine:
    """A loaded model, shared by everything that wants words out of audio.

    One recogniser serves every caller — it holds hundreds of megabytes, so a second copy is not worth
    having — and the lock is what makes that safe: sherpa's recogniser is not re-entrant, so decodes
    are serialised. They are fast enough (tens of times real time) that queueing behind one is cheaper
    than loading another.
    """

    def __init__(self, model: SpeechModel, directory: Path, *, threads: int = 2, language: str = "") -> None:
        self.model = model
        self.directory = directory
        self.threads = threads
        self.language = language
        self.lock = threading.Lock()
        self.recognizer = _build(model, directory, threads, language)

    def decode(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        """One finished piece of audio as words. Blocking; callers hold the lock."""
        if len(pcm16) < 2:
            return ""
        if self.model.streaming:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, to_float(pcm16))
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            stream.input_finished()
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            return str(self.recognizer.get_result(stream)).strip()
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sample_rate, to_float(pcm16))
        self.recognizer.decode_stream(stream)
        return str(stream.result.text).strip()

    async def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE, language: str = "") -> str:
        """A whole recording as words. ``language`` is accepted for symmetry and is set at load time."""

        def run() -> str:
            with self.lock:
                return self.decode(pcm16, sample_rate)

        return await asyncio.to_thread(run)

    def session(self) -> StreamSession:
        """A stream to feed while the operator talks."""
        return StreamSession(self)


class EngineCache:
    """The one loaded model, kept between utterances and swapped when the operator picks another.

    Loading is slow and happens under a lock, so two utterances arriving together load once and both
    wait. Selecting a different model drops the old recogniser; there is never more than one resident.
    """

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._key: tuple[str, int, str] | None = None
        self._lock = asyncio.Lock()
        self._fields = threading.Lock()

    async def get(self, model: SpeechModel, directory: Path, *, threads: int, language: str) -> Engine:
        key = (model.id, threads, language)
        async with self._lock:
            with self._fields:
                if self._engine is not None and self._key == key:
                    return self._engine
                self._engine = None
                self._key = None
            logger.warning("loading local speech model %s (%d threads)", model.id, threads)
            engine = await asyncio.to_thread(Engine, model, directory, threads=threads, language=language)
            with self._fields:
                self._engine, self._key = engine, key
            return engine

    def loaded(self) -> str:
        """The id of the resident model, or empty. For the doctor line and the page's recogniser chip."""
        return self._engine.model.id if self._engine is not None else ""

    def drop(self) -> None:
        """Let go of the model — the operator deleted it or chose another.

        Synchronous, because it is called from ``save_config`` and from the delete endpoint, neither
        of which should have to be async for this. The fields are therefore guarded by a threading
        lock as well as the async one: ``get`` holds both while it loads, so a drop arriving during a
        load waits rather than clearing the reference the loader is about to write.
        """
        with self._fields:
            self._engine = None
            self._key = None


CACHE = EngineCache()
"""Process-wide, because the model is."""


def clamp_rate(sample_rate: int) -> int:
    """A capture rate the engine may be told about, or a refusal.

    The browser negotiates its own rate and sends it; sherpa resamples whatever it is given, so any
    real rate is fine. What is not fine is an arbitrary number: the batch endpointer divides by it to
    decide when an utterance ended, so a rate of a billion makes every duration nearly zero and the
    session never flushes. Eight to forty-eight kilohertz covers every capture device there is.
    """
    if not MIN_RATE <= sample_rate <= MAX_RATE:
        raise SpeechError(f"{sample_rate} Hz is not a capture rate this can use ({MIN_RATE}-{MAX_RATE})")
    return sample_rate


__all__ = [
    "BATCH_MAX_SECONDS",
    "BATCH_SILENCE_SECONDS",
    "CACHE",
    "CHUNK_SECONDS",
    "MAX_BUFFER_BYTES",
    "MAX_RATE",
    "MIN_RATE",
    "SAMPLE_RATE",
    "Engine",
    "EngineCache",
    "Partial",
    "SpeechError",
    "StreamSession",
    "clamp_rate",
    "require_sherpa",
    "resolve",
    "rms",
    "to_float",
]
