"""The contract every command-line harness adapter keeps, and the values that cross it.

An adapter knows one CLI: how to launch it inside a terminal, how to tell when it is ready, busy,
asking or done, how to hand it a message and prove it arrived, and where it keeps its transcript.
It knows nothing of staff, projects, queues or the database. The staff runtime for command-line
staff wraps an adapter, feeds its events through the state machine (``state.py``) and reports to the
orchestrator's ingress; the harness manager uses the version, catalog and sign-in half.

The adapter never touches a terminal or an environment directly. It is handed two narrow ports,
``TerminalPort`` for the one terminal it drives and ``EnvironmentPort`` for the environment the CLI
lives in, both implemented over the terminal daemon's connection. That keeps an adapter testable
against a fake daemon, and keeps every write to a terminal on the one audited path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from daedalus.harness.capabilities import Capabilities

Environment = Literal["container", "host"]
SendMode = Literal["queue", "steer", "interrupt"]
DeliveryState = Literal["queued", "written", "submitted", "acknowledged", "failed"]

ANSWER_CHOICES = ("allow_once", "allow_always", "deny", "deny_with_note")
"""What a permission answer can be, whatever words the CLI's dialog uses for it. A question is
answered with one of its own options by label, or with free text."""


class ScreenClass(StrEnum):
    """What the terminal's screen shows, as the adapter reads it. Only ever a fallback: the screen is
    read to find readiness, a dialog, or a turn end that no structured signal reported."""

    IDLE_COMPOSER = "idle_composer"
    BUSY = "busy"
    DIALOG = "dialog"
    UNKNOWN = "unknown"


class EventKind(StrEnum):
    """The normalised events an adapter emits. Only the state machine turns them into a status."""

    READY = "ready"
    PROMPT_ACKNOWLEDGED = "prompt_acknowledged"
    TURN_STARTED = "turn_started"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    ACTIVITY = "activity"
    PERMISSION_REQUESTED = "permission_requested"
    QUESTION_ASKED = "question_asked"
    REQUEST_RESOLVED = "request_resolved"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_FAILED = "turn_failed"
    SEEN = "seen"
    SESSION_ENDED = "session_ended"
    PROCESS_EXITED = "process_exited"
    QUIET = "quiet"
    RECONCILED = "reconciled"
    NOTIFICATION = "notification"
    USAGE = "usage"
    TRANSCRIPT = "transcript"


@dataclass(frozen=True, slots=True)
class StaffEvent:
    """One thing a CLI did, in the adapter's words normalised.

    The payload keys the state machine reads: ``summary`` (what a permission or question is about),
    ``failure`` (why a turn failed), ``screen`` (a ``ScreenClass`` value for ``reconciled``),
    ``exit_code``. Anything else is for the log and the feed.
    """

    kind: EventKind
    at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    native_id: str = ""
    """The CLI's own handle for what the event is about: a tool-use id, a JSON-RPC id, a request id."""
    launch_id: str = ""


@dataclass(frozen=True, slots=True)
class InstallInfo:
    installed: bool
    path: str = ""
    version: str = ""
    method: str = ""
    """How it was installed (``native``, ``npm``, ``installer``), which decides how it is updated."""
    detail: str = ""
    """Why it counts as not installed, when a binary of that name is something else."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    ok: bool
    previous: str
    version: str
    output: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class CheckStep:
    name: str
    """``launch``, ``hook``, ``ready``, ``deliver``, ``reply``, ``exit``."""
    ok: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    steps: tuple[CheckStep, ...]
    version: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class AgentEntry:
    name: str
    source: str
    """``project``, ``user`` or ``builtin``: where the definition lives."""
    description: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class Catalog:
    agents: tuple[AgentEntry, ...] = ()
    models: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    """Permission modes, or sandbox and approval choices, by the names the CLI's own flags take."""
    profiles: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoginState:
    state: Literal["yes", "no", "unknown"]
    detail: str = ""
    """What the CLI said, without secrets: an account name or a plan, never a token."""


@dataclass(frozen=True, slots=True)
class HookSpec:
    """The hooks a launch listens for. ``hold_ms`` keeps the request open at the daemon's ingress
    until the host replies, for hooks whose answer is the decision (a permission, a team question)."""

    sources: tuple[str, ...] = ()
    hold_ms: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompanionSpec:
    """A second process the TUI needs, run in a terminal of its own before the TUI starts."""

    role: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    ready_pattern: str = ""
    """A regular expression on the companion's output that says it is listening."""


LAUNCH_DIR = "{launch_dir}"
"""Stands for the launch's private directory in ``LaunchPlan.argv`` and ``env`` values. The daemon
creates the directory when the launch is registered, so the adapter cannot know its path; the
runtime substitutes it before the terminal is created."""


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything an adapter needs to plan a launch, composed by the host.

    The host composes the brief and the first message (persona, instructions, notes, task), so the
    adapter never reads the board or the staff tables; it only decides how this CLI is given them.
    """

    harness: str
    env: Environment
    cwd: str
    launch_id: str
    first_prompt: str | None
    brief_text: str = ""
    model: str = ""
    effort: str = ""
    agent: str = ""
    permission_mode: str = ""
    permission_level: Literal["ask", "edits", "all"] = "ask"
    """What the project's autonomy lets through without asking, for the adapter to map onto the
    CLI's own mode when the member has no ``permission_mode`` of its own."""
    team_url: str = ""
    team_token: str = ""
    """Left empty by the runtime: the team tools post to the launch's hook listener, which a CLI
    reaches from either environment, while the host's own team route is loopback of the host's
    container and unreachable from the terminals container."""
    session_ref: str = ""
    """The CLI session to resume; empty for a new one."""


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    files: Mapping[str, bytes] = field(default_factory=dict)
    """Overlay files by name, written by the daemon into the launch directory and removed with it —
    never into the CLI's own configuration."""
    ports: tuple[int, ...] = ()
    """Loopback ports the launch listens on, which the daemon lets the host dial."""
    companions: tuple[CompanionSpec, ...] = ()
    session_ref: str = ""
    """The CLI session id chosen before the launch, where the CLI allows choosing it."""
    transcript_hint: str | None = None
    first_prompt: str | None = None
    first_prompt_via: Literal["argv", "channel"] = "argv"
    hooks: HookSpec = field(default_factory=HookSpec)


@dataclass(frozen=True, slots=True)
class Launch:
    """A launch as the host remembers it, so a restarted host can attach to a CLI still running."""

    launch_id: str
    staff_session_id: str
    harness: str
    env: Environment
    terminal_id: str | None
    companion_terminal_id: str | None
    launch_dir: str
    session_ref: str
    harness_version: str
    started_at: str
    ended_at: str | None = None


@dataclass(frozen=True, slots=True)
class HookPost:
    """One post to the launch's hook listener, as the adapter's ``events`` reads it.

    ``name`` is the last part of the hook's path (a Claude event name, ``grok``, ``pi``); ``reply_id``
    is set when the post is held for an answer, which ``TerminalPort.reply`` gives. The team tools'
    posts (``name == "team"``) never reach an adapter: the runtime answers those itself.
    """

    name: str
    body: Any
    at: str = ""
    reply_id: str | None = None
    hold_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReadyStep:
    """What the readiness gate does about the screen it just read, as the adapter judges it.

    ``wait``: nothing recognised yet, or the CLI is still drawing. ``keys``: a dialog the launch
    answers (the folder-trust question), with the keys that answer it — only after the adapter has
    checked that the highlighted row is the one those keys choose. ``fail``: the CLI will not get
    ready by itself (a sign-in screen), and ``reason`` says why in the operator's words.
    """

    action: Literal["wait", "keys", "fail"] = "wait"
    keys: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Answer:
    """A decision on a pending request, in the harness's normalised terms."""

    choice: str
    """One of ``ANSWER_CHOICES`` for a permission; an option's label or ``text`` for a question."""
    note: str = ""


@dataclass(frozen=True, slots=True)
class Delivery:
    """How far a message got, as the adapter knows it. A write to the terminal is ``written`` and
    never more: only the CLI saying it took the prompt makes it ``acknowledged``."""

    message_id: str
    state: DeliveryState
    via: str = ""
    """``paste``, ``pointer`` or the structured channel's name."""
    degraded_to: Literal["", "queue", "interrupt"] = ""
    client_ref: str = ""
    """The id the CLI echoes back for this message, where it takes one, so a restarted host can find it."""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ToolUse:
    name: str
    summary: str
    ok: bool | None = None


@dataclass(frozen=True, slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn of a transcript, read from the CLI's own store, never from the screen."""

    index: int
    role: Literal["user", "assistant", "orchestrator", "system"]
    text: str
    tools: tuple[ToolUse, ...] = ()
    started_at: str = ""
    ended_at: str = ""
    usage: TurnUsage | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class TerminalPort(Protocol):
    """The one terminal an adapter drives. Every write goes through the daemon's writer queue and the
    audit, and waits for the human's quiet time unless the caller is the human."""

    @property
    def id(self) -> str: ...

    @property
    def env(self) -> Environment: ...

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None) -> None: ...

    async def screen(self, *, scrollback: int = 0) -> str: ...

    async def modes(self) -> Mapping[str, bool]:
        """The terminal modes of interest, by name (``bracketed_paste``, ``alt_screen``)."""
        ...

    async def wait_for(self, *, regex: str | None = None, idle_ms: int | None = None, timeout: float) -> bool: ...

    def hooks(self) -> AsyncIterator[HookPost]:
        """The launch's hook posts in the daemon's order, ending when the launch ends. One reader:
        the adapter's ``events``."""
        ...

    async def reply(self, reply_id: str, body: Any) -> bool:
        """Answer a held post; false when nothing waits any more (the hold expired, the CLI went)."""
        ...


class EnvironmentPort(Protocol):
    """The environment a CLI lives in, through the daemon's allowlisted side channels."""

    @property
    def name(self) -> Environment: ...

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult: ...

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes: ...

    async def stat(self, path: str) -> Mapping[str, Any] | None: ...

    async def list(self, path: str) -> list[str]: ...


@runtime_checkable
class HarnessAdapter(Protocol):
    """One command-line agent.

    Rules every adapter keeps: the CLI is the terminal's own process; hooks and settings go per
    launch, never into the user's CLI configuration; the CLI's own updater is off; a write is never
    an acknowledgement; no Enter while a permission or a dialog is open; and a notification that the
    CLI is idle or wants input is never taken for a question.
    """

    name: str
    capabilities: Capabilities

    async def installed(self, env: EnvironmentPort) -> InstallInfo: ...

    async def latest(self, env: EnvironmentPort) -> str: ...

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        """Run the CLI's own updater; the manager runs ``self_check`` after it."""
        ...

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        """Launch, see a hook arrive, deliver one line, see it acknowledged, exit."""
        ...

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog: ...

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        """Asked of the CLI itself, never by reading its credential files."""
        ...

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan: ...

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan: ...

    def readiness(self, screen: str) -> ReadyStep:
        """The readiness gate's judgement of one screen. The runtime runs the gate: it reads the
        screen until the CLI's ready signal arrives through ``events``, types what this answers for a
        dialog, and gives up with this reason or after its timeout."""
        ...

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """Called once the gate saw the CLI ready: hand over the first prompt when it goes by
        channel. Nothing is typed before the CLI proved it is ready."""
        ...

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        """Take up a launch that outlived a host restart: re-dial side channels and re-read the
        transcript's tail, so ``events`` continues where the previous host stopped."""
        ...

    def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]: ...

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery: ...

    async def interrupt(self, term: TerminalPort) -> None: ...

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        """Deliver a decision; true once the CLI confirmed it (the tool ran, was declined, or the
        dialog closed), false when it could not be confirmed and the operator must answer in the
        terminal."""
        ...

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]: ...

    async def stop(self, term: TerminalPort) -> None:
        """Ask the CLI to exit; the runtime kills the terminal after the grace period."""
        ...

    def classify_screen(self, text: str) -> ScreenClass: ...

    def composer_holds(self, screen: str, text: str) -> bool:
        """Whether the composer on ``screen`` holds ``text`` (or this CLI's paste marker for it).
        When unsure, false: a message that is not seen in the composer is not submitted."""
        ...


__all__ = [
    "ANSWER_CHOICES",
    "LAUNCH_DIR",
    "AgentEntry",
    "Answer",
    "Catalog",
    "CheckResult",
    "CheckStep",
    "CompanionSpec",
    "Delivery",
    "DeliveryState",
    "Environment",
    "EnvironmentPort",
    "EventKind",
    "ExecResult",
    "HarnessAdapter",
    "HookPost",
    "HookSpec",
    "InstallInfo",
    "Launch",
    "LaunchPlan",
    "LaunchSpec",
    "LoginState",
    "ReadyStep",
    "ScreenClass",
    "SendMode",
    "StaffEvent",
    "TerminalPort",
    "ToolUse",
    "Turn",
    "TurnUsage",
    "UpdateResult",
]
