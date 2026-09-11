"""Services: long-running processes a session starts and the operator can reach.

A demo site, a dev server, a worker — anything that must keep running after the
turn that started it. A service is a detached process group in the session's
workspace, its output in a log file there, its port taken from the range the
container publishes, so the operator opens it from their own network. The
processes are children of the bot; a bot restart leaves them running (they are
reparented, the table remembers them), a container rebuild does not — at boot
what the table says was running and is not is started again when it was
marked ``restart``, and reported otherwise.

Sharing: a service is reachable on the LAN at ``SERVICES_PUBLIC_HOST:<port>``; the operator can
also open it through the bot's public address (``MINIAPP_PUBLIC_URL``, the reverse-proxied site)
under ``/s/<slug>/`` — to anyone (``public``) or to whoever presents its key (``key``); the API
proxies those requests to the service's port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.tools.shell import sandbox_argv, shell_environment

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

NAME_MAX = 32
LOG_DIR = ".services"
STOP_GRACE_SECONDS = 5.0
SHARE_MODES = ("local", "public", "key")
SHARE_COOKIE_PREFIX = "dshare-"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def parse_range(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    a, b = int(lo), int(hi or lo)
    if a < 1 or b < a or b > 65535:
        raise ValueError(f"bad port range {text!r}")
    return a, b


def pid_alive(pid: int | None) -> bool:
    """True when the process exists and is not a zombie left for the supervisor."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        return bool(fields) and fields[0] != "Z"
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


class Services:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.lo, self.hi = parse_range(app.settings.services_port_range)
        self.public_host = app.settings.services_public_host.strip()
        self.public_base = app.settings.miniapp_public_url.strip().rstrip("/").removesuffix("/app")
        self._procs: dict[tuple[str, str], subprocess.Popen[bytes]] = {}

    # -- views ----------------------------------------------------------------------

    def url_for(self, port: int | None) -> str | None:
        if not port:
            return None
        return f"http://{self.public_host or '<host>'}:{port}"

    def share_url(self, row: dict[str, Any], *, with_key: bool = True) -> str | None:
        """The public address of a shared service: through the bot's site, with the key in the query when it has one."""
        mode = row.get("share_mode") or "local"
        slug = row.get("share_slug")
        if mode == "local" or not slug or not row.get("port"):
            return None
        base = self.public_base or "<public-url>"
        url = f"{base}/s/{slug}/"
        if mode == "key" and with_key and row.get("share_key"):
            url += f"?key={row['share_key']}"
        return url

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        alive = row["status"] == "running" and pid_alive(row.get("pid"))
        mode = row.get("share_mode") or "local"
        return {
            "share": {
                "mode": mode,
                "slug": row.get("share_slug"),
                "key": row.get("share_key") if mode == "key" else None,
                "url": self.share_url(row),
                "public_base": self.public_base,
            },
            "name": row["name"],
            "command": row["command"],
            "cwd": row["cwd"],
            "port": row.get("port"),
            "url": self.url_for(row.get("port")),
            "pid": row.get("pid"),
            "status": "running" if alive else ("stopped" if row["status"] == "stopped" else "dead"),
            "restart": bool(row.get("restart")),
            "note": row.get("note"),
            "started_at": row["started_at"],
            "stopped_at": row.get("stopped_at"),
            "log_path": row["log_path"],
        }

    async def rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM services ORDER BY started_at")]
        return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM services WHERE session_id = ? ORDER BY started_at", (session_id,))]

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        return [self.view(r) for r in await self.rows(session_id)]

    async def list_all(self) -> list[dict[str, Any]]:
        """Every session's services, each with the session it belongs to."""
        titles = {row["id"]: row["title"] for row in await self.app.db.fetchall("SELECT id, title FROM sessions")}
        out = []
        for r in await self.rows():
            out.append({**self.view(r), "session_id": r["session_id"], "session_title": titles.get(r["session_id"], "(deleted session)")})
        out.sort(key=lambda s: (s["status"] != "running", s["session_title"], s["name"]))
        return out

    async def get(self, session_id: str, name: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM services WHERE session_id = ? AND name = ?", (session_id, name))
        return dict(row) if row else None

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM services WHERE share_slug = ?", (slug,))
        return dict(row) if row else None

    # -- sharing --------------------------------------------------------------------

    async def share(self, session_id: str, name: str, mode: str, *, rotate_key: bool = False) -> dict[str, Any]:
        """Switch how the service is reached from outside: LAN only, anyone through the site, or key holders.

        The slug is minted once and kept across mode changes, so a link stays valid when the key is
        dropped; the key is minted when key mode is first chosen and replaced only on ``rotate_key``.
        """
        if mode not in SHARE_MODES:
            raise ValueError(f"share mode must be one of {', '.join(SHARE_MODES)}")
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        if mode != "local" and not row.get("port"):
            raise ValueError("a service without a port cannot be shared")
        slug = row.get("share_slug")
        if not slug:
            # The slug is the only secret of a public share: long enough that it cannot be guessed or crawled into.
            slug = f"{name}-{secrets.token_hex(12)}"
            while await self.by_slug(slug) is not None:
                slug = f"{name}-{secrets.token_hex(12)}"
        key = row.get("share_key")
        if mode == "key" and (not key or rotate_key):
            key = secrets.token_urlsafe(18)
        await self.app.db.execute("UPDATE services SET share_mode = ?, share_slug = ?, share_key = ? WHERE session_id = ? AND name = ?", (mode, slug, key, session_id, name))
        return self.view(await self.get(session_id, name) or {})

    def share_allows(self, row: dict[str, Any], presented_key: str | None) -> bool:
        """Whether a request from outside may reach the service: public, or key mode with the right key."""
        mode = row.get("share_mode") or "local"
        if mode == "public":
            return True
        if mode == "key" and row.get("share_key") and presented_key:
            return secrets.compare_digest(str(row["share_key"]), presented_key)
        return False

    # -- ports ----------------------------------------------------------------------

    async def allocate_port(self, wanted: int | str | None) -> int | None:
        """A port in the published range that no service holds and nothing listens on; None = no port."""
        if wanted in (None, "", "none"):
            return None
        taken = {int(r["port"]) for r in await self.rows() if r.get("port") and r["status"] == "running" and pid_alive(r.get("pid"))}
        if wanted != "auto":
            port = int(wanted)
            if not (self.lo <= port <= self.hi):
                raise ValueError(f"port {port} is outside the published range {self.lo}-{self.hi}; use port='auto' or one inside it")
            if port in taken or not port_free(port):
                raise ValueError(f"port {port} is in use")
            return port
        for port in range(self.lo, self.hi + 1):
            if port not in taken and port_free(port):
                return port
        raise ValueError(f"no free port in {self.lo}-{self.hi}; stop a service first")

    # -- lifecycle ------------------------------------------------------------------

    async def start(self, session_id: str, *, name: str, command: str, cwd: str | None = None, port: int | str | None = "auto", restart: bool = True) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        name = name.strip().lower().replace(" ", "-")
        if not name or len(name) > NAME_MAX or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError("a service name is 1–32 characters: letters, digits, '-' or '_'")
        command = command.strip()
        if not command:
            raise ValueError("the command is empty")
        existing = await self.get(session_id, name)
        if existing and existing["status"] == "running" and pid_alive(existing.get("pid")):
            raise ValueError(f"service {name!r} is already running (pid {existing['pid']}); ServiceStop it first")
        workdir = Path(cwd) if cwd else state.workspace
        if not workdir.is_absolute():
            workdir = state.workspace / workdir
        if not workdir.is_dir():
            raise ValueError(f"working directory does not exist: {workdir}")
        chosen = await self.allocate_port(port)
        log_dir = state.workspace / LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"
        env = shell_environment(session_id, {"PORT": str(chosen)} if chosen else None)
        env["HOST"] = "0.0.0.0"
        # The same wall Exec has: a service is a long-lived command, not a way around the sandbox.
        session_services = manager.locator_services(session_id)
        argv, _sandboxed = await sandbox_argv(command, workdir, state.workspace, self.app.config.tools.exec, writable=getattr(session_services, "writable", ()))
        with open(log_path, "ab") as log:
            log.write(f"\n=== {_now()} start: {command}\n".encode())
            proc = subprocess.Popen(argv, cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)  # noqa: S603
        self._procs[(session_id, name)] = proc
        await self.app.db.execute(
            "INSERT INTO services(session_id, name, command, cwd, port, pid, status, restart, log_path, note, started_at, stopped_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL)"
            " ON CONFLICT(session_id, name) DO UPDATE SET command = excluded.command, cwd = excluded.cwd, port = excluded.port, pid = excluded.pid, status = 'running', restart = excluded.restart, log_path = excluded.log_path, note = NULL, started_at = excluded.started_at, stopped_at = NULL",
            (session_id, name, command, str(workdir), chosen, proc.pid, int(restart), str(log_path), _now()),
        )
        # A command that dies within a moment is a broken command, not a service: say so with the log.
        await asyncio.sleep(1.5)
        if proc.poll() is not None:
            await self.app.db.execute("UPDATE services SET status = 'dead', note = ?, stopped_at = ? WHERE session_id = ? AND name = ?", (f"exited with code {proc.returncode} right after start", _now(), session_id, name))
            tail = await self.logs(session_id, name, 40)
            raise RuntimeError(f"service {name!r} exited with code {proc.returncode} right after start. Log:\n{tail}")
        return self.view(await self.get(session_id, name) or {})

    async def stop(self, session_id: str, name: str, *, note: str = "stopped") -> dict[str, Any]:
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        pid = row.get("pid")
        if row["status"] == "running" and pid_alive(pid):
            await self._terminate(int(pid))
        self._procs.pop((session_id, name), None)
        await self.app.db.execute("UPDATE services SET status = 'stopped', note = ?, stopped_at = ?, restart = 0 WHERE session_id = ? AND name = ?", (note, _now(), session_id, name))
        return self.view(await self.get(session_id, name) or {})

    async def _terminate(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            logger.warning("could not signal service group %s", pid)
        deadline = asyncio.get_running_loop().time() + STOP_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline and pid_alive(pid):
            await asyncio.sleep(0.2)
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc = next((p for p in self._procs.values() if p.pid == pid), None)
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    async def remove(self, session_id: str, name: str) -> bool:
        row = await self.get(session_id, name)
        if row is None:
            return False
        if row["status"] == "running" and pid_alive(row.get("pid")):
            await self._terminate(int(row["pid"]))
        await self.app.db.execute("DELETE FROM services WHERE session_id = ? AND name = ?", (session_id, name))
        self._procs.pop((session_id, name), None)
        return True

    async def stop_all(self, session_id: str) -> int:
        n = 0
        for row in await self.rows(session_id):
            if row["status"] == "running" and pid_alive(row.get("pid")):
                await self._terminate(int(row["pid"]))
                n += 1
        await self.app.db.execute("DELETE FROM services WHERE session_id = ?", (session_id,))
        return n

    async def logs(self, session_id: str, name: str, lines: int = 60) -> str:
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        path = Path(row["log_path"])
        if not path.exists():
            return "(no log yet)"
        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"(log unreadable: {exc})"
        text = data[-200_000:].decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-max(1, lines):]) or "(empty)"

    # -- boot -----------------------------------------------------------------------

    async def reconcile(self) -> None:
        """After a restart: what the table calls running either still runs, is restarted, or is reported dead."""
        manager = self.app.manager
        assert manager is not None
        inbox = self.app.extensions.get("inbox")
        for row in await self.rows():
            if row["status"] != "running":
                continue
            if pid_alive(row.get("pid")):
                continue  # survived the bot restart (reparented); the table is right
            sid, name = row["session_id"], row["name"]
            if row.get("restart") and await manager.get_state(sid) is not None:
                try:
                    await self.start(sid, name=name, command=row["command"], cwd=row["cwd"], port=row.get("port") or None, restart=True)
                    logger.warning("service %s/%s restarted after the rebuild", sid, name)
                    continue
                except (ValueError, RuntimeError, KeyError) as exc:
                    await self.app.db.execute("UPDATE services SET status = 'dead', note = ?, stopped_at = ? WHERE session_id = ? AND name = ?", (f"could not restart after the rebuild: {exc}"[:400], _now(), sid, name))
                    if inbox is not None:
                        await inbox.post("service", f"Service '{name}' did not come back", str(exc)[:400], severity="warning", session_id=sid)
                    continue
            await self.app.db.execute("UPDATE services SET status = 'dead', note = 'not running after the restart', stopped_at = ? WHERE session_id = ? AND name = ?", (_now(), sid, name))
            if inbox is not None:
                await inbox.post("service", f"Service '{name}' is not running", "It did not survive the restart; ServiceStart runs it again.", severity="notice", session_id=sid)

    # -- service hook for the tools ---------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        sid = kwargs.pop("session_id")
        if op == "start":
            return await self.start(sid, **kwargs)
        if op == "stop":
            return await self.stop(sid, kwargs["name"])
        if op == "list":
            return await self.list(sid)
        if op == "logs":
            return await self.logs(sid, kwargs["name"], int(kwargs.get("lines") or 60))
        if op == "share":
            return await self.share(sid, kwargs["name"], kwargs.get("mode") or "local")
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    services = Services(app)
    app.extensions["services"] = services
    assert app.manager is not None
    app.manager.service_hooks["services"] = services.service
    app.manager.delete_hooks.append(services.stop_all)
    await services.reconcile()
    return []
