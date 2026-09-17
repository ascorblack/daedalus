"""Local speech recognition: the catalog, the download manager, the engine's interface and the API."""

from __future__ import annotations

import asyncio
import bz2
import hashlib
import io
import math
import struct
import tarfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.speech import catalog
from daedalus.speech.engine import SAMPLE_RATE, Partial, SpeechError, resolve, rms, to_float
from daedalus.speech.models import DownloadError, Downloads, sha256_of, verify, view
from daedalus.speech.service import LocalSpeech, decode_file, is_ogg

# -- the catalog ------------------------------------------------------------------------------


def test_every_entry_is_complete_and_unique() -> None:
    ids = [m.id for m in catalog.MODELS]
    assert len(ids) == len(set(ids)), "two entries share an id; the directory they install into would collide"
    for model in catalog.MODELS:
        assert model.archive.endswith(".tar.bz2"), model.id
        assert model.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/"), model.id
        assert model.size_bytes > 1_000_000 and model.unpacked_bytes >= model.size_bytes, model.id
        assert model.memory_mb > 0 and model.licence, model.id
        assert 0 < model.accuracy <= 100 and 0 < model.speed <= 100, model.id
        assert model.languages and model.note, model.id
        assert model.languages_shown >= len(model.languages), model.id
        # A checksum is optional, but one that is there must be a sha256 and not a placeholder.
        assert not model.sha256 or len(model.sha256) == 64, model.id


def test_every_kind_is_one_the_engine_can_build() -> None:
    known = {"transducer", "nemo_transducer", "whisper", "moonshine", "sense_voice", "ctc", "t_one_ctc"}
    assert {m.kind for m in catalog.MODELS} <= known


def test_the_catalog_covers_what_the_operator_actually_speaks() -> None:
    """English and Russian both need a streaming model and a careful one; Russian is the owner's."""
    russian = [m for m in catalog.MODELS if "ru" in m.languages or "ru" in m.aliases]
    assert any(m.streaming for m in russian) and any(not m.streaming for m in russian)
    english = [m for m in catalog.MODELS if "en" in m.languages]
    assert any(m.streaming for m in english) and any(m.size_bytes < 150_000_000 for m in english)
    assert any(m.languages_shown >= 25 and m.streaming for m in catalog.MODELS), "no multilingual streaming model"
    assert any(m.kind == "whisper" and m.languages_shown > 50 for m in catalog.MODELS)


def test_recommendations_are_per_language_not_one_flag() -> None:
    assert catalog.recommended("ru").id == "gigaam-ru"
    assert catalog.recommended("en").id == "parakeet-unified-en"
    # A language with no recommendation of its own falls back to a multilingual model that speaks it.
    assert catalog.recommended("de").languages_shown >= 25
    assert catalog.recommended("xx") is None


def test_speaks_accepts_auto_and_rejects_what_it_cannot() -> None:
    gigaam = catalog.get("gigaam-ru")
    assert gigaam.speaks("ru") and gigaam.speaks("auto") and gigaam.speaks("")
    assert not gigaam.speaks("ja")
    assert catalog.get("sense-voice").speaks("yue"), "aliases are part of what a model speaks"
    with pytest.raises(KeyError, match="no speech model"):
        catalog.get("nope")


# -- finding the files inside an unpacked archive ------------------------------------------------


def _files(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"onnx")
    return directory


def test_model_files_are_found_by_shape_and_prefer_the_quantised_build(tmp_path: Path) -> None:
    whisper = _files(tmp_path / "w", "tiny-encoder.onnx", "tiny-encoder.int8.onnx", "tiny-decoder.onnx", "tiny-decoder.int8.onnx", "tiny-tokens.txt")
    found = resolve(whisper, "whisper")
    assert found["encoder"].name == "tiny-encoder.int8.onnx" and found["decoder"].name == "tiny-decoder.int8.onnx"
    assert found["tokens"].name == "tiny-tokens.txt"

    transducer = _files(tmp_path / "t", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")
    assert set(resolve(transducer, "transducer")) == {"encoder", "decoder", "joiner", "tokens"}

    moonshine = _files(tmp_path / "m", "preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt")
    assert resolve(moonshine, "moonshine")["preprocessor"].name == "preprocess.onnx"

    single = _files(tmp_path / "s", "model.int8.onnx", "tokens.txt")
    assert set(resolve(single, "sense_voice")) == {"model", "tokens"}


def test_an_incomplete_download_is_refused_where_it_is_unpacked(tmp_path: Path) -> None:
    short = _files(tmp_path / "x", "encoder.onnx", "tokens.txt")
    with pytest.raises(SpeechError, match="decoder"):
        resolve(short, "transducer")
    with pytest.raises(SpeechError, match="tokens"):
        resolve(_files(tmp_path / "y", "encoder.onnx", "decoder.onnx", "joiner.onnx"), "transducer")
    with pytest.raises(SpeechError, match="unknown model kind"):
        resolve(short, "no-such-kind")


# -- samples ------------------------------------------------------------------------------------


def tone(seconds: float, *, hz: int = 220, level: float = 0.3, rate: int = SAMPLE_RATE) -> bytes:
    """A sine, which is loud enough to count as speech to a level detector and is not silence."""
    count = int(seconds * rate)
    return struct.pack(f"<{count}h", *(int(level * 32767 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)))


def quiet(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def test_samples_convert_and_measure() -> None:
    floats = to_float(struct.pack("<4h", 0, 32767, -32768, 16384))
    assert floats[0] == 0 and floats[1] == pytest.approx(1.0, abs=1e-4) and floats[2] == -1.0
    assert rms(b"") == 0 and rms(quiet(0.1)) == 0 and rms(tone(0.1)) > 380


# -- the stream session, with a fake recogniser behind the same interface -------------------------


class FakeRecognizer:
    """Stands in for sherpa's recogniser: the engine's wheel is optional and CI has no model.

    It answers the same calls in the same order, which is what the session actually depends on —
    everything else in :class:`StreamSession` is buffering and endpointing, and that is what is
    being tested.
    """

    def __init__(self, text: str = "hello there") -> None:
        self.text = text
        self.decoded = 0

    def create_stream(self) -> Any:
        return type("S", (), {"accept_waveform": lambda self, rate, data: None, "input_finished": lambda self: None, "result": type("R", (), {"text": self.text})()})()

    def decode_stream(self, stream: Any) -> None:
        self.decoded += 1


class FakeEngine:
    """The engine's surface as :class:`StreamSession` uses it, with a batch model's answers."""

    def __init__(self, model: catalog.SpeechModel, text: str = "hello there") -> None:
        self.model = model
        self.lock = threading.Lock()
        self.recognizer = FakeRecognizer(text)
        self.text = text
        self.decodes: list[int] = []

    def decode(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        self.decodes.append(len(pcm16))
        return self.text


def batch_session() -> Any:
    from daedalus.speech.engine import StreamSession

    return StreamSession(FakeEngine(catalog.get("gigaam-ru")))


async def test_a_batch_model_says_nothing_until_the_talking_stops() -> None:
    session = batch_session()
    for _ in range(10):
        answer = await session.feed(tone(0.1))
        assert answer == Partial(text="", final=False), "a batch model has no words mid-sentence and must not invent any"
    # Silence past the endpoint: the buffer is decoded and one utterance comes back.
    finals = [await session.feed(quiet(0.1)) for _ in range(12)]
    assert any(f.final and f.text == "hello there" for f in finals)
    assert session._engine.decodes and session._engine.decodes[0] > SAMPLE_RATE, "the whole utterance is decoded, not one chunk"


async def test_a_cough_is_not_an_utterance() -> None:
    session = batch_session()
    await session.feed(tone(0.1))
    answer = await session.finish()
    assert answer == Partial(text="", final=True) and not session._engine.decodes


async def test_a_batch_model_decodes_a_speaker_who_never_pauses() -> None:
    from daedalus.speech.engine import BATCH_MAX_SECONDS

    session = batch_session()
    answers = [await session.feed(tone(0.5)) for _ in range(int(BATCH_MAX_SECONDS / 0.5) + 2)]
    assert any(a.final for a in answers), "an unbroken monologue must still be transcribed"


async def test_finishing_flushes_what_is_held() -> None:
    session = batch_session()
    for _ in range(6):
        await session.feed(tone(0.1))
    answer = await session.finish()
    assert answer.final and answer.text == "hello there"


# -- the download manager --------------------------------------------------------------------------


def make_archive(name: str, files: dict[str, bytes]) -> bytes:
    """A .tar.bz2 with one top-level directory, exactly as the model zoo ships one."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for filename, body in files.items():
            info = tarfile.TarInfo(f"{name}/{filename}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return bz2.compress(raw.getvalue())


MODEL_FILES = {
    "encoder.int8.onnx": b"E" * 2048,
    "decoder.onnx": b"D" * 2048,
    "joiner.onnx": b"J" * 2048,
    "tokens.txt": b"<blk> 0\n",
}


class Zoo(BaseHTTPRequestHandler):
    """A model host that honours ranges, so resuming is exercised against something that resumes."""

    payload = b""
    served: list[str] = []
    stall_after = 0
    hold = 0.0

    def do_GET(self) -> None:  # noqa: N802
        body = Zoo.payload
        start = 0
        header = self.headers.get("range", "")
        Zoo.served.append(header or "whole")
        if header.startswith("bytes="):
            start = int(header[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        chunk = body[start:]
        if Zoo.stall_after:
            chunk = chunk[: Zoo.stall_after]
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(chunk)
        self.wfile.flush()
        if Zoo.hold:
            # The rest never comes. A download stuck like this is the only state in which cancelling
            # means anything, so it is the state the test needs.
            time.sleep(Zoo.hold)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def zoo() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Zoo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Zoo.served = []
    Zoo.stall_after = 0
    Zoo.hold = 0.0
    yield f"http://127.0.0.1:{server.server_port}/model.tar.bz2"
    server.shutdown()


def fake_entry(payload: bytes, *, digest: str = "") -> catalog.SpeechModel:
    return catalog.SpeechModel(
        id="test-model", label="Test", archive="test.tar.bz2", kind="transducer", languages=("en",),
        size_bytes=len(payload), unpacked_bytes=len(payload) * 2, memory_mb=10, licence="MIT",
        accuracy=50, speed=50, note="for the tests", sha256=digest,
    )


async def install(tmp_path: Path, url: str, model: catalog.SpeechModel) -> Downloads:
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=url)
        for _ in range(400):
            await asyncio.sleep(0.02)
            state = downloads.progress().get(model.id)
            if state and state.state in ("installed", "failed", "cancelled"):
                break
    finally:
        catalog.BY_ID.pop(model.id, None)
    return downloads


async def test_a_model_is_downloaded_unpacked_under_its_own_id_and_recorded(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("sherpa-onnx-whatever-2026-01-01", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = await install(tmp_path, zoo, model)
    assert downloads.progress()[model.id].state == "installed"
    # The archive's own directory name is the zoo's; what the engine looks for is the catalog id.
    assert (downloads.directory(model.id) / "encoder.int8.onnx").exists()
    assert downloads.is_installed(model.id) and downloads.manifest()[model.id].sha256 == model.sha256
    assert downloads.disk_usage() > 4096
    assert not list((downloads.parts).glob("*.tar.bz2")), "the archive is deleted once it is unpacked"


async def test_a_wrong_checksum_installs_nothing(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest="0" * 64)
    downloads = await install(tmp_path, zoo, model)
    state = downloads.progress()[model.id]
    assert state.state == "failed" and "checksum" in state.error
    assert not downloads.is_installed(model.id) and not downloads.directory(model.id).exists()


async def test_an_archive_that_is_missing_a_model_file_is_refused(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", {"encoder.onnx": b"E" * 2048, "tokens.txt": b"t"})
    downloads = await install(tmp_path, zoo, fake_entry(Zoo.payload))
    state = downloads.progress()["test-model"]
    assert state.state == "failed" and "incomplete" in state.error, "a short archive must fail here, not at the first utterance"
    assert not downloads.is_installed("test-model")


async def test_a_half_finished_download_resumes_rather_than_starting_again(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = Downloads(tmp_path / "stt")
    downloads.parts.mkdir(parents=True)
    half = len(Zoo.payload) // 2
    (downloads.parts / model.archive).write_bytes(Zoo.payload[:half])
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        for _ in range(400):
            await asyncio.sleep(0.02)
            if downloads.progress()[model.id].state in ("installed", "failed"):
                break
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert downloads.progress()[model.id].state == "installed"
    assert Zoo.served == [f"bytes={half}-"], "the second attempt asked for the rest, not the whole file"


async def test_a_part_file_longer_than_the_model_is_thrown_away(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = Downloads(tmp_path / "stt")
    downloads.parts.mkdir(parents=True)
    (downloads.parts / model.archive).write_bytes(b"junk" * len(Zoo.payload))
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        for _ in range(400):
            await asyncio.sleep(0.02)
            if downloads.progress()[model.id].state in ("installed", "failed"):
                break
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert downloads.progress()[model.id].state == "installed" and Zoo.served == ["whole"]


async def test_a_cancelled_download_keeps_what_arrived(tmp_path: Path, zoo: str) -> None:
    # A payload that never finishes arriving: the host sends part of it and holds the rest, which is
    # the only state in which "cancel" means anything at all.
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload)
    Zoo.stall_after = 64
    Zoo.hold = 10.0
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        await asyncio.sleep(0.15)
        assert downloads.cancel(model.id)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if downloads.progress()[model.id].state == "cancelled":
                break
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert downloads.progress()[model.id].state == "cancelled" and not downloads.is_installed(model.id)
    assert not downloads.cancel(model.id), "cancelling what is not running says so rather than pretending"


async def test_progress_reaches_a_watcher_and_a_full_queue_does_not_stall_the_download(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload)
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    seen: list[str] = []
    try:
        async with downloads.watch() as queue:
            downloads.start(model.id, url=zoo)
            for _ in range(400):
                await asyncio.sleep(0.02)
                while not queue.empty():
                    seen.append(queue.get_nowait().state)
                if "installed" in seen or "failed" in seen:
                    break
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert "installed" in seen and seen[0] == "downloading"
    assert not downloads._watchers, "the queue is let go of when the watcher leaves"


def test_deleting_a_model_frees_the_disk_and_forgets_it(tmp_path: Path) -> None:
    downloads = Downloads(tmp_path / "stt")
    directory = downloads.directory("whisper-tiny")
    directory.mkdir(parents=True)
    (directory / "tiny-encoder.onnx").write_bytes(b"x" * 1024)
    downloads._write_manifest({"whisper-tiny": __import__("daedalus.speech.models", fromlist=["Installed"]).Installed(
        id="whisper-tiny", archive="a.tar.bz2", sha256="", disk_bytes=1024)})
    assert downloads.is_installed("whisper-tiny")
    assert downloads.delete("whisper-tiny") and not downloads.is_installed("whisper-tiny")
    assert not downloads.delete("whisper-tiny")


def test_a_manifest_entry_whose_files_are_gone_is_not_installed(tmp_path: Path) -> None:
    """Somebody emptied the directory by hand; the manifest must not keep claiming the model is there."""
    from daedalus.speech.models import Installed

    downloads = Downloads(tmp_path / "stt")
    downloads._write_manifest({"whisper-tiny": Installed(id="whisper-tiny", archive="a", sha256="", disk_bytes=1)})
    assert not downloads.is_installed("whisper-tiny")


def test_a_short_or_mistyped_download_is_refused_before_it_is_unpacked(tmp_path: Path) -> None:
    payload = make_archive("x", MODEL_FILES)
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(payload)
    verify(archive, fake_entry(payload, digest=hashlib.sha256(payload).hexdigest()))
    with pytest.raises(DownloadError, match="bytes where the catalog says"):
        verify(archive, fake_entry(payload + b"x"))
    assert sha256_of(archive) == hashlib.sha256(payload).hexdigest()


def test_a_tar_that_climbs_out_of_the_directory_is_not_unpacked(tmp_path: Path) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("../escaped.onnx")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    payload = bz2.compress(raw.getvalue())
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(payload)
    downloads = Downloads(tmp_path / "stt")
    with pytest.raises(DownloadError):
        downloads._unpack(archive, fake_entry(payload))
    assert not (tmp_path / "escaped.onnx").exists()


def test_the_picker_view_joins_the_catalog_to_what_is_on_disk(tmp_path: Path) -> None:
    downloads = Downloads(tmp_path / "stt")
    body = view(downloads, selected="gigaam-ru")
    assert len(body["models"]) == len(catalog.MODELS) and body["selected"] == "gigaam-ru"
    assert body["languages"] and "ru" in body["languages"]
    entry = next(m for m in body["models"] if m["id"] == "gigaam-ru")
    assert entry["selected"] and not entry["installed"] and entry["streaming"] is False
    assert entry["verified"] is True and entry["size_bytes"] > 0


# -- decoding a recording ---------------------------------------------------------------------


def wav_bytes(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


async def test_a_plain_wav_is_read_without_shelling_out(tmp_path: Path) -> None:
    pcm = tone(0.5, rate=8000)
    samples, rate = await decode_file(wav_bytes(tmp_path / "a.wav", pcm, rate=8000))
    assert samples == pcm and rate == 8000, "the engine resamples; the reader must not second-guess it"


async def test_something_nothing_here_can_decode_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daedalus.speech.service.shutil.which", lambda name: None)
    blob = tmp_path / "note.m4a"
    blob.write_bytes(b"\x00\x00\x00\x20ftypM4A ")
    with pytest.raises(SpeechError, match="opus-tools"):
        await decode_file(blob)


def test_an_ogg_is_recognised_by_its_bytes_not_its_name(tmp_path: Path) -> None:
    ogg = tmp_path / "voice.bin"
    ogg.write_bytes(b"OggS\x00\x02rest of a voice note")
    assert is_ogg(ogg)
    assert not is_ogg(wav_bytes(tmp_path / "b.wav", tone(0.1)))
    assert not is_ogg(tmp_path / "missing.oga")


# -- precedence and the API ---------------------------------------------------------------------


class FakeManager:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def get_state(self, session_id: str) -> None:
        return None

    class providers:  # noqa: N801
        @staticmethod
        def available() -> list[str]:
            return []


class FakeApp:
    """The application as the API reads it, with a real models directory under tmp_path."""

    def __init__(self, tmp_path: Path, config: RuntimeConfig | None = None) -> None:
        self.settings = Settings(_env_file=None, state_dir=tmp_path / "state")  # type: ignore[call-arg]
        self.config = config or RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.speech = LocalSpeech(self.settings.state_dir, self.config)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.speech.config = config


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app = FakeApp(tmp_path)
    with TestClient(build_app(app, "tok")) as c:  # type: ignore[arg-type]
        c.app_state = app  # type: ignore[attr-defined]
        yield c


HEAD = {"X-Daedalus-Token": "tok"}


def test_the_picker_is_served_with_everything_the_page_needs(client: TestClient) -> None:
    body = client.get("/api/stt", headers=HEAD).json()
    assert len(body["models"]) == len(catalog.MODELS)
    assert body["selected"] == "" and body["language"] == "auto" and body["threads"] == 2
    assert set(body["decoders"]) == {"opus", "any"} and "en" in body["recommended"]
    assert body["recommended"]["ru"] == "gigaam-ru"


def test_a_model_that_is_not_installed_cannot_be_selected(client: TestClient) -> None:
    refused = client.post("/api/stt/select", json={"model": "gigaam-ru"}, headers=HEAD)
    assert refused.status_code == 409 and "not installed" in refused.json()["detail"]
    assert client.app_state.config.stt.local_model == ""  # type: ignore[attr-defined]


def test_downloading_something_that_is_not_in_the_catalog_is_a_404(client: TestClient) -> None:
    assert client.post("/api/stt/models/not-a-model/download", headers=HEAD).status_code == 404


def test_the_language_and_the_threads_are_saved_on_their_own(client: TestClient) -> None:
    body = client.post("/api/stt/select", json={"language": "ru", "threads": 4}, headers=HEAD).json()
    assert body["language"] == "ru" and body["threads"] == 4
    assert client.app_state.config.stt.local_threads == 4  # type: ignore[attr-defined]
    assert client.post("/api/stt/select", json={"threads": 99}, headers=HEAD).status_code == 422


def test_the_stream_refuses_when_no_model_is_selected(client: TestClient) -> None:
    refused = client.post("/api/voice/listen?stream=abc", content=b"\x00\x00", headers=HEAD)
    assert refused.status_code == 409 and "no local speech model" in refused.json()["detail"]


def test_a_chunk_larger_than_the_limit_is_refused(client: TestClient, tmp_path: Path) -> None:
    _install_fake_model(client.app_state)  # type: ignore[attr-defined]
    huge = b"\x00" * (3 << 20)
    assert client.post("/api/voice/listen?stream=abc", content=huge, headers=HEAD).status_code == 413


def _install_fake_model(app: FakeApp) -> None:
    """Make the application believe a model is installed and selected, without one being there."""
    from daedalus.speech.models import Installed

    directory = app.speech.downloads.directory("gigaam-ru")
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (directory / name).write_bytes(b"x")
    app.speech.downloads._write_manifest({"gigaam-ru": Installed(id="gigaam-ru", archive="a", sha256="", disk_bytes=4)})
    app.config = app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": "gigaam-ru"})})
    app.speech.config = app.config


def test_a_selected_model_is_what_the_voice_page_and_the_site_are_told(client: TestClient) -> None:
    _install_fake_model(client.app_state)  # type: ignore[attr-defined]
    asr = client.get("/api/asr", headers=HEAD).json()
    # No endpoint is configured at all, and the microphone must still be offered.
    assert asr["configured"] is True and asr["local"]["active"] is True
    assert asr["local"]["label"] == "GigaAM v3 Russian" and asr["local"]["streaming"] is False


def test_an_id_that_left_the_catalog_is_treated_as_no_choice(tmp_path: Path) -> None:
    config = RuntimeConfig()
    config = config.model_copy(update={"stt": config.stt.model_copy(update={"local_model": "retired-model"})})
    app = FakeApp(tmp_path, config)
    assert app.speech.selected() is None and not app.speech.available()


def test_the_precedence_prefers_the_local_model_and_does_not_fall_back_past_it(tmp_path: Path) -> None:
    from daedalus.speech.service import recogniser_available, transcribe_recording
    from daedalus.transport.telegram.voice import TranscriptionError

    app = FakeApp(tmp_path)
    assert not recogniser_available(app.speech, app.config)
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"url": "http://asr.local/v1"})})
    assert recogniser_available(app.speech, app.config)

    _install_fake_model(app)

    async def boom(path: Path) -> str:
        raise SpeechError("the model would not load")

    app.speech.transcribe_file = boom  # type: ignore[method-assign]
    with pytest.raises(TranscriptionError, match="the local speech model could not transcribe"):
        asyncio.run(transcribe_recording(app.speech, app.config, app.manager, tmp_path / "x.wav"))


def test_an_endpoint_is_used_when_no_local_model_is_selected(tmp_path: Path) -> None:
    from daedalus.speech.service import transcribe_recording

    app = FakeApp(tmp_path)
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"url": "http://asr.local/v1"})})
    wav_bytes(tmp_path / "x.wav", tone(0.2))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(200, json={"text": "from the endpoint"})

    from daedalus.transport.telegram import voice as transport_voice

    original = transport_voice.transcribe

    async def patched(path: Path, config: Any, **kw: Any) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await original(path, config, client=client)

    transport_voice.transcribe = patched  # type: ignore[assignment]
    try:
        words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, tmp_path / "x.wav"))
    finally:
        transport_voice.transcribe = original  # type: ignore[assignment]
    assert words == "from the endpoint"


def test_deleting_the_model_in_use_also_stops_using_it(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    _install_fake_model(app)
    body = client.delete("/api/stt/models/gigaam-ru", headers=HEAD).json()
    assert body["deleted"] and body["selected"] == ""
    assert app.config.stt.local_model == "" and not app.speech.available()


def test_the_doctor_says_which_of_the_three_things_is_missing(tmp_path: Path) -> None:
    from daedalus.doctor import DoctorContext, _local_speech

    app = FakeApp(tmp_path)
    ctx = DoctorContext(settings=app.settings, config=app.config)
    assert _local_speech(ctx).ok and "none" in _local_speech(ctx).message

    _install_fake_model(app)
    ctx = DoctorContext(settings=app.settings, config=app.config)
    assert "GigaAM v3 Russian" in _local_speech(ctx).message

    config = app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": "whisper-small"})})
    check = _local_speech(DoctorContext(settings=app.settings, config=config))
    assert not check.ok and "never downloaded" in check.message


# -- the streaming endpoint, fed a synthetic stream ------------------------------------------------


class ScriptedSession:
    """A stream session that answers on a script, so the endpoint's bookkeeping can be tested alone.

    What the endpoint owns is the identity of a stream, its reuse across requests, its closing and
    its pruning — none of which needs a model, and all of which is where the bugs would be.
    """

    opened = 0
    closed = 0

    def __init__(self, words: list[str]) -> None:
        self.words = list(words)
        self.fed = 0
        self.chunks = 0
        ScriptedSession.opened += 1

    async def feed(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> Partial:
        self.fed += len(pcm16)
        self.chunks += 1
        # Every third chunk ends an utterance, which is roughly how an endpoint detector behaves.
        if self.words and self.chunks % 3 == 0:
            return Partial(text=self.words.pop(0), final=True)
        return Partial(text="…", final=False)

    async def finish(self) -> Partial:
        ScriptedSession.closed += 1
        return Partial(text=self.words.pop(0) if self.words else "", final=True)


@pytest.fixture
def listening(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    app = client.app_state  # type: ignore[attr-defined]
    _install_fake_model(app)
    ScriptedSession.opened = ScriptedSession.closed = 0
    sessions: list[ScriptedSession] = []

    async def session() -> ScriptedSession:
        made = ScriptedSession(["раз два три", "четыре пять"])
        sessions.append(made)
        return made

    monkeypatch.setattr(app.speech, "session", session)
    return client, sessions


def pcm_chunk(seconds: float = 0.2) -> bytes:
    return tone(seconds)


def test_one_stream_id_keeps_one_decoder_across_chunks(listening: Any) -> None:
    client, sessions = listening
    answers = [client.post("/api/voice/listen?stream=s1", content=pcm_chunk(), headers=HEAD).json() for _ in range(3)]
    assert ScriptedSession.opened == 1, "each chunk must not open a new decoder"
    assert [a["final"] for a in answers] == [False, False, True]
    assert answers[-1]["text"] == "раз два три"
    assert sessions[0].fed == 3 * len(pcm_chunk())


def test_two_pages_listening_at_once_do_not_share_a_decoder(listening: Any) -> None:
    client, _ = listening
    client.post("/api/voice/listen?stream=a", content=pcm_chunk(), headers=HEAD)
    client.post("/api/voice/listen?stream=b", content=pcm_chunk(), headers=HEAD)
    assert ScriptedSession.opened == 2


def test_the_final_chunk_flushes_and_lets_go_of_the_stream(listening: Any) -> None:
    client, _ = listening
    client.post("/api/voice/listen?stream=s1", content=pcm_chunk(), headers=HEAD)
    body = client.post("/api/voice/listen?stream=s1&final=true", content=b"", headers=HEAD).json()
    assert body["final"] and body["text"] == "раз два три" and ScriptedSession.closed == 1
    # The id is free again, so the next utterance opens a fresh decoder rather than a stale one.
    client.post("/api/voice/listen?stream=s1", content=pcm_chunk(), headers=HEAD)
    assert ScriptedSession.opened == 2


def test_closing_a_stream_decodes_nothing_and_is_idempotent(listening: Any) -> None:
    client, _ = listening
    client.post("/api/voice/listen?stream=s1", content=pcm_chunk(), headers=HEAD)
    assert client.post("/api/voice/listen/close?stream=s1", headers=HEAD).json() == {"closed": True}
    assert client.post("/api/voice/listen/close?stream=s1", headers=HEAD).json() == {"closed": False}
    assert ScriptedSession.closed == 0, "closing is letting go, not finishing: nothing is decoded"


def test_a_page_that_never_closes_its_streams_does_not_accumulate_them(listening: Any) -> None:
    from daedalus.extensions.api import LISTEN_MAX_STREAMS

    client, _ = listening
    for n in range(LISTEN_MAX_STREAMS + 3):
        client.post(f"/api/voice/listen?stream=s{n}", content=pcm_chunk(), headers=HEAD)
    # Opening one more prunes the oldest; what matters is that the count is bounded, not which went.
    assert ScriptedSession.opened == LISTEN_MAX_STREAMS + 3


def test_an_empty_chunk_is_a_keepalive_and_not_an_utterance(listening: Any) -> None:
    client, _ = listening
    body = client.post("/api/voice/listen?stream=s1", content=b"", headers=HEAD).json()
    assert body == {"text": "", "final": False}


# -- the engine on a real model, where one has been fetched -----------------------------------------

REAL_MODELS = Path("/tmp/claude-1000/stt-models")


@pytest.mark.skipif(not (REAL_MODELS / "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16").is_dir(),
                    reason="no downloaded model here; the catalog's archives are hundreds of megabytes")
async def test_a_real_streaming_model_hears_its_own_test_recording() -> None:
    """The one test that loads sherpa and a model. Skipped everywhere a model was not fetched by hand."""
    from daedalus.speech.engine import Engine

    directory = REAL_MODELS / "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16"
    engine = Engine(catalog.get("zipformer-ru-small"), directory, threads=2)
    with wave.open(str(next((directory / "test_wavs").glob("*.wav")))) as handle:
        rate = handle.getframerate()
        pcm = handle.readframes(handle.getnframes())
    assert (await engine.transcribe(pcm, rate)).strip(), "the model produced no words at all"

    session = engine.session()
    step = int(rate * 0.1) * 2
    heard = []
    for at in range(0, len(pcm), step):
        answer = await session.feed(pcm[at : at + step], rate)
        if answer.final and answer.text:
            heard.append(answer.text)
    last = await session.finish()
    if last.text:
        heard.append(last.text)
    assert heard, "streaming produced no utterance where the batch decode produced words"
