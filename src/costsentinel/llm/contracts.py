"""Structured-output contracts: the only shapes a model response may take.

Everything here is untrusted input (CLAUDE.md golden rule 4). ``extra="forbid"``
means a response carrying an unexpected field is a validation error rather than
silently-passed data, and :class:`~costsentinel.domain.recommendations.ActionType`
being a closed enum means an unrecognised action cannot survive validation at all.

Note what is *absent*: no contract here contains a monetary amount. A model is asked
to explain and to rank, never to quantify. Savings are computed arithmetically by the
planner from provider data (ARCHITECTURE.md D17).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from costsentinel.domain.recommendations import ActionType


class Untrusted(BaseModel):
    """Base for a validated model response.

    Not frozen: these are transient parse results, not domain facts. They are
    converted into frozen domain types immediately by the node that requested them.
    """

    model_config = ConfigDict(extra="forbid")


class PlannedRemediation(Untrusted):
    """A model's proposal for one waste signal.

    Note what the model is *not* asked for: no amount, and no preconditions.
    Preconditions are safety-critical and come from the policy store, not from a
    model response -- a compromised response must not be able to drop the check
    that a disk is still unattached before it is deleted.
    """

    signal_id: str = Field(min_length=1)
    action: ActionType = Field(
        description="Must be one of the candidate actions the prompt offered for this signal."
    )
    rationale: str = Field(min_length=1, max_length=1200)
    priority: int = Field(
        ge=1,
        description="The model's own ranking. Advisory only -- the planner re-ranks by savings.",
    )


class RemediationPlan(Untrusted):
    """The planner's requested response: one proposal per signal."""

    items: tuple[PlannedRemediation, ...] = Field(max_length=500)

    def for_signal(self, signal_id: str) -> PlannedRemediation | None:
        """The proposal for one signal, or ``None`` if the model omitted it."""
        return next((item for item in self.items if item.signal_id == signal_id), None)


class FindingNote(Untrusted):
    """Client-facing commentary on one recommendation."""

    recommendation_id: str = Field(min_length=1)
    note: str = Field(min_length=1, max_length=800)


class ReportNarrative(Untrusted):
    """The report author's requested response: prose only, no figures."""

    executive_summary: str = Field(min_length=1, max_length=4000)
    notes: tuple[FindingNote, ...] = Field(default=(), max_length=500)

    def note_for(self, recommendation_id: str) -> str:
        """Commentary for one recommendation, or an empty string."""
        return next(
            (n.note for n in self.notes if n.recommendation_id == recommendation_id),
            "",
        )
