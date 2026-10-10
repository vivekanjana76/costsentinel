"""Task-based model routing.

Different reasoning tasks deserve different models: classifying a hundred signals is
cheap and high-volume, judging a report's groundedness is neither. Routing lives in a
table rather than in code so that model choice is an operational change
(ARCHITECTURE.md section 5).

The fake backend ignores which model it was routed to, as it should. The real
backends take their model identifiers from the environment, so a model swap is a
configuration change and the trace records the identifier that actually served the
call rather than a placeholder.
"""

from __future__ import annotations

from costsentinel.config import LLMBackend, Settings
from costsentinel.domain.common import Frozen
from costsentinel.llm.base import LLMTask

# --- the task vocabulary ---------------------------------------------------

TASK_ROOT_CAUSE = LLMTask(
    name="root_cause",
    description="Explain why a waste signal exists, from the evidence attached to it.",
)
TASK_PLANNING = LLMTask(
    name="planning",
    description="Propose and rank a remediation for each waste signal.",
)
TASK_REPORT_PROSE = LLMTask(
    name="report_prose",
    description="Compose the executive summary and per-finding commentary.",
)
TASK_JUDGE = LLMTask(
    name="judge",
    description="Score a report's groundedness against the state it was built from.",
)

ALL_TASKS: tuple[LLMTask, ...] = (
    TASK_ROOT_CAUSE,
    TASK_PLANNING,
    TASK_REPORT_PROSE,
    TASK_JUDGE,
)


class ModelRoute(Frozen):
    """The model configuration chosen for one task."""

    task_name: str
    backend: LLMBackend
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 2048
    notes: str = ""


#: Which tier each task needs. ``small`` is for high-volume classification and
#: summarisation; ``large`` is for synthesis and judging.
_TASK_TIER: dict[str, str] = {
    TASK_ROOT_CAUSE.name: "small",
    TASK_PLANNING.name: "large",
    TASK_REPORT_PROSE.name: "large",
    TASK_JUDGE.name: "large",
}

#: What the fake backend is called in a trace. It ignores the identifier entirely,
#: but recording one keeps every run's trace the same shape.
FAKE_MODEL = "fake-deterministic"

#: Stands in when a real backend is routed without a model configured. It is
#: deliberately conspicuous: a trace saying ``unconfigured`` is a configuration bug
#: somebody can find, where a plausible-looking default would hide one.
UNCONFIGURED_MODEL = "unconfigured"


def model_for(backend: LLMBackend, tier: str, settings: Settings) -> str:
    """The model identifier a backend and tier resolve to under one configuration.

    The small tier falls back to the large deployment, so a single-deployment setup
    works without a second environment variable. Nothing falls back the other way:
    quietly sending synthesis work to a small model would degrade a report with no
    sign of why.
    """
    match backend:
        case LLMBackend.FAKE:
            return FAKE_MODEL
        case LLMBackend.AZURE_OPENAI:
            large = settings.azure_openai_deployment
            small = settings.azure_openai_deployment_small or large
            chosen = small if tier == "small" else large
        case LLMBackend.GEMINI:
            large = settings.gemini_model
            small = settings.gemini_model_small or large
            chosen = small if tier == "small" else large
    return chosen or UNCONFIGURED_MODEL


def tier_for(task: LLMTask) -> str:
    """The model tier a task needs.

    An unrouted task falls back to ``large``: being needlessly expensive is a better
    failure than being silently under-powered.
    """
    return _TASK_TIER.get(task.name, "large")


def resolve_route(task: LLMTask, settings: Settings) -> ModelRoute:
    """Resolve the model configuration for one task under one configuration."""
    tier = tier_for(task)
    backend = settings.llm_backend
    model = model_for(backend, tier, settings)
    return ModelRoute(
        task_name=task.name,
        backend=backend,
        model=model,
        temperature=0.0,
        notes=f"tier={tier}",
    )
