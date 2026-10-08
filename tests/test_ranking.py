"""Composite ranking tests.

The behaviour worth pinning is the trade-off itself: a smaller, safer, more certain
saving should be able to outrank a bigger, riskier, less certain one. If it cannot,
the composite is just savings-ordering with extra arithmetic.
"""

from __future__ import annotations

from decimal import Decimal

from costsentinel.agents.ranking import (
    WEIGHT_CONFIDENCE,
    WEIGHT_RISK,
    WEIGHT_SAVINGS,
    composite_score,
    rank_order,
    safety_component,
    savings_component,
    score_recommendations,
)
from costsentinel.domain.common import MoneyAmount, Provenance, ProvenanceSource
from costsentinel.domain.recommendations import (
    ActionClass,
    ActionType,
    Recommendation,
    SavingsEstimate,
)

CALCULATED = Provenance.calculated("test")


def _recommendation(
    *,
    rec_id: str,
    monthly: str | None,
    action_class: ActionClass = ActionClass.REVIEW,
    confidence: str = "0.9",
) -> Recommendation:
    savings = (
        SavingsEstimate.from_monthly(
            MoneyAmount.of(Decimal(monthly), provenance=CALCULATED),
            basis="test",
            is_estimated=False,
        )
        if monthly is not None
        else SavingsEstimate.undetermined("not priced")
    )
    return Recommendation(
        recommendation_id=rec_id,
        signal_id=f"ws-{rec_id}",
        client="acme",
        subscription_id="sub-1",
        target_resource_id=f"/r/{rec_id}",
        target_resource_name=rec_id,
        action=ActionType.NOTIFY_OWNER,
        risk_class=action_class,
        rationale="because",
        rationale_provenance=Provenance(source=ProvenanceSource.LLM_INFERENCE),
        preconditions=("x.",),
        savings=savings,
        confidence=Decimal(confidence),
        rank=1,
    )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def test_the_weights_sum_to_one() -> None:
    """Otherwise the composite is not on a 0-1 scale and the field bound would fail."""
    assert Decimal(1) == WEIGHT_SAVINGS + WEIGHT_RISK + WEIGHT_CONFIDENCE


def test_savings_weighs_most_but_not_everything() -> None:
    """Savings should dominate without being able to override safety entirely."""
    assert WEIGHT_SAVINGS > WEIGHT_RISK + WEIGHT_CONFIDENCE
    assert Decimal(1) > WEIGHT_SAVINGS


def test_safety_is_higher_for_less_dangerous_classes() -> None:
    assert (
        safety_component(ActionClass.ALLOW)
        > safety_component(ActionClass.REVIEW)
        > safety_component(ActionClass.BLOCK)
    )


def test_savings_component_is_a_share_of_the_largest() -> None:
    assert savings_component(Decimal("50"), largest_monthly=Decimal("100")) == Decimal("0.5")
    assert savings_component(Decimal("100"), largest_monthly=Decimal("100")) == Decimal(1)


def test_savings_component_is_zero_for_an_unpriced_finding() -> None:
    """Zero on this component only; it still ranks on safety and confidence."""
    assert savings_component(None, largest_monthly=Decimal("100")) == Decimal(0)
    assert savings_component(Decimal("10"), largest_monthly=None) == Decimal(0)
    assert savings_component(Decimal("10"), largest_monthly=Decimal(0)) == Decimal(0)


def test_savings_component_is_capped_at_one() -> None:
    assert savings_component(Decimal("200"), largest_monthly=Decimal("100")) == Decimal(1)


def test_composite_is_the_weighted_sum() -> None:
    expected = WEIGHT_SAVINGS * Decimal("0.5") + WEIGHT_RISK + WEIGHT_CONFIDENCE
    assert composite_score(
        savings=Decimal("0.5"), safety=Decimal(1), confidence=Decimal(1)
    ) == expected.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# The trade-off
# ---------------------------------------------------------------------------


def test_a_safe_certain_saving_can_outrank_a_bigger_risky_one() -> None:
    """The reason the composite exists at all.

    Pinned as a crossover rather than a single data point, because the interesting
    property is *where* safety starts to win. With the current weights a safe,
    certain finding overtakes a blocked one holding the largest saving somewhere
    between 45% and 60% of that saving.
    """
    risky = _recommendation(
        rec_id="risky-big", monthly="100", action_class=ActionClass.BLOCK, confidence="0.5"
    )

    modest = _recommendation(
        rec_id="safe-modest", monthly="45", action_class=ActionClass.ALLOW, confidence="0.99"
    )
    # Below the crossover, the much larger saving still leads -- which is right: a
    # client should hear about their biggest opportunity even if they must do it by
    # hand.
    assert rank_order([risky, modest], score_recommendations([risky, modest]))[0] == "risky-big"

    substantial = _recommendation(
        rec_id="safe-substantial",
        monthly="60",
        action_class=ActionClass.ALLOW,
        confidence="0.99",
    )
    # Above it, safety and certainty win.
    order = rank_order([risky, substantial], score_recommendations([risky, substantial]))
    assert order[0] == "safe-substantial"


def test_savings_still_decides_between_equally_safe_findings() -> None:
    big = _recommendation(rec_id="big", monthly="100")
    small = _recommendation(rec_id="small", monthly="10")
    order = rank_order([small, big], score_recommendations([small, big]))
    assert order == ("big", "small")


def test_confidence_breaks_a_tie_on_savings_and_safety() -> None:
    sure = _recommendation(rec_id="sure", monthly="50", confidence="0.95")
    unsure = _recommendation(rec_id="unsure", monthly="50", confidence="0.60")
    order = rank_order([unsure, sure], score_recommendations([unsure, sure]))
    assert order == ("sure", "unsure")


def test_an_unpriced_finding_still_ranks_rather_than_vanishing() -> None:
    priced = _recommendation(rec_id="priced", monthly="100")
    unpriced = _recommendation(rec_id="unpriced", monthly=None)
    order = rank_order([priced, unpriced], score_recommendations([priced, unpriced]))
    assert set(order) == {"priced", "unpriced"}
    assert order[0] == "priced"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_order_is_total_and_reproducible() -> None:
    """Identical scores must still produce a stable order, not an arbitrary one."""
    left = _recommendation(rec_id="aaa", monthly="50")
    right = _recommendation(rec_id="bbb", monthly="50")
    first = rank_order([left, right], score_recommendations([left, right]))
    second = rank_order([right, left], score_recommendations([right, left]))
    assert first == second == ("aaa", "bbb")


def test_scores_carry_their_own_arithmetic_as_provenance() -> None:
    recommendation = _recommendation(rec_id="rec-1", monthly="50")
    score = score_recommendations([recommendation])[0]
    assert score.provenance.source is ProvenanceSource.CALCULATION
    assert not score.provenance.is_model_derived
    reference = score.provenance.reference or ""
    assert str(WEIGHT_SAVINGS) in reference
    assert "savings" in reference and "safety" in reference and "confidence" in reference


def test_the_breakdown_is_human_readable() -> None:
    score = score_recommendations([_recommendation(rec_id="rec-1", monthly="50")])[0]
    explained = score.explain()
    assert "savings" in explained
    assert "safety" in explained
    assert "confidence" in explained
    assert "->" in explained


def test_scoring_nothing_yields_nothing() -> None:
    assert score_recommendations([]) == ()
    assert rank_order([], []) == ()


def test_a_recommendation_without_a_score_sorts_last() -> None:
    """Defensive: a missing score must not crash the ordering."""
    scored = _recommendation(rec_id="scored", monthly="10")
    unscored = _recommendation(rec_id="unscored", monthly="500")
    order = rank_order([scored, unscored], score_recommendations([scored]))
    assert order[-1] == "unscored"
