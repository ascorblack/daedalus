"""Configuration: immutable environment settings and the mutable runtime config.

Two layers, deliberately separate:

* :class:`Settings` — secrets and machine facts, read once from the environment
  (``.env`` in development, container env in production). Never edited by the agent.
* :class:`RuntimeConfig` — everything the owner may change while the bot runs
  (model, thinking, spend thresholds, approval mode, ...). Stored as ``config.toml``
  on the state volume and edited through the bot commands and the Mini App.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[1]

ReasoningEffort = Literal["low", "medium", "high"]
ApprovalMode = Literal["manual", "auto"]
ScheduleTopicMode = Literal["per_task", "per_run"]

ProviderKind = Literal["deepseek", "openrouter", "vllm", "openai_compat"]
PROVIDER_KINDS: tuple[ProviderKind, ...] = ("deepseek", "openrouter", "vllm", "openai_compat")


class Settings(BaseSettings):
    """Environment-only settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_local_mode: bool = False
    """True when ``telegram_api_base`` points at a local Bot API server (``--local``)."""
    owner_user_id: int = 0

    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    vllm_base_url: str = ""
    vllm_api_key: str = ""
    github_token: str = ""

    state_dir: Path = Path("/srv/state")
    workspaces_dir: Path = Path("/srv/workspaces")
    bot_repo_dir: Path = _REPO_ROOT
    core_repo_dir: Path = _REPO_ROOT.parent / "protocore-exp"
    supervisor_socket: Path = Path("/run/daedalus/supervisor.sock")

    api_host: str = "127.0.0.1"
    api_port: int = 8765
    miniapp_public_url: str = ""

    usd_per_day: float = 20.0
    """Daily spend cap. Enforced by the supervisor from its own environment, never from config.toml."""

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "daedalus.sqlite"

    @property
    def blobs_dir(self) -> Path:
        return self.state_dir / "blobs"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    @property
    def skills_dir(self) -> Path:
        return self.bot_repo_dir / "skills"


class ModelConfig(BaseModel):
    provider: str = "deepseek"
    name: str = "deepseek-v4-flash"
    preset: str = ""
    """Id of the model preset the default was chosen from; ``provider``/``name`` mirror it."""
    thinking: bool = True
    reasoning_effort: ReasoningEffort = "medium"
    chain: list[str] = Field(default_factory=lambda: ["deepseek", "openrouter"])
    """Fallback order of provider ids; the first entry is the primary."""
    context_window: int = Field(default=128_000, ge=8_000, le=4_000_000)
    """Tokens of history the run may hold before compaction; set below the model's real window to keep runs cheap."""
    max_output_tokens: int = Field(default=32_000, ge=1_024, le=1_000_000)
    """Cap on one model reply (``max_tokens``); thinking tokens count against it."""


class ModelPresetConfig(BaseModel):
    """A named model choice: which client (provider) serves which model id.

    Presets are what the operator picks, as the global default and per session; the
    provider entries only describe endpoints.
    """

    provider: str = ""
    model: str = ""
    label: str = ""
    """Display label; empty shows ``provider/model``."""

    def display(self, preset_id: str = "") -> str:
        return self.label or f"{self.provider}/{self.model}" if self.provider else (self.label or preset_id)


class ProviderConfig(BaseModel):
    """One configured provider endpoint (all OpenAI-compatible)."""

    kind: ProviderKind = "openai_compat"
    base_url: str = ""
    default_model: str = ""
    api_key: str = ""
    """Optional key for this endpoint, stored in ``config.toml`` on the state volume
    (masked in the Mini App, never echoed back). Empty means "no key" for self-hosted
    endpoints and "use the environment key" for the built-in kinds (deepseek, openrouter, vllm)."""
    supports_images: bool = False
    supports_thinking: bool = False
    timeout_seconds: float = 600.0
    pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-model USD per 1M tokens overriding the built-in table (``daedalus.providers.pricing``):
    ``{"model": {"input", "output", "cache_hit"[, "*_off_peak", "peak_utc", "peak_weekdays_only"]}}``.
    A model with no price anywhere is recorded with an unknown cost."""


class PromptConfig(BaseModel):
    """Operator-editable part of the system prompt."""

    rules: str = ""
    """Working rules appended after the persona; empty means the built-in default text."""


class VisionConfig(BaseModel):
    """Model used by the ImageView tool (cheap, fast, image-capable)."""

    provider: str = "openrouter"
    model: str = "qwen/qwen3.7-flash"


class McpOAuthConfig(BaseModel):
    """OAuth 2.1 (authorization code + PKCE) for remote MCP servers that require it.

    The flow is interactive: ``McpOAuthBegin`` returns an authorization URL the owner
    opens, the server redirects back to ``redirect_uri`` (loopback; the owner pastes the
    final URL back via ``McpOAuthFinish``), and the client exchanges the code for tokens.
    Tokens live on the state volume (0600), never in chat or tool arguments.
    """

    issuer: str = ""
    """Discovery base for ``/.well-known/oauth-authorization-server``; empty = origin of the server URL."""
    scopes: list[str] = Field(default_factory=list)
    """OAuth scopes requested, e.g. ``["board:read", "board:write"]``."""
    redirect_uri: str = ""
    """Loopback redirect registered with the server; default ``http://127.0.0.1:8931/callback``."""
    client_name: str = ""
    """Client name sent during dynamic client registration (default: ``"<server> MCP client"``)."""


class McpServerConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    timeout_seconds: float = 120.0
    oauth: McpOAuthConfig | None = None
    """When set, the HTTP transport authenticates with OAuth bearer tokens instead of static headers."""


class McpConfig(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    """Configured servers; every session starts with all of them switched off."""


class SelfChangeConfig(BaseModel):
    approval: ApprovalMode = "manual"
    auto_rebuild: bool = True
    """Trigger a rebuild automatically after a merge."""


class LimitsConfig(BaseModel):
    max_iterations: int = 200
    tool_timeout_seconds: float = 900.0


class BalanceConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 60
    thresholds_usd: list[float] = Field(default_factory=lambda: [5.0, 2.0, 0.5])


class SchedulerConfig(BaseModel):
    topic_mode: ScheduleTopicMode = "per_task"
    catch_up_missed: bool = True


class TelegramConfig(BaseModel):
    forum_chat_id: int = 0
    """Supergroup with topics; 0 means "not bound yet"."""
    general_topic_id: int = 0
    status_edit_interval_seconds: float = 1.0
    inbound_merge_window_seconds: float = 1.5
    verbosity: int = 1
    """0 = final answers only, 1 = tool summaries, 2 = everything."""
    streaming: bool = True
    """Stream the answer as a live draft (sendMessageDraft) while it is generated; private chats only."""
    draft_interval_seconds: float = 0.35


class RuntimeConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    presets: dict[str, ModelPresetConfig] = Field(default_factory=dict)
    """Named model choices keyed by id; seeded from the providers' default models."""
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "deepseek": ProviderConfig(
                kind="deepseek",
                base_url="https://api.deepseek.com",
                default_model="deepseek-v4-flash",
                supports_thinking=True,
            ),
            "openrouter": ProviderConfig(
                kind="openrouter",
                base_url="https://openrouter.ai/api/v1",
                default_model="deepseek/deepseek-v4-flash",
                supports_images=True,
                supports_thinking=True,
            ),
            "vllm": ProviderConfig(kind="vllm", base_url="", default_model=""),
        }
    )
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    self_change: SelfChangeConfig = Field(default_factory=SelfChangeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    balance: BalanceConfig = Field(default_factory=BalanceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    answer_language: str = "auto"
    """"auto" answers in the language of the request; otherwise a language name."""

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        if not path.exists():
            config = cls()
            config.save(path)
            return config
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        if _migrate(raw):
            config = cls.model_validate(raw)
            config.save(path)
            return config
        return cls.model_validate(raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(_without_none(self.model_dump(mode="json")), fh)
        tmp.replace(path)


def _without_none(value: Any) -> Any:
    """TOML has no null: optional sections that are unset are simply omitted."""
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_none(v) for v in value if v is not None]
    return value


def preset_id_for(provider_id: str, model: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in model).strip(".-") or "model"
    return f"{provider_id}.{slug}"


def _seed_presets(raw: dict[str, Any]) -> bool:
    """Back-fill presets for configs written before they existed: one per provider default
    model, plus the active ``[model]`` pair, which becomes ``[model].preset``."""
    if isinstance(raw.get("presets"), dict) and raw["presets"]:
        return False
    model = raw.get("model") or {}
    presets: dict[str, Any] = {}
    for provider_id, provider in (raw.get("providers") or {}).items():
        default_model = (provider.get("default_model") or "").strip() if isinstance(provider, dict) else ""
        if default_model:
            presets.setdefault(preset_id_for(provider_id, default_model), {"provider": provider_id, "model": default_model, "label": ""})
    active_provider, active_name = model.get("provider") or "", (model.get("name") or "").strip()
    if active_provider and active_name:
        pid = next((k for k, v in presets.items() if v["provider"] == active_provider and v["model"] == active_name), None)
        if pid is None:
            pid = preset_id_for(active_provider, active_name)
            presets[pid] = {"provider": active_provider, "model": active_name, "label": ""}
        raw["model"] = {**model, "preset": pid}
    raw["presets"] = presets
    return True


def _migrate(raw: dict[str, Any]) -> bool:
    """Rewrite config shapes older versions wrote; returns True when something changed."""
    changed = _seed_presets(raw)
    for provider in (raw.get("providers") or {}).values():
        pricing = provider.get("pricing") if isinstance(provider, dict) else None
        if not isinstance(pricing, dict):
            continue
        for model, entry in list(pricing.items()):
            if isinstance(entry, dict) and "off_peak_utc" in entry and "peak_utc" not in entry:
                # The seeded DeepSeek tables used an off-peak window; the built-in schedule replaces them.
                del pricing[model]
                changed = True
    return changed


__all__ = [
    "preset_id_for",
    "ApprovalMode",
    "BalanceConfig",
    "LimitsConfig",
    "McpConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    "ModelConfig",
    "ModelPresetConfig",
    "PROVIDER_KINDS",
    "PromptConfig",
    "ProviderConfig",
    "ProviderKind",
    "ReasoningEffort",
    "RuntimeConfig",
    "SchedulerConfig",
    "SelfChangeConfig",
    "Settings",
    "TelegramConfig",
    "VisionConfig",
]
