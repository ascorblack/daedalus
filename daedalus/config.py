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
    thinking: bool = True
    reasoning_effort: ReasoningEffort = "medium"
    chain: list[str] = Field(default_factory=lambda: ["deepseek", "openrouter"])
    """Fallback order of provider ids; the first entry is the primary."""
    context_window: int = 128_000


class ProviderConfig(BaseModel):
    """One configured provider endpoint (all OpenAI-compatible)."""

    kind: Literal["deepseek", "openrouter", "vllm", "openai_compat"] = "openai_compat"
    base_url: str = ""
    default_model: str = ""
    supports_images: bool = False
    supports_thinking: bool = False
    timeout_seconds: float = 600.0
    pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-model USD per 1M tokens: ``{"model": {"input", "output", "cache_hit"[, "*_off_peak", "off_peak_utc"]}}``.
    Empty means the cost is reported as unknown; the usage itself is always recorded."""


class VisionConfig(BaseModel):
    """Model used by the ImageView tool (cheap, fast, image-capable)."""

    provider: str = "openrouter"
    model: str = "qwen/qwen3.7-flash"


class McpServerConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    timeout_seconds: float = 120.0


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


class RuntimeConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "deepseek": ProviderConfig(
                kind="deepseek",
                base_url="https://api.deepseek.com",
                default_model="deepseek-v4-flash",
                supports_thinking=True,
                pricing={
                    # Published list prices (USD per 1M tokens); off-peak window is 16:30-00:30 UTC.
                    "deepseek-v4-flash": {"input": 0.44, "cache_hit": 0.014, "output": 1.32, "input_off_peak": 0.22, "cache_hit_off_peak": 0.007, "output_off_peak": 0.66, "off_peak_utc": "16:30-00:30"},
                    "deepseek-v4-flash-vision-exp": {"input": 0.44, "cache_hit": 0.014, "output": 1.32, "input_off_peak": 0.22, "cache_hit_off_peak": 0.007, "output_off_peak": 0.66, "off_peak_utc": "16:30-00:30"},
                    "deepseek-v4-pro": {"input": 1.32, "cache_hit": 0.044, "output": 3.96, "input_off_peak": 0.66, "cache_hit_off_peak": 0.022, "output_off_peak": 1.98, "off_peak_utc": "16:30-00:30"},
                },
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
            return cls.model_validate(tomllib.load(fh))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(self.model_dump(mode="json"), fh)
        tmp.replace(path)


__all__ = [
    "ApprovalMode",
    "BalanceConfig",
    "LimitsConfig",
    "McpConfig",
    "McpServerConfig",
    "ModelConfig",
    "ProviderConfig",
    "ReasoningEffort",
    "RuntimeConfig",
    "SchedulerConfig",
    "SelfChangeConfig",
    "Settings",
    "TelegramConfig",
    "VisionConfig",
]
