"""Secret redaction.

Two strategies, applied together:

* **Known values** — every credential the process was configured with (bot token,
  provider keys, MCP headers and env values) is replaced wherever it appears, whatever
  it looks like. This is the only reliable check: a key that is not shaped like a key
  is still a key.
* **Shapes** — common token formats (``sk-…``, GitHub ``ghp_…``, GitLab ``glpat-…``, AWS
  ``AKIA…``, Telegram bot tokens, ``Authorization: Bearer …``, PEM blocks, ``PASSWORD=…``
  assignments and ``"api_key": "…"`` JSON members, ``user:pass@host``) are masked even
  when they were never configured here, because a tool output that prints somebody
  else's key is just as much of a leak.

The shapes are deliberately conservative about *values*: a key-like name alone is not
enough (this agent reads and rewrites its own source, where ``tokens`` and ``password``
are ordinary identifiers), the value must itself look like a credential — quoted, or a
long unbroken run that mixes letters and digits — and never like code.

One :class:`Redactor` instance is shared by the tool-result hook (so a secret never
reaches the model, the transcript or the provider's logs), the Telegram renderer and the
Mini App views (the display path), and the logging filter.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable
from typing import Any

MASK = "•••"
MIN_VALUE_LENGTH = 8
"""Configured values shorter than this are not masked: they are too likely to collide with ordinary text."""

_SECRET_NAME = r"(?:API_?KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_?KEY|ACCESS_?KEY|AUTH)"
_CRED_VALUE = r"(?=[^\s\"'`,;]*\d)(?=[^\s\"'`,;]*[A-Za-z])[A-Za-z0-9_\-./+=~:]{16,}"
"""An unbroken run of at least 16 credential characters containing both a letter and a digit.
Bare words, integers, dotted attribute chains and anything with brackets do not qualify."""
_SECRET_NAME_RE = re.compile(_SECRET_NAME, re.IGNORECASE)
_CRED_VALUE_RE = re.compile(_CRED_VALUE)

_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("telegram", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("openai", re.compile(r"\bsk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_-]{16,}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("gitlab", re.compile(r"\bgl(?:pat|dt|rt|ptt|oas)-[A-Za-z0-9_-]{16,}\b")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # ``Authorization: Bearer x`` / ``token x`` / ``Basic x`` and bare ``Bearer x``.
    ("auth_header", re.compile(r"(?i)\b((?:authorization\s*:\s*)?(?:bearer|basic)\s+|authorization\s*:\s*token\s+)([A-Za-z0-9._~+/=-]{12,})")),
    # ``X-Api-Key: value`` style headers.
    ("api_header", re.compile(r"(?i)\b((?:x-)?(?:api[-_]?key|auth[-_]?token|access[-_]?token|private[-_]?token)\s*:\s*)([A-Za-z0-9._~+/=-]{12,})")),
    # ``API_KEY=…`` / ``export DB_PASSWORD='…'`` (upper-snake env style) and ``"api_key": "…"``
    # (JSON/YAML/TOML members, any case, quoted).
    ("env_assignment", re.compile(r"\b([A-Z][A-Z0-9_]*" + _SECRET_NAME + r"[A-Z0-9_]*\s*=\s*)([\"']?)(" + _CRED_VALUE + r"|(?<=[\"'])[^\"'\n]{8,})(\2)")),
    ("quoted_member", re.compile(r"(?i)([\"'][A-Za-z0-9_.-]*" + _SECRET_NAME + r"[A-Za-z0-9_.-]*[\"']\s*[:=]\s*[\"'])([^\"'\n]{8,})([\"'])")),
    ("bare_member", re.compile(r"(?i)\b([A-Za-z0-9_.-]*" + _SECRET_NAME + r"\s*:\s*)(" + _CRED_VALUE + r")")),
    ("url_userinfo", re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@/\s]{3,})(@)")),
)


class Redactor:
    """Replace configured secret values and secret-shaped strings with a mask."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: list[str] = []
        self.add_values(values)

    def add_values(self, values: Iterable[str]) -> None:
        for value in values:
            if isinstance(value, str) and len(value.strip()) >= MIN_VALUE_LENGTH:
                cleaned = value.strip()
                if cleaned not in self._values:
                    self._values.append(cleaned)
        # Longest first, so a key that contains another key masks as one blob.
        self._values.sort(key=len, reverse=True)

    def replace_values(self, values: Iterable[str]) -> None:
        self._values = []
        self.add_values(values)

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self._values)

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = text
        for value in self._values:
            if value in out:
                out = out.replace(value, MASK)
        for name, pattern in _SHAPES:
            if name in ("auth_header", "api_header", "bare_member"):
                out = pattern.sub(lambda m: f"{m.group(1)}{MASK}", out)
            elif name == "env_assignment":
                out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(4)}", out)
            elif name in ("quoted_member", "url_userinfo"):
                out = pattern.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
            else:
                out = pattern.sub(MASK, out)
        return out

    def redact_any(self, value: Any) -> Any:
        """Redact strings nested in dicts and lists (tool arguments, JSON payloads)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self._redact_member(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_any(v) for v in value]
        return value

    def _redact_member(self, key: Any, value: Any) -> Any:
        """Redact a dict value, using its key as context for the key-name-based shapes.

        ``redact`` alone sees only the value, so a credential sitting under a secret-named
        key (``{"api_key": "…"}``) is missed — the JSON-string path catches it because the
        key is present in the text. Passing the key here keeps the structured path as safe
        as the string path, without over-masking (both a secret-named key AND a
        credential-looking value are required).
        """
        if isinstance(value, str):
            out = self.redact(value)
            if out == value and isinstance(key, str) and _SECRET_NAME_RE.search(key) and _CRED_VALUE_RE.search(value):
                out = MASK
            return out
        return self.redact_any(value)

    def contains_secret(self, text: str) -> bool:
        return self.redact(text) != text


class RedactingFilter(logging.Filter):
    """Masks secrets in log records before any handler formats them."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken format string is the handler's problem, not ours
            return True
        cleaned = self.redactor.redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        # The formatter builds ``record.exc_text`` lazily AFTER filters run, so a secret
        # carried by the exception itself (``raise ValueError(secret)``) would otherwise
        # reach the handler unmasked. Build it here, redact it, and let the formatter reuse
        # the cleaned text (it only computes ``exc_text`` when it is still ``None``).
        if record.exc_info is not None and record.exc_text is None:
            record.exc_text = self.redactor.redact("".join(traceback.format_exception(record.exc_info[1])))
        elif record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        return True


def install_logging_filter(redactor: Redactor) -> None:
    """Mask secrets on every logger that formats its own records, not only the root handlers.

    uvicorn installs handlers on its own loggers with ``propagate=False``; a filter on a
    *logger* runs for every record created through it regardless of handler.
    """
    filt = RedactingFilter(redactor)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "aiogram", "httpx"):
        logging.getLogger(name).addFilter(filt)
    for handler in logging.getLogger().handlers:
        handler.addFilter(filt)


_shared = Redactor()


def shared() -> Redactor:
    """The process-wide redactor; configured once at startup, consulted everywhere."""
    return _shared


def redact(text: str) -> str:
    return _shared.redact(text)


__all__ = ["MASK", "Redactor", "RedactingFilter", "install_logging_filter", "redact", "shared"]
