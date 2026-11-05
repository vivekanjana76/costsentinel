# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false
# Reads sweep state through a compiled LangGraph graph, whose generics are only
# partially typed -- the same per-file allowance the graph package makes, for the same
# reason, rather than a relaxation across src.
"""The one decision path: every surface that decides calls :func:`decide`.

Phase 11 made the dashboard decide through the same three calls as the CLI and asserted
it in both directions (D124). Phase 10b adds a third surface, Slack, and three things
every surface needs and none should implement for itself: authorisation, idempotency
on a repeated interaction, and an answer for an item that is already decided. So the
three calls move here, and the CLI, the dashboard and the Slack webhook each call this
function and nothing below it (D133). A test asserts that no other module calls
``resume``.

What this function does, in order
---------------------------------

1. **A repeated interaction returns its stored outcome.** Slack redelivers callbacks;
   the second delivery of a click is the same click, not a second decision.
2. **Find the item.** If it is waiting, that is the item. If nothing waits on it but a
   sweep resolved it already, that resolution is the answer.
3. **Authorise** with :func:`~costsentinel.approvals.identity.authorise`, against the
   client the item belongs to. A refusal is written to the ledger and nothing else
   happens: the sweep stays exactly where it was.
4. **Decide**: build a :class:`DecisionRequest` and :func:`resume` the sweep. The gate
   validates the request against the queue, writes the record and emits the audit
   events, exactly as it did before this module existed.

What it cannot do
-----------------

Act. A decision ends at the gate, and an approval ends at a hand-off to a person. This
module imports no provider for acting, no cloud client and nothing executable.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from costsentinel.approvals.identity import (
    ApproverIdentity,
    AuthorisationPolicy,
    Surface,
    authorise,
)
from costsentinel.approvals.ledger import DecisionLedger, DecisionOutcome, OutcomeKind
from costsentinel.config import Settings
from costsentinel.domain.common import utc_now
from costsentinel.domain.governance import (
    ApprovalRecord,
    AuditEvent,
    AuditEventType,
    Decision,
    DecisionRequest,
)
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import (
    NothingPendingError,
    approvals_graph,
    find_pending,
    paused_sweep,
    paused_sweeps,
    resume,
    thread_ids,
)
from costsentinel.graph.checkpointer import checkpointer_for
from costsentinel.observability.logging import get_logger

_log = get_logger("approvals.service")

#: One decision at a time in this process. Two deliveries of the same Slack click can
#: arrive on two worker threads; without this, both could find the item still pending
#: before either resumed it. The ledger's unique key is the backstop across processes.
_DECIDING = threading.Lock()


class MissingReasonError(ValueError):
    """An authorised approver gave no reason. Nothing is recorded; they can try again.

    Raised after authorisation, so an unauthorised click with an empty reason box is
    still recorded as the refusal it is, rather than turned away as merely incomplete.
    """


def _resolved_record(
    graph: CompiledStateGraph[ScanState, None, ScanState, ScanState],
    saver: BaseCheckpointSaver[str],
    recommendation_id: str,
) -> ApprovalRecord | None:
    """The most recent record that *resolved* this item, in the newest sweep that has one.

    A deferral resolves nothing, so it is not an answer to "is this already decided?".
    """
    for run_id in thread_ids(saver):
        snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
        if not snapshot.values:
            continue
        state = ScanState.model_validate(snapshot.values)
        for record in reversed(state.approval_records):
            if record.recommendation_id == recommendation_id and record.decision.resolves:
                return record
    return None


def _event(
    *,
    kind: AuditEventType,
    run_id: str,
    identity: ApproverIdentity,
    recommendation_id: str,
    surface: Surface,
    detail: dict[str, str],
    interaction_key: str | None,
) -> AuditEvent:
    occurred = utc_now()
    seed = interaction_key or f"{surface}:{recommendation_id}:{occurred.isoformat()}"
    return AuditEvent(
        event_id=f"ledger-{hashlib.sha256(seed.encode()).hexdigest()[:16]}",
        run_id=run_id,
        event_type=kind,
        actor=identity.name,
        subject=recommendation_id,
        detail={
            "surface": surface.value,
            "approver_assurance": identity.assurance.value,
            "principal": identity.principal or "none -- the name was typed by the approver",
            "verified_by": identity.verified_by or "nothing",
            **detail,
        },
        occurred_at=occurred,
    )


def decide(
    settings: Settings,
    *,
    recommendation_id: str,
    decision: Decision,
    reason: str,
    identity: ApproverIdentity,
    surface: Surface,
    local: bool,
    interaction_key: str | None = None,
    policy: AuthorisationPolicy | None = None,
    ledger: DecisionLedger | None = None,
) -> DecisionOutcome:
    """Decide one gated recommendation, or say why not, through the one path.

    Args:
        settings: Where the checkpoint and the ledger live.
        recommendation_id: The item, as the report named it. The only fact a request
            supplies; everything else is copied from the pending item (D120).
        decision: Approve, reject or defer.
        reason: Why. Mandatory, as everywhere.
        identity: Who, as the surface's authenticator established it.
        surface: Where the request came from, for the record.
        local: Whether it arrived on this machine. Only then may a self-asserted
            identity decide.
        interaction_key: A key that identifies this request across redeliveries, when
            the surface has one. The same key always returns the same outcome.
        policy: The allowlist. Defaults to the configured one.
        ledger: Where outcomes are kept. Defaults to the configured one.

    Raises:
        NothingPendingError: If nothing waits on this item and no sweep resolved it.
            Nothing is recorded, because there is nothing it could be recorded against.
        MissingReasonError: If an authorised approver gave no reason. Nothing is
            recorded.
    """
    book = ledger or DecisionLedger(settings.decision_ledger_path)
    allowlist = policy or AuthorisationPolicy.from_settings(settings)

    with _DECIDING:
        if interaction_key is not None:
            stored = book.get(interaction_key)
            if stored is not None:
                _log.info(
                    "repeated interaction answered from the ledger",
                    extra={"interaction_key": interaction_key, "kind": stored.kind.value},
                )
                return stored.model_copy(update={"replayed": True})

        outcome = _decide(
            settings,
            recommendation_id=recommendation_id,
            decision=decision,
            reason=reason,
            identity=identity,
            surface=surface,
            local=local,
            interaction_key=interaction_key,
            policy=allowlist,
        )
        book.append(outcome, interaction_key=interaction_key)

    _log.info(
        "decision request handled",
        extra={
            "recommendation_id": recommendation_id,
            "kind": outcome.kind.value,
            "surface": surface.value,
            "approver": identity.name,
            "approver_assurance": identity.assurance.value,
            "run_id": outcome.run_id,
        },
    )
    return outcome


def _decide(
    settings: Settings,
    *,
    recommendation_id: str,
    decision: Decision,
    reason: str,
    identity: ApproverIdentity,
    surface: Surface,
    local: bool,
    interaction_key: str | None,
    policy: AuthorisationPolicy,
) -> DecisionOutcome:
    common: dict[str, Any] = {
        "identity": identity,
        "recommendation_id": recommendation_id,
        "surface": surface,
        "interaction_key": interaction_key,
    }
    with checkpointer_for(settings) as saver:
        graph = approvals_graph(settings, saver)
        try:
            sweep, pending = find_pending(paused_sweeps(graph, saver), recommendation_id)
        except NothingPendingError:
            prior = _resolved_record(graph, saver, recommendation_id)
            if prior is None:
                raise
            sweep, pending = None, None
            client, run_id = prior.client, prior.run_id
        else:
            prior = None
            client, run_id = pending.client, sweep.run_id

        verdict = authorise(identity, client=client, local=local, policy=policy)
        if not verdict.allowed:
            return DecisionOutcome(
                kind=OutcomeKind.REFUSED,
                surface=surface,
                recommendation_id=recommendation_id,
                client=client,
                run_id=run_id,
                statement=f"{identity.name} may not decide {recommendation_id}: {verdict.reason}.",
                event=_event(
                    kind=AuditEventType.APPROVAL_REFUSED,
                    run_id=run_id,
                    detail={
                        "requested": decision.value,
                        "reason": verdict.reason,
                        "decided": "false -- a refusal records no decision",
                    },
                    **common,
                ),
            )

        if prior is not None:
            return DecisionOutcome(
                kind=OutcomeKind.ALREADY_DECIDED,
                surface=surface,
                recommendation_id=recommendation_id,
                client=client,
                run_id=run_id,
                statement=f"Already decided; nothing changed. {prior.statement()}",
                record=prior,
                event=_event(
                    kind=AuditEventType.DECISION_ALREADY_RECORDED,
                    run_id=run_id,
                    detail={"requested": decision.value, "existing_record": prior.record_id},
                    **common,
                ),
            )

        if not reason.strip():
            msg = f"a reason is required to decide {recommendation_id}; nothing was recorded"
            raise MissingReasonError(msg)

        assert sweep is not None and pending is not None  # noqa: S101 -- narrowed above
        state = resume(
            graph,
            sweep.run_id,
            DecisionRequest(
                recommendation_id=pending.recommendation_id,
                decision=decision,
                approver=identity.name,
                reason=reason,
                approver_assurance=identity.assurance,
                verified_by=identity.verified_by,
            ),
        )
        remaining = paused_sweep(graph, sweep.run_id)

    record = next(
        (
            one
            for one in reversed(state.approval_records)
            if one.recommendation_id == recommendation_id
        ),
        None,
    )
    if record is None:  # pragma: no cover -- the gate always records
        msg = f"the decision on {recommendation_id!r} was not recorded"
        raise RuntimeError(msg)

    return DecisionOutcome(
        kind=OutcomeKind.DECIDED,
        surface=surface,
        recommendation_id=recommendation_id,
        client=client,
        run_id=run_id,
        statement=record.statement(),
        record=record,
        remaining=len(remaining.pending) if remaining else 0,
        event=_event(
            kind=AuditEventType.APPROVAL_RECORDED,
            run_id=run_id,
            detail={
                "decision": decision.value,
                "record_id": record.record_id,
                "authorised_by": verdict.matched or verdict.reason,
            },
            **common,
        ),
    )
