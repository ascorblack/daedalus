"""Getting a chosen model onto the disk, and off it again.

A model is half a gigabyte over a link that may not survive it, so a download here is resumable: the
archive lands in a part file, an interrupted one is continued with a range request rather than begun
again, and the operator can stop one mid-flight. What arrives is checked before it is believed — the
published size always, the sha256 where the catalog has one, and in every case the unpacked directory
against what its kind actually needs to load. Only then does it become installed; a download that
fails any of those leaves nothing behind but a line in the log.

Progress is a stream of events rather than a number to poll, because the Mini App shows a bar and the
bar should move. :meth:`Downloads.watch` hands out a queue that the API turns into SSE.

Nothing here imports the engine's wheel: downloading a model must work on an installation that has not
installed the extra yet, since that is exactly the installation that is about to.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import tarfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from daedalus.speech.catalog import SpeechModel, get

logger = logging.getLogger(__name__)

CHUNK = 1 << 20
"""A megabyte at a time: big enough that the progress callback is not the expensive part."""

PROGRESS_INTERVAL = 0.4
"""How often a running download reports itself. A bar does not need more, and a queue should not get it."""

MANIFEST = "installed.json"


class DownloadError(RuntimeError):
    """A download that will not become a working model, with the reason in the message."""


@dataclass
class Progress:
    """One download, as the picker draws it."""

    id: str
    state: str
    """``downloading``, ``verifying``, ``unpacking``, ``installed``, ``failed`` or ``cancelled``."""
    done_bytes: int = 0
    total_bytes: int = 0
    error: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.done_bytes / self.total_bytes) if self.total_bytes else 0.0


@dataclass
class Installed:
    """What the manifest remembers about a model that is on the disk."""

    id: str
    archive: str
    sha256: str
    disk_bytes: int
    installed_at: float = field(default_factory=time.time)


class Downloads:
    """The models directory: what is in it, what is arriving, and what to throw away.

    One instance per process, held by the application. It owns the directory layout — ``<id>/`` for an
    installed model, ``.part/`` for archives in flight — and nothing else writes there.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.parts = root / ".part"
        self._running: dict[str, asyncio.Task[None]] = {}
        self._progress: dict[str, Progress] = {}
        self._watchers: list[asyncio.Queue[Progress]] = []

    # -- what is on the disk ----------------------------------------------------------------

    def directory(self, model_id: str) -> Path:
        return self.root / model_id

    def manifest(self) -> dict[str, Installed]:
        """Everything installed, read from disk each time: the file is small and another process may write it."""
        path = self.root / MANIFEST
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        out: dict[str, Installed] = {}
        for entry in raw.get("models", []):
            try:
                record = Installed(**entry)
            except TypeError:
                continue
            if self.directory(record.id).is_dir():
                out[record.id] = record
        return out

    def _write_manifest(self, records: dict[str, Installed]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / MANIFEST
        body = {"models": [asdict(r) for r in sorted(records.values(), key=lambda r: r.id)]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=1))
        tmp.replace(path)

    def is_installed(self, model_id: str) -> bool:
        return model_id in self.manifest()

    def disk_usage(self) -> int:
        """Bytes the models directory occupies, part files included — what the operator would get back."""
        total = 0
        for path in self.root.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
        return total

    def delete(self, model_id: str) -> bool:
        """Remove an installed model. Answers whether there was anything to remove."""
        records = self.manifest()
        directory = self.directory(model_id)
        existed = directory.is_dir()
        shutil.rmtree(directory, ignore_errors=True)
        if records.pop(model_id, None) is not None or existed:
            self._write_manifest(records)
            self._publish(Progress(id=model_id, state="deleted"))
            return True
        return False

    # -- progress ----------------------------------------------------------------------------

    def progress(self) -> dict[str, Progress]:
        """Every download this process knows about, running or finished."""
        return dict(self._progress)

    def _publish(self, update: Progress) -> None:
        self._progress[update.id] = update
        for queue in list(self._watchers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(update)

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[Progress]]:
        """A queue of progress updates for as long as the caller holds it.

        Bounded, and a full queue drops rather than blocks: a client that stopped reading must not be
        able to stall a download.
        """
        queue: asyncio.Queue[Progress] = asyncio.Queue(maxsize=64)
        self._watchers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(queue)

    # -- downloading -------------------------------------------------------------------------

    def start(self, model_id: str, *, client: httpx.AsyncClient | None = None, url: str = "") -> Progress:
        """Begin, or report the one already running. Returns at once; the work is a background task."""
        model = get(model_id)
        if model_id in self._running and not self._running[model_id].done():
            return self._progress.get(model_id, Progress(id=model_id, state="downloading"))
        if self.is_installed(model_id):
            state = Progress(id=model_id, state="installed", done_bytes=model.size_bytes, total_bytes=model.size_bytes)
            self._publish(state)
            return state
        state = Progress(id=model_id, state="downloading", total_bytes=model.size_bytes)
        self._publish(state)
        self._running[model_id] = asyncio.create_task(self._run(model, client, url or model.url))
        return state

    def cancel(self, model_id: str) -> bool:
        """Stop a download in flight. The part file stays, so starting again resumes where it stopped."""
        task = self._running.get(model_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        """Stop everything in flight and wait for it, so shutdown does not leave tasks decoding."""
        for task in list(self._running.values()):
            task.cancel()
        for task in list(self._running.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._running.clear()

    async def _run(self, model: SpeechModel, client: httpx.AsyncClient | None, url: str) -> None:
        try:
            archive = await self._fetch(model, client, url)
            self._publish(Progress(id=model.id, state="verifying", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
            await asyncio.to_thread(verify, archive, model)
            self._publish(Progress(id=model.id, state="unpacking", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
            disk = await asyncio.to_thread(self._unpack, archive, model)
            records = self.manifest()
            records[model.id] = Installed(id=model.id, archive=model.archive, sha256=model.sha256, disk_bytes=disk)
            self._write_manifest(records)
            archive.unlink(missing_ok=True)
            self._publish(Progress(id=model.id, state="installed", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
            logger.warning("local speech model %s installed (%d MB on disk)", model.id, disk >> 20)
        except asyncio.CancelledError:
            self._publish(Progress(id=model.id, state="cancelled", total_bytes=model.size_bytes))
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes one message for the operator
            shutil.rmtree(self.directory(model.id), ignore_errors=True)
            logger.warning("local speech model %s failed to install: %s", model.id, exc)
            self._publish(Progress(id=model.id, state="failed", total_bytes=model.size_bytes, error=str(exc)))
        finally:
            self._running.pop(model.id, None)

    async def _fetch(self, model: SpeechModel, client: httpx.AsyncClient | None, url: str) -> Path:
        """The archive on disk, continuing a part file where one is already there."""
        self.parts.mkdir(parents=True, exist_ok=True)
        target = self.parts / model.archive
        have = target.stat().st_size if target.exists() else 0
        if have > model.size_bytes:
            # A part file longer than the published size is not this archive; start it over rather
            # than resume into something that can never verify.
            target.unlink()
            have = 0
        if have == model.size_bytes:
            return target
        owns = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True)
        headers = {"range": f"bytes={have}-"} if have else {}
        last = 0.0
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if have and response.status_code == 200:
                    # The host ignored the range and is sending the whole file: take it from the top.
                    have = 0
                    target.unlink(missing_ok=True)
                elif have and response.status_code != 206:
                    raise DownloadError(f"the download host answered HTTP {response.status_code} to a resume")
                elif response.status_code >= 400:
                    raise DownloadError(f"the download host answered HTTP {response.status_code}")
                with target.open("ab" if have else "wb") as fh:
                    async for chunk in response.aiter_bytes(CHUNK):
                        fh.write(chunk)
                        have += len(chunk)
                        now = time.monotonic()
                        if now - last >= PROGRESS_INTERVAL:
                            last = now
                            self._publish(Progress(id=model.id, state="downloading", done_bytes=have, total_bytes=model.size_bytes))
        except httpx.HTTPError as exc:
            raise DownloadError(f"the download did not finish: {type(exc).__name__}") from exc
        finally:
            if owns:
                await client.aclose()
        return target

    def _unpack(self, archive: Path, model: SpeechModel) -> int:
        """Unpack into place and answer with the bytes it took. Blocking; runs on a worker thread.

        The archive holds one top-level directory whose name is the zoo's, not ours, so the contents
        are lifted out of it into ``<id>/``: the catalog id is what the config stores and what the
        engine looks for, and it must not depend on how a release happened to be named.
        """
        directory = self.directory(model.id)
        shutil.rmtree(directory, ignore_errors=True)
        staging = self.parts / f"{model.id}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                members = [m for m in tar.getmembers() if _safe_member(m.name)]
                if not members:
                    raise DownloadError("the archive is empty or its paths are not safe to unpack")
                tar.extractall(staging, members=members, filter="data")
            roots = [p for p in staging.iterdir() if p.is_dir()]
            source = roots[0] if len(roots) == 1 and not any(p.is_file() for p in staging.iterdir()) else staging
            directory.parent.mkdir(parents=True, exist_ok=True)
            source.rename(directory)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        _check_loadable(directory, model)
        return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def _safe_member(name: str) -> bool:
    """Whether a tar entry may be written: no absolute paths and nothing climbing out of the directory."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    return ".." not in Path(name).parts


def sha256_of(path: Path) -> str:
    """The file's digest. Blocking, and worth a worker thread for anything this size."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def verify(archive: Path, model: SpeechModel) -> None:
    """Refuse an archive that is not what the catalog describes.

    Size is checked always; the digest only where the catalog has one, because the model zoo publishes
    no checksum file and half the entries were never hashed here. That is the honest position: a model
    without a digest is verified by its length and by unpacking into something the engine can load,
    which is weaker than a hash and much better than nothing.
    """
    size = archive.stat().st_size
    if size != model.size_bytes:
        raise DownloadError(f"the download is {size} bytes where the catalog says {model.size_bytes}")
    if model.sha256:
        actual = sha256_of(archive)
        if actual != model.sha256:
            raise DownloadError(f"the download's checksum is {actual[:16]}…, not the published one")


def _check_loadable(directory: Path, model: SpeechModel) -> None:
    """The unpacked directory holds what its kind needs. Imported late: this must work without the extra."""
    from daedalus.speech.engine import (  # Lazy: downloading must work before the engine's wheel is installed
        SpeechError,
        resolve,
    )

    try:
        resolve(directory, model.kind)
    except SpeechError as exc:
        raise DownloadError(str(exc)) from exc


def view(downloads: Downloads, *, selected: str = "") -> dict[str, Any]:
    """The whole picker in one object: the catalog, what is installed, what is arriving, what it costs."""
    from daedalus.speech.catalog import (  # Lazy: only this view joins the catalog to the manager
        MODELS,
        as_json,
        languages,
    )

    records = downloads.manifest()
    running = downloads.progress()
    entries = []
    for model in MODELS:
        entry = as_json(model)
        record = records.get(model.id)
        progress = running.get(model.id)
        entry["installed"] = record is not None
        entry["installed_bytes"] = record.disk_bytes if record else 0
        entry["selected"] = model.id == selected
        entry["verified"] = bool(model.sha256)
        if progress is not None and progress.state in ("downloading", "verifying", "unpacking", "failed"):
            entry["progress"] = {"state": progress.state, "fraction": progress.fraction, "error": progress.error}
        entries.append(entry)
    return {
        "models": entries,
        "languages": languages(),
        "selected": selected,
        "disk_bytes": downloads.disk_usage(),
        "root": str(downloads.root),
    }


__all__ = ["MANIFEST", "DownloadError", "Downloads", "Installed", "Progress", "sha256_of", "verify", "view"]
