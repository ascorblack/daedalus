"""Fake command-line agents for the harness tests: stand-ins for Claude Code, Codex, OpenCode, pi and
Grok Build that run in a real terminal, draw a composer, take the real CLI's arguments, and talk
back through the same channels the real one does (hooks, an app server, an HTTP server with server
events, a bridge socket, session files) — with a scripted "model" instead of a network.

``install(bin_dir)`` puts executables named like the real CLIs on a directory for ``PATH``; the
scripts use nothing but the standard library, so they start quickly and need no environment.
Each one's behaviour, and the quirks it imitates, is described at the top of its module.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CLIS = {
    "claude": "fake_claude.py",
    "codex": "fake_codex.py",
    "opencode": "fake_opencode.py",
    "pi": "fake_pi.py",
    "grok": "fake_grok.py",
}
"""The command name each fake answers to, as a launch plan's ``argv[0]`` names the real one."""

HELPERS = {
    "hook-post": "fake_hook_post.py",
}
"""Stand-ins for the terminal daemon's ``hook-post`` command (and, through the ``ptyd`` script
``install`` writes, for ``ptyd team-mcp``, ``ptyd hook`` and ``ptyd hook-post``)."""


def install(bin_dir: Path, *, python: str | None = None, ptyd: Path | None = None) -> dict[str, Path]:
    """Write one executable per fake (and helper) into ``bin_dir`` and return them by name.

    Each is a two-line shell script running the fake with this interpreter, so the name a launch
    plan uses resolves on ``PATH`` exactly as the real CLI's would, and ``exec.run``'s allowlist,
    which goes by the program's name, sees the real name too.

    ``ptyd`` names a built daemon: ``ptyd`` and ``hook-post`` are then links to it (the daemon runs
    as ``hook-post`` when called by that name), so the fakes use the real bridge commands.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = python or sys.executable
    out: dict[str, Path] = {}
    for name, script in {**CLIS, **HELPERS}.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\nexec {shlex.quote(interpreter)} {shlex.quote(str(HERE / script))} \"$@\"\n")
        path.chmod(0o755)
        out[name] = path
    if ptyd is not None:
        for name in ("ptyd", "hook-post"):
            (bin_dir / name).unlink(missing_ok=True)
            (bin_dir / name).symlink_to(ptyd.resolve())
            out[name] = bin_dir / name
        return out
    # The daemon's own binary, as far as a launch uses it: ``<ptyd> team-mcp`` in an MCP entry and
    # ``<ptyd> hook <source>`` in a command hook.
    stand_in = bin_dir / "ptyd"
    py = shlex.quote(interpreter)
    hook_post = shlex.quote(str(HERE / "fake_hook_post.py"))
    stand_in.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  team-mcp) shift; exec {py} {shlex.quote(str(HERE / "fake_team_mcp.py"))} "$@" ;;\n'
        f'  hook-post) shift; exec {py} {hook_post} "$@" ;;\n'
        f'  hook) shift; exec {py} {hook_post} --always-zero "$@" ;;\n'
        '  *) echo "the fake ptyd knows only team-mcp, hook and hook-post" >&2; exit 2 ;;\n'
        "esac\n"
    )
    stand_in.chmod(0o755)
    out["ptyd"] = stand_in
    return out


def search_path(bin_dir: Path, base: str | None = None) -> str:
    """``PATH`` with the fakes first, so no real CLI on the machine can answer in their place."""
    rest = os.environ.get("PATH", "/usr/bin:/bin") if base is None else base
    return f"{bin_dir}{os.pathsep}{rest}"


__all__ = ["CLIS", "HELPERS", "HERE", "install", "search_path"]
