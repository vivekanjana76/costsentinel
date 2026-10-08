"""Task-based model routing.

Different reasoning tasks deserve different models: classifying a hundred signals is
cheap and high-volume, judging a report's groundedness is neither. Routing lives in a
table rather than in code so that model choice is an operational change
(ARCHITECTURE.md section 5).

Phase 1 has one backend -- the fake -- so every route resolves to it. The seam is
real now; Phase 3 fills the table with deployments.
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

#: Per-backend model identifiers by tier. Phase 3 fills in the real deployments from
#: configuration; the fake backend ignores the identifier entirely.
_BACKEND_MODELS: dict[LLMBackend, dict[str, str]] = {
    LLMBackend.FAKE: {"small": "fake-deterministic", "large": "fake-deterministic"},
    LLMBackend.AZURE_OPENAI: {"small": "configured-small", "large": "configured-large"},
    LLMBackend.GEMINI: {"small": "configured-small", "large": "configured-large"},
}


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
    model = _BACKEND_MODELS[backend][tier]
    return ModelRoute(
        task_name=task.name,
        backend=backend,
        model=model,
        temperature=0.0,
        notes=f"tier={tier}",
    )
