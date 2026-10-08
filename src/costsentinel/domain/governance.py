"""Governance records: classifications, approval requests, decisions and audit events.

Nothing here executes anything. These are the artefacts that answer, after the fact,
"who approved touching that resource, on what evidence, and when?".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import Frozen, Provenance, utc_now
from costsentinel.domain.recommendations import ActionClass, ActionType


class ActionClassification(Frozen):
    """The Policy Guard's verdict on one proposed action.

    ``policy_reference`` names the rule that decided it, so a classification can be
    disputed against a rule rather than against a black box.
    """

    recommendation_id: str
    action: ActionType
    action_class: ActionClass
    floor_class: ActionClass = Field(
        description="The built-in minimum class for this action; policy may tighten, never loosen."
    )
    requires_approval: bool
    is_automatable: bool
    policy_reference: str
    reason: str
    classified_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance


class Decision(StrEnum):
    """The outcome of an approval request.

    There is no "timed out means approved" member, and absence of a decision is not
    a member at all -- a run with no recorded decision simply stays halted.
    """

    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(Frozen):
    """A request put to a human before a gated action may proceed."""

    request_id: str
    run_id: str
    recommendation_id: str
    client: str
    subscription_id: str
    target_resource_id: str
    action: ActionType
    action_class: ActionClass
    summary: str
    monthly_saving_display: str = Field(
        description="Pre-rendered so an approver sees the same figure the report shows."
    )
    requested_at: datetime = Field(default_factory=utc_now)


class ApprovalDecision(Frozen):
    """A recorded human decision on one recommendation.

    Persisted before the graph proceeds. This record, not an in-memory flag, is what
    authorises an action.
    """

    recommendation_id: str
    decision: Decision
    approver: str
    decided_at: datetime = Field(default_factory=utc_now)
    note: str | None = None

    @property
    def is_approved(self) -> bool:
        """Whether this decision authorises the action."""
        return self.decision is Decision.APPROVED


class AuditEventType(StrEnum):
    """The audit vocabulary. Every graph node emits at least one of these."""

    SCAN_STARTED = "scan_started"
    ESTATE_READ = "estate_read"
    SIGNAL_DETECTED = "signal_detected"
    RECOMMENDATION_PROPOSED = "recommendation_proposed"
    ACTION_CLASSIFIED = "action_classified"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RECORDED = "approval_recorded"
    REPORT_GENERATED = "report_generated"
    SCAN_COMPLETED = "scan_completed"
    ERROR = "error"


class AuditEvent(Frozen):
    """One append-only entry in the audit trail.

    ``detail`` is a flat string-to-string mapping on purpose: it serialises
    unambiguously, is safe to render in a client-facing trail, and cannot smuggle an
    arbitrary object into an audit record.
    """

    event_id: str
    run_id: str
    event_type: AuditEventType
    actor: str = Field(description="The node, adapter or person responsible.")
    subject: str = Field(description="What the event is about: a resource, signal or report id.")
    detail: Mapping[str, str] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)
