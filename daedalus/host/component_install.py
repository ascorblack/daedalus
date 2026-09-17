"""Putting a missing component in place, one at a time, with the work visible while it happens.

Three things an installer of this kind has to get right, and each of them is a decision in the code
below rather than a detail.

**One at a time.** Two ``uv sync`` runs into the same environment at once is how a site-packages
directory ends up half written, and two launcher actions at once is refused by the launcher anyway.
So a second request while one is running is a 409 naming what is running, not a queue.

**The work is somewhere else.** A Python extra is a subprocess of this process, and its output is
lines this module can forward. Node and the headless browser are the launcher's: it was asked over
the loopback bridge, it answered "started", and what it does next is visible only as its own status.
So progress here is honest about which of the two it is — a line of output while there are lines, and
otherwise the plain fact that the launcher is working on it.

**A finished install is not always a working component.** The speech engine is imported on demand and
appears at once; Node and the browser are found through the environment the supervisor started with,
so the process has to be restarted before it can see them. That is carried on every frame as
``restart_required`` rather than discovered by an operator whose new browser does not work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from daedalus.config import Settings
from daedalus.host import components, launcher_bridge

logger = logging.getLogger(__name__)

SYNC_TIMEOUT = 900.0
"""How long a ``uv sync`` may take before it is killed. Wheels over a slow link, not a resolution."""

LAUNCHER_TIMEOUT = 1800.0
"""The headless browser is a few hundred megabytes and the launcher unpacks it; this is the outer
bound on waiting for it, not an expectation."""

LAUNCHER_POLL = 2.0
"""How often the launcher's status is asked while it works. Its own page polls at about this rate."""

QUEUE_SIZE = 64


class Busy(RuntimeError):
    """Something is already installing. Carries what, so the answer can name it."""

    def __init__(self, component_id: str) -> None:
        super().__init__(component_id)
        self.component_id = component_id


class NotInstallable(RuntimeError):
    """This component cannot be installed by this process. Carries the sentence that says what can."""

    def __init__(self, reason: str, fix: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


class Installer:
    """The state machine behind ``POST /api/components/{id}/install``.

    Held by the application for its lifetime, because the progress of an install has to outlive the
    request that started it: the browser is fetched over minutes and the page that asked for it may
    be reloaded, closed and opened again while it happens.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._progress: dict[str, dict[str, object]] = {}
        self._task: asyncio.Task[None] | None = None
        self._running_id = ""
        self._process: asyncio.subprocess.Process | None = None
        self._watchers: list[asyncio.Queue[dict[str, object]]] = []

    # -- what is happening --------------------------------------------------------------

    def running_id(self) -> str:
        """The component being installed, or an empty string. A finished task is not running."""
        if self._task is not None and self._task.done():
            self._running_id = ""
        return self._running_id

    def progress_of(self, component_id: str) -> dict[str, object] | None:
        return self._progress.get(component_id)

    def all_progress(self) -> list[dict[str, object]]:
        return list(self._progress.values())

    def launcher_present(self) -> bool:
        """Whether a launcher is holding this installation — the answer the page needs before it
        offers a button that only a launcher can honour."""
        return launcher_bridge.read(self.settings.state_dir) is not None

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[dict[str, object]]]:
        """Frames for as long as the caller holds the queue.

        Bounded, and a full queue drops rather than blocks: a client that stopped reading must not be
        able to stall an install.
        """
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._watchers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(queue)

    def _publish(self, component_id: str, state: str, *, step: str = "", error: str = "", restart: bool = False) -> dict[str, object]:
        frame: dict[str, object] = {"id": component_id, "state": state, "step": step, "error": error, "restart_required": restart}
        self._progress[component_id] = frame
        for queue in list(self._watchers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)
        return frame

    # -- starting and stopping ----------------------------------------------------------

    def start(self, status: components.Status) -> dict[str, object]:
        """Begin installing one component. Returns the first frame; the work is a background task.

        Raises :class:`Busy` when something else is running and :class:`NotInstallable` when this
        process cannot do it here — the two answers the endpoint turns into 409 and 501.
        """
        if (running := self.running_id()) and running != status.id:
            raise Busy(running)
        if running == status.id:
            return self._progress[status.id]
        if not status.installable:
            raise NotInstallable(status.detail or f"{status.id} cannot be installed from here", status.fix)
        component = components.CATALOGUE[status.id]
        self._running_id = status.id
        frame = self._publish(status.id, "queued", step="starting", restart=component.requires_restart)
        runner = self._sync_extra if status.how == "extra" else self._ask_launcher
        self._task = asyncio.ensure_future(self._run(runner, status.id, component.requires_restart))
        return frame

    async def _run(self, runner, component_id: str, restart: bool) -> None:  # type: ignore[no-untyped-def]
        try:
            await runner(component_id)
        except asyncio.CancelledError:
            self._publish(component_id, "cancelled", step="stopped")
            raise
        except NotInstallable as exc:
            self._publish(component_id, "failed", error=exc.reason)
        except Exception as exc:  # noqa: BLE001 - the frame is the report; nothing above this reads it
            logger.warning("installing %s failed: %s: %s", component_id, type(exc).__name__, exc)
            self._publish(component_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self._settle(component_id)
            self._publish(component_id, "installed", step="done", restart=restart)
        finally:
            self._running_id = ""
            self._process = None

    def _settle(self, component_id: str) -> None:
        """Let go of whatever cached the component's absence, now that it is there.

        The speech engine's presence is asked once and remembered — it cannot appear while a container
        runs — which stops being true the moment an installation can install it into itself. A cache
        that outlives the fact it caches is how a finished install goes on reading as missing.
        """
        if component_id == components.SPEECH:
            from daedalus.speech import service as speech_service

            speech_service.forget_engine()

    async def wait(self, component_id: str) -> None:
        """Block until this component's install is over. For the callers that need it done, not started."""
        task = self._task
        if task is None or self.running_id() != component_id:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    def cancel(self, component_id: str) -> bool:
        """Stop an install, where the step underneath can be stopped.

        A ``uv sync`` is a child of this process and is killed. A launcher action is not: the launcher
        owns it, and asking it to stop half way through unpacking a browser would leave the folder in
        a state neither side describes. So this reports what it could do rather than claiming both.
        """
        if self.running_id() != component_id or self._task is None:
            return False
        if self._process is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()
        self._task.cancel()
        return True

    def cancellable(self, component_id: str) -> bool:
        """Whether :meth:`cancel` would do anything: there is a child process of ours to kill."""
        return self.running_id() == component_id and self._process is not None

    # -- the two ways a component arrives -----------------------------------------------

    async def _sync_extra(self, component_id: str) -> None:
        """A Python extra, synced into the environment this process is importing from.

        ``--inexact`` is what keeps it from being destructive: without it uv removes everything the
        lock does not mention, which in a running installation is whatever other extra was installed
        earlier. ``--frozen`` keeps it from relocking, which would be a change to a tracked file.
        """
        uv = shutil.which("uv")
        if uv is None:
            raise NotInstallable("uv is not on the PATH, so the extra cannot be synced from here",
                                 f"uv sync --frozen --inexact --extra {component_id}")
        repo = Path(self.settings.bot_repo_dir)
        self._publish(component_id, "running", step=f"uv sync --extra {component_id}")
        process = await asyncio.create_subprocess_exec(
            uv, "sync", "--frozen", "--inexact", "--extra", component_id,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._process = process
        tail: list[str] = []
        assert process.stdout is not None
        try:
            async with asyncio.timeout(SYNC_TIMEOUT):
                async for raw in process.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-20]
                    self._publish(component_id, "running", step=line)
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise NotInstallable(f"installing {component_id} took longer than {int(SYNC_TIMEOUT)} s and was stopped") from None
        if process.returncode:
            last = " / ".join(tail[-4:])[:400]
            # The engine ships wheels and no sdist, so a platform with no prebuilt wheel fails at
            # resolution rather than while building. That is not a broken installation, and an
            # operator should not go looking for one.
            raise NotInstallable(
                f"{component_id} could not be installed: {last}. If this says no matching distribution, "
                f"this platform has no prebuilt wheel for it."
            )

    async def _ask_launcher(self, component_id: str) -> None:
        """Node or the headless browser: the launcher's folder, so the launcher's job.

        If no launcher is holding the installation — a headless native start, or one the operator
        stopped — this says so with the command that does the same thing from a terminal, rather than
        failing with a connection error nobody can act on.
        """
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is None:
            raise NotInstallable(
                "no launcher is running, and only the launcher can put this into the installation's runtime folder",
                f"daedalus-desktop --extra {component_id}",
            )
        self._publish(component_id, "running", step="the launcher was asked")
        await launcher_bridge.act(launcher, f"extra/{component_id}")
        await self._wait_for_launcher(component_id, launcher)

    async def _wait_for_launcher(self, component_id: str, launcher: launcher_bridge.Launcher) -> None:
        """Watch the launcher until the action it was given is over, then read what became of it.

        The launcher answers 202 and works behind it, reporting one action at a time as ``busy`` and
        the last error as ``failure``. So the wait is: see it pick the work up, see it put it down,
        and then ask whether it went wrong. A launcher that never picks it up — because it was already
        doing something else — is the same as one that finished, and the status afterwards is what
        says which.
        """
        started = False
        async with asyncio.timeout(LAUNCHER_TIMEOUT):
            while True:
                busy, failure = await launcher_bridge.busy(launcher)
                if busy:
                    started = True
                    self._publish(component_id, "running", step=busy)
                elif started:
                    if failure:
                        raise NotInstallable(f"the launcher could not install {component_id}: {failure}")
                    return
                else:
                    # Not started yet, or done before the first poll. Either way the component itself
                    # is the answer, and it is measured rather than inferred.
                    self._publish(component_id, "running", step="the launcher was asked")
                    if failure:
                        raise NotInstallable(f"the launcher could not install {component_id}: {failure}")
                await asyncio.sleep(LAUNCHER_POLL)

    # -- the restart the launcher offers ------------------------------------------------

    async def restart(self) -> str:
        """Ask the launcher to restart the agent, so a new PATH and a new browser are in force.

        Only the launcher can do this natively: the supervisor's environment is decided when the
        launcher starts it, and a process cannot give itself a PATH it was not started with.
        """
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is None:
            raise NotInstallable(
                "no launcher is running, so the restart has to be done where this installation was started",
                "daedalus-desktop restart",
            )
        await launcher_bridge.act(launcher, "restart")
        return "the launcher was asked to restart the agent"


__all__ = ["Busy", "Installer", "NotInstallable"]
