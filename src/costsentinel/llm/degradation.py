"""What a sweep does when the model is unavailable, failing, or too expensive.

Until Phase 9 a model failure lost the whole sweep. The CLI caught :class:`LLMError`
and exited, which meant a transient 503 from one backend threw away every
deterministic finding, every savings figure and the approval queue -- none of which the
model produced or could have produced. That is the wrong trade in both directions: it
risks a client's whole report on a service CostSentinel does not need in order to be
correct, and it offers no way to cap what the model costs.

Two layers, and one invariant
-----------------------------

:class:`FallbackLLM` tries the primary backend and, on failure, the secondary. The
fallback is **recorded**, not silent: a degradation that nobody can see is one that
goes on forever.

:class:`BudgetedLLM` caps the tokens one run may spend. When the ceiling is reached it
stops issuing calls and raises :class:`LLMBudgetExhaustedError`, which the prose
call sites treat as "no prose available" rather than as a failure.

The invariant both depend on, and which :func:`optional_prose` exists to make
structural: **prose degrades and nothing else does.** Every figure, classification,
finding, rank and queue entry in a CostSentinel report is deterministic arithmetic over
provider data. A missing, fallen-back or budget-cut model changes the *narration* of
those facts and cannot change one of them, because the model never set one in the first
place. So a degraded report is a complete report with less explanation in it -- and
that is tested by comparing the deterministic parts of three reports built with prose,
with fallback prose, and with none.

Why the gap is marked rather than left blank
--------------------------------------------

A missing executive summary renders as a stated reason, not as an empty space. A
client who opens a report with no narrative and no explanation has no way to tell a
degraded run from a run that found nothing worth saying, and the second reading is
considerably worse than the truth.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from costsentinel.domain.observability import TokenUsage
from costsentinel.llm.base import LLM, LLMError, LLMTask, Prompt
from costsentinel.observability.logging import get_logger

_log = get_logger("llm.degradation")

StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)

#: What a report says where a model-written passage would have been. Stated rather
#: than blank: a client cannot tell an empty narrative from a degraded one, and the
#: wrong reading is the more flattering one.
NARRATIVE_UNAVAILABLE = "Narrative unavailable (token budget reached)."

#: The same, when the backends failed rather than the budget running out.
NARRATIVE_UNAVAILABLE_BACKEND = "Narrative unavailable (no language model was reachable)."


class LLMBudgetExhaustedError(LLMError):
    """The run's token ceiling was reached, so no further model call was made.

    A subclass of :class:`LLMError` so every existing call site already handles it,
    and a distinct type so a caller that cares can tell a spend ceiling from a broken
    backend. The two are different operationally -- one is working as configured.
    """

    def __init__(self, *, spent: int, budget: int, task: str) -> None:
        """Carry the numbers, so a caller can report the ceiling it hit."""
        self.spent = spent
        self.budget = budget
        self.task = task
        super().__init__(
            f"token budget reached: {spent} of {budget} token(s) spent, so the "
            f"{task!r} call was not made. Every figure, finding and classification in "
            f"this run is unaffected; only the model-written prose is missing."
        )


class FallbackLLM:
    """A primary backend with a secondary behind it. Satisfies :class:`LLM`.

    Wraps rather than subclasses, like :class:`~costsentinel.llm.traced.TracedLLM`, so
    the two compose in either order and neither has to know about the other.

    A failure on the primary -- an outage, a rate limit, a response that will not
    validate -- is retried once on the secondary. A failure on both re-raises the
    *secondary's* error, with the primary's recorded in the log: the second failure is
    the one that ended the attempt, and the first is already visible in the trace.

    :class:`LLMBudgetExhaustedError` is deliberately **not** retried. A spend ceiling
    is not a backend problem, and trying the other backend would spend past the ceiling that
    had just been reached.
    """

    def __init__(self, primary: LLM, secondary: LLM) -> None:
        """Wrap two backends, tried in the order given."""
        self._primary = primary
        self._secondary = secondary
        self._fallbacks: list[FallbackEvent] = []

    @property
    def name(self) -> str:
        """Both backends, so a provenance reference says what could have served it."""
        return f"{self._primary.name}+fallback:{self._secondary.name}"

    @property
    def primary(self) -> LLM:
        """The backend tried first."""
        return self._primary

    @property
    def secondary(self) -> LLM:
        """The backend tried when the primary fails."""
        return self._secondary

    @property
    def fallbacks(self) -> tuple[FallbackEvent, ...]:
        """Every fallback this instance served, in order.

        Read by the run metrics and the trace. A silent fallback would mean a client's
        report was written by a model nobody chose, and nobody would know.
        """
        return tuple(self._fallbacks)

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        """Ask the primary, then the secondary.

        Raises:
            LLMBudgetExhaustedError: Straight through from the primary, unretried.
            LLMError: If both backends failed. The secondary's error, since that is
                the one that ended the attempt.
        """
        try:
            return self._primary.structured(task=task, prompt=prompt, schema=schema)
        except LLMBudgetExhaustedError:
            raise
        except LLMError as primary_error:
            event = FallbackEvent(
                task=task.name,
                from_backend=self._primary.name,
                to_backend=self._secondary.name,
                reason=str(primary_error),
            )
            self._fallbacks.append(event)
            _log.warning(
                "primary llm backend failed; falling back",
                extra={
                    "task": task.name,
                    "from_backend": event.from_backend,
                    "to_backend": event.to_backend,
                    "error": event.reason,
                },
            )
            return self._secondary.structured(task=task, prompt=prompt, schema=schema)


class FallbackEvent(BaseModel):
    """One occasion the secondary backend served a call the primary could not."""

    model_config = {"frozen": True}

    task: str
    from_backend: str
    to_backend: str
    reason: str

    def describe(self) -> str:
        """One line for a log, a trace or an audit detail."""
        return (
            f"{self.task}: {self.from_backend} failed ({self.reason}), served by {self.to_backend}"
        )


class BudgetedLLM:
    """A per-run ceiling on model token spend. Satisfies :class:`LLM`.

    Counts what each call reported and refuses to issue a new one once the ceiling is
    reached. Deliberately a *pre-call* check against the running total rather than a
    prediction of the next call's size: predicting would mean refusing calls that would
    have fit, and a budget that stops short of itself is a budget nobody trusts.

    The consequence is that the ceiling can be overshot by one call. That is the right
    way round for this system -- the alternative is a report with a missing section
    because an estimate was pessimistic -- and the overshoot is bounded by one call and
    recorded.

    The deterministic pipeline is untouched by any of this. Exhausting the budget costs
    the prose and nothing else.
    """

    def __init__(self, inner: LLM, *, budget: int) -> None:
        """Wrap a backend with a ceiling.

        Raises:
            ValueError: If the budget is not positive. A zero budget would mean a run
                that can never call a model, which is what the fake backend is for,
                and a negative one is meaningless.
        """
        if budget <= 0:
            msg = "a token budget must be positive; omit the budget to run uncapped"
            raise ValueError(msg)
        self._inner = inner
        self._budget = budget
        self._spent = 0
        self._refused: list[str] = []

    @property
    def name(self) -> str:
        """The inner backend's name. A budget is a policy, not a model."""
        return self._inner.name

    @property
    def budget(self) -> int:
        """The ceiling, in tokens."""
        return self._budget

    @property
    def spent(self) -> int:
        """Tokens reported by the calls made so far."""
        return self._spent

    @property
    def refused(self) -> tuple[str, ...]:
        """The tasks that were not attempted because the ceiling had been reached."""
        return tuple(self._refused)

    @property
    def exhausted(self) -> bool:
        """Whether the ceiling has been reached."""
        return self._spent >= self._budget

    def last_usage(self) -> TokenUsage | None:
        """Pass the inner backend's reported usage through, for the tracer."""
        reporter = getattr(self._inner, "last_usage", None)
        if callable(reporter):
            reported = reporter()
            if isinstance(reported, TokenUsage):
                return reported
        return None

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        """Make the call if the ceiling allows it.

        Raises:
            LLMBudgetExhaustedError: If the ceiling was already reached. Nothing is sent.
        """
        if self.exhausted:
            self._refused.append(task.name)
            _log.warning(
                "token budget reached; no further model calls will be made",
                extra={"task": task.name, "spent": self._spent, "budget": self._budget},
            )
            raise LLMBudgetExhaustedError(spent=self._spent, budget=self._budget, task=task.name)

        response = self._inner.structured(task=task, prompt=prompt, schema=schema)
        usage = self.last_usage()
        if usage is not None:
            self._spent += usage.total_tokens
        return response


def optional_prose[ResponseT: BaseModel](
    call: Callable[[], ResponseT],
    *,
    task: str,
    fallback: ResponseT,
    errors: list[str],
) -> ResponseT:
    """Run a prose call, or carry on without it.

    The single place the prose-only degradation rule is implemented, so it is one
    decision rather than four copies of one. Every model call in this system produces
    *narration* of facts the deterministic pipeline has already established, which is
    what makes carrying on possible at all: the fallback is an empty contract, every
    figure is already computed, and the report comes out complete with less explanation
    in it.

    Args:
        call: The model call, as a thunk, so this function does not need the task,
            prompt and schema types.
        task: What was being asked, for the recorded reason.
        fallback: The empty contract to use instead. Supplied by the caller because
            only the caller knows what an empty one of its own response looks like.
        errors: The state's error list, appended to. A degradation is recorded as a
            processing note so it reaches the report's own "incomplete" reasons -- a
            client is entitled to know their report is missing its narrative.

    Returns:
        The model's response, or ``fallback``.
    """
    try:
        return call()
    except LLMBudgetExhaustedError as exhausted:
        errors.append(
            f"{task}: no model prose, because the run's token budget of "
            f"{exhausted.budget} was reached. Every figure, finding and classification "
            f"is unaffected."
        )
        return fallback
    except LLMError as failure:
        errors.append(
            f"{task}: no model prose, because no language model was reachable "
            f"({failure}). Every figure, finding and classification is unaffected."
        )
        _log.warning("prose unavailable; continuing without it", extra={"task": task})
        return fallback


def degradation_notes(errors: Sequence[str]) -> tuple[str, ...]:
    """The subset of a run's processing notes that describe a missing narrative.

    Used by the report so a degraded run says so in the document rather than only in
    a log nobody reads.
    """
    return tuple(note for note in errors if "no model prose" in note)
