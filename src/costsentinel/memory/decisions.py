"""Recording what a human decided about a recommendation.

This is deliberately not the approval gate. The gate (Phase 8) halts a run until a
decision exists and then authorises an action. This records an outcome *after* a
report has been read, so the next sweep knows better -- and it exists now, several
phases early, because otherwise there would be no way to demonstrate that the
learning loop closes.

The two are different in a way worth being precise about. An
:class:`~costsentinel.domain.governance.ApprovalDecision` authorises an action and is
binary: approved or rejected. A :class:`~costsentinel.domain.memory.Outcome` is a
learning signal and has three values, because "not now" is different information from
"no". A deferral means the client has considered the advice and intends to act later;
re-pitching it as new next month is a small insult, and forgetting it entirely is a
lost saving.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from costsentinel.domain.governance import AuditEvent, AuditEventType
from costsentinel.domain.memory import EpisodicEntry, Outcome, RecommendationOutcome
from costsentinel.memory.base import MemoryStore, MemoryStoreError
from costsentinel.memory.episodic import build_outcome
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.decisions")


class UnknownRecommendationError(MemoryStoreError):
    """No recommendation with that id has ever been proposed.

    Refused rather than recorded against nothing. An outcome whose recommendation
    does not exist would sit in the history matching nothing forever, and the
    operator who mistyped the id would believe they had recorded a decision.
    """


def record_decision(
    store: MemoryStore,
    *,
    recommendation_id: str,
    outcome: Outcome,
    reason: str,
    decided_by: str,
    decided_at: datetime | None = None,
) -> tuple[RecommendationOutcome, AuditEvent]:
    """Record one human decision and the audit event for it.

    The resource, action and waste kind are copied from the recommendation as it was
    proposed, not supplied by the caller. A typo in those would attach the decision
    to the wrong resource and suppress the wrong advice next month, which is a worse
    failure than a rejected command.

    Returns:
        The stored outcome and its audit event.

    Raises:
        UnknownRecommendationError: If the id was never proposed.
        OutcomeAlreadyRecordedError: If a decision already exists for it.
        MemoryStoreError: If memory is switched off, or the write failed.
    """
    if not store.is_enabled:
        msg = (
            "memory is switched off (MEMORY_BACKEND=off), so there is nowhere to "
            "record this decision. Set MEMORY_BACKEND=sqlite (the default) to keep a "
            "local history."
        )
        raise MemoryStoreError(msg)

    entry = store.entry_for(recommendation_id)
    if entry is None:
        msg = (
            f"no recommendation {recommendation_id!r} has been proposed, so there is "
            f"nothing to decide about. Run a scan first, then use the "
            f"recommendation id from the report."
        )
        raise UnknownRecommendationError(msg)

    built = build_outcome(
        entry,
        outcome=outcome,
        reason=reason,
        decided_by=decided_by,
        decided_at=decided_at,
    )
    if not store.record_outcome(built):
        msg = (
            f"the decision on {recommendation_id} could not be written to "
            f"{store.name}. It has not been recorded."
        )
        raise MemoryStoreError(msg)

    event = AuditEvent(
        event_id=f"{built.outcome_id}-audit",
        run_id=entry.run_id,
        event_type=AuditEventType.APPROVAL_RECORDED,
        actor=decided_by,
        subject=recommendation_id,
        detail={
            "outcome": outcome.value,
            "reason": reason,
            "client": entry.client,
            "resource_id": entry.target_resource_id,
            "action": entry.action.value,
            "monthly_saving": entry.monthly_saving_display,
            "store": store.name,
        },
        occurred_at=built.decided_at,
    )
    _log.info(
        "decision recorded",
        extra={
            "recommendation_id": recommendation_id,
            "client": entry.client,
            "outcome": outcome.value,
            "decided_by": decided_by,
            "store": store.name,
        },
    )
    return built, event


def decided_entries(
    store: MemoryStore, client: str
) -> tuple[tuple[EpisodicEntry, RecommendationOutcome], ...]:
    """Every recorded decision for one client, newest first, with what it was about.

    Pairs the outcome with the recommendation it applies to, because an outcome on
    its own tells an operator a decision exists and not what it was about.
    """
    outcomes = store.outcomes_for(client)
    paired: list[tuple[EpisodicEntry, RecommendationOutcome]] = []
    for outcome in outcomes:
        entry = store.entry_for(outcome.recommendation_id)
        if entry is not None:
            paired.append((entry, outcome))
    return tuple(paired)


def pending_entries(
    store: MemoryStore, client: str, *, limit: int = 500
) -> tuple[EpisodicEntry, ...]:
    """Recommendations this client has been shown and not yet decided on.

    What an operator needs in order to use ``decide``: a list of ids that are
    actually decidable, rather than the whole history.
    """
    decided = {outcome.recommendation_id for outcome in store.outcomes_for(client)}
    return tuple(
        entry
        for entry in store.entries_for(client, limit=limit)
        if entry.recommendation_id not in decided
    )


def clients_with_history(store: MemoryStore, clients: Sequence[str]) -> tuple[str, ...]:
    """Which of these clients have anything recorded.

    Takes the candidate list rather than discovering it, because the store has no
    method that enumerates clients -- and it should not: a query that returns every
    client is one filter away from returning every client's history.
    """
    return tuple(client for client in clients if store.entries_for(client, limit=1))
