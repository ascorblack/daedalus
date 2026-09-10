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
import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOW, ASK, DENY = "allow", "ask", "deny"
PATTERN_MAX_CHARS = 200
PATTERN_TEXT_CHARS = 20_000
"""An operator's regular expression runs on the event loop: a bounded pattern over a bounded text."""
_SEVERITY = {ALLOW: 0, ASK: 1, DENY: 2}

OPERATORS = {";", "&&", "||", "|", "(", ")", "&"}
KEYWORDS = {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "case", "esac", "in", "select", "function", "{", "}", "!", "[[", "]]", "[", "]", "coproc"}
"""Shell reserved words: they precede a simple command without being one, so `then rm -rf /` is `rm -rf /`."""
REDIRECTS = {"<", ">", ">>", "<<", "<<<", "2>", "2>>", "&>", "&>>", ">|"}
WRAPPERS = {"sudo", "nohup", "time", "nice", "env", "exec", "command", "builtin", "stdbuf", "timeout"}
SHELLS = {"bash", "sh", "zsh", "dash"}
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "socat"}
_DANGEROUS_BASES = ("/", "/srv", "/opt", "/etc", "/usr", "/var", "/home", "/root", "/boot", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys", "/dev", "/run", "/tmp")
DANGEROUS_TARGETS = {"~", "~/", "~/*", "$HOME", "$HOME/*", "${HOME}"} | {form for base in _DANGEROUS_BASES for form in (base, base.rstrip("/") + "/", base.rstrip("/") + "/*")}
"""What a recursive delete or a recursive chmod must never be aimed at: the machine's own directories, whole or globbed."""
OPERATOR_CHECKOUTS = ("/srv/daedalus", "/srv/protocore-exp")
"""The operator's repositories as mounted in the container: they change only through pull requests. The host adds
the checkouts' real paths when it builds the policy."""
WRITERS = {"cp", "mv", "install", "rsync", "ln"}
"""Commands whose last non-flag argument is their destination."""
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


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


SHELL_TOOLS = ("Exec", "Verify", "ServiceStart")


def canonical(tool: str, arguments: dict[str, Any]) -> str:
    """The text the rules read: the shell command, the URL, or the arguments as JSON."""
    if tool in SHELL_TOOLS:
        return str(arguments.get("command") or "")
    if tool in ("WebFetch",):
        return str(arguments.get("url") or "")
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def approval_key(tool: str, arguments: dict[str, Any]) -> str:
    """A grant is for one exact call: the key covers every argument (cwd, env, background …), not only the command."""
    return hashlib.sha256((tool + "\x00" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)).encode("utf-8")).hexdigest()[:12]


def _norm(path: str) -> str:
    """A path as the rules compare it: ``$HOME`` and ``~`` kept as written, ``..`` and doubled slashes folded."""
    if path.startswith(("~", "$")):
        return path
    if path.startswith("/"):
        folded = posixpath.normpath(path)
        return folded + "/*" if path.endswith("/*") and not folded.endswith("*") else folded
    return path


def shell_segments(command: str, *, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell command line, each as its argv, wrappers and reserved words stripped.

    ``cd a && sudo rm -rf /`` → ``[["cd", "a"], ["rm", "-rf", "/"]]``; ``if x; then rm -rf /; fi`` yields
    ``["rm", "-rf", "/"]`` too. ``bash -c "…"`` is opened one level, ``xargs`` and ``find -exec`` hand their
    command on. Unparseable input (an unbalanced quote) comes back as whitespace-split words: the rules
    still see every word.

    What a lexer cannot see, and this one does not claim to: a command assembled at run time (``eval``,
    ``$(...)``, backticks, a variable holding the verb), a heredoc fed to ``sh``, and code inside
    another interpreter (``python -c``, ``perl -e``). The container is the boundary for those; the
    policy catches what is written plainly.
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
        for _ in range(4):  # `then sudo env X=1 rm …`: keywords and wrappers alternate
            while words and words[0] in KEYWORDS:
                words.pop(0)
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
        if words[0] == "xargs" and len(words) > 1:
            handed = [w for w in _without_redirects(words)[1:] if not (w.startswith("-") and w[1:2] in ("0", "n", "I", "L", "P", "d", "a", "s", "t", "p"))]
            if handed and not handed[0].startswith("-"):
                out.append(handed + ["<xargs-input>"])
                continue
        if words[0] == "find":
            if "-delete" in words:
                out.append(["rm", "-r", *[w for w in words[1:] if not w.startswith("-") and w not in ("f", "d")][:1]])
            if "-exec" in words or "-execdir" in words:
                i = max(words.index(w) for w in ("-exec", "-execdir") if w in words)
                out.append([w for w in words[i + 1 :] if w not in (";", "\\;", "+", "{}")] + [w for w in words[1:2] if not w.startswith("-")])
            out.append(words)
            continue
        out.append(words)
    return out


def _flags(words: Iterable[str]) -> str:
    return "".join(w.lstrip("-") for w in words if w.startswith("-") and not w.startswith("--"))


def _redirect_targets(words: list[str]) -> list[str]:
    targets = []
    for i, w in enumerate(words):
        if w in (">", ">>", "&>", "&>>", ">|") and i + 1 < len(words):
            targets.append(words[i + 1])
    return targets


def _without_redirects(words: list[str]) -> list[str]:
    """The argv with redirections and their operands removed, so a destination heuristic sees the real arguments."""
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if w in REDIRECTS:
            skip = True
            continue
        out.append(w)
    return out


def _git_subcommand(words: list[str]) -> tuple[str, str | None]:
    """``(subcommand, -C path)`` of a git invocation, skipping the options that take a value."""
    at = None
    i = 1
    while i < len(words):
        w = words[i]
        if w in GIT_OPTIONS_WITH_VALUE and i + 1 < len(words):
            if w == "-C":
                at = words[i + 1]
            i += 2
            continue
        if w.startswith("--") and "=" in w:
            if w.startswith("--git-dir=") or w.startswith("--work-tree="):
                at = w.split("=", 1)[1]
            i += 1
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w, at
    return "", at


def _written_paths(head: str, words: list[str]) -> list[str]:
    """Where a command writes, by its own conventions: redirects, copy/move/link destinations, archive
    extraction directories, download output files, in-place edits."""
    targets = list(_redirect_targets(words))
    args = [w for w in _without_redirects(words)[1:]]
    plain = [w for w in args if not w.startswith("-")]
    if head in WRITERS and plain:
        targets.append(plain[-1])
        for i, w in enumerate(args):
            if w in ("-t", "--target-directory") and i + 1 < len(args):
                targets.append(args[i + 1])
            elif w.startswith("--target-directory="):
                targets.append(w.split("=", 1)[1])
    elif head == "tee":
        targets.extend(plain)
    elif head in ("curl", "wget"):
        for i, w in enumerate(args):
            if w in ("-o", "--output", "-O") and i + 1 < len(args):
                targets.append(args[i + 1])
            elif w.startswith("--output="):
                targets.append(w.split("=", 1)[1])
    elif head == "tar":
        for i, w in enumerate(args):
            if w in ("-C", "--directory") and i + 1 < len(args):
                targets.append(posixpath.join(args[i + 1], "*"))
            elif w.startswith("--directory="):
                targets.append(posixpath.join(w.split("=", 1)[1], "*"))
    elif head == "unzip":
        for i, w in enumerate(args):
            if w == "-d" and i + 1 < len(args):
                targets.append(posixpath.join(args[i + 1], "*"))
    elif head == "dd":
        targets.extend(w.split("=", 1)[1] for w in args if w.startswith("of="))
    elif head == "sed" and ("i" in _flags(args) or "--in-place" in args or any(w.startswith("--in-place") for w in args)):
        targets.extend(plain)
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

    def __init__(self, *, protected_paths: Iterable[Path] = (), egress_allow: Iterable[str] = (), rules: Iterable[Rule] = (), workspace_roots: Iterable[Path] = (), operator_checkouts: Iterable[Path] = ()) -> None:
        self.protected = [str(p) for p in protected_paths]
        self.egress_allow = [e for e in egress_allow if e.strip()]
        self.rules = list(rules)
        self.workspace_roots = [str(p) for p in workspace_roots]
        self.operator_checkouts = [*OPERATOR_CHECKOUTS, *(str(p) for p in operator_checkouts)]

    # -- built-in judgement -----------------------------------------------------------

    def _shell(self, command: str, cwd: str | None) -> Decision:
        segments = shell_segments(command)
        hosts = hosts_in(segments)
        worst = Decision(ALLOW, hosts=hosts)
        checkouts = list(self.operator_checkouts)
        where = _norm(cwd) if cwd else ""

        def escalate(action: str, reason: str, rule: str) -> None:
            nonlocal worst
            if _SEVERITY[action] > _SEVERITY[worst.action]:
                worst = Decision(action, reason, rule, hosts=hosts)

        if re.search(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", command):
            escalate(DENY, "a fork bomb", "shell.forkbomb")
        for words in segments:
            if words[0] == "cd" and len(words) > 1:
                where = _norm(words[1]) if words[1].startswith("/") else where  # a `cd` earlier in the line moves every later command
            head = words[0].lstrip("\\")
            if head.startswith("/") and head.count("/") >= 2:
                head = head.rsplit("/", 1)[-1]  # /bin/rm is rm
            if head == "busybox" and len(words) > 1:
                words = words[1:]
                head = words[0]
            plain = [_norm(w) for w in _without_redirects(words)[1:] if not w.startswith("-")]
            if head in ("mkfs", "shutdown", "reboot", "halt", "poweroff", "init", "telinit") or head.startswith("mkfs."):
                escalate(DENY, f"`{head}` acts on the machine, not on the workspace", "shell.machine")
            if head == "dd" and any(w.startswith("of=/dev/") for w in words):
                escalate(DENY, "writing a raw device", "shell.rawdevice")
            if head == "rm":
                flags = _flags(words[1:])
                recursive = "r" in flags or "R" in flags or "--recursive" in words or "-r" in words
                if recursive and any(t in DANGEROUS_TARGETS or t.rstrip("/") in DANGEROUS_TARGETS for t in plain):
                    escalate(DENY, f"recursive delete of {', '.join(plain)}", "shell.rm_root")
                elif recursive and any(_under(t, self.protected + checkouts) for t in plain):
                    escalate(DENY, f"recursive delete inside a protected path ({', '.join(plain)})", "shell.rm_protected")
                elif recursive and self.workspace_roots and any(t.rstrip("/") in self.workspace_roots or t.rstrip("/").endswith("/*") and t.rstrip("/*") in self.workspace_roots for t in plain):
                    escalate(DENY, "recursive delete of every workspace at once", "shell.rm_workspaces")
                elif recursive and self.workspace_roots and any(str(Path(t).parent).rstrip("/") in self.workspace_roots for t in plain):
                    escalate(ASK, "recursive delete of a whole workspace directory", "shell.rm_workspace")
                elif recursive and "<xargs-input>" in plain:
                    escalate(ASK, "recursive delete of paths piped through xargs (the targets are not visible here)", "shell.rm_piped")
            if head in ("chmod", "chown", "chgrp") and "R" in _flags(words[1:]) and any(t in DANGEROUS_TARGETS for t in plain):
                escalate(DENY, f"recursive `{head}` on a system path", "shell.chmod_root")
            for target in _written_paths(head, words):
                normed = _norm(target)
                if _under(normed, self.protected + checkouts if head != "sed" else self.protected):
                    escalate(DENY, f"writing to a protected path ({target})", "shell.protected_write")
                elif normed in DANGEROUS_TARGETS or normed.rstrip("/*") in _DANGEROUS_BASES[1:]:
                    escalate(DENY, f"writing into a system directory ({target})", "shell.system_write")
            if head == "git":
                sub, at = _git_subcommand(words)
                if sub == "push":
                    if any(w in ("-f", "--force") for w in words) and "--force-with-lease" not in words:
                        escalate(ASK, "a forced push (use --force-with-lease, or ask)", "git.force_push")
                    origin = _norm(at) if at else where
                    if (origin and _under(origin, checkouts)) or any(_under(_norm(w), checkouts) for w in words[1:]):
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
        """The operator's rules: a match can raise the severity; an ``allow`` can lower an ``ask`` that came from the
        egress allowlist or from another operator rule, never a built-in safety question and never a ``deny``."""
        sample = text[:PATTERN_TEXT_CHARS]
        for rule in self.rules:
            if rule.tool not in ("*", tool) or not rule.pattern or len(rule.pattern) > PATTERN_MAX_CHARS:
                continue
            try:
                if not re.search(rule.pattern, sample, re.DOTALL):
                    continue
            except re.error:
                continue
            if rule.action == ALLOW:
                if current.action == ASK and (current.rule.startswith("egress.") or current.rule.startswith("config.") or current.rule in {r.id for r in self.rules}):
                    current = Decision(ALLOW, f"allowed by rule {rule.id}", rule.id, hosts=current.hosts)
                continue
            if _SEVERITY[rule.action] > _SEVERITY[current.action]:
                current = Decision(rule.action, rule.note or f"matched rule {rule.id}", rule.id, hosts=current.hosts)
        return current

    def evaluate(self, tool: str, arguments: dict[str, Any], *, grants: Iterable[str] = ()) -> Decision:
        text = canonical(tool, arguments)
        if tool in SHELL_TOOLS:
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
            ("shell.rm_workspaces", "Exec", DENY, "recursive delete of every workspace at once"),
            ("shell.rm_workspace", "Exec", ASK, "recursive delete of a whole workspace directory"),
            ("shell.rm_piped", "Exec", ASK, "recursive delete of paths piped through xargs"),
            ("shell.system_write", "Exec", DENY, "extracting or copying into a system directory"),
            ("shell.chmod_root", "Exec", DENY, "recursive chmod/chown on a system path"),
            ("shell.protected_write", "Exec", DENY, "a redirect, copy, move, link, download, extraction or in-place edit onto a protected path or the operator's checkouts"),
            ("git.force_push", "Exec", ASK, "git push --force without --force-with-lease"),
            ("git.operator_push", "Exec", DENY, "git push from the operator's checkouts"),
            ("egress.allowlist", "Exec, WebFetch", ASK, "a host outside the egress allowlist (when one is configured)"),
        ]
        rows = [{"id": i, "tool": t, "action": a, "note": n, "source": "builtin"} for i, t, a, n in builtins]
        rows += [{"id": r.id, "tool": r.tool, "action": r.action, "note": r.note, "pattern": r.pattern, "source": r.source} for r in self.rules]
        return rows


__all__ = ["ALLOW", "ASK", "DENY", "SHELL_TOOLS", "Decision", "Policy", "Rule", "approval_key", "canonical", "host_allowed", "hosts_in", "shell_segments"]
