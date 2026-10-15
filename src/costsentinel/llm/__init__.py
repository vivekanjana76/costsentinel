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
    "EvidenceBlock",
    "EvidenceField",
    "FakeLLM",
    "FindingNote",
    "GeminiLLM",
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
    "get_llm",
    "model_for",
    "resolve_route",
    "retrying",
    "tier_for",
    "validate",
]


def get_llm(settings: Settings) -> LLM:
    """Build the language model this configuration selects.

    Args:
        settings: Resolved configuration. ``LLM_BACKEND=fake`` (the default) yields
            the deterministic fake, which is the only backend CI ever uses;
            ``azure_openai`` and ``gemini`` yield the real adapters.

    Returns:
        An object satisfying :class:`LLM`.

    Raises:
        LLMNotConfiguredError: If a real backend is selected but cannot be built
            with the configuration given.
    """
    match settings.llm_backend:
        case LLMBackend.FAKE:
            return FakeLLM()
        case LLMBackend.AZURE_OPENAI:
            return AzureOpenAILLM(settings)
        case LLMBackend.GEMINI:
            return GeminiLLM(settings)
