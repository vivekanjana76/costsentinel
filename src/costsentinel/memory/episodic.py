"""Turning a finished run into a history, and reading one back.

The write happens once, after the report is composed, from the final state. A
half-written history is worse than none: the next sweep reads it as fact, and a run
that died between its fourth and fifth recommendation would leave a client looking
permanently as though they had only ever been shown four.

Nothing here holds a :class:`~costsentinel.domain.common.MoneyAmount`. Savings travel
into history as the pre-rendered strings the client saw at the time, so a stale figure
cannot be summed into a later total. That is the invariant the whole phase rests on:
memory may reorder and annotate, never recompute.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.memory import (
    EpisodicEntry,
    EpisodicRecord,
    Outcome,
    RecommendationOutcome,
)
from costsentinel.domain.recommendations import Recommendation
from costsentinel.domain.state import ScanState
from costsentinel.memory.base import MemoryStore
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.episodic")


def _provenance(run_id: str, detail: str) -> Provenance:
    """Provenance for a history record.

    ``VERIFIED`` because the fact being recorded is "CostSentinel showed this to this
    client in this run", which CostSentinel is the authority on. It says nothing
    about whether the underlying cost figure was verified -- that provenance travelled
    with the figure and is not re-asserted here.
    """
    return Provenance(
        source=ProvenanceSource.CALCULATION,
        retrieved_at=datetime.now(UTC),
        reference=f"episodic record for run {run_id}: {detail}",
        verification=Verification.VERIFIED,
    )


def entry_for(state: ScanState, recommendation: Recommendation) -> EpisodicEntry | None:
    """One history entry from a recommendation and its classification.

    Returns ``None`` when the recommendation was never classified. An unclassified
    recommendation is a bug upstream rather than something to record with a guessed
    action class -- and a history entry claiming the wrong class would later suppress
    or promote advice on a false premise.
    """
    signal = state.signal(recommendation.signal_id)
    if signal is None:
        return None
    classification = state.classification_for(recommendation.recommendation_id)
    if classification is None:
        return None

    return EpisodicEntry(
        recommendation_id=recommendation.recommendation_id,
        signal_id=recommendation.signal_id,
        run_id=state.run_id,
        client=recommendation.client,
        subscription_id=recommendation.subscription_id,
        target_resource_id=recommendation.target_resource_id,
        target_resource_name=recommendation.target_resource_name,
        waste_kind=signal.kind,
        action=recommendation.action,
        action_class=classification.action_class,
        requires_approval=classification.requires_approval,
        policy_reference=classification.policy_reference,
        monthly_saving_display=recommendation.savings.monthly.display(),
        rank=recommendation.rank,
        provenance=_provenance(state.run_id, f"recommendation {recommendation.recommendation_id}"),
    )


def build_record(state: ScanState) -> EpisodicRecord:
    """One run's history, from its final state."""
    entries = tuple(
        entry
        for entry in (entry_for(state, rec) for rec in state.recommendations)
        if entry is not None
    )
    subscriptions = (
        tuple(sub.subscription_id for sub in state.estate.subscriptions)
        if state.estate is not None
        else ()
    )
    return EpisodicRecord(
        run_id=state.run_id,
        client=state.client,
        subscriptions=subscriptions,
        resources_examined=len(state.estate.resources) if state.estate is not None else 0,
        entries=entries,
        provenance=_provenance(state.run_id, f"{len(entries)} recommendation(s)"),
    )


def persist(store: MemoryStore, state: ScanState) -> bool:
    """Write one run's history, returning whether it was stored.

    Never raises. A memory write that failed should cost a client a slightly worse
    ranking next month, not this month's report -- so the failure is logged by the
    store, reported here as ``False``, and the sweep continues.
    """
    if not store.is_enabled:
        return False
    record = build_record(state)
    stored = store.record_run(record)
    _log.info(
        "episodic history written" if stored else "episodic history not written",
        extra={
            "run_id": state.run_id,
            "client": state.client,
            "entries": record.entry_count,
            "store": store.name,
        },
    )
    return stored


def outcome_id_for(recommendation_id: str, decided_at: datetime) -> str:
    """A stable id for one outcome.

    Derived from the recommendation and the decision time rather than random, so
    re-recording the same decision at the same instant is idempotent while a genuine
    second decision gets its own id.
    """
    digest = hashlib.blake2b(
        f"{recommendation_id}:{decided_at.isoformat()}".encode(), digest_size=8
    ).hexdigest()
    return f"outcome-{digest}"


def build_outcome(
    entry: EpisodicEntry,
    *,
    outcome: Outcome,
    reason: str,
    decided_by: str,
    decided_at: datetime | None = None,
) -> RecommendationOutcome:
    """An outcome record for a recommendation that was actually proposed.

    Takes an :class:`EpisodicEntry` rather than loose identifiers on purpose: an
    outcome can only be recorded against something CostSentinel proposed, and the
    resource, action and waste kind are copied from what was shown rather than
    supplied by whoever is recording the decision. Otherwise a typo could attach a
    rejection to the wrong resource, and the next sweep would suppress the wrong
    advice.
    """
    when = decided_at or datetime.now(UTC)
    return RecommendationOutcome(
        outcome_id=outcome_id_for(entry.recommendation_id, when),
        recommendation_id=entry.recommendation_id,
        run_id=entry.run_id,
        client=entry.client,
        subscription_id=entry.subscription_id,
        target_resource_id=entry.target_resource_id,
        target_resource_name=entry.target_resource_name,
        waste_kind=entry.waste_kind,
        action=entry.action,
        outcome=outcome,
        reason=reason,
        decided_by=decided_by,
        decided_at=when,
        monthly_saving_display=entry.monthly_saving_display,
        provenance=Provenance(
            source=ProvenanceSource.OPERATOR,
            retrieved_at=when,
            reference=(
                f"{outcome.value} by {decided_by} against recommendation "
                f"{entry.recommendation_id} from run {entry.run_id}"
            ),
            verification=Verification.VERIFIED,
        ),
    )


def latest_outcomes(
    outcomes: Sequence[RecommendationOutcome],
) -> tuple[RecommendationOutcome, ...]:
    """One outcome per recommendation, keeping the most recent.

    The store refuses to overwrite an outcome, so in practice there is one each --
    but a superseded decision may be recorded deliberately later, and a consult step
    must act on what the client said most recently rather than first.
    """
    newest: dict[str, RecommendationOutcome] = {}
    for outcome in outcomes:
        existing = newest.get(outcome.recommendation_id)
        if existing is None or outcome.decided_at > existing.decided_at:
            newest[outcome.recommendation_id] = outcome
    return tuple(
        sorted(newest.values(), key=lambda o: (o.decided_at, o.recommendation_id), reverse=True)
    )
