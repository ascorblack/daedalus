"""The hook manager the engine calls around every tool: redaction on the way in."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from protocore.contracts.hooks import HookActionKind, HookResult, HookSpec, IHookManager
from protocore.contracts.types import HookEvent

from daedalus.security.redact import Redactor

logger = logging.getLogger(__name__)


class DaedalusHookManager(IHookManager):
    """Rewrites tool output so a secret never reaches the model, the history or the provider.

    ``PostToolUse`` fires inside the core dispatcher before the result is appended to the
    history, so masking here covers the transcript, snapshots, compaction summaries and
    every later model call at once — for tools that returned. A tool that *raised* takes
    the dispatcher's failure path, which does not fire the hook; the session runner masks
    that text when the event reaches it. Nothing is retained between calls.
    """

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.redacted_calls = 0
        self._specs: dict[str, HookSpec] = {}

    async def invoke(self, event: HookEvent, payload: dict[str, Any], tenant_id: str) -> HookResult:
        if event is not HookEvent.post_tool_use:
            return HookResult(action=HookActionKind.ALLOW)
        output = payload.get("tool_output")
        if not isinstance(output, str):
            return HookResult(action=HookActionKind.ALLOW)
        cleaned = self.redactor.redact(output)
        if cleaned == output:
            return HookResult(action=HookActionKind.ALLOW)
        self.redacted_calls += 1
        logger.warning("secret masked in the output of %s", payload.get("tool_name"))
        return HookResult(action=HookActionKind.MODIFY, reason="secret redacted", modifications={"tool_output": cleaned})

    async def register(self, spec: HookSpec) -> None:
        self._specs[spec.id] = spec

    async def unregister(self, hook_id: str, tenant_id: str) -> None:
        spec = self._specs.get(hook_id)
        if spec is not None and spec.tenant_id == tenant_id:
            self._specs.pop(hook_id)

    async def list(self, tenant_id: str, *, event: HookEvent | None = None) -> Sequence[HookSpec]:
        items = [s for s in self._specs.values() if s.tenant_id == tenant_id]
        if event is not None:
            items = [s for s in items if s.event is event]
        return items


__all__ = ["DaedalusHookManager"]
