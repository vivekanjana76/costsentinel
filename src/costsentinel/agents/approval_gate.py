"""The approval gate: where a sweep stops and waits for a person.

The seam has existed since Phase 1 and the supervisor has routed `REQUIRE_APPROVAL`
since Phase 2 -- always to the report author, always with the honest reason that
nothing had an execution path anyway. This node makes the halt real. The sweep pauses
at a LangGraph checkpoint, the process may exit, and a decision recorded days later
from a different process resumes the same run from where it stopped.

Where the pause sits, and why after the report
----------------------------------------------

The gate runs **after** the Report Author, not between the Policy Guard and it. Pausing
earlier would mean no document exists until somebody decides, so a client waiting on
their cost review would be waiting on their own approval of the things in it --
circular, and it would withhold the one artefact that is correct and complete either
way. The report is produced, the queue in it is real, and *then* the sweep waits
(ARCHITECTURE.md D119).

One interrupt per node execution
--------------------------------

The node handles exactly one decision per execution and returns to the supervisor,
which routes back here if anything is still pending. The alternative -- several
`interrupt()` calls inside one execution -- looks tidier and is wrong: a resumed node
re-runs from the top, so the earlier interrupts return their already-consumed values
and the recording code runs again on items that were already recorded. Returning
between decisions means every pause/resume cycle commits its own state, and a replayed
node has nothing left to double-record.

What an approval does, and does not
-----------------------------------

It moves the item to :attr:`~costsentinel.domain.governance.ApprovalState.HANDED_OFF`
and writes a record. That is the end of the line: a person has agreed and a person must
now carry it out. There is no execution path in this system, and that is a structural
property rather than an accident of what has been built so far -- this module imports
no provider, no cloud client and nothing that could mutate anything, and four
independent tests assert the property from four directions (ARCHITECTURE.md D118).
"""

from __future__ import annotations

import hashlib

from langgraph.types import interrupt
from pydantic import ValidationError

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.config import Settings
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification, utc_now
from costsentinel.domain.governance import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalState,
    ApproverAssurance,
    AuditEventType,
    Decision,
    DecisionRequest,
    FrozenFigures,
    PendingApproval,
)
from costsentinel.domain.report import ReportFinding
from costsentinel.domain.state import ScanState
from costsentinel.observability.logging import get_logger

#: Recorded as the actor on every audit event this node emits.
ACTOR = "approval_gate"

_log = get_logger("agents.approval_gate")


def pending_for(state: ScanState) -> tuple[PendingApproval, ...]:
    """Every gated recommendation in this sweep that still needs a decision.

    Read from the *report* rather than recomputed, so the queue an operator decides
    from is the queue the client was shown. A figure that differed between the two
    would make an approval mean something slightly different from what it appeared to
    mean, which is the failure :class:`FrozenFigures` exists to prevent.

    A deferred item is still pending. "Not now" is a recorded answer that resolves
    nothing (see :class:`Decision`).
    """
    report = state.report
    if report is None:
        return ()

    by_recommendation = {finding.recommendation_id: finding for finding in report.findings}
    queue: list[PendingApproval] = []

    for classification in state.classifications:
        if not classification.requires_approval:
            continue
        existing = _record_for(state, classification.recommendation_id)
        if existing is not None and existing.decision.resolves:
            continue
        finding = by_recommendation.get(classification.recommendation_id)
        if finding is None:
            # Classified but absent from the report: nothing to show an approver, so
            # nothing to ask them about. Recorded as a processing note upstream.
            continue
        queue.append(
            PendingApproval(
                run_id=state.run_id,
                recommendation_id=finding.recommendation_id,
                client=state.client,
                subscription_id=finding.subscription_id,
                target_resource_id=finding.resource_id,
                target_resource_name=finding.resource_name,
                action=finding.proposed_action,
                action_class=finding.action_class,
                waste_kind=finding.waste_kind.value,
                summary=_summarise(finding),
                figures=figures_for(finding),
                state=(
                    ApprovalState.DEFERRED
                    if existing is not None
                    else ApprovalState.AWAITING_DECISION
                ),
                deferrals=_deferrals(state, finding.recommendation_id),
            )
        )

    return tuple(queue)


def figures_for(finding: ReportFinding) -> FrozenFigures:
    """The figures an approver is shown, copied off the report exactly as rendered."""
    return FrozenFigures(
        monthly_saving_display=finding.monthly_saving.display(),
        annual_saving_display=finding.annual_saving.display(),
        current_monthly_cost_display=finding.current_monthly_cost.display(),
        savings_basis=finding.savings_basis,
    )


def _summarise(finding: ReportFinding) -> str:
    action = finding.proposed_action.value.replace("_", " ")
    return (
        f"{action} on {finding.resource_name} "
        f"({finding.waste_kind.value.replace('_', ' ')}), "
        f"worth {finding.monthly_saving.display()} per month"
    )


def _record_for(state: ScanState, recommendation_id: str) -> ApprovalRecord | None:
    """The most recent decision recorded in this sweep for one recommendation."""
    matching = [r for r in state.approval_records if r.recommendation_id == recommendation_id]
    return matching[-1] if matching else None


def _deferrals(state: ScanState, recommendation_id: str) -> int:
    return sum(
        1
        for record in state.approval_records
        if record.recommendation_id == recommendation_id and record.decision is Decision.DEFERRED
    )


def record_id_for(run_id: str, recommendation_id: str, attempt: int) -> str:
    """A stable id for one decision on one recommendation in one sweep.

    Derived rather than random, so a replayed resume produces the same id and a
    duplicate is recognisable as one. The attempt is included because a deferral can
    legitimately be followed by another decision on the same item.
    """
    digest = hashlib.sha256(f"{run_id}:{recommendation_id}:{attempt}".encode()).hexdigest()
    return f"apr-{digest[:12]}"


def build_record(
    pending: PendingApproval,
    *,
    decision: Decision,
    approver: str,
    reason: str,
    attempt: int = 0,
    approver_assurance: ApproverAssurance = ApproverAssurance.SELF_ASSERTED,
    verified_by: str | None = None,
) -> ApprovalRecord:
    """Turn one decision on one pending item into the record of it.

    The resource, action, class and figures come from ``pending`` rather than from the
    caller. A decision command that supplied them could attach an approval to the wrong
    resource, or to the right resource at the wrong price, and either would be a worse
    failure than a rejected command -- the same reasoning as
    :func:`costsentinel.memory.decisions.record_decision`.

    The provenance's verification follows the approver's assurance. The decision was
    checked against the queue either way, but a record is a claim about *who* decided,
    and calling it verified when the name was only typed would be the one overclaim
    this record exists to prevent (D127).
    """
    verified = approver_assurance is ApproverAssurance.VERIFIED
    return ApprovalRecord(
        record_id=record_id_for(pending.run_id, pending.recommendation_id, attempt),
        run_id=pending.run_id,
        recommendation_id=pending.recommendation_id,
        client=pending.client,
        subscription_id=pending.subscription_id,
        target_resource_id=pending.target_resource_id,
        target_resource_name=pending.target_resource_name,
        action=pending.action,
        action_class=pending.action_class,
        decision=decision,
        state=ApprovalState.for_decision(decision),
        approver=approver,
        approver_assurance=approver_assurance,
        verified_by=verified_by,
        reason=reason,
        figures=pending.figures,
        provenance=Provenance(
            source=ProvenanceSource.OPERATOR,
            retrieved_at=utc_now(),
            reference=f"{ACTOR}:{pending.run_id}",
            verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
        ),
    )


def verified_identity(record: ApprovalRecord) -> bool:
    """Whether the approver on a record is an established identity, not a typed name."""
    return record.approver_assurance is ApproverAssurance.VERIFIED


class DecisionPayloadError(ValueError):
    """A resume value did not describe a decision this gate can record."""


def read_payload(
    payload: object, queue: tuple[PendingApproval, ...]
) -> tuple[PendingApproval, DecisionRequest]:
    """Validate a resume value, then check it against the queue it claims to decide on.

    Two steps, and both matter. The schema refuses a malformed or over-specified
    request; the queue check refuses a well-formed request that names something nobody
    is waiting on, which would otherwise record a decision against nothing.

    Raises:
        DecisionPayloadError: If the value will not validate, or names an item that is
            not awaiting a decision in this sweep.
    """
    try:
        request = DecisionRequest.model_validate(payload)
    except ValidationError as exc:
        allowed = ", ".join(sorted(member.value for member in Decision))
        msg = (
            f"a resume value must describe one decision -- recommendation_id, "
            f"decision ({allowed}), approver and reason -- and nothing else: {exc}"
        )
        raise DecisionPayloadError(msg) from exc

    pending = next(
        (item for item in queue if item.recommendation_id == request.recommendation_id), None
    )
    if pending is None:
        waiting = ", ".join(item.recommendation_id for item in queue) or "nothing"
        msg = (
            f"{request.recommendation_id!r} is not awaiting a decision in this sweep; "
            f"waiting on: {waiting}"
        )
        raise DecisionPayloadError(msg)

    return pending, request


def make_approval_gate(*, settings: Settings) -> Node:
    """Build the node that halts a sweep until a person decides.

    Takes no provider, no cloud client and no execution handler, because there is
    nothing for it to call. The signature is part of the no-mutation proof: a reviewer
    can see from one line that this node has nothing to act with.
    """
    _ = settings

    def approval_gate(state: ScanState) -> NodeUpdate:
        """Pause for one decision, record it, and hand back to the supervisor."""
        queue = pending_for(state)
        if not queue:
            return {
                "audit": with_audit(
                    state,
                    [
                        audit(
                            state,
                            actor=ACTOR,
                            event_type=AuditEventType.APPROVAL_REQUESTED,
                            subject=state.client,
                            detail={"pending": "0", "reason": "nothing requires a decision"},
                        )
                    ],
                )
            }

        _log.info(
            "sweep paused for approval",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "pending": len(queue),
                "resume_key": state.run_id,
            },
        )

        # The pause. Everything after this line runs only once a decision arrives,
        # possibly in a different process, days later.
        payload = interrupt(
            {
                "run_id": state.run_id,
                "client": state.client,
                "pending": [item.model_dump(mode="json") for item in queue],
                "resume_with": (
                    "a mapping of recommendation_id, decision "
                    "(approved|rejected|deferred), approver and reason"
                ),
            }
        )

        pending, request = read_payload(payload, queue)
        decision, approver, reason = request.decision, request.approver, request.reason
        record = build_record(
            pending,
            decision=decision,
            approver=approver,
            reason=reason,
            attempt=pending.deferrals,
            approver_assurance=request.approver_assurance,
            verified_by=request.verified_by,
        )
        # Every event naming the approver also says how much is known about them, so
        # an audit trail read on its own cannot overclaim who decided (D127).
        identity = {
            "approver_assurance": record.approver_assurance.value,
            "approver_verified": str(verified_identity(record)).lower(),
            "verified_by": record.verified_by or "nothing -- the name was typed by the approver",
        }

        events = [
            audit(
                state,
                actor=approver,
                event_type=AuditEventType.APPROVAL_RECORDED,
                subject=record.recommendation_id,
                detail={
                    "decision": decision.value,
                    "state": record.state.value,
                    "reason": reason,
                    "action": record.action.value,
                    "action_class": record.action_class.value,
                    "resource_id": record.target_resource_id,
                    "monthly_saving": record.figures.monthly_saving_display,
                    "record_id": record.record_id,
                    **identity,
                },
            )
        ]
        if record.authorises_a_person:
            events.append(
                audit(
                    state,
                    actor=ACTOR,
                    event_type=AuditEventType.ACTION_HANDED_OFF,
                    subject=record.recommendation_id,
                    detail={
                        "approved_by": approver,
                        **identity,
                        "action": record.action.value,
                        "resource_id": record.target_resource_id,
                        # Stated in the audit trail, not only in a docstring: this is
                        # as far as an approval goes.
                        "performed_by": "a human operator, by hand",
                        "executed_by_costsentinel": "false -- no execution path exists",
                    },
                    offset=1,
                )
            )

        _log.info(
            "approval decision recorded",
            extra={
                "run_id": state.run_id,
                "recommendation_id": record.recommendation_id,
                "decision": decision.value,
                "state": record.state.value,
                "approver": approver,
                "approver_assurance": record.approver_assurance.value,
            },
        )

        update: NodeUpdate = {
            "approval_records": (*state.approval_records, record),
            "audit": with_audit(state, events),
        }
        if decision.resolves:
            update["decisions"] = (
                *state.decisions,
                ApprovalDecision(
                    recommendation_id=record.recommendation_id,
                    decision=decision,
                    approver=approver,
                    approver_assurance=record.approver_assurance,
                    verified_by=record.verified_by,
                    decided_at=record.decided_at,
                    note=reason,
                ),
            )
        return update

    return approval_gate
