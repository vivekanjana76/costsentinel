"""Environment-driven configuration.

Defaults are deliberately the fully offline ones: ``MODE=mock`` and
``LLM_BACKEND=fake``. A fresh clone with no ``.env`` runs the whole pipeline with no
credentials, no cloud calls and no model calls (CLAUDE.md golden rule 2), and CI
relies on exactly that.

Real-backend settings are present but optional. They are validated at the point a
real backend is *selected*, so a missing key surfaces as a clear configuration error
rather than a failure deep inside a graph run.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(StrEnum):
    """Which provider implementation backs a run."""

    MOCK = "mock"
    REAL = "real"


class LLMBackend(StrEnum):
    """Which language-model implementation backs a run."""

    FAKE = "fake"
    AZURE_OPENAI = "azure_openai"
    GEMINI = "gemini"


class LogLevel(StrEnum):
    """Structured-logging verbosity."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _env(*names: str) -> AliasChoices:
    """Accept both the bare and the ``COSTSENTINEL_``-prefixed environment name.

    The bare names are what CLAUDE.md and ``.env.example`` document; the prefixed
    ones avoid collisions in a shared environment such as a CI runner or a
    container with other services' variables.
    """
    return AliasChoices(*names, *(f"COSTSENTINEL_{name}" for name in names))


class Settings(BaseSettings):
    """All CostSentinel configuration, resolved from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- mode flags ------------------------------------------------------
    mode: RunMode = Field(default=RunMode.MOCK, validation_alias=_env("MODE"))
    llm_backend: LLMBackend = Field(default=LLMBackend.FAKE, validation_alias=_env("LLM_BACKEND"))

    # --- mock provider ---------------------------------------------------
    mock_seed: int = Field(default=1337, validation_alias=_env("MOCK_SEED"))

    # --- runtime ---------------------------------------------------------
    state_dir: Path = Field(default=Path(".costsentinel"), validation_alias=_env("STATE_DIR"))
    log_level: LogLevel = Field(default=LogLevel.INFO, validation_alias=_env("LOG_LEVEL"))

    # --- rightsizing policy ----------------------------------------------
    rightsize_headroom_factor: Decimal = Field(
        default=Decimal("2.0"),
        gt=Decimal(1),
        validation_alias=_env("RIGHTSIZE_HEADROOM_FACTOR"),
        description=(
            "Multiplier applied to observed peak CPU when choosing a smaller SKU. "
            "An explicit, documented policy parameter -- not a derived quantity."
        ),
    )

    # --- Azure (MODE=real, Phase 2) --------------------------------------
    azure_tenant_id: str | None = Field(default=None, validation_alias=_env("AZURE_TENANT_ID"))
    azure_client_id: str | None = Field(default=None, validation_alias=_env("AZURE_CLIENT_ID"))
    azure_client_secret: SecretStr | None = Field(
        default=None, validation_alias=_env("AZURE_CLIENT_SECRET")
    )
    azure_subscription_ids: str | None = Field(
        default=None, validation_alias=_env("AZURE_SUBSCRIPTION_IDS")
    )

    # --- Azure OpenAI (Phase 3) ------------------------------------------
    azure_openai_endpoint: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_ENDPOINT")
    )
    azure_openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_API_KEY")
    )
    azure_openai_api_version: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_API_VERSION")
    )
    azure_openai_deployment: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_DEPLOYMENT")
    )

    # --- Gemini (Phase 3) ------------------------------------------------
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias=_env("GEMINI_API_KEY"))
    gemini_model: str | None = Field(default=None, validation_alias=_env("GEMINI_MODEL"))

    # --- memory (Phase 4) ------------------------------------------------
    database_url: SecretStr | None = Field(default=None, validation_alias=_env("DATABASE_URL"))

    # --- observability (Phase 3) -----------------------------------------
    langfuse_host: str | None = Field(default=None, validation_alias=_env("LANGFUSE_HOST"))
    langfuse_public_key: str | None = Field(
        default=None, validation_alias=_env("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias=_env("LANGFUSE_SECRET_KEY")
    )

    # --- derived ---------------------------------------------------------
    @property
    def is_mock_mode(self) -> bool:
        """Whether this run uses the deterministic synthetic estate."""
        return self.mode is RunMode.MOCK

    @property
    def uses_fake_llm(self) -> bool:
        """Whether this run uses the deterministic fake language model."""
        return self.llm_backend is LLMBackend.FAKE

    @property
    def requires_no_credentials(self) -> bool:
        """Whether this configuration can run with no secrets at all."""
        return self.is_mock_mode and self.uses_fake_llm

    @property
    def memory_enabled(self) -> bool:
        """Whether a memory database is configured (Phase 4).

        False means stateless operation: ranking is not improved by prior outcomes,
        but correctness is unaffected (ARCHITECTURE.md D11).
        """
        return self.database_url is not None

    @property
    def tracing_enabled(self) -> bool:
        """Whether Langfuse is configured. False makes tracing a no-op."""
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def checkpoint_path(self) -> Path:
        """Path to the LangGraph SQLite checkpoint database."""
        return self.state_dir / "checkpoints.sqlite"

    def ensure_state_dir(self) -> Path:
        """Create the state directory if it does not exist, and return it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment.

    Cached so that configuration is resolved a single time per process. Tests that
    need different settings construct :class:`Settings` directly and pass it in --
    every component takes settings as an argument rather than reaching for this.
    """
    return Settings()
