"""Typed application configuration.

All values come from the environment (optionally via a repo-root ``.env``).
Nothing sensitive has a committed default; secrets are operator-supplied.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_API_STYLES = {"openai", "anthropic", "auto"}
VALID_PROVIDERS = {"fake", "commandcode"}
VALID_SEARCH_PROVIDERS = {"duckduckgo", "wikipedia", "searxng"}

# Optional fields that must accept a blank value in `.env` (e.g. `FOO=`) as "unset".
_BLANK_AS_NONE = (
    "commandcode_api_key",
    "model_context_window",
    "model_max_output_tokens",
    "toolloom_secrets_key",
    "toolloom_data_dir",
    "toolloom_price_input_per_1k",
    "toolloom_price_output_per_1k",
    "toolloom_search_url",
)


class Settings(BaseSettings):
    """Environment-driven settings. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator(*_BLANK_AS_NONE, mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    version: str = "0.1.0"

    # Provider selection. Defaults to the real Command Code Provider API; set
    # LLM_PROVIDER=fake only for offline tests/development.
    llm_provider: str = "commandcode"

    # Command Code Provider API
    commandcode_api_key: str | None = None
    commandcode_base_url: str = "https://api.commandcode.ai/provider/v1"
    commandcode_model: str = "deepseek/deepseek-v4-flash"
    commandcode_api_style: str = "openai"
    commandcode_zdr: int = 0

    # Model budgeting
    # Hard cap on one turn's output. Blank means adaptive: derive it from the model's
    # context window instead, so the operator does not have to guess a number that is
    # large enough for reasoning models yet small enough to leave room for the prompt.
    model_max_output_tokens: int | None = Field(default=None, ge=1)
    model_context_window: int | None = Field(default=None, ge=512)
    toolloom_auto_titles: bool = True

    # Coding work is many turns of edit/run/read, not a chat exchange. These were
    # tuned for a cautious MVP and stopped ordinary tasks; the time limit is the real
    # bound, and the count caps exist only to stop a runaway loop.
    toolloom_max_iterations: int = 100
    toolloom_max_output_continuations: int = Field(default=3, ge=0, le=20)
    toolloom_max_tool_calls: int = 300

    # Server bind (local only by default)
    toolloom_host: str = "127.0.0.1"
    toolloom_port: int = 8000

    # Local data location
    toolloom_data_dir: Path | None = None

    # Secrets vault key override
    toolloom_secrets_key: str | None = None

    # Operator-managed stdio MCP servers (fixed executable + args only).
    toolloom_allow_stdio: bool = False
    toolloom_allow_python: bool = False
    toolloom_python_timeout_seconds: int = Field(default=15, ge=1, le=120)
    toolloom_python_output_bytes: int = Field(default=64_000, ge=1024, le=1_000_000)
    toolloom_run_timeout_seconds: int = Field(default=1800, ge=1, le=7200)

    # Test/load-harness fixture only: seeds the synthetic `demo` local server
    # (demo__add). Not enabled for a normal installation, which ships `sandbox`.
    toolloom_seed_demo: bool = False

    # Keyless web search backing sandbox__web_search. No API key is used by any
    # provider; `searxng` additionally needs an operator-run instance URL.
    toolloom_search_provider: str = "duckduckgo"
    toolloom_search_url: str | None = None
    toolloom_search_max_results: int = Field(default=5, ge=1, le=10)
    toolloom_search_timeout_seconds: int = Field(default=15, ge=1, le=60)

    # Optional token pricing for a cost estimate. Never invented — only used when set.
    toolloom_price_input_per_1k: float | None = None
    toolloom_price_output_per_1k: float | None = None

    # Tool Foundry: generate, validate and install reusable CLI capabilities.
    # Off by default because validation runs generated code. Installed tools are
    # additionally disabled and unapproved until the operator turns them on.
    toolloom_allow_foundry: bool = False
    toolloom_foundry_timeout_seconds: int = Field(default=30, ge=1, le=600)
    toolloom_foundry_test_timeout_seconds: int = Field(default=60, ge=1, le=600)
    toolloom_foundry_output_bytes: int = Field(default=64_000, ge=1_024, le=1_000_000)

    # Shell execution: the verification primitive. Commands run through a real
    # shell as the operator and are NOT OS-isolated, so this is off by default.
    # The default timeout is generous because test suites are slow.
    toolloom_allow_shell: bool = False
    toolloom_shell_timeout_seconds: int = Field(default=120, ge=1, le=600)
    toolloom_shell_output_bytes: int = Field(default=64_000, ge=1_024, le=1_000_000)

    @property
    def data_dir(self) -> Path:
        return (self.toolloom_data_dir or (REPO_ROOT / ".local")).resolve()

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'toolloom.db'}"

    @property
    def secrets_key_path(self) -> Path:
        return self.data_dir / "secrets.key"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> None:
        """Fail fast on invalid enum-style configuration."""
        if self.llm_provider not in VALID_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(VALID_PROVIDERS)}, got {self.llm_provider!r}"
            )
        if self.commandcode_api_style not in VALID_API_STYLES:
            raise ValueError(
                f"COMMANDCODE_API_STYLE must be one of {sorted(VALID_API_STYLES)}, "
                f"got {self.commandcode_api_style!r}"
            )
        if self.toolloom_search_provider not in VALID_SEARCH_PROVIDERS:
            raise ValueError(
                f"TOOLLOOM_SEARCH_PROVIDER must be one of {sorted(VALID_SEARCH_PROVIDERS)}, "
                f"got {self.toolloom_search_provider!r}"
            )

    @property
    def provider_configured(self) -> bool:
        """Whether the selected provider has everything it needs to run."""
        if self.llm_provider == "commandcode":
            return bool(self.commandcode_api_key)
        return True

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        """Cost estimate, or None when no prices are configured (never invented)."""
        if self.toolloom_price_input_per_1k is None and self.toolloom_price_output_per_1k is None:
            return None
        cost = 0.0
        cost += (input_tokens / 1000.0) * (self.toolloom_price_input_per_1k or 0.0)
        cost += (output_tokens / 1000.0) * (self.toolloom_price_output_per_1k or 0.0)
        return round(cost, 6)


@lru_cache
def get_settings() -> Settings:
    return Settings()
