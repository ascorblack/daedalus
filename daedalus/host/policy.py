"""Tool policy: what a call may do, decided from its arguments before it runs.

The container is the agent's; the operator's machine, secrets, repositories and the outside world
are not. Governance says so in prose; this module says it in rules the dispatcher enforces on every
call. A rule looks at the tool and its arguments (a shell command is parsed into its simple commands
first, so ``cd x && rm -rf /`` is seen as ``rm -rf /``) and answers allow, deny or ask.

*deny* is final. *ask* is a denial the operator can lift: the answer carries an approval key, the
operator grants it (``/allow <key>`` in chat, the Mini App, or the API), and the same call passes
once. The built-in rules live here, in the repository, so they change only through a reviewed pull
request; the operator's rules in ``config.toml`` can add denials and questions but can never allow
what a built-in rule denies.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOW, ASK, DENY = "allow", "ask", "deny"
_SEVERITY = {ALLOW: 0, ASK: 1, DENY: 2}

OPERATORS = {";", "&&", "||", "|", "(", ")", "&"}
WRAPPERS = {"sudo", "nohup", "time", "nice", "env", "exec", "command", "builtin", "stdbuf", "timeout"}
SHELLS = {"bash", "sh", "zsh", "dash"}
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "socat"}
DANGEROUS_TARGETS = {"/", "/*", "~", "~/", "$HOME", "/srv", "/srv/*", "/opt", "/etc", "/usr", "/var", "/home", "/root", "/boot", "/lib", "/bin", "/sbin", "/proc", "/sys", "/dev"}
OPERATOR_CHECKOUTS = ("/srv/daedalus", "/srv/protocore-exp")
"""The operator's repositories as mounted in the container: they change only through pull requests."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    tool: str
    """A tool name, or ``*`` for every tool."""
    action: str
    note: str
    pattern: str = ""
    """A regular expression over the call's canonical text (a shell command, a URL, or the JSON of the arguments)."""
    source: str = "builtin"


@dataclass(slots=True)
class Decision:
    action: str
    reason: str = ""
    rule: str = ""
    key: str = ""
    """The approval key of an ``ask`` decision; a grant for it lets the same call through once."""
    hosts: list[str] = field(default_factory=list)
    """Network hosts the call reaches, for the egress log."""


def canonical(tool: str, arguments: dict[str, Any]) -> str:
    if tool in ("Exec", "Verify"):
        return str(arguments.get("command") or "")
    if tool in ("WebFetch",):
        return str(arguments.get("url") or "")
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def approval_key(tool: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256((tool + "\x00" + canonical(tool, arguments)).encode("utf-8")).hexdigest()[:12]


def shell_segments(command: str, *, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell command line, each as its argv, wrappers stripped.

    ``cd a && sudo rm -rf /`` → ``[["cd", "a"], ["rm", "-rf", "/"]]``. ``bash -c "…"`` is opened one
    level. Unparseable input (an unbalanced quote) comes back as a single segment of whitespace-split
    words: the rules still see every word.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in OPERATORS or tok in (";;", "&&", "||", "|&", "<", ">", ">>", "<<"):
            if tok in ("<", ">", ">>", "<<"):
                current.append(tok)
                continue
            if current:
                segments.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    out: list[list[str]] = []
    for seg in segments:
        words = list(seg)
        while words and (words[0] in WRAPPERS or "=" in words[0] and not words[0].startswith(("-", "/", ".")) and words[0].split("=", 1)[0].isidentifier()):
            head = words.pop(0)
            if head == "timeout" and words and re.fullmatch(r"\d+[smhd]?", words[0]):
                words.pop(0)
            if head in ("sudo", "env", "nice") and words and words[0].startswith("-"):
                words.pop(0)
        if not words:
            continue
        if words[0] in SHELLS and depth < 3:
            inner = next((words[i + 1] for i, w in enumerate(words) if w in ("-c", "-lc", "-ec", "-ic") and i + 1 < len(words)), None)
            if inner:
                out.extend(shell_segments(inner, depth=depth + 1))
                continue
        out.append(words)
    return out


def _flags(words: Iterable[str]) -> str:
    return "".join(w.lstrip("-") for w in words if w.startswith("-") and not w.startswith("--"))


def _redirect_targets(words: list[str]) -> list[str]:
    targets = []
    for i, w in enumerate(words):
        if w in (">", ">>") and i + 1 < len(words):
            targets.append(words[i + 1])
    return targets


def _host_of(token: str) -> str | None:
    """The host a network argument points at: a URL, ``user@host``, ``host:path``, or a bare name."""
    if "://" in token:
        return (urlsplit(token).hostname or "").lower() or None
    if "@" in token and not token.startswith("-"):
        return token.rsplit("@", 1)[1].split(":", 1)[0].lower() or None
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?", token) or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", token):
        return token.split(":", 1)[0].lower()
    return None


def hosts_in(segments: list[list[str]]) -> list[str]:
    hosts: list[str] = []
    for words in segments:
        if not words or words[0] not in NETWORK_COMMANDS and not (words[0] == "git" and len(words) > 1 and words[1] in ("clone", "fetch", "pull", "push", "ls-remote")):
            continue
        for tok in words[1:]:
            host = _host_of(tok)
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def host_allowed(host: str, allow: Iterable[str]) -> bool:
    for entry in allow:
        entry = entry.lower().strip()
        if not entry:
            continue
        if entry.startswith("*."):
            if host == entry[2:] or host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _under(path: str, roots: Iterable[str]) -> bool:
    p = path.rstrip("/") or "/"
    return any(p == r.rstrip("/") or p.startswith(r.rstrip("/") + "/") for r in roots)


class Policy:
    """The rule set: built-ins plus the operator's, evaluated per call."""

    def __init__(self, *, protected_paths: Iterable[Path] = (), egress_allow: Iterable[str] = (), rules: Iterable[Rule] = (), workspace_roots: Iterable[Path] = ()) -> None:
        self.protected = [str(p) for p in protected_paths]
        self.egress_allow = [e for e in egress_allow if e.strip()]
        self.rules = list(rules)
        self.workspace_roots = [str(p) for p in workspace_roots]

    # -- built-in judgement -----------------------------------------------------------

    def _shell(self, command: str, cwd: str | None) -> Decision:
        segments = shell_segments(command)
        hosts = hosts_in(segments)
        worst = Decision(ALLOW, hosts=hosts)

        def escalate(action: str, reason: str, rule: str) -> None:
            nonlocal worst
            if _SEVERITY[action] > _SEVERITY[worst.action]:
                worst = Decision(action, reason, rule, hosts=hosts)

        joined = " ".join(" ".join(s) for s in segments)
        if re.search(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", command):
            escalate(DENY, "a fork bomb", "shell.forkbomb")
        for words in segments:
            head = words[0]
            if head in ("mkfs", "shutdown", "reboot", "halt", "poweroff") or head.startswith("mkfs."):
                escalate(DENY, f"`{head}` acts on the machine, not on the workspace", "shell.machine")
            if head == "dd" and any(w.startswith("of=/dev/") for w in words):
                escalate(DENY, "writing a raw device", "shell.rawdevice")
            if head == "rm":
                flags = _flags(words[1:])
                recursive = "r" in flags or "R" in flags or "--recursive" in words
                targets = [w for w in words[1:] if not w.startswith("-")]
                if recursive and any(t in DANGEROUS_TARGETS or t.rstrip("/") in DANGEROUS_TARGETS for t in targets):
                    escalate(DENY, f"recursive delete of {', '.join(targets)}", "shell.rm_root")
                elif recursive and any(_under(t, self.protected + list(OPERATOR_CHECKOUTS)) for t in targets):
                    escalate(DENY, f"recursive delete inside a protected path ({', '.join(targets)})", "shell.rm_protected")
                elif recursive and self.workspace_roots and any(t.rstrip("/") in self.workspace_roots or str(Path(t).parent).rstrip("/") in self.workspace_roots for t in targets):
                    escalate(ASK, "recursive delete of a whole workspace directory", "shell.rm_workspace")
            if head in ("chmod", "chown") and "R" in _flags(words[1:]) and any(t in DANGEROUS_TARGETS for t in words[1:] if not t.startswith("-")):
                escalate(DENY, f"recursive `{head}` on a system path", "shell.chmod_root")
            written = _redirect_targets(words) + ([words[-1]] if head in ("cp", "mv", "install", "rsync", "tee") and len(words) > 1 else []) + ([w for w in words[1:] if not w.startswith("-")] if head == "sed" and ("i" in _flags(words[1:]) or "--in-place" in words) else [])
            for target in written:
                if _under(target, self.protected):
                    escalate(DENY, f"writing to a protected path ({target})", "shell.protected_write")
            if head == "git" and len(words) > 1 and words[1] == "push":
                if any(w in ("-f", "--force") for w in words) and "--force-with-lease" not in words:
                    escalate(ASK, "a forced push (use --force-with-lease, or ask)", "git.force_push")
                where = cwd or ""
                if _under(where, OPERATOR_CHECKOUTS) or any(_under(w, OPERATOR_CHECKOUTS) for w in joined.split()):
                    escalate(DENY, "pushing from the operator's checkout; changes to the host and core go through SelfPropose", "git.operator_push")
        if self.egress_allow:
            blocked = [h for h in hosts if not host_allowed(h, self.egress_allow)]
            if blocked:
                escalate(ASK, f"network access to {', '.join(blocked)} is outside the egress allowlist", "egress.allowlist")
        return worst

    def _web(self, url: str) -> Decision:
        host = (urlsplit(url).hostname or "").lower()
        hosts = [host] if host else []
        if self.egress_allow and host and not host_allowed(host, self.egress_allow):
            return Decision(ASK, f"fetching {host} is outside the egress allowlist", "egress.allowlist", hosts=hosts)
        return Decision(ALLOW, hosts=hosts)

    def _config_rules(self, tool: str, text: str, current: Decision) -> Decision:
        """The operator's rules: a match can raise the severity, and an ``allow`` can lower an ``ask`` back
        to allow, but nothing lowers a built-in ``deny``."""
        for rule in self.rules:
            if rule.tool not in ("*", tool):
                continue
            try:
                if not rule.pattern or not re.search(rule.pattern, text, re.DOTALL):
                    continue
            except re.error:
                continue
            if rule.action == ALLOW:
                if current.action == ASK and current.rule != "builtin":
                    current = Decision(ALLOW, hosts=current.hosts)
                continue
            if _SEVERITY[rule.action] > _SEVERITY[current.action]:
                current = Decision(rule.action, rule.note or f"matched rule {rule.id}", rule.id, hosts=current.hosts)
        return current

    def evaluate(self, tool: str, arguments: dict[str, Any], *, grants: Iterable[str] = ()) -> Decision:
        text = canonical(tool, arguments)
        if tool in ("Exec", "Verify"):
            decision = self._shell(text, str(arguments.get("cwd") or "") or None)
        elif tool == "WebFetch":
            decision = self._web(text)
        else:
            decision = Decision(ALLOW)
        decision = self._config_rules(tool, text, decision)
        if decision.action == ASK:
            decision.key = approval_key(tool, arguments)
            if decision.key in set(grants):
                return Decision(ALLOW, f"granted by the operator ({decision.key})", decision.rule, key=decision.key, hosts=decision.hosts)
        return decision

    def describe(self) -> list[dict[str, str]]:
        builtins = [
            ("shell.forkbomb", "Exec", DENY, "fork bombs"),
            ("shell.machine", "Exec", DENY, "mkfs, shutdown, reboot, halt, poweroff"),
            ("shell.rawdevice", "Exec", DENY, "dd onto a raw device"),
            ("shell.rm_root", "Exec", DENY, "recursive delete of a system path"),
            ("shell.rm_protected", "Exec", DENY, "recursive delete inside a protected path or the operator's checkouts"),
            ("shell.rm_workspace", "Exec", ASK, "recursive delete of a whole workspace directory"),
            ("shell.chmod_root", "Exec", DENY, "recursive chmod/chown on a system path"),
            ("shell.protected_write", "Exec", DENY, "a redirect, cp, mv, tee or sed -i onto a protected path"),
            ("git.force_push", "Exec", ASK, "git push --force without --force-with-lease"),
            ("git.operator_push", "Exec", DENY, "git push from the operator's checkouts"),
            ("egress.allowlist", "Exec, WebFetch", ASK, "a host outside the egress allowlist (when one is configured)"),
        ]
        rows = [{"id": i, "tool": t, "action": a, "note": n, "source": "builtin"} for i, t, a, n in builtins]
        rows += [{"id": r.id, "tool": r.tool, "action": r.action, "note": r.note, "pattern": r.pattern, "source": r.source} for r in self.rules]
        return rows


__all__ = ["ALLOW", "ASK", "DENY", "Decision", "Policy", "Rule", "approval_key", "canonical", "host_allowed", "hosts_in", "shell_segments"]
