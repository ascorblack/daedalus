"""Secret redaction.

Two strategies, applied together:

* **Known values** — every credential the process was configured with (bot token,
  provider keys, MCP headers and env values) is replaced wherever it appears, whatever
  it looks like. This is the only reliable check: a key that is not shaped like a key
  is still a key.
* **Shapes** — common token formats (``sk-…``, GitHub ``ghp_…``, AWS ``AKIA…``, Telegram
  bot tokens, ``Bearer …``, PEM blocks, ``PASSWORD=…`` assignments, ``user:pass@host``)
  are masked even when they were never configured here, because a tool output that
  prints somebody else's key is just as much of a leak.

One :class:`Redactor` instance is shared by the tool-result hook (so a secret never
reaches the model, the transcript or the provider's logs), the Telegram renderer and the
Mini App views (the display path), and the logging filter.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

MASK = "•••"
MIN_VALUE_LENGTH = 8
"""Configured values shorter than this are not masked: they are too likely to collide with ordinary text."""

_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("telegram", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("openai", re.compile(r"\bsk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_-]{16,}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{16,})")),
    (
        "assignment",
        re.compile(
            r"(?i)\b((?:[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY)[A-Z0-9_]*)"
            r"\s*[=:]\s*[\"']?)([^\s\"'`,;]{6,})"
        ),
    ),
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
            if name in ("bearer", "assignment"):
                out = pattern.sub(lambda m: f"{m.group(1)}{MASK}", out)
            elif name == "url_userinfo":
                out = pattern.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
            else:
                out = pattern.sub(MASK, out)
        return out

    def redact_any(self, value: Any) -> Any:
        """Redact strings nested in dicts and lists (tool arguments, JSON payloads)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_any(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_any(v) for v in value]
        return value

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
        if record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        return True


_shared = Redactor()


def shared() -> Redactor:
    """The process-wide redactor; configured once at startup, consulted everywhere."""
    return _shared


def redact(text: str) -> str:
    return _shared.redact(text)


__all__ = ["MASK", "Redactor", "RedactingFilter", "redact", "shared"]
