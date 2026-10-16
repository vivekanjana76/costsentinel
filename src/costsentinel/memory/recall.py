"""Matching a recommendation against what the client already said about it.

This is the module the whole phase rests on, and it is deliberately small and
deterministic. Two rules govern it.

**Memory may reorder; it may never recompute.** Nothing here touches a
:class:`~costsentinel.domain.common.MoneyAmount`. The penalty is applied to the
*ranking composite* -- a dimensionless 0-1 score that exists only to order findings
-- and then ranks are reassigned. Every saving, total and projection in the resulting
report is byte-identical to the one a memory-less run would produce. A test asserts
exactly that, because it is the kind of property that is easy to claim and easy to
break.

**An exact match and a pattern match are different evidence.** Declining to delete
*this* disk is strong evidence against offering to delete *this* disk again. It is
weak evidence against offering to delete a different disk, because the reason may
have been specific to the resource. So both are surfaced to the client, and only the
exact match is demoted hard. Treating them the same would mean one "no" quietly
suppressing a whole category of genuine waste, which is how a cost tool starts
under-reporting.

Nothing is ever *removed*. A client declining advice does not make the waste
disappear, and a report that silently dropped it would misstate the estate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.analysis import RankingScore
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.memory import (
    MemoryRecall,
    Outcome,
    RecallKind,
    RecommendationOutcome,
)
from costsentinel.domain.recommendations import Recommendation
from costsentinel.domain.signals import WasteSignal

#: Share of the ranking composite withheld when the client declined or deferred
#: *this exact* advice about *this exact* resource. Large, because re-pitching
#: something already refused is the specific failure memory exists to prevent --
#: but not 1.0, so the finding keeps a stable relative position among other
#: previously-declined items rather than collapsing into an arbitrary tie.
PENALTY_EXACT = Decimal("0.70")

#: Share withheld when the same *kind* of advice was declined elsewhere in this
#: client's estate. Small: the reason may have been specific to the other resource,
#: and demoting hard on this evidence would let one "no" bury a category.
PENALTY_PATTERN = Decimal("0.20")

#: Applied to a previously *accepted* pattern. Zero -- acceptance is recorded and
#: surfaced, and it does not move the order.
#:
#: The temptation is to promote it: the client liked this sort of thing, so show more.
#: Resisted, because ranking is supposed to reflect what is worth doing, and a client
#: having agreed to something last month is not evidence that a different resource is
#: more wasteful this month. Promoting on it would make the order reflect
#: agreeableness rather than value.
PENALTY_ACCEPTED = Decimal("0")

_PLACES = Decimal("0.000001")


def penalty_for(kind: RecallKind, outcome: Outcome) -> Decimal:
    """How much ranking weight a prior outcome withholds."""
    if not outcome.discourages_repitch:
        return PENALTY_ACCEPTED
    return PENALTY_EXACT if kind is RecallKind.EXACT else PENALTY_PATTERN


def annotate(outcome: RecommendationOutcome, kind: RecallKind) -> str:
    """The line the client reads.

    Names the date and the reason, because "deprioritised due to prior feedback"
    tells a client nothing they can act on, and the point of recording a reason is
    that it can be quoted back.
    """
    when = outcome.decided_at.date().isoformat()
    if kind is RecallKind.EXACT:
        if outcome.outcome is Outcome.ACCEPTED:
            return f"Previously accepted on {when}: {outcome.reason}"
        verb = "declined" if outcome.outcome is Outcome.REJECTED else "deferred"
        return f"Previously {verb} on {when}, reason: {outcome.reason}"
    if outcome.outcome is Outcome.ACCEPTED:
        return (
            f"A similar recommendation for {outcome.target_resource_name or 'another resource'} "
            f"was accepted on {when}."
        )
    verb = "declined" if outcome.outcome is Outcome.REJECTED else "deferred"
    return (
        f"A similar recommendation for "
        f"{outcome.target_resource_name or 'another resource'} was {verb} on {when}, "
        f"reason: {outcome.reason}"
    )


def _provenance(outcome: RecommendationOutcome, kind: RecallKind) -> Provenance:
    return Provenance(
        source=ProvenanceSource.OPERATOR,
        retrieved_at=datetime.now(UTC),
        reference=(
            f"{kind.value} match against outcome {outcome.outcome_id}: "
            f"{outcome.recommendation_id} was {outcome.outcome.value} on "
            f"{outcome.decided_at.date().isoformat()}"
        ),
        verification=Verification.VERIFIED,
    )


def match(
    recommendation: Recommendation,
    signal: WasteSignal | None,
    outcomes: Sequence[RecommendationOutcome],
) -> tuple[RecallKind, RecommendationOutcome] | None:
    """The most relevant prior outcome for one recommendation, or ``None``.

    An exact match always wins over a pattern match, and among equals the most
    recent decision wins -- what the client said last is what they think now.
    """
    exact: list[RecommendationOutcome] = []
    pattern: list[RecommendationOutcome] = []
    for outcome in outcomes:
        if outcome.client != recommendation.client:
            # Cannot happen through the store, whose reads are client-scoped. Checked
            # anyway: a cross-client annotation would be a confidentiality failure,
            # not a ranking bug.
            continue
        if (
            outcome.target_resource_id == recommendation.target_resource_id
            and outcome.action is recommendation.action
        ):
            exact.append(outcome)
        elif (
            signal is not None
            and outcome.waste_kind is signal.kind
            and outcome.action is recommendation.action
        ):
            pattern.append(outcome)

    if exact:
        return RecallKind.EXACT, max(exact, key=lambda o: o.decided_at)
    if pattern:
        return RecallKind.PATTERN, max(pattern, key=lambda o: o.decided_at)
    return None


def adjusted_composite(base: Decimal, penalty: Decimal) -> Decimal:
    """The base ranking composite with a share withheld.

    Multiplicative rather than subtractive so the result stays inside 0-1 whatever
    the base was, and so the penalty means the same thing for a high-scoring finding
    as for a low one.
    """
    return (base * (Decimal(1) - penalty)).quantize(_PLACES, rounding=ROUND_HALF_UP)


def consult(
    *,
    recommendations: Sequence[Recommendation],
    signals: Sequence[WasteSignal],
    ranking: Sequence[RankingScore],
    outcomes: Sequence[RecommendationOutcome],
) -> tuple[tuple[Recommendation, ...], tuple[MemoryRecall, ...]]:
    """Re-rank and annotate a set of recommendations against prior outcomes.

    Returns the recommendations in their new rank order with ``rank`` reassigned, and
    one :class:`MemoryRecall` per recommendation that matched something. The set of
    recommendations is unchanged -- same members, same savings, same everything
    except the integer rank.

    With no outcomes the returned order and ranks are identical to the input, which
    is what makes the memory-less path and the empty-memory path the same code.
    """
    if not recommendations:
        return (), ()

    base_rank = {rec.recommendation_id: rec.rank for rec in recommendations}
    composite = {score.recommendation_id: score.composite for score in ranking}
    signal_by_id = {signal.signal_id: signal for signal in signals}

    matches: dict[str, tuple[RecallKind, RecommendationOutcome]] = {}
    adjusted: dict[str, Decimal] = {}
    for recommendation in recommendations:
        found = match(
            recommendation,
            signal_by_id.get(recommendation.signal_id),
            outcomes,
        )
        base = composite.get(recommendation.recommendation_id, Decimal(0))
        if found is None:
            adjusted[recommendation.recommendation_id] = base
            continue
        kind, outcome = found
        matches[recommendation.recommendation_id] = (kind, outcome)
        adjusted[recommendation.recommendation_id] = adjusted_composite(
            base, penalty_for(kind, outcome.outcome)
        )

    # Ties break on the recommendation id, exactly as the base ranking does, so the
    # order is total and reproducible.
    ordered = sorted(
        recommendations,
        key=lambda rec: (-adjusted[rec.recommendation_id], rec.recommendation_id),
    )
    new_rank = {rec.recommendation_id: position for position, rec in enumerate(ordered, start=1)}

    reranked = tuple(
        rec.model_copy(update={"rank": new_rank[rec.recommendation_id]}) for rec in ordered
    )
    recalls = tuple(
        _recall(
            recommendation_id=recommendation_id,
            kind=kind,
            outcome=outcome,
            base_rank=base_rank[recommendation_id],
            adjusted_rank=new_rank[recommendation_id],
        )
        for recommendation_id, (kind, outcome) in sorted(matches.items())
    )
    return reranked, recalls


def _recall(
    *,
    recommendation_id: str,
    kind: RecallKind,
    outcome: RecommendationOutcome,
    base_rank: int,
    adjusted_rank: int,
) -> MemoryRecall:
    return MemoryRecall(
        recommendation_id=recommendation_id,
        kind=kind,
        prior_outcome=outcome.outcome,
        prior_recommendation_id=outcome.recommendation_id,
        prior_reason=outcome.reason,
        decided_at=outcome.decided_at,
        decided_by=outcome.decided_by,
        annotation=annotate(outcome, kind),
        penalty=penalty_for(kind, outcome.outcome),
        base_rank=base_rank,
        adjusted_rank=adjusted_rank,
        provenance=_provenance(outcome, kind),
    )


def recall_summary(recalls: Sequence[MemoryRecall]) -> str:
    """The line the report carries, or an empty string when nothing matched.

    Counted by outcome rather than lumped together, because "two declined, one
    deferred" is a different conversation from "three declined".
    """
    if not recalls:
        return ""
    declined = sum(1 for r in recalls if r.prior_outcome is Outcome.REJECTED)
    deferred = sum(1 for r in recalls if r.prior_outcome is Outcome.DEFERRED)
    accepted = sum(1 for r in recalls if r.prior_outcome is Outcome.ACCEPTED)
    parts = [
        f"{count} previously {label}"
        for count, label in ((declined, "declined"), (deferred, "deferred"), (accepted, "accepted"))
        if count
    ]
    joined = ", ".join(parts)
    return (
        f"{len(recalls)} of these findings {'has' if len(recalls) == 1 else 'have'} "
        f"been seen before: {joined}. They are annotated and ranked lower rather "
        f"than re-pitched as new."
    )


def recalls_by_recommendation(
    recalls: Sequence[MemoryRecall],
) -> Mapping[str, MemoryRecall]:
    """Index recalls by the recommendation they apply to."""
    return {recall.recommendation_id: recall for recall in recalls}
