"""Governance records: classifications, approval requests, decisions and audit events.

Nothing here executes anything. These are the artefacts that answer, after the fact,
"who approved touching that resource, on what evidence, and when?".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

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


class RouteAction(StrEnum):
    """What the supervisor decided to do next.

    A closed vocabulary so routing is auditable: every transition in a run is one of
    these, with a recorded reason, rather than an implicit consequence of control
    flow.
    """

    PROCEED = "proceed"
    RETRY = "retry"
    ESCALATE = "escalate"
    REQUIRE_APPROVAL = "require_approval"
    SHORT_CIRCUIT = "short_circuit"
    COMPLETE = "complete"


class SupervisorDecision(Frozen):
    """One routing decision, recorded so a sweep can be explained afterwards.

    ``reason`` is mandatory. A retry or an escalation that does not say why it
    happened is indistinguishable from a bug.
    """

    from_node: str
    action: RouteAction
    to_node: str
    reason: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    decided_at: datetime = Field(default_factory=utc_now)


class Decision(StrEnum):
    """The outcome of an approval request.

    There is no "timed out means approved" member, and absence of a decision is not
    a member at all -- a run with no recorded decision simply stays halted.

    ``DEFERRED`` was added in Phase 10, when the gate became a durable interrupt and
    an approver gained the ability to say "not now" to a sweep that is actually
    waiting on them. It is a *recorded* answer that resolves nothing: the item stays
    in the queue and the sweep stays paused. That is deliberately different from a
    rejection, which resolves it, and from silence, which records nothing -- "ask me
    next week" and "no" lead to different conversations with a client, and a gate
    that collapsed them would either nag about something they intend to do or forget
    something they only postponed.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"

    @property
    def resolves(self) -> bool:
        """Whether this answer takes the item out of the pending queue.

        A deferral does not. It is the one answer that leaves the sweep exactly where
        it was, with a record of having been asked.
        """
        return self is not Decision.DEFERRED


class ApproverAssurance(StrEnum):
    """How much the record knows about *who* decided (ARCHITECTURE.md D127).

    Two members, and the default is the weaker one. A name typed into a terminal or a
    form is a claim; until something checks it, the honest record of it is "this is who
    they said they were". The audit trail must never read as if an identity was
    established when it was only asserted -- a client asking "who approved deleting
    that disk?" deserves to be told which of the two answers they are getting.
    """

    #: Typed by the person deciding. Nothing checked it.
    SELF_ASSERTED = "self_asserted"

    #: Established by an authenticator, which the record names.
    VERIFIED = "verified"

    @property
    def label(self) -> str:
        """The words a reader sees next to the name."""
        match self:
            case ApproverAssurance.SELF_ASSERTED:
                return "self-asserted, unverified"
            case ApproverAssurance.VERIFIED:
                return "verified"


def _check_assurance(assurance: ApproverAssurance, verified_by: str | None) -> None:
    """A verified identity names its verifier; a self-asserted one cannot name any.

    The second half matters as much as the first. A self-asserted record carrying an
    authenticator's name would read as half-verified, which is a claim nothing made.
    """
    if assurance is ApproverAssurance.VERIFIED and not verified_by:
        msg = "a verified approver must name the authenticator that verified them"
        raise ValueError(msg)
    if assurance is ApproverAssurance.SELF_ASSERTED and verified_by:
        msg = "a self-asserted approver was verified by nothing; verified_by must be empty"
        raise ValueError(msg)


_ASSURANCE_DESCRIPTION = (
    "Whether the approver's identity was verified or only asserted. Defaults to "
    "self-asserted, so a record written before identities could be verified -- and "
    "every record taken without an authenticator -- reads as unverified."
)
_VERIFIED_BY_DESCRIPTION = "The authenticator that established the identity, when one did."


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
    approver_assurance: ApproverAssurance = Field(
        default=ApproverAssurance.SELF_ASSERTED, description=_ASSURANCE_DESCRIPTION
    )
    verified_by: str | None = Field(default=None, description=_VERIFIED_BY_DESCRIPTION)
    decided_at: datetime = Field(default_factory=utc_now)
    note: str | None = None

    @model_validator(mode="after")
    def _assurance_is_consistent(self) -> ApprovalDecision:
        _check_assurance(self.approver_assurance, self.verified_by)
        return self

    @property
    def is_approved(self) -> bool:
        """Whether this decision authorises the action.

        "Authorises" means a person may now carry it out by hand. It has never meant
        that CostSentinel will, and from Phase 10 that is asserted four ways rather
        than being true only because no execution code happens to exist yet
        (ARCHITECTURE.md D118).
        """
        return self.decision is Decision.APPROVED


class ApprovalState(StrEnum):
    """Where a gated recommendation has got to.

    Four states and no fifth. The one that matters is what is *missing*: there is no
    ``EXECUTED``, no ``APPLYING`` and no ``FAILED``, because there is no execution
    path for a state machine to describe. An approval ends at
    :attr:`HANDED_OFF` -- a person has agreed and a person must now do it -- and
    adding a member here would be the first half of building the thing this system
    deliberately cannot do.
    """

    #: Classified as needing a decision; nobody has given one.
    AWAITING_DECISION = "awaiting_decision"

    #: Approved. A human operator carries it out; CostSentinel does not and cannot.
    HANDED_OFF = "handed_off"

    #: Rejected. Resolved, and remembered so it is not re-pitched as new.
    DECLINED = "declined"

    #: Deferred. Recorded, still awaiting a decision.
    DEFERRED = "deferred"

    @classmethod
    def for_decision(cls, decision: Decision) -> ApprovalState:
        """The state one decision leads to.

        Exhaustive over :class:`Decision` by construction: a member added there with
        no mapping here is a static error rather than a silent fall-through to
        something permissive.
        """
        match decision:
            case Decision.APPROVED:
                return cls.HANDED_OFF
            case Decision.REJECTED:
                return cls.DECLINED
            case Decision.DEFERRED:
                return cls.DEFERRED

    @property
    def is_pending(self) -> bool:
        """Whether this item is still waiting on a human."""
        return self in {ApprovalState.AWAITING_DECISION, ApprovalState.DEFERRED}


class FrozenFigures(Frozen):
    """The figures as they stood when a decision was made.

    Copied rather than referenced, and the reason is the whole point of the type. An
    approver agrees to a *specific* saving on a *specific* resource at a moment in
    time. A re-scan next month that reprices the resource, or a catalogue refresh that
    moves the rate, must not retroactively change what they agreed to -- and if the
    record pointed at live figures it silently would.

    Strings rather than ``MoneyAmount``, because what is being frozen is what the
    approver was *shown*: the display the report rendered, down to the grouping and
    the currency. Re-deriving it later from an amount would be re-deriving it from
    data that may have moved.
    """

    monthly_saving_display: str = Field(description="As the report rendered it.")
    annual_saving_display: str = Field(description="As the report rendered it.")
    current_monthly_cost_display: str = Field(description="As the report rendered it.")
    savings_basis: str = Field(
        default="", description="How the saving was derived, as the approver saw it."
    )


class PendingApproval(Frozen):
    """One gated recommendation waiting on a human, as a paused sweep presents it.

    What an operator sees in ``costsentinel approvals list`` and what the interrupt
    payload carries. Everything needed to decide, and nothing that could carry out the
    decision: there is no handle, no command and no endpoint on this type.
    """

    run_id: str = Field(description="The sweep that is paused. Also its checkpoint thread id.")
    recommendation_id: str
    client: str
    subscription_id: str
    target_resource_id: str
    target_resource_name: str
    action: ActionType
    action_class: ActionClass
    waste_kind: str = Field(default="", description="What was detected, for context.")
    summary: str = Field(min_length=1, description="What is being proposed, in a line.")
    figures: FrozenFigures
    requires_approval: bool = True
    state: ApprovalState = ApprovalState.AWAITING_DECISION
    deferrals: int = Field(
        default=0,
        ge=0,
        description=(
            "How many times this item has been deferred. Carried so an operator can "
            "see that an item keeps being postponed, which is itself a decision about "
            "it that nobody has written down."
        ),
    )


class DecisionRequest(Frozen):
    """One decision arriving from outside the graph, before it is trusted.

    A resume value comes from a CLI invocation today and from an adapter's webhook in
    Phase 10b, so it is untrusted input in exactly the sense an LLM response is -- and
    it is handled the same way: validated against a schema rather than read field by
    field, with unknown keys refused rather than ignored.

    What it deliberately cannot carry is any of the *facts*. No resource id, no action,
    no figure, no action class. Those are copied from the pending item the
    recommendation id names, so a malformed or hostile request can fail to decide
    anything and cannot decide something other than what it appears to
    (ARCHITECTURE.md D120).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendation_id: str = Field(min_length=1)
    decision: Decision
    approver: str = Field(min_length=1, description="Who decided. Never defaulted.")
    reason: str = Field(
        min_length=1,
        description=(
            "Why. Mandatory even for an approval: a decision with no stated reason "
            "teaches the episodic memory nothing and cannot be explained to a client."
        ),
    )
    approver_assurance: ApproverAssurance = Field(
        default=ApproverAssurance.SELF_ASSERTED, description=_ASSURANCE_DESCRIPTION
    )
    verified_by: str | None = Field(default=None, description=_VERIFIED_BY_DESCRIPTION)

    @model_validator(mode="after")
    def _assurance_is_consistent(self) -> DecisionRequest:
        _check_assurance(self.approver_assurance, self.verified_by)
        return self


class ApprovalRecord(Frozen):
    """A recorded decision on one gated recommendation, against a paused sweep.

    Distinct from :class:`ApprovalDecision`, which is the minimal authorisation the
    gate protocol passes around. This is the *audit* record: who, what, when, why, and
    the figures they were looking at.

    Carries **no executable handle of any kind** -- no callable, no command, no
    endpoint, no resource client. The absence is asserted against the field set, so a
    field added later fails a test rather than quietly becoming the first half of an
    execution path (ARCHITECTURE.md D118).
    """

    record_id: str
    run_id: str
    recommendation_id: str
    client: str
    subscription_id: str
    target_resource_id: str
    target_resource_name: str
    action: ActionType
    action_class: ActionClass
    decision: Decision
    state: ApprovalState
    approver: str = Field(min_length=1, description="Who decided. Never a default or a system id.")
    approver_assurance: ApproverAssurance = Field(
        default=ApproverAssurance.SELF_ASSERTED, description=_ASSURANCE_DESCRIPTION
    )
    verified_by: str | None = Field(default=None, description=_VERIFIED_BY_DESCRIPTION)
    reason: str = Field(
        min_length=1,
        description=(
            "Why. Mandatory on every decision including an approval, because a "
            "decision with no stated reason teaches the episodic memory nothing and "
            "cannot be explained back to a client later."
        ),
    )
    figures: FrozenFigures
    decided_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    @model_validator(mode="after")
    def _assurance_is_consistent(self) -> ApprovalRecord:
        _check_assurance(self.approver_assurance, self.verified_by)
        return self

    @property
    def approver_display(self) -> str:
        """The name with how much is known about it, so the two are never separated."""
        return f"{self.approver} ({self.approver_assurance.label})"

    @property
    def authorises_a_person(self) -> bool:
        """Whether a human operator may now carry this out by hand.

        Named for what it actually permits. ``is_authorised`` would invite a reader to
        assume something in this process acts on it, and nothing does.
        """
        return self.state is ApprovalState.HANDED_OFF

    def statement(self) -> str:
        """The decision in plain words, composed here rather than by a model."""
        verb = {
            ApprovalState.HANDED_OFF: "approved",
            ApprovalState.DECLINED: "rejected",
            ApprovalState.DEFERRED: "deferred",
            ApprovalState.AWAITING_DECISION: "left awaiting a decision",
        }[self.state]
        tail = (
            " It is handed off for a person to carry out; CostSentinel has no path to perform it."
            if self.state is ApprovalState.HANDED_OFF
            else ""
        )
        return (
            f"{self.approver_display} {verb} {self.action.value.replace('_', ' ')} on "
            f"{self.target_resource_name} ({self.figures.monthly_saving_display} / month) "
            f"on {self.decided_at.date().isoformat()}: {self.reason}{tail}"
        )


class AuditEventType(StrEnum):
    """The audit vocabulary. Every graph node emits at least one of these."""

    SCAN_STARTED = "scan_started"
    ESTATE_READ = "estate_read"
    SIGNAL_DETECTED = "signal_detected"
    ANOMALY_DETECTED = "anomaly_detected"
    SPEND_ASSESSED = "spend_assessed"
    EXEMPTION_APPLIED = "exemption_applied"
    RECOMMENDATION_PROPOSED = "recommendation_proposed"
    ACTION_CLASSIFIED = "action_classified"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RECORDED = "approval_recorded"
    #: The sweep halted at a durable checkpoint, waiting on a human (Phase 10).
    SWEEP_PAUSED = "sweep_paused"
    #: A decision was recorded and the sweep continued from where it stopped.
    SWEEP_RESUMED = "sweep_resumed"
    #: An approved action was handed to a human operator. This is as far as an
    #: approval goes: there is no execution event, because there is no execution.
    ACTION_HANDED_OFF = "action_handed_off"
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
