"""Build a ``QueryEngine`` for one session run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocore.contracts.llm import IProviderChain
from protocore.contracts.tool_registry import IToolRegistry, ToolVisibilityPolicy
from protocore.runtime.query_engine import QueryEngine, QueryEngineConfig
from protocore.runtime.runtime_constants import default_runtime_constants
from protocore.runtime.tool_dispatch import ToolDispatcher
from protocore.runtime.tool_permission import ToolPermissionGate

from daedalus.config import ModeConfig, RuntimeConfig
from daedalus.host import prompts
from daedalus.host.hooks import DaedalusHookManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.sqlite import SqliteEventStream

TENANT = "daedalus"


@dataclass(slots=True)
class EngineDeps:
    tool_registry: IToolRegistry
    event_stream: SqliteEventStream
    blob_store: FileBlobStore
    skill_store: Any
    hook_manager: DaedalusHookManager
    bot_repo: Path
    core_repo: Path
    governance_path: Path


def runtime_constants(config: RuntimeConfig, *, context_window: int, max_output_tokens: int, thinking: bool, mode: ModeConfig | None = None) -> Any:
    context_window = max(8_000, int(context_window))
    output_cap = max(1024, min(max_output_tokens, context_window))
    max_iterations = mode.max_iterations if mode is not None and mode.max_iterations else config.limits.max_iterations
    tool_timeout = mode.tool_timeout_seconds if mode is not None and mode.tool_timeout_seconds else config.limits.tool_timeout_seconds
    return default_runtime_constants(
        model_context_window=context_window,
        llm_output_max_tokens_ratio=min(1.0, max(0.01, output_cap / context_window)),
        max_iterations=max_iterations,
        tool_timeout_seconds=int(tool_timeout),
        steer_follow_up_enabled=True,
        steer_default_mode="all",
        follow_up_default_mode="all",
        mid_session_controls_enabled=True,
        memory_enabled=True,
        # Advertise every registered tool; the registry would otherwise clip the list.
        tool_retrieval_top_k=200,
        agent_thinking_default=thinking,
        # Summaries must not be cut mid-JSON: give the summariser room for a full sentence pair.
        compaction_summary_max_output_tokens=1024,
        # Long tasks are the point: no per-run tool-call cap; spend and iterations bound the run.
        leader_tool_call_soft_cap=0,
        compaction_protect_first_user_turn=True,
        # A reasoning model that returns neither text nor a tool call gets a nudge to
        # continue instead of ending the run; a repeating text tail is cut and nudged,
        # and the same tool call with the same arguments is refused after a few repeats.
        resilience_post_tool_empty_nudge_enabled=True,
        loop_guard_enabled=True,
        # The core's defaults (3 identical calls, 1 nudge) were tuned for short runs; a 200-iteration
        # run legitimately re-runs the same test command or re-reads the same file many times.
        loop_guard_identical_tool_limit=30,
        loop_guard_nudge_max=3,
    )


def build_engine(
    *,
    deps: EngineDeps,
    config: RuntimeConfig,
    run_id: str,
    session_id: str,
    session_title: str,
    workspace: Path,
    rungs: list[tuple[OpenAICompatibleProvider, str]],
    provider_chain: IProviderChain | None,
    model_name: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "medium",
    context_window: int = 128_000,
    max_output_tokens: int = 32_000,
    extra_notes: str = "",
    blocked_tools: set[str] | None = None,
    mode: ModeConfig | None = None,
) -> QueryEngine:
    primary_provider, primary_model = rungs[0]
    model = model_name or primary_model
    all_tools = {t.name for t in deps.tool_registry.list_all()}
    sections = (
        prompts.PERSONA,
        prompts.rules_section(config.prompt.rules),
        prompts.language_section(config.answer_language),
        prompts.governance_section(deps.governance_path),
        prompts.SELF_DEVELOPMENT,
        prompts.HISTORY,
        prompts.BOARD,
        prompts.SCHEDULING,
        (mode.prompt.strip() + "\n") if mode is not None and mode.prompt.strip() else "",
        prompts.environment_section(
            workspace=workspace,
            bot_repo=deps.bot_repo,
            core_repo=deps.core_repo,
            session_title=session_title,
            model=model,
            extra_notes=extra_notes,
        ),
    )
    engine_config = QueryEngineConfig(
        run_id=run_id,
        tenant_id=TENANT,
        session_id=session_id,
        account_id=TENANT,
        root_run_id=run_id,
        model_name=model,
        system_prompt_sections=tuple(s for s in sections if s),
        tool_visibility_policy=ToolVisibilityPolicy(pinned=set(all_tools) - set(blocked_tools or ()), blocked=set(blocked_tools or ())),
        rc=runtime_constants(config, context_window=context_window, max_output_tokens=max_output_tokens, thinking=thinking, mode=mode),
        thinking_enabled=thinking,
        reasoning_effort=reasoning_effort,
    )
    engine = QueryEngine(
        config=engine_config,
        llm_provider=primary_provider,
        tool_registry=deps.tool_registry,
        event_stream=deps.event_stream,
        hook_manager=deps.hook_manager,
        skill_store=deps.skill_store,
        blob_store=deps.blob_store,
        provider_chain=provider_chain,
    )
    # The container is the boundary: no shell deny patterns, no path isolation.
    engine._tool_dispatcher = ToolDispatcher(  # type: ignore[attr-defined]
        registry=deps.tool_registry,
        permission_gate=ToolPermissionGate(policies=[]),
        hook_manager=deps.hook_manager,
    )
    deps.event_stream.bind_run(run_id, session_id)
    return engine


__all__ = ["TENANT", "EngineDeps", "build_engine", "runtime_constants"]
