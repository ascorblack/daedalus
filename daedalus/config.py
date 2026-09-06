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


DEFAULT_PRESET = "deepseek.deepseek-v4-flash"


class ModelConfig(BaseModel):
    """Which model preset runs by default, and which ones stand in when it fails."""

    preset: str = DEFAULT_PRESET
    chain: list[str] = Field(default_factory=lambda: ["openrouter.deepseek-v4-flash"])
    """Fallback preset ids tried in order after the default one."""


class ModelPresetConfig(BaseModel):
    """A named model: a client (provider endpoint), a model id and everything about how it is run.

    Presets are the unit the operator picks, as the global default and per session; provider
    entries only describe endpoints (URL, key, timeout, prices).
    """

    provider: str = ""
    model: str = ""
    label: str = ""
    """Display label; empty shows ``provider/model``."""
    thinking: bool = True
    reasoning_effort: ReasoningEffort = "medium"
    images: bool = False
    """The model accepts images (needed for ImageView and for photos sent in chat)."""
    context_window: int = Field(default=128_000, ge=8_000, le=4_000_000)
    """Tokens of history a run may hold before compaction; set below the model's real window to keep runs cheap."""
    max_output_tokens: int = Field(default=32_000, ge=1_024, le=1_000_000)
    """Cap on one reply (``max_tokens``); thinking tokens count against it."""

    def display(self, preset_id: str = "") -> str:
        if self.label:
            return self.label
        return f"{self.provider}/{self.model}" if self.provider else preset_id


class ProviderConfig(BaseModel):
    """One configured provider endpoint (all OpenAI-compatible)."""

    kind: ProviderKind = "openai_compat"
    base_url: str = ""
    api_key: str = ""
    """Optional key for this endpoint, stored in ``config.toml`` on the state volume
    (masked in the Mini App, never echoed back). Empty means "no key" for self-hosted
    endpoints and "use the environment key" for the built-in kinds (deepseek, openrouter, vllm)."""
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

    preset: str = "openrouter.qwen-qwen3.7-flash"
    """A preset with ``images = true``; empty picks the first image-capable preset."""
    max_output_tokens: int = Field(default=2000, ge=100, le=32_000)


class WebToolsConfig(BaseModel):
    """WebFetch / WebSearch behaviour."""

    fetch_timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    search_timeout_seconds: float = Field(default=30.0, ge=1, le=600)
    proxy: str = ""
    """HTTP(S)/SOCKS proxy URL for both tools, e.g. ``socks5://127.0.0.1:1080``; empty = direct."""
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) Daedalus/0.1"
    fetch_max_chars: int = Field(default=40_000, ge=1_000, le=500_000)
    search_url: str = "https://html.duckduckgo.com/html/"
    search_region: str = "wt-wt"
    search_results: int = Field(default=8, ge=1, le=30)


class ExecToolsConfig(BaseModel):
    """Exec / Read / Find output handling (the timeout itself is ``limits.tool_timeout_seconds``)."""

    max_output_chars: int = Field(default=60_000, ge=2_000, le=1_000_000)


class ToolsConfig(BaseModel):
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolsConfig = Field(default_factory=ExecToolsConfig)


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
    usd_per_run: float = Field(default=5.0, ge=0)
    """Spend cap for one run; the run is stopped after the model call that crosses it. 0 = no cap.
    Calls without a known price (self-hosted models) cannot count towards it."""


class BalanceConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 60
    thresholds_usd: list[float] = Field(default_factory=lambda: [5.0, 2.0, 0.5])


class SchedulerConfig(BaseModel):
    topic_mode: ScheduleTopicMode = "per_task"
    catch_up_missed: bool = True
    max_failures: int = Field(default=3, ge=1)
    """A recurring task that failed this many times in a row is switched off (an inbox entry says so)."""
    question_timeout_minutes: int = Field(default=120, ge=1)
    """An unattended run (schedule, heartbeat) that asks a question waits this long, then continues on its own judgement."""
    lazy_ttl_hours: int = Field(default=24, ge=1)
    """A lazy reminder not yet seen by the operator after this long becomes an agent task."""
    inbox_keep_days: int = Field(default=30, ge=1)
    """Read inbox entries older than this are pruned."""


class OpsConfig(BaseModel):
    """Operational thresholds: boot-loop guard, delivery ledger, doctor."""

    boot_loop_window_minutes: int = Field(default=10, ge=1)
    boot_loop_threshold: int = Field(default=3, ge=2)
    """Unclean boots inside the window after which boot recovery is skipped once."""
    delivery_max_attempts: int = Field(default=3, ge=1)
    delivery_max_age_hours: int = Field(default=24, ge=1)
    delivery_keep_days: int = Field(default=7, ge=1)
    doctor_min_free_gb: float = Field(default=1.0, ge=0)
    doctor_workspaces_warn_gb: float = Field(default=20.0, ge=0)
    doctor_stale_snapshot_hours: int = Field(default=24, ge=1)
    doctor_probe_timeout_seconds: float = Field(default=6.0, ge=1)
    checkpoint_max_gb: float = Field(default=2.0, ge=0)
    """Workspaces larger than this are not snapshotted (revert then restores the history only)."""


class HeartbeatConfig(BaseModel):
    """A periodic unattended check driven by the HEARTBEAT.md file on the state volume."""

    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=5)
    active_hours: str = Field(default="08:00-23:00", pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    """UTC window ``HH:MM-HH:MM`` in which heartbeats run; may wrap past midnight."""
    preset: str = ""
    """Model preset for heartbeat runs; empty = the default preset."""
    max_runs_per_day: int = Field(default=24, ge=1)


class TelegramConfig(BaseModel):
    forum_chat_id: int = 0
    """Supergroup with topics; 0 means "not bound yet"."""
    general_topic_id: int = 0
    status_edit_interval_seconds: float = 1.0
    status_edit_tiers: list[tuple[float, float]] = Field(default_factory=lambda: [(60, 1), (300, 2), (900, 5), (0, 10)])
    """``[[run age in seconds, multiplier], …]``: the status message is edited less often as a run ages
    (the last entry, age 0, applies beyond the previous one). Multiplies ``status_edit_interval_seconds``."""
    slow_tool_seconds: int = Field(default=60, ge=5)
    """A tool call running longer than this is marked as slow in the status message."""
    inbound_merge_window_seconds: float = 1.5
    photo_caption_wait_seconds: float = 8.0
    """A photo without a caption waits this long for the message that usually follows it (a voice note, the text)."""
    stale_after_seconds: int = Field(default=300, ge=0)
    """Messages older than this when received (queued while the bot was down) are acknowledged, not executed. 0 = off."""
    max_inbound_file_mb: int = Field(default=1500, ge=1)
    """Files larger than this are refused before download."""
    forward_unknown_commands: bool = True
    """``/anything`` that is not a bot command goes to the agent as text."""
    reactions: bool = True
    """React to the operator's messages with the run's state (👀 received, 🔥 done, 💔 failed)."""
    topic_status_emoji: bool = True
    """Prefix a session's topic name with its state (🟢 running, ❓ waiting, ✅ done, 💥 failed)."""
    verbosity: int = 1
    """0 = final answers only, 1 = tool summaries, 2 = everything."""
    streaming: bool = True
    """Stream the answer as a live draft (sendMessageDraft) while it is generated; private chats only."""
    draft_interval_seconds: float = 0.35


class RuntimeConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    presets: dict[str, ModelPresetConfig] = Field(
        default_factory=lambda: {
            DEFAULT_PRESET: ModelPresetConfig(provider="deepseek", model="deepseek-v4-flash"),
            "openrouter.deepseek-v4-flash": ModelPresetConfig(provider="openrouter", model="deepseek/deepseek-v4-flash", images=True),
            "openrouter.qwen-qwen3.7-flash": ModelPresetConfig(
                provider="openrouter", model="qwen/qwen3.7-flash", label="Qwen 3.7 Flash (vision)", thinking=False, images=True, max_output_tokens=4_000
            ),
        }
    )
    """Named models keyed by id; the operator adds more in the Mini App."""
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "deepseek": ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com"),
            "openrouter": ProviderConfig(kind="openrouter", base_url="https://openrouter.ai/api/v1"),
            "vllm": ProviderConfig(kind="vllm", base_url=""),
        }
    )
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    self_change: SelfChangeConfig = Field(default_factory=SelfChangeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    balance: BalanceConfig = Field(default_factory=BalanceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    answer_language: str = "auto"
    """"auto" answers in the language of the request; otherwise a language name."""

    def preset(self, preset_id: str | None = None) -> tuple[str, ModelPresetConfig]:
        """The named preset, or the default one when the id is empty/unknown."""
        if preset_id and preset_id in self.presets:
            return preset_id, self.presets[preset_id]
        if self.model.preset in self.presets:
            return self.model.preset, self.presets[self.model.preset]
        if self.presets:
            first = next(iter(self.presets))
            return first, self.presets[first]
        raise RuntimeError("no model presets are configured")

    def vision_preset(self) -> tuple[str, ModelPresetConfig] | None:
        if self.vision.preset and self.vision.preset in self.presets and self.presets[self.vision.preset].images:
            return self.vision.preset, self.presets[self.vision.preset]
        for pid, preset in self.presets.items():
            if preset.images:
                return pid, preset
        return None

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


LEGACY_PROVIDER_KEYS = ("default_model", "supports_images", "supports_thinking")
LEGACY_MODEL_KEYS = ("provider", "name", "thinking", "reasoning_effort", "context_window", "max_output_tokens")


def _seed_presets(raw: dict[str, Any]) -> bool:
    """Convert configs written before presets carried the model settings.

    Provider ``default_model``/``supports_*`` and the global ``[model]`` thinking/window
    fields become presets (one per provider default model, plus the active pair), the
    chain of provider ids becomes a chain of preset ids, ``[vision]`` points at a preset,
    and the legacy keys are dropped. Returns True when anything changed.
    """
    model = dict(raw.get("model") or {})
    providers = raw.get("providers") or {}
    legacy = any(k in model for k in LEGACY_MODEL_KEYS) or any(
        isinstance(p, dict) and any(k in p for k in LEGACY_PROVIDER_KEYS) for p in providers.values()
    ) or any(k in (raw.get("vision") or {}) for k in ("provider", "model"))
    if not legacy:
        return False
    presets: dict[str, Any] = {pid: dict(p) for pid, p in (raw.get("presets") or {}).items() if isinstance(p, dict)}
    base = {
        "thinking": bool(model.get("thinking", True)),
        "reasoning_effort": model.get("reasoning_effort") or "medium",
        "context_window": int(model.get("context_window") or 128_000),
        "max_output_tokens": int(model.get("max_output_tokens") or 32_000),
    }
    by_provider: dict[str, str] = {}
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        default_model = str(provider.pop("default_model", "") or "").strip()
        images = bool(provider.pop("supports_images", False))
        provider.pop("supports_thinking", None)
        if default_model:
            pid = next((k for k, v in presets.items() if v.get("provider") == provider_id and v.get("model") == default_model), None)
            if pid is None:
                pid = preset_id_for(provider_id, default_model)
                presets[pid] = {"provider": provider_id, "model": default_model, "label": ""}
            presets[pid].setdefault("images", images)
            by_provider.setdefault(provider_id, pid)
    active_provider, active_name = model.get("provider") or "", (model.get("name") or "").strip()
    default_pid = model.get("preset") or ""
    if active_provider and active_name:
        default_pid = next((k for k, v in presets.items() if v["provider"] == active_provider and v["model"] == active_name), "")
        if not default_pid:
            default_pid = preset_id_for(active_provider, active_name)
            presets[default_pid] = {"provider": active_provider, "model": active_name, "label": ""}
        by_provider.setdefault(active_provider, default_pid)
    vision = dict(raw.get("vision") or {})
    v_provider, v_model = vision.pop("provider", ""), vision.pop("model", "")
    if v_provider and v_model:
        vpid = next((k for k, v in presets.items() if v["provider"] == v_provider and v["model"] == v_model), None)
        if vpid is None:
            vpid = preset_id_for(v_provider, v_model)
            presets[vpid] = {"provider": v_provider, "model": v_model, "label": "", "thinking": False}
        presets[vpid]["images"] = True
        vision["preset"] = vpid
    for preset in presets.values():
        for key, value in base.items():
            preset.setdefault(key, value)
        preset.setdefault("images", False)
    chain = [by_provider[p] for p in (model.get("chain") or []) if isinstance(p, str) and p in by_provider and by_provider[p] != default_pid]
    chain += [c for c in (model.get("chain") or []) if isinstance(c, str) and c in presets and c not in chain and c != default_pid]
    raw["model"] = {"preset": default_pid or (next(iter(presets)) if presets else DEFAULT_PRESET), "chain": chain}
    raw["vision"] = vision
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
    "DEFAULT_PRESET",
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
    "ToolsConfig",
    "WebToolsConfig",
    "ExecToolsConfig",
    "HeartbeatConfig",
    "OpsConfig",
    "VisionConfig",
]
