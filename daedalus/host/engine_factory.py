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
from protocore.tests_support.adapters import InMemoryHookManager

from daedalus.config import RuntimeConfig
from daedalus.host import prompts
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
    hook_manager: InMemoryHookManager
    bot_repo: Path
    core_repo: Path
    governance_path: Path


def runtime_constants(config: RuntimeConfig, *, context_window: int) -> Any:
    return default_runtime_constants(
        model_context_window=context_window,
        max_iterations=config.limits.max_iterations,
        tool_timeout_seconds=int(config.limits.tool_timeout_seconds),
        steer_follow_up_enabled=True,
        memory_enabled=True,
        # Advertise every registered tool; the registry would otherwise clip the list.
        tool_retrieval_top_k=200,
        agent_thinking_default=config.model.thinking,
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
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    context_window: int = 128_000,
    extra_notes: str = "",
    blocked_tools: set[str] | None = None,
) -> QueryEngine:
    primary_provider, primary_model = rungs[0]
    model = model_name or primary_model
    all_tools = {t.name for t in deps.tool_registry.list_all()}
    sections = (
        prompts.PERSONA,
        prompts.language_section(config.answer_language),
        prompts.governance_section(deps.governance_path),
        prompts.SELF_DEVELOPMENT,
        prompts.SCHEDULING,
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
        rc=runtime_constants(config, context_window=context_window),
        thinking_enabled=config.model.thinking if thinking is None else thinking,
        reasoning_effort=reasoning_effort or config.model.reasoning_effort,
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
