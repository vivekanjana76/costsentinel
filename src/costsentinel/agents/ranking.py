"""Composite ranking: savings, safety and confidence.

Phase 1 ranked by savings alone, which quietly told a client that the most valuable
thing to do is the biggest number. It is not. A 25-dollar saving that is certain and
reversible is often a better first move than a 400-dollar saving that is risky and
inferred from a thin observation window.

So the ordering is a weighted composite of three normalised components, computed
here with no model involvement. The weights are explicit policy constants, stated
below and reported on every finding, so a client can see the trade-off being made
rather than having to trust an opaque ordering.

**An unknown saving ranks low until quantified.** A finding with a quantified saving
outranks one without, whatever their composites say. This is a *tier* applied in
:func:`ordering_key` rather than a term in the weighted sum, and the difference
matters: expressed as a weight it would hold only for the current weights and would
break silently the first time somebody retuned them. Expressed as a tier it cannot be
broken by a weight at all (ARCHITECTURE.md D104).

The reason is a product one. A FinOps tool must not tell a client that the thing it
cannot cost is their biggest opportunity -- that is a claim about money made without a
number, which is the same failure ``MoneyAmount`` refusing to hold an unknown as zero
exists to prevent, arriving by a different route.

:meth:`RankingScore.order_key` is the *only* definition of the order, and it lives on
the domain type rather than here with the weights. The planner uses it and so does the
memory consult step, so a penalty applied to a composite cannot lift an unquantified
finding above a quantified one. One definition rather than two that drift -- and in
``domain`` rather than ``agents`` because ``agents`` already depends on ``memory``, so
the reverse import would close a cycle.

The model's only contribution to ranking is the *rationale* attached afterwards. A
hostile model can change the wording and cannot change the rank -- which is a
property the tests assert directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.analysis import TIER_UNQUANTIFIED, RankingScore
from costsentinel.domain.common import Provenance
from costsentinel.domain.recommendations import ActionClass, Recommendation

#: Weight on the saving, as a share of the largest saving in the run.
WEIGHT_SAVINGS = Decimal("0.60")

#: Weight on safety. Sized so that a comparably-valuable safe action outranks a
#: risky one: with these weights, an `allow`-class finding at or above roughly 55%
#: of the largest saving beats a `block`-class finding holding the largest saving.
#: It does *not* guarantee a blocked action can never lead -- if the single biggest
#: opportunity in an estate is something CostSentinel will not touch, a client
#: should still hear about it first.
WEIGHT_RISK = Decimal("0.25")

#: Weight on the detector's confidence.
WEIGHT_CONFIDENCE = Decimal("0.15")

#: How safe each class is, where 1.0 is "apply it without asking".
#: `review` scores well above `block`: it needs a human, but it is something
#: CostSentinel can then carry out, whereas `block` is advice to do by hand.
_SAFETY: dict[ActionClass, Decimal] = {
    ActionClass.ALLOW: Decimal("1.0"),
    ActionClass.REVIEW: Decimal("0.6"),
    ActionClass.BLOCK: Decimal("0.2"),
}

#: Ranking resolution. Scores are quantised so ordering is stable and reproducible
#: rather than sensitive to the last bits of a division.
_SCORE_PLACES = Decimal("0.000001")


def safety_component(action_class: ActionClass) -> Decimal:
    """How safe an action is, as a 0-1 score where higher is safer."""
    return _SAFETY.get(action_class, Decimal("0.2"))


def savings_component(monthly: Decimal | None, *, largest_monthly: Decimal | None) -> Decimal:
    """A saving as a share of the largest saving in the run.

    An unpriced finding scores zero on *this component only*. It still ranks on
    safety and confidence, so it stays in the report rather than vanishing -- which
    is the point of counting unknowns rather than zeroing them.
    """
    if monthly is None or largest_monthly is None or largest_monthly <= 0:
        return Decimal(0)
    return min(Decimal(1), monthly / largest_monthly)


def composite_score(*, savings: Decimal, safety: Decimal, confidence: Decimal) -> Decimal:
    """Weighted composite of the three components."""
    total = WEIGHT_SAVINGS * savings + WEIGHT_RISK * safety + WEIGHT_CONFIDENCE * confidence
    return total.quantize(_SCORE_PLACES, rounding=ROUND_HALF_UP)


def score_recommendations(
    recommendations: Sequence[Recommendation],
) -> tuple[RankingScore, ...]:
    """Score every recommendation, in the order given.

    Normalisation is against the largest saving *in this run*, so the components are
    comparable within a report. That makes the savings component relative rather
    than absolute, which is the right choice for ordering one client's findings and
    the wrong one for comparing clients -- a portfolio roll-up will need its own
    normalisation.
    """
    amounts = [
        r.savings.monthly.amount for r in recommendations if r.savings.monthly.amount is not None
    ]
    largest = max(amounts) if amounts else None

    scores: list[RankingScore] = []
    for recommendation in recommendations:
        savings = savings_component(recommendation.savings.monthly.amount, largest_monthly=largest)
        safety = safety_component(recommendation.risk_class)
        confidence = Decimal(recommendation.confidence)
        scores.append(
            RankingScore(
                recommendation_id=recommendation.recommendation_id,
                savings_known=recommendation.savings.monthly.is_known,
                savings_component=savings,
                risk_component=safety,
                confidence_component=confidence,
                composite=composite_score(savings=savings, safety=safety, confidence=confidence),
                provenance=Provenance.calculated(
                    f"{WEIGHT_SAVINGS} x savings({savings}) + "
                    f"{WEIGHT_RISK} x safety({safety}) + "
                    f"{WEIGHT_CONFIDENCE} x confidence({confidence})"
                ),
            )
        )
    return tuple(scores)


def rank_order(
    recommendations: Sequence[Recommendation], scores: Sequence[RankingScore]
) -> tuple[str, ...]:
    """Recommendation ids in rank order, by :meth:`RankingScore.order_key`.

    A recommendation with no score ranks last. That should not happen -- the planner
    scores everything it proposes -- but ranking something *above* a scored finding
    on the strength of a missing score would be the wrong way to fail.
    """
    by_id = {score.recommendation_id: score for score in scores}

    def key(recommendation: Recommendation) -> tuple[int, Decimal, Decimal, str]:
        score = by_id.get(recommendation.recommendation_id)
        if score is None:
            return (TIER_UNQUANTIFIED, Decimal(0), Decimal(0), recommendation.recommendation_id)
        return score.order_key()

    return tuple(r.recommendation_id for r in sorted(recommendations, key=key))
