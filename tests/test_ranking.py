"""Composite ranking tests.

Two behaviours worth pinning.

The trade-off itself: a smaller, safer, more certain saving should be able to outrank
a bigger, riskier, less certain one. If it cannot, the composite is just
savings-ordering with extra arithmetic.

And the tier: **an unknown saving ranks low until quantified** (ARCHITECTURE.md D104).
That one is asserted exhaustively rather than by example, because it is a rule about
*every* pair rather than a property of one ordering -- and because the rule exists to
stop a FinOps tool telling a client that the thing it cannot cost is their biggest
opportunity.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

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
from costsentinel.domain.analysis import (
    TIER_QUANTIFIED,
    TIER_UNQUANTIFIED,
    RankingScore,
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
    """Counted and reported, not dropped. A declined price is not a declined finding."""
    priced = _recommendation(rec_id="priced", monthly="100")
    unpriced = _recommendation(rec_id="unpriced", monthly=None)
    order = rank_order([priced, unpriced], score_recommendations([priced, unpriced]))
    assert set(order) == {"priced", "unpriced"}
    assert order[0] == "priced"


# ---------------------------------------------------------------------------
# The rule: an unknown saving ranks low until quantified
# ---------------------------------------------------------------------------

#: Every quantified magnitude worth trying, including the smallest a cost report can
#: carry. One cent is the hard case: it is almost nothing, and the rule still says it
#: outranks a finding nobody can cost.
MAGNITUDES = ("0.01", "1", "3.65", "50", "280.32", "99999.99")

#: Every class and confidence an unknown finding could use to argue its way up. The
#: most favourable combination for the unknown is `allow` at full confidence, which is
#: what makes the exhaustive version of this test worth writing.
CLASSES = (ActionClass.ALLOW, ActionClass.REVIEW, ActionClass.BLOCK)
CONFIDENCES = ("0.1", "0.5", "0.95", "1.0")


@pytest.mark.parametrize("magnitude", MAGNITUDES)
@pytest.mark.parametrize("known_class", CLASSES)
@pytest.mark.parametrize("unknown_class", CLASSES)
def test_an_unknown_saving_never_outranks_a_known_one(
    magnitude: str, known_class: ActionClass, unknown_class: ActionClass
) -> None:
    """The rule, over the whole cross product rather than one example.

    Every quantified magnitude against every pairing of action classes, including the
    case the rule exists for: an unknown, perfectly safe, perfectly confident finding
    against a one-cent saving on an irreversible action. The known one still wins,
    because the claim "we can tell you what this is worth" is the thing being ranked.
    """
    known = _recommendation(
        rec_id="aaa-known", monthly=magnitude, action_class=known_class, confidence="0.1"
    )
    unknown = _recommendation(
        rec_id="zzz-unknown", monthly=None, action_class=unknown_class, confidence="1.0"
    )
    scores = score_recommendations([known, unknown])
    order = rank_order([known, unknown], scores)
    assert order == ("aaa-known", "zzz-unknown"), (magnitude, known_class, unknown_class)

    # Reversing the input must not change it: the rule is a property of the pair.
    assert rank_order([unknown, known], score_recommendations([unknown, known])) == order


@pytest.mark.parametrize("confidence", CONFIDENCES)
def test_no_confidence_lifts_an_unknown_above_a_known(confidence: str) -> None:
    """Confidence in a detector is not confidence in a figure that does not exist."""
    known = _recommendation(rec_id="aaa", monthly="0.01", action_class=ActionClass.BLOCK)
    unknown = _recommendation(
        rec_id="zzz", monthly=None, action_class=ActionClass.ALLOW, confidence=confidence
    )
    order = rank_order([known, unknown], score_recommendations([known, unknown]))
    assert order[0] == "aaa"


def test_the_unknown_can_hold_the_higher_composite_and_still_rank_below() -> None:
    """The case that shows this is a tier and not a weight.

    The savings component is a share of the *largest* saving in the run, so a one-cent
    finding alongside a large one normalises to nearly zero and keeps only its safety
    and confidence: about 0.065 for an irreversible, low-confidence action. An
    `allow`-class unknown at full confidence scores 0.40. The unknown's composite is
    six times larger and it still ranks last, which is the whole point -- the rule is
    applied above the composite rather than inside it.

    This was also the shape of the real discordant pair in the Phase 5 scorecard:
    `sql-reporting` (unknown, reversible) held a composite of 0.270 against
    `snap-web-01-pre-migration`'s 0.250 (33.79, irreversible), and sat above it.
    """
    anchor = _recommendation(rec_id="aaa-anchor", monthly="99999.99")
    known = _recommendation(
        rec_id="mmm-tiny", monthly="0.01", action_class=ActionClass.BLOCK, confidence="0.1"
    )
    unknown = _recommendation(
        rec_id="zzz", monthly=None, action_class=ActionClass.ALLOW, confidence="1.0"
    )
    group = [anchor, known, unknown]
    scores = score_recommendations(group)
    by_id = {s.recommendation_id: s for s in scores}
    assert by_id["zzz"].composite > by_id["mmm-tiny"].composite
    assert rank_order(group, scores) == ("aaa-anchor", "mmm-tiny", "zzz")


def test_no_weighting_can_break_the_rule() -> None:
    """Structural, not arithmetic.

    The tier is the first key of the order, so it holds for any composite at all --
    including the degenerate one where the unknown scores a perfect 1.0 and the known
    scores 0. A rule encoded as a weight would not survive this.
    """
    known = RankingScore(
        recommendation_id="aaa",
        savings_known=True,
        savings_component=Decimal(0),
        risk_component=Decimal(0),
        confidence_component=Decimal(0),
        composite=Decimal(0),
        provenance=CALCULATED,
    )
    unknown = RankingScore(
        recommendation_id="zzz",
        savings_known=False,
        savings_component=Decimal(1),
        risk_component=Decimal(1),
        confidence_component=Decimal(1),
        composite=Decimal(1),
        provenance=CALCULATED,
    )
    assert known.order_key() < unknown.order_key()
    assert known.tier == TIER_QUANTIFIED
    assert unknown.tier == TIER_UNQUANTIFIED


def test_reversibility_breaks_a_tie_between_comparable_knowns() -> None:
    """Equal magnitude, equal confidence, different reversibility: the safer one leads.

    This is "reversibility breaks ties" in the literal sense. Safety is already inside
    the composite, so two findings with the same saving and confidence separate there;
    the explicit safety key is the fallback for when the composites come out exactly
    equal, which a weight alone cannot guarantee breaking the right way.
    """
    reversible = _recommendation(rec_id="zzz-safe", monthly="100", action_class=ActionClass.REVIEW)
    irreversible = _recommendation(
        rec_id="aaa-risky", monthly="100", action_class=ActionClass.BLOCK
    )
    order = rank_order(
        [irreversible, reversible], score_recommendations([irreversible, reversible])
    )
    # Despite sorting last alphabetically, the reversible action leads.
    assert order == ("zzz-safe", "aaa-risky")


def test_an_exact_composite_tie_breaks_on_reversibility_before_the_identifier() -> None:
    """A tie that the composite genuinely cannot separate.

    Constructed directly, because the composite is designed so that safety moves it --
    so a real pair with equal composites and unequal safety cannot arise from
    ``score_recommendations``. The key still has to order them the right way round,
    and alphabetically would be the wrong way.
    """
    safe = RankingScore(
        recommendation_id="zzz",
        savings_known=True,
        savings_component=Decimal("0.5"),
        risk_component=Decimal("1.0"),
        confidence_component=Decimal("0.5"),
        composite=Decimal("0.5"),
        provenance=CALCULATED,
    )
    risky = safe.model_copy(update={"recommendation_id": "aaa", "risk_component": Decimal("0.2")})
    assert safe.order_key() < risky.order_key()


def test_the_tier_is_reported_so_a_client_can_see_the_rule() -> None:
    """An order that moved for reasons nobody can see is worse than no rule at all."""
    known = score_recommendations([_recommendation(rec_id="k", monthly="10")])[0]
    unknown = score_recommendations([_recommendation(rec_id="u", monthly=None)])[0]
    assert known.savings_known
    assert not unknown.savings_known
    assert "quantified" in known.explain()
    assert "ranks below every priced finding" in unknown.explain()


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
    """A client must be able to see the trade-off rather than trust the order."""
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
