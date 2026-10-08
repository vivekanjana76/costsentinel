"""A deterministic language model for tests, CI and ``MODE=mock``.

:class:`FakeLLM` derives its response from the evidence in the prompt rather than
returning a canned fixture (ARCHITECTURE.md D22). That choice matters: a fixture
would make a graph test pass while proving nothing about the wiring, whereas reading
the evidence means a passing test actually demonstrates that the real signal reached
the model seam with the fields the node claimed to send.

It explains and ranks. It never originates a quantity -- every figure in its output
is copied verbatim from an evidence field, which is why the structured-output
contracts contain no numeric fields for it to fill in.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ValidationError

from costsentinel.domain.recommendations import ActionType
from costsentinel.llm.base import (
    EvidenceBlock,
    LLMTask,
    LLMValidationError,
    Prompt,
    StructuredResponseT,
)
from costsentinel.llm.contracts import (
    FindingNote,
    PlannedRemediation,
    RemediationPlan,
    ReportNarrative,
)

#: Evidence block labels the fake understands. The planner and report author
#: produce exactly these; a real backend reads them as rendered text.
LABEL_WASTE_SIGNAL = "waste_signal"
LABEL_RECOMMENDATION = "recommendation"
LABEL_TOTALS = "totals"

_UNRANKED = Decimal("-1")


def _to_decimal(raw: str | None) -> Decimal:
    """Parse a decimal from an evidence value, or return a sentinel.

    An unparseable or absent cost sorts last rather than raising: a signal whose
    cost is unknown is still worth reporting, just not worth ranking first.
    """
    if raw is None:
        return _UNRANKED
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return _UNRANKED


def _build_remediation_plan(prompt: Prompt) -> RemediationPlan:
    """Propose one remediation per signal, ranked by the cost at stake.

    The action is taken from the ``candidate_actions`` the prompt offered, which is
    how a model is constrained to the closed vocabulary the policy store permits for
    that waste kind.
    """
    blocks = prompt.blocks(LABEL_WASTE_SIGNAL)
    ordered = sorted(
        blocks,
        key=lambda b: (
            -_to_decimal(b.field("current_monthly_cost_amount")),
            b.require("signal_id"),
        ),
    )

    items: list[PlannedRemediation] = []
    for priority, block in enumerate(ordered, start=1):
        candidates = [
            part.strip()
            for part in (block.field("candidate_actions") or "").split(",")
            if part.strip()
        ]
        if not candidates:
            msg = (
                f"evidence block {block.label!r} for signal "
                f"{block.field('signal_id')!r} offered no candidate_actions"
            )
            raise LLMValidationError(msg)

        items.append(
            PlannedRemediation(
                signal_id=block.require("signal_id"),
                action=ActionType(candidates[0]),
                rationale=_rationale_for(block),
                priority=priority,
            )
        )
    return RemediationPlan(items=tuple(items))


def _rationale_for(block: EvidenceBlock) -> str:
    """Compose a rationale from the block's own evidence.

    Every quantity in the sentence is a verbatim copy of an evidence value. The fake
    adds sentence structure and nothing else.
    """
    kind = block.field("waste_kind") or "waste"
    name = block.require("resource_name")
    cost = block.field("current_monthly_cost") or "an unknown monthly cost"
    subscription = block.field("subscription_id") or "an unidentified subscription"

    observations = [
        f"{f.name.removeprefix('observation.')} is {f.value}"
        for f in block.fields
        if f.name.startswith("observation.")
    ]
    because = "; ".join(observations) if observations else "no further observations were recorded"

    return (
        f"{name} in subscription {subscription} was flagged as {kind.replace('_', ' ')} "
        f"and currently costs {cost} per month. The detector's evidence: {because}. "
        f"Remediating removes or reduces that charge without affecting any other resource, "
        f"subject to the preconditions attached to this recommendation."
    )


def _build_report_narrative(prompt: Prompt) -> ReportNarrative:
    """Compose an executive summary and per-finding notes from the evidence."""
    totals = prompt.blocks(LABEL_TOTALS)
    recommendations = prompt.blocks(LABEL_RECOMMENDATION)

    client = (totals[0].field("client") if totals else None) or "the client"
    spend = (totals[0].field("observed_monthly_spend") if totals else None) or "an unknown amount"
    monthly = (
        totals[0].field("projected_monthly_savings") if totals else None
    ) or "an unknown amount"
    annual = (
        totals[0].field("projected_annual_savings") if totals else None
    ) or "an unknown amount"
    awaiting = (totals[0].field("awaiting_approval_count") if totals else None) or "0"
    subscriptions = (totals[0].field("subscriptions_in_scope") if totals else None) or "0"

    if not recommendations:
        summary = (
            f"This sweep examined {subscriptions} subscription(s) for {client} and found no "
            f"actionable waste. Observed spend over the period was {spend}. No action is "
            f"required and nothing is awaiting your approval."
        )
        return ReportNarrative(executive_summary=summary, notes=())

    top = recommendations[0]
    summary = (
        f"Across {subscriptions} subscription(s) for {client}, CostSentinel identified "
        f"{len(recommendations)} actionable finding(s) worth {monthly} per month "
        f"({annual} annually) against observed spend of {spend}. The largest single "
        f"opportunity is {top.require('resource_name')}, where the recommended action is "
        f"{(top.field('action') or 'no action').replace('_', ' ')}, worth "
        f"{top.field('monthly_saving') or 'an unknown amount'} per month. "
        f"{awaiting} of these finding(s) require your explicit approval before anything "
        f"is changed; nothing has been or will be modified without it."
    )

    notes = tuple(
        FindingNote(
            recommendation_id=block.require("recommendation_id"),
            note=(
                f"{block.require('resource_name')}: "
                f"{(block.field('action') or 'no action').replace('_', ' ')} to save "
                f"{block.field('monthly_saving') or 'an unknown amount'} per month. "
                + (
                    "Requires your approval before it is applied."
                    if block.field("requires_approval") == "true"
                    else "Safe and reversible; no approval needed."
                )
            ),
        )
        for block in recommendations
    )
    return ReportNarrative(executive_summary=summary, notes=notes)


#: Which contracts the fake can satisfy. A request for anything else is a loud
#: failure rather than an empty object, so a new node cannot quietly get a stub.
_BUILDERS: dict[type[BaseModel], Callable[[Prompt], BaseModel]] = {
    RemediationPlan: _build_remediation_plan,
    ReportNarrative: _build_report_narrative,
}


class FakeLLM:
    """Deterministic structured output, derived from the prompt's evidence.

    Records every call so a test can assert on what a node actually sent, which is
    how the graph tests verify wiring rather than merely verifying output shape.
    """

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[tuple[str, Prompt]] = []

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "fake-llm"

    @property
    def call_count(self) -> int:
        """How many times this instance has been asked for a response."""
        return len(self.calls)

    def tasks_called(self) -> tuple[str, ...]:
        """The task names this instance has been asked for, in order."""
        return tuple(name for name, _ in self.calls)

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        """Produce a validated instance of ``schema`` from the prompt's evidence.

        Raises:
            LLMValidationError: If the schema is one the fake does not implement, or
                if the derived response fails validation. The response is validated
                here exactly as a real backend's would be, so the fake cannot
                produce output a real backend would not be allowed to.
        """
        self.calls.append((task.name, prompt))

        builder = _BUILDERS.get(schema)
        if builder is None:
            supported = ", ".join(sorted(s.__name__ for s in _BUILDERS))
            msg = (
                f"FakeLLM has no deterministic builder for {schema.__name__}. "
                f"Supported: {supported}. Add one in costsentinel.llm.fake so that "
                f"tests and CI keep working without an API key."
            )
            raise LLMValidationError(msg)

        try:
            return schema.model_validate(builder(prompt).model_dump())
        except ValidationError as exc:
            msg = f"FakeLLM produced a response that failed {schema.__name__} validation: {exc}"
            raise LLMValidationError(msg) from exc
