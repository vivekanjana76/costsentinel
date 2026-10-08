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
    RemediationPlan,
    ReportNarrative,
)
from costsentinel.llm.fake import (
    LABEL_RECOMMENDATION,
    LABEL_TOTALS,
    LABEL_WASTE_SIGNAL,
    FakeLLM,
)
from costsentinel.llm.gemini import GeminiLLM
from costsentinel.llm.routing import (
    ALL_TASKS,
    TASK_JUDGE,
    TASK_PLANNING,
    TASK_REPORT_PROSE,
    TASK_ROOT_CAUSE,
    ModelRoute,
    resolve_route,
    tier_for,
)

__all__ = [
    "ALL_TASKS",
    "LABEL_RECOMMENDATION",
    "LABEL_TOTALS",
    "LABEL_WASTE_SIGNAL",
    "LLM",
    "TASK_JUDGE",
    "TASK_PLANNING",
    "TASK_REPORT_PROSE",
    "TASK_ROOT_CAUSE",
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
    "RemediationPlan",
    "ReportNarrative",
    "get_llm",
    "resolve_route",
    "tier_for",
]


def get_llm(settings: Settings) -> LLM:
    """Build the language model this configuration selects.

    Args:
        settings: Resolved configuration. ``LLM_BACKEND=fake`` (the default) yields
            the deterministic fake; the real backends arrive in Phase 3.

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
