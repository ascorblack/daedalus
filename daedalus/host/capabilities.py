"""What this installation is actually able to do, decided once at startup.

Self-development is not a property of the code but of the installation around it: a server with a
GitHub token and a rebuild channel can open pull requests against its own repositories and replace
itself; a desktop install has a checkout and nothing else; an install with neither should not carry
tools, routes, prompt text or a navigation entry for any of it.

``selfdev.mode`` names which of the three it is. The operator sets it in the configuration
(``[self_change] mode = "off" | "local" | "server"``); ``auto``, the default, is resolved here from
the prerequisites that are really present. The resolution is pure — the same settings and
configuration always give the same answer — so the doctor can recompute it without a running
session manager.

The mode is fixed for the life of the process: tools are registered, routes are mounted and the
prompt is assembled from it at startup. Changing it in the configuration takes effect on a restart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from daedalus.config import RuntimeConfig, Settings

SelfDevMode = Literal["off", "local", "server"]
SELFDEV_MODES: tuple[SelfDevMode, ...] = ("off", "local", "server")

SELFDEV_TOOLS: dict[SelfDevMode, frozenset[str]] = {
    "off": frozenset(),
    # Local mode has a checkout and no remote: a worktree to work in is the part that already means
    # something. The tool that applies a local change — commit, preflight, restart — joins this set
    # when it exists; nothing else has to change here for it.
    "local": frozenset({"SelfWorkspace"}),
    "server": frozenset({"SelfWorkspace", "SelfPropose", "SelfRebuild", "SelfRollback"}),
}
"""The self-development tools each mode registers. Every tool a mode leaves out is not registered at
all, so the model never sees a name it cannot call."""

ALL_SELFDEV_TOOLS: frozenset[str] = frozenset().union(*SELFDEV_TOOLS.values())


@dataclass(frozen=True, slots=True)
class SelfDev:
    """The resolved self-development mode, what was asked for, and why it came out this way."""

    mode: SelfDevMode
    configured: str
    """What the configuration asked for: ``auto`` or an explicit mode."""
    reasons: list[str] = field(default_factory=list)
    """Prose the operator reads in the doctor and the app: what was found, in the order it was probed."""
    missing: list[str] = field(default_factory=list)
    """Prerequisites of ``mode`` that are absent. Non-empty only when the operator named the mode
    explicitly; an ``auto`` resolution never lands on a mode whose prerequisites it did not find."""

    @property
    def tools(self) -> frozenset[str]:
        return SELFDEV_TOOLS[self.mode]

    @property
    def disabled_tools(self) -> frozenset[str]:
        """Tool names discovery must skip in this mode."""
        return ALL_SELFDEV_TOOLS - self.tools

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "configured": self.configured, "reasons": list(self.reasons), "missing": list(self.missing), "tools": sorted(self.tools)}


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Everything the app tells its surfaces about what this installation can do."""

    selfdev: SelfDev

    def as_dict(self) -> dict[str, Any]:
        return {"selfdev": self.selfdev.as_dict()}


# -- prerequisite probes ------------------------------------------------------------------


def _git_dir(repo: Path) -> Path | None:
    """The repository's git directory, for a normal checkout and for a linked worktree alike."""
    marker = repo / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.partition(":")[2].strip())
    if not target.is_absolute():
        target = (repo / target).resolve()
    # A linked worktree's git directory is <main>/.git/worktrees/<name>; the remotes are the main one's.
    if target.parent.name == "worktrees":
        target = target.parent.parent
    return target if target.is_dir() else None


def writable_checkout(repo: Path) -> bool:
    """A git checkout this process may write to — the minimum for changing one's own code at all."""
    git_dir = _git_dir(repo)
    return git_dir is not None and os.access(repo, os.W_OK)


def has_origin(repo: Path) -> bool:
    """Whether the checkout has an ``origin`` remote to push a branch to.

    Read from the git configuration rather than asked of git: this runs on the way up, and a probe
    that can block on a subprocess is a probe that can delay every start.
    """
    git_dir = _git_dir(repo)
    if git_dir is None:
        return False
    try:
        text = (git_dir / "config").read_text(encoding="utf-8")
    except OSError:
        return False
    return '[remote "origin"]' in text


def rebuild_channel(settings: Settings) -> str:
    """How a new build reaches the running process, or an empty string when nothing can deliver one."""
    if settings.supervisor_socket.exists():
        return "the supervisor socket"
    compose = settings.bot_repo_dir / "deploy" / "compose.yaml"
    try:
        text = compose.read_text(encoding="utf-8")
    except OSError:
        return ""
    return "the compose rebuilder" if "\n  rebuilder:" in text else ""


# -- resolution ---------------------------------------------------------------------------


def _probe(settings: Settings) -> tuple[dict[str, bool], list[str]]:
    """What is present, and one line per finding."""
    bot, core = settings.bot_repo_dir, settings.core_repo_dir
    found = {
        "checkouts": writable_checkout(bot) and writable_checkout(core),
        "token": bool(settings.github_token.strip()),
        "remotes": has_origin(bot) and has_origin(core),
        "rebuild": bool(rebuild_channel(settings)),
    }
    channel = rebuild_channel(settings)
    reasons = [
        "both repositories are writable git checkouts" if found["checkouts"] else "the host and core repositories are not both writable git checkouts",
        "a GitHub token is configured" if found["token"] else "no GitHub token is configured",
        "both checkouts have an origin remote" if found["remotes"] else "the checkouts have no origin remote to push to",
        f"a rebuild reaches the running process through {channel}" if channel else "nothing can deliver a new build to the running process",
    ]
    return found, reasons


_PREREQUISITES: dict[SelfDevMode, tuple[tuple[str, str], ...]] = {
    "off": (),
    "local": (("checkouts", "a writable git checkout of the host and the core"),),
    "server": (
        ("checkouts", "a writable git checkout of the host and the core"),
        ("token", "a GitHub token"),
        ("remotes", "an origin remote on both checkouts"),
        ("rebuild", "a rebuild channel (the supervisor socket or the compose rebuilder)"),
    ),
}


def resolve_selfdev(settings: Settings, config: RuntimeConfig) -> SelfDev:
    """The mode this installation runs in, from the configuration and what is really there."""
    configured = str(config.self_change.mode or "auto")
    found, reasons = _probe(settings)
    server_ready = all(found[key] for key, _ in _PREREQUISITES["server"])
    local_ready = all(found[key] for key, _ in _PREREQUISITES["local"])
    if configured == "auto":
        mode: SelfDevMode = "server" if server_ready else "local" if local_ready else "off"
        headline = {
            "server": "self-development runs against the repositories on GitHub",
            "local": "self-development is local: the checkout is edited and applied by a restart",
            "off": "self-development is off: this installation cannot change its own code",
        }[mode]
        return SelfDev(mode=mode, configured=configured, reasons=[headline, *reasons])
    mode = configured if configured in SELFDEV_MODES else "off"  # type: ignore[assignment]
    missing = [label for key, label in _PREREQUISITES[mode] if not found[key]]
    headline = f"the operator set selfdev.mode = {mode}" if configured in SELFDEV_MODES else f"selfdev.mode = {configured!r} is not a mode; treated as off"
    return SelfDev(mode=mode, configured=configured, reasons=[headline, *reasons], missing=missing)


def resolve(settings: Settings, config: RuntimeConfig) -> Capabilities:
    return Capabilities(selfdev=resolve_selfdev(settings, config))


__all__ = [
    "ALL_SELFDEV_TOOLS",
    "SELFDEV_MODES",
    "SELFDEV_TOOLS",
    "Capabilities",
    "SelfDev",
    "SelfDevMode",
    "has_origin",
    "rebuild_channel",
    "resolve",
    "resolve_selfdev",
    "writable_checkout",
]
