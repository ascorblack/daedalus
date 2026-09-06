"""The hook manager the engine calls around every tool: redaction on the way in."""

from __future__ import annotations

import logging
from typing import Any

from protocore.contracts.hooks import HookActionKind, HookResult
from protocore.contracts.types import HookEvent
from protocore.tests_support.adapters import InMemoryHookManager

from daedalus.security.redact import Redactor

logger = logging.getLogger(__name__)


class DaedalusHookManager(InMemoryHookManager):
    """Rewrites tool output so a secret never reaches the model, the history or the provider.

    ``PostToolUse`` fires inside the core dispatcher before the result is appended to the
    history, so masking here covers the transcript, snapshots, compaction summaries and
    every later model call at once.
    """

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor
        self.redacted_calls = 0

    async def invoke(self, event: HookEvent, payload: dict[str, Any], tenant_id: str) -> HookResult:
        scripted = await super().invoke(event, payload, tenant_id)
        if event is not HookEvent.post_tool_use or scripted.action != HookActionKind.ALLOW:
            return scripted
        output = payload.get("tool_output")
        if not isinstance(output, str):
            return scripted
        cleaned = self.redactor.redact(output)
        if cleaned == output:
            return scripted
        self.redacted_calls += 1
        logger.warning("secret masked in the output of %s", payload.get("tool_name"))
        return HookResult(action=HookActionKind.MODIFY, reason="secret redacted", modifications={"tool_output": cleaned})


__all__ = ["DaedalusHookManager"]
