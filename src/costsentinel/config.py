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

from pydantic import AliasChoices, Field, SecretStr, model_validator
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


class MemoryBackend(StrEnum):
    """Which store backs episodic and semantic memory.

    ``SQLITE`` is the default and needs nothing: no service, no network, no
    dependency beyond the standard library. That is what lets the learning loop be
    demonstrated offline and tested in CI.

    ``OFF`` is a first-class choice rather than an absence. A deployment that does
    not want a history on disk should be able to say so, and every consulting node
    already handles an empty store, so turning memory off is a configuration change
    rather than a different code path.
    """

    SQLITE = "sqlite"
    POSTGRES = "postgres"
    OFF = "off"


class EmbedderBackend(StrEnum):
    """Which embedder turns text into a vector.

    ``FAKE`` is deterministic and is what CI and the demo use. It is not a stub: it
    hashes tokens into a fixed-width vector, so cosine similarity genuinely tracks
    token overlap and retrieval results are both meaningful and assertable.
    """

    FAKE = "fake"
    AZURE_OPENAI = "azure_openai"


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
    llm_fallback_backend: LLMBackend | None = Field(
        default=None,
        validation_alias=_env("LLM_FALLBACK_BACKEND"),
        description=(
            "Backend to retry on when the primary fails or returns something that "
            "will not validate. ``None`` (the default) means no fallback: a failure "
            "costs the prose and nothing else, which is already a safe outcome, so a "
            "second backend is a reliability choice rather than a requirement.\n"
            "\n"
            "Must differ from the primary. Retrying the same backend is a retry, not a "
            "fallback, and configuring one as the other would make a trace say "
            "something untrue."
        ),
    )
    llm_token_budget: int | None = Field(
        default=None,
        gt=0,
        validation_alias=_env("LLM_TOKEN_BUDGET"),
        description=(
            "Ceiling on model tokens per sweep. ``None`` means uncapped, which is the "
            "default because the right ceiling depends on the estate -- a figure "
            "guessed here would either stop a large sweep narrating itself or fail to "
            "cap anything.\n"
            "\n"
            "Reaching it costs the prose and nothing else: every figure, finding, "
            "classification and approval-queue entry is deterministic arithmetic the "
            "model never touched, so a capped run produces a complete report with its "
            "narrative replaced by a stated reason."
        ),
    )

    # --- A2A surface -----------------------------------------------------
    a2a_enabled: bool = Field(
        default=False,
        validation_alias=_env("A2A_ENABLED"),
        description=(
            "Whether to serve the A2A endpoint. Off by default: a surface another "
            "agent can reach should be something an operator turned on, not something "
            "that appeared. The Agent Card is published either way, so a consumer can "
            "discover what is available rather than probe for it."
        ),
    )
    a2a_url: str = Field(
        default="http://localhost:8000",
        validation_alias=_env("A2A_URL"),
        description="Where the card says the endpoint lives.",
    )
    a2a_signing_key: SecretStr | None = Field(
        default=None,
        validation_alias=_env("A2A_SIGNING_KEY"),
        description=(
            "Key the Agent Card is signed with. Unset in mock mode uses the committed "
            "synthetic key, so the signature is exercised in CI -- one whose signing "
            "has never been checked is not a feature. Unset in real mode produces an "
            "*unsigned* card rather than one signed with a published key, because a "
            "signature anybody can forge invites a consumer to trust it."
        ),
    )

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

    # --- exemption policy -------------------------------------------------
    exemption_rules_path: Path | None = Field(
        default=None,
        validation_alias=_env("EXEMPTION_RULES_PATH"),
        description=(
            "Ruleset naming the tag conventions that mark a resource as idle, stale "
            "or oversized on purpose. Unset uses the built-in rules. A client whose "
            "tags read 'dr-role: standby' rather than 'role: disaster-recovery' is a "
            "file, not a code change."
        ),
    )

    # --- Azure (MODE=real) -----------------------------------------------
    # There is deliberately no client-secret setting. CostSentinel authenticates
    # with DefaultAzureCredential, which picks up an existing ``az login``, a
    # managed identity or a workload identity. A secret CostSentinel never reads is
    # a secret CostSentinel cannot leak into a log, a trace or a commit.
    azure_tenant_id: str | None = Field(default=None, validation_alias=_env("AZURE_TENANT_ID"))
    azure_subscription_ids: str | None = Field(
        default=None,
        validation_alias=_env("AZURE_SUBSCRIPTION_IDS"),
        description=(
            "Comma-separated subscription ids a real run may read. There is no "
            "wildcard: a sweep reads what the operator named, never everything a "
            "credential happens to reach."
        ),
    )
    azure_client_tag: str = Field(
        default="client",
        validation_alias=_env("AZURE_CLIENT_TAG"),
        description=(
            "Subscription tag naming the client a subscription belongs to. Azure has "
            "no notion of CostSentinel's client boundary, so a managed-service "
            "provider marks it with a tag."
        ),
    )

    # --- Azure OpenAI (LLM_BACKEND=azure_openai) -------------------------
    azure_openai_endpoint: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_ENDPOINT")
    )
    azure_openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=_env("AZURE_OPENAI_API_KEY"),
        description=(
            "Optional. When unset, the adapter authenticates with "
            "DefaultAzureCredential, which is the preferred path: no key to store, "
            "rotate or leak."
        ),
    )
    azure_openai_api_version: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_API_VERSION")
    )
    azure_openai_deployment: str | None = Field(
        default=None,
        validation_alias=_env("AZURE_OPENAI_DEPLOYMENT"),
        description="Deployment name for synthesis and judging work.",
    )
    azure_openai_deployment_small: str | None = Field(
        default=None,
        validation_alias=_env("AZURE_OPENAI_DEPLOYMENT_SMALL"),
        description=(
            "Deployment for high-volume classification work. Falls back to "
            "AZURE_OPENAI_DEPLOYMENT, so a single-deployment setup needs no second "
            "variable."
        ),
    )

    # --- Gemini (LLM_BACKEND=gemini) -------------------------------------
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias=_env("GEMINI_API_KEY"))
    gemini_model: str | None = Field(default=None, validation_alias=_env("GEMINI_MODEL"))
    gemini_model_small: str | None = Field(
        default=None,
        validation_alias=_env("GEMINI_MODEL_SMALL"),
        description="Falls back to GEMINI_MODEL.",
    )

    # --- model call behaviour --------------------------------------------
    llm_max_attempts: int = Field(
        default=2,
        ge=1,
        le=5,
        validation_alias=_env("LLM_MAX_ATTEMPTS"),
        description=(
            "How many times a structured call may be attempted. A real model "
            "occasionally returns something that does not validate, and the second "
            "attempt is told exactly what was wrong."
        ),
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        validation_alias=_env("LLM_TIMEOUT_SECONDS"),
    )

    # --- model token prices ----------------------------------------------
    # Quoted per million tokens, and deliberately with no default. A per-run cost
    # is a cost figure, so inventing a rate for whatever deployment the operator
    # named would be exactly the fabrication CLAUDE.md golden rule 5 forbids. Unset
    # means the run reports its model cost as unknown, which is the truth.
    llm_price_small_prompt: Decimal | None = Field(
        default=None, ge=Decimal(0), validation_alias=_env("LLM_PRICE_SMALL_PROMPT")
    )
    llm_price_small_completion: Decimal | None = Field(
        default=None, ge=Decimal(0), validation_alias=_env("LLM_PRICE_SMALL_COMPLETION")
    )
    llm_price_large_prompt: Decimal | None = Field(
        default=None, ge=Decimal(0), validation_alias=_env("LLM_PRICE_LARGE_PROMPT")
    )
    llm_price_large_completion: Decimal | None = Field(
        default=None, ge=Decimal(0), validation_alias=_env("LLM_PRICE_LARGE_COMPLETION")
    )

    # --- memory ----------------------------------------------------------
    memory_backend: MemoryBackend = Field(
        default=MemoryBackend.SQLITE, validation_alias=_env("MEMORY_BACKEND")
    )
    memory_path: Path | None = Field(
        default=None,
        validation_alias=_env("MEMORY_PATH"),
        description=(
            "Where the SQLite memory database lives. Defaults to memory.sqlite "
            "inside STATE_DIR. Set to a path to keep a history across runs."
        ),
    )
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias=_env("DATABASE_URL"),
        description="Required for MEMORY_BACKEND=postgres. Never read from the repository.",
    )
    memory_recall_limit: int = Field(
        default=500,
        ge=1,
        le=10_000,
        validation_alias=_env("MEMORY_RECALL_LIMIT"),
        description="How many historical recommendations a consult step reads per client.",
    )

    # --- embedding -------------------------------------------------------
    embedder_backend: EmbedderBackend = Field(
        default=EmbedderBackend.FAKE, validation_alias=_env("EMBEDDER_BACKEND")
    )
    embedding_dimensions: int = Field(
        default=4096,
        ge=16,
        le=8192,
        validation_alias=_env("EMBEDDING_DIMENSIONS"),
        description=(
            "Vector width. One setting rather than two, because it is also the "
            "pgvector column width, which has to match whatever embedder is "
            "actually in use. Set it to your real embedder's width when using one. "
            "Changing it invalidates every stored chunk, which the embedder "
            "identifier on each chunk is there to make visible."
        ),
    )
    azure_openai_embedding_deployment: str | None = Field(
        default=None, validation_alias=_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    )

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

    @model_validator(mode="after")
    def _fallback_differs_from_primary(self) -> Settings:
        """A backend cannot fall back to itself.

        Refused rather than silently ignored, because a configuration that looks like
        a fallback and is not one would make the trace claim a resilience this run
        does not have.
        """
        if self.llm_fallback_backend is not None and self.llm_fallback_backend is self.llm_backend:
            msg = (
                f"llm_fallback_backend is {self.llm_backend.value!r}, the same as the "
                f"primary. Retrying the same backend is a retry, not a fallback; set a "
                f"different backend or leave the fallback unset."
            )
            raise ValueError(msg)
        return self

    @property
    def has_llm_fallback(self) -> bool:
        """Whether a secondary backend is configured."""
        return self.llm_fallback_backend is not None

    @property
    def llm_is_capped(self) -> bool:
        """Whether this run has a token ceiling."""
        return self.llm_token_budget is not None

    @property
    def uses_fake_llm(self) -> bool:
        """Whether this run uses the deterministic fake language model."""
        return self.llm_backend is LLMBackend.FAKE

    @property
    def subscription_scope(self) -> tuple[str, ...]:
        """The subscriptions a real run may read, parsed from the environment.

        Empty when none were named, which the real provider refuses to run with.
        """
        if not self.azure_subscription_ids:
            return ()
        return tuple(
            part.strip() for part in self.azure_subscription_ids.split(",") if part.strip()
        )

    @property
    def requires_no_credentials(self) -> bool:
        """Whether this configuration can run with no secrets at all.

        The default SQLite memory store needs none, so memory does not appear here;
        a real embedder or a Postgres URL does.
        """
        return (
            self.is_mock_mode
            and self.uses_fake_llm
            and self.uses_fake_embedder
            and self.memory_backend is not MemoryBackend.POSTGRES
        )

    def token_rates(self, tier: str) -> tuple[Decimal, Decimal] | None:
        """Configured prompt and completion rates per million tokens for one tier.

        ``None`` when either rate is unset, because half a rate cannot price a call
        and a missing half silently read as zero would understate a run's cost.
        """
        if tier == "small":
            prompt, completion = self.llm_price_small_prompt, self.llm_price_small_completion
        else:
            prompt, completion = self.llm_price_large_prompt, self.llm_price_large_completion
        if prompt is None or completion is None:
            return None
        return prompt, completion

    @property
    def memory_enabled(self) -> bool:
        """Whether memory persists anything at all.

        False means stateless operation: ranking is not improved by prior outcomes,
        but correctness is unaffected (ARCHITECTURE.md D11). Note this is *not* the
        same as "a database is configured" -- the default SQLite store persists
        happily with no database in sight.
        """
        return self.memory_backend is not MemoryBackend.OFF

    @property
    def uses_fake_embedder(self) -> bool:
        """Whether this run embeds with the deterministic fake."""
        return self.embedder_backend is EmbedderBackend.FAKE

    @property
    def memory_db_path(self) -> Path:
        """Where the SQLite memory database lives."""
        return self.memory_path or (self.state_dir / "memory.sqlite")

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
