"""Language-model access: a provider-agnostic, structured-output-only seam.

:func:`get_llm` is the only place a concrete backend is named. Agents depend on the
:class:`~costsentinel.llm.base.LLM` protocol, so swapping the fake for Azure OpenAI
is a configuration change.
"""

from costsentinel.config import LLMBackend, Settings
from costsentinel.llm.azure_openai import AzureOpenAILLM
from costsentinel.llm.base import (
    LLM,
    EvidenceBlock,
    EvidenceField,
    LLMError,
    LLMNotConfiguredError,
    LLMTask,
    LLMValidationError,
    Prompt,
)
from costsentinel.llm.contracts import (
    FindingNote,
    PlannedRemediation,
    RankingNote,
    RankingRationale,
    RemediationPlan,
    ReportNarrative,
    RootCauseAnalysis,
    RootCauseExplanation,
    RootCauseFactor,
)
from costsentinel.llm.degradation import (
    BudgetedLLM,
    FallbackLLM,
    LLMBudgetExhaustedError,
)
from costsentinel.llm.fake import (
    LABEL_RANKED,
    LABEL_RECOMMENDATION,
    LABEL_TOTALS,
    LABEL_WASTE_SIGNAL,
    FakeLLM,
)
from costsentinel.llm.gemini import GeminiLLM
from costsentinel.llm.routing import (
    ALL_TASKS,
    FAKE_MODEL,
    TASK_JUDGE,
    TASK_PLANNING,
    TASK_REPORT_PROSE,
    TASK_ROOT_CAUSE,
    UNCONFIGURED_MODEL,
    ModelRoute,
    model_for,
    resolve_route,
    tier_for,
)
from costsentinel.llm.structured import retrying, validate

__all__ = [
    "ALL_TASKS",
    "FAKE_MODEL",
    "LABEL_RANKED",
    "LABEL_RECOMMENDATION",
    "LABEL_TOTALS",
    "LABEL_WASTE_SIGNAL",
    "LLM",
    "TASK_JUDGE",
    "TASK_PLANNING",
    "TASK_REPORT_PROSE",
    "TASK_ROOT_CAUSE",
    "UNCONFIGURED_MODEL",
    "AzureOpenAILLM",
    "BudgetedLLM",
    "EvidenceBlock",
    "EvidenceField",
    "FakeLLM",
    "FallbackLLM",
    "FindingNote",
    "GeminiLLM",
    "LLMBudgetExhaustedError",
    "LLMError",
    "LLMNotConfiguredError",
    "LLMTask",
    "LLMValidationError",
    "ModelRoute",
    "PlannedRemediation",
    "Prompt",
    "RankingNote",
    "RankingRationale",
    "RemediationPlan",
    "ReportNarrative",
    "RootCauseAnalysis",
    "RootCauseExplanation",
    "RootCauseFactor",
    "build_backend",
    "get_llm",
    "model_for",
    "resolve_route",
    "retrying",
    "tier_for",
    "validate",
]


def build_backend(backend: LLMBackend, settings: Settings) -> LLM:
    """One bare backend, with no fallback and no budget around it.

    Separate from :func:`get_llm` because the fallback needs to build a second backend
    without recursing into the wrapping logic.

    Raises:
        LLMNotConfiguredError: If a real backend cannot be built from this
            configuration.
    """
    match backend:
        case LLMBackend.FAKE:
            return FakeLLM()
        case LLMBackend.AZURE_OPENAI:
            return AzureOpenAILLM(settings)
        case LLMBackend.GEMINI:
            return GeminiLLM(settings)


def get_llm(settings: Settings) -> LLM:
    """Build the language model this configuration selects, with its guardrails.

    Composed outwards from the backend: the primary, then a fallback to the secondary
    if one is configured, then a token ceiling if one is set. That order matters -- the
    budget wraps the fallback rather than the other way round, so the ceiling applies
    to the run as a whole and a fallback cannot spend past it by trying a second
    backend.

    Args:
        settings: Resolved configuration. ``LLM_BACKEND=fake`` (the default) yields
            the deterministic fake, which is the only backend CI ever uses;
            ``azure_openai`` and ``gemini`` yield the real adapters. A fallback backend
            and a token budget are both opt-in and both off by default.

    Returns:
        An object satisfying :class:`LLM`.

    Raises:
        LLMNotConfiguredError: If a real backend is selected but cannot be built
            with the configuration given.
    """
    built = build_backend(settings.llm_backend, settings)

    if settings.llm_fallback_backend is not None:
        built = FallbackLLM(built, build_backend(settings.llm_fallback_backend, settings))

    if settings.llm_token_budget is not None:
        built = BudgetedLLM(built, budget=settings.llm_token_budget)

    return built
