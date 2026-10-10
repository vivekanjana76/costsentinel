"""Composite ranking: savings, safety and confidence.

Phase 1 ranked by savings alone, which quietly told a client that the most valuable
thing to do is the biggest number. It is not. A 25-dollar saving that is certain and
reversible is often a better first move than a 400-dollar saving that is risky and
inferred from a thin observation window.

So the ordering is a weighted composite of three normalised components, computed
here with no model involvement. The weights are explicit policy constants, stated
below and reported on every finding, so a client can see the trade-off being made
rather than having to trust an opaque ordering.

The model's only contribution to ranking is the *rationale* attached afterwards. A
hostile model can change the wording and cannot change the rank -- which is a
property the tests assert directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.analysis import RankingScore
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
    """Recommendation ids in rank order, highest composite first.

    Ties break on the recommendation id, so the order is total and reproducible
    rather than dependent on input ordering.
    """
    by_id = {score.recommendation_id: score for score in scores}
    return tuple(
        recommendation.recommendation_id
        for recommendation in sorted(
            recommendations,
            key=lambda r: (
                -(
                    by_id[r.recommendation_id].composite
                    if r.recommendation_id in by_id
                    else Decimal(0)
                ),
                r.recommendation_id,
            ),
        )
    )
