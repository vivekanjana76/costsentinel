"""Attribution: what accounted for a spend change, or an honest unknown.

Phase 7 answered "did spend change?" and named a scope. This answers "what changed?",
and the claim worth testing is not that it finds a cause -- that is arithmetic -- but
that it declines to find one when there is none. A detector that always names its
largest component is right about every concentrated change and hands a client a
confident wrong cause on every proportional one, which is worse than silence: somebody
goes and investigates the wrong meter.

So the cases here are the four honest failures as much as the success, and the
disproportion rule that separates a cause from a component that merely rose along with
everything else.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from costsentinel.agents.attribution import (
    BASELINE_DAYS,
    DETECTOR,
    MAX_NAMED_COMPONENTS,
    MIN_DISPROPORTION,
    MIN_EXPLAINED_SHARE,
    MIN_WINDOW_DAYS,
    attribute,
    attribute_all,
    baseline_window,
    days_reported,
)
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    CostBreakdown,
    CostComponent,
    CostDimension,
    CostPoint,
    CostSeries,
)
from costsentinel.domain.signals import (
    AnomalyDirection,
    AnomalyKind,
    AttributionOutcome,
    CostAnomaly,
    CostAttribution,
    UnattributedReason,
)
from costsentinel.providers.mock import (
    BreakdownShape,
    MockAzureProvider,
    synthetic_breakdown,
    wobbled_baseline,
)

CLIENT = "acme-industrial"
SUBSCRIPTION = "00000000-0000-4000-8000-00000000f001"
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
WINDOW = 30
OBSERVED = 7


def _provenance() -> Provenance:
    return Provenance(
        source=ProvenanceSource.MOCK_PROVIDER,
        retrieved_at=AS_OF,
        reference="test",
        verification=Verification.VERIFIED,
    )


def _days() -> list[date]:
    first = AS_OF.date() - timedelta(days=WINDOW - 1)
    return [first + timedelta(days=offset) for offset in range(WINDOW)]


def _series(dailies: list[Decimal], days: list[date] | None = None) -> CostSeries:
    at = days or _days()
    return CostSeries(
        subscription_id=SUBSCRIPTION,
        points=tuple(
            CostPoint(day=day, amount=amount) for day, amount in zip(at, dailies, strict=True)
        ),
        provenance=_provenance(),
    )


def _component(key: str, dailies: list[Decimal], days: list[date] | None = None) -> CostComponent:
    at = days or _days()
    return CostComponent(
        key=key,
        label=key.replace("-", " ").title(),
        points=tuple(
            CostPoint(day=day, amount=amount) for day, amount in zip(at, dailies, strict=True)
        ),
    )


def _breakdown(*components: CostComponent) -> CostBreakdown:
    return CostBreakdown(
        subscription_id=SUBSCRIPTION,
        dimension=CostDimension.METER,
        components=components,
        provenance=_provenance(),
    )


def _anomaly(*, baseline: str = "100.00", observed: str = "200.00") -> CostAnomaly:
    days = _days()
    return CostAnomaly(
        anomaly_id="ca-test-0000000001",
        client=CLIENT,
        subscription_id=SUBSCRIPTION,
        kind=AnomalyKind.LEVEL_SHIFT,
        direction=AnomalyDirection.INCREASE,
        baseline=MoneyAmount.of(Decimal(baseline), provenance=_provenance()),
        observed=MoneyAmount.of(Decimal(observed), provenance=_provenance()),
        delta=MoneyAmount.of(abs(Decimal(observed) - Decimal(baseline)), provenance=_provenance()),
        observed_from=days[-OBSERVED],
        observed_to=days[-1],
        window_days=WINDOW,
        baseline_days=BASELINE_DAYS,
        deviation_score=Decimal("12.5"),
        threshold_score=Decimal("3.5"),
        confidence=Decimal("0.85"),
        detector="spend_level_shift/v1",
        provenance=_provenance(),
    )


def _split(before: list[Decimal], during: list[Decimal]) -> list[Decimal]:
    """A component's series: a baseline stretch then an observed stretch."""
    return before + during


# ---------------------------------------------------------------------------
# A cause that can be named
# ---------------------------------------------------------------------------


def test_a_concentrated_rise_names_its_meter() -> None:
    """The straightforward case: one meter carries the whole change."""
    flat = [Decimal("20.00")] * WINDOW
    culprit = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("120.00")] * OBSERVED)
    totals = [a + b for a, b in zip(flat, culprit, strict=True)]

    result = attribute(
        _anomaly(),
        _series(totals),
        _breakdown(_component("steady", flat), _component("sql", culprit)),
    )

    assert result.is_attributed
    assert result.reason is None
    assert [part.key for part in result.components] == ["sql"]
    named = result.components[0]
    assert named.baseline.display() == "20.00 USD"
    assert named.observed.display() == "120.00 USD"
    assert named.contribution.display() == "100.00 USD"
    assert named.direction is AnomalyDirection.INCREASE
    assert named.share == Decimal(1)


def test_the_statement_carries_every_number_a_client_needs() -> None:
    """Before, after, difference and share, composed by the domain and not a model."""
    flat = [Decimal("20.00")] * WINDOW
    culprit = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("120.00")] * OBSERVED)
    totals = [a + b for a, b in zip(flat, culprit, strict=True)]
    result = attribute(
        _anomaly(),
        _series(totals),
        _breakdown(_component("steady", flat), _component("sql", culprit)),
    )

    statement = result.statement()
    assert "20.00 USD to 120.00 USD per day" in statement
    assert "up 100.00 USD" in statement
    assert "100% of the change" in statement


def test_a_falling_meter_is_not_a_cause_of_a_rise() -> None:
    """However large it is. A rise is explained by things that rose."""
    faller = _split([Decimal("50.00")] * (WINDOW - OBSERVED), [Decimal("10.00")] * OBSERVED)
    riser = _split([Decimal("10.00")] * (WINDOW - OBSERVED), [Decimal("150.00")] * OBSERVED)
    totals = [a + b for a, b in zip(faller, riser, strict=True)]

    result = attribute(
        _anomaly(),
        _series(totals),
        _breakdown(_component("bandwidth", faller), _component("compute", riser)),
    )

    assert result.is_attributed
    assert [part.key for part in result.components] == ["compute"]


def test_a_share_may_exceed_one_when_something_moved_the_other_way() -> None:
    """A real pattern, not an error: 140 up and 40 down is a net 100."""
    faller = _split([Decimal("50.00")] * (WINDOW - OBSERVED), [Decimal("10.00")] * OBSERVED)
    riser = _split([Decimal("10.00")] * (WINDOW - OBSERVED), [Decimal("150.00")] * OBSERVED)
    totals = [a + b for a, b in zip(faller, riser, strict=True)]
    result = attribute(
        _anomaly(),
        _series(totals),
        _breakdown(_component("bandwidth", faller), _component("compute", riser)),
    )
    assert result.components[0].share > Decimal(1)


def test_at_most_three_components_are_named() -> None:
    """A client investigating four meters at once is not investigating."""
    components = []
    totals = [Decimal(0)] * WINDOW
    for index in range(6):
        series = _split(
            [Decimal("10.00")] * (WINDOW - OBSERVED),
            [Decimal("10.00") + Decimal(60 - index * 2)] * OBSERVED,
        )
        components.append(_component(f"meter-{index}", series))
        totals = [a + b for a, b in zip(totals, series, strict=True)]

    result = attribute(_anomaly(), _series(totals), _breakdown(*components))
    assert len(result.components) <= MAX_NAMED_COMPONENTS


# ---------------------------------------------------------------------------
# Unknown stays unknown
# ---------------------------------------------------------------------------


def test_a_proportional_rise_is_unattributed() -> None:
    """The case the disproportion rule exists for, and the one that forced it.

    Every meter rises by its own share, so the largest is still the largest and a
    detector ranking by contribution alone would name it. Its share of the change
    equals its share of the spend, which makes it a passenger rather than a cause.
    """
    big = _split([Decimal("80.00")] * (WINDOW - OBSERVED), [Decimal("160.00")] * OBSERVED)
    small = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("40.00")] * OBSERVED)
    totals = [a + b for a, b in zip(big, small, strict=True)]

    result = attribute(
        _anomaly(), _series(totals), _breakdown(_component("big", big), _component("small", small))
    )

    assert not result.is_attributed
    assert result.reason is UnattributedReason.DIFFUSE
    assert result.components == ()
    assert result.considered == 2
    assert "spread across the meters" in result.statement()


def test_no_breakdown_says_so_rather_than_naming_nothing() -> None:
    """A gap in what we retrieved, which is a different claim from "no cause"."""
    result = attribute(_anomaly(), _series([Decimal("100.00")] * WINDOW), None)
    assert result.reason is UnattributedReason.NO_BREAKDOWN
    assert result.dimension is None
    assert "not a finding that the change had no cause" in result.statement()


def test_an_empty_breakdown_is_treated_as_no_breakdown() -> None:
    """Zero components is not evidence that nothing changed."""
    result = attribute(_anomaly(), _series([Decimal("100.00")] * WINDOW), _breakdown())
    assert result.reason is UnattributedReason.NO_BREAKDOWN


def test_a_breakdown_that_does_not_add_up_names_nothing() -> None:
    """A fault in the data, where naming a component would be a guess.

    And a guess that would rank plausibly enough that nobody would question it.
    """
    short = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("120.00")] * OBSERVED)
    totals = [amount + Decimal("40.00") for amount in short]  # 40 a day unaccounted for

    result = attribute(_anomaly(), _series(totals), _breakdown(_component("sql", short)))

    assert result.reason is UnattributedReason.DOES_NOT_RECONCILE
    assert result.considered == 1
    assert "does not sum to the total" in result.statement()


def test_too_little_history_names_nothing() -> None:
    """Two days of "before" can differ from each other by more than the change."""
    days = _days()[-(OBSERVED + 2) :]
    dailies = [Decimal("100.00")] * len(days)
    series = _series(dailies, days)
    breakdown = _breakdown(_component("only", dailies, days))

    result = attribute(_anomaly(), series, breakdown)
    assert result.reason is UnattributedReason.INSUFFICIENT_HISTORY


def test_a_change_too_small_to_apportion_names_nothing() -> None:
    """Guarding the denominator: a near-zero change must not manufacture a 900% share."""
    flat = [Decimal("50.00")] * WINDOW
    result = attribute(
        _anomaly(),
        _series([a * 2 for a in flat]),
        _breakdown(_component("a", flat), _component("b", flat)),
    )
    assert result.reason is UnattributedReason.DIFFUSE


# ---------------------------------------------------------------------------
# The type refuses to hold an incoherent claim
# ---------------------------------------------------------------------------


def test_an_attributed_outcome_must_name_something() -> None:
    with pytest.raises(ValidationError, match="must name at least one component"):
        CostAttribution(
            anomaly_id="ca-1",
            scope=SUBSCRIPTION,
            outcome=AttributionOutcome.ATTRIBUTED,
            detector=DETECTOR,
            provenance=_provenance(),
        )


def test_an_unattributed_outcome_must_say_why() -> None:
    with pytest.raises(ValidationError, match="must say why"):
        CostAttribution(
            anomaly_id="ca-1",
            scope=SUBSCRIPTION,
            outcome=AttributionOutcome.UNATTRIBUTED,
            detector=DETECTOR,
            provenance=_provenance(),
        )


def test_an_unattributed_outcome_cannot_carry_a_ranked_list() -> None:
    """Which would read as an answer, whatever the outcome field said."""
    flat = [Decimal("20.00")] * WINDOW
    culprit = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("120.00")] * OBSERVED)
    totals = [a + b for a, b in zip(flat, culprit, strict=True)]
    attributed = attribute(
        _anomaly(),
        _series(totals),
        _breakdown(_component("steady", flat), _component("sql", culprit)),
    )

    with pytest.raises(ValidationError, match="must name no components"):
        CostAttribution(
            anomaly_id="ca-1",
            scope=SUBSCRIPTION,
            outcome=AttributionOutcome.UNATTRIBUTED,
            reason=UnattributedReason.DIFFUSE,
            components=attributed.components,
            detector=DETECTOR,
            provenance=_provenance(),
        )


def test_an_attribution_carries_no_action_class_or_saving() -> None:
    """D106, one layer on. Knowing which meter rose is a better investigation.

    Asserted against the field set rather than by reading the code, so a field added
    later fails this rather than passing unnoticed.
    """
    fields = set(CostAttribution.model_fields)
    for forbidden in ("action", "proposed_action", "action_class", "saving", "monthly_saving"):
        assert forbidden not in fields, forbidden


# ---------------------------------------------------------------------------
# The arithmetic is the only thing producing an attribution
# ---------------------------------------------------------------------------


def test_the_attribution_module_imports_no_language_model() -> None:
    """Checkable in one glance at the imports, and asserted rather than trusted.

    The same check the anomaly and savings modules carry, for the same reason: "a
    model cannot set this figure" is a claim about the import graph, so it should be
    tested against the import graph.
    """
    source = Path("src/costsentinel/agents/attribution.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any("llm" in name for name in imported), imported
    assert "costsentinel.llm.base" not in imported


def test_the_same_inputs_attribute_identically_twice() -> None:
    """Deterministic arithmetic, so a checkpoint resume and a re-run agree."""
    flat = [Decimal("20.00")] * WINDOW
    culprit = _split([Decimal("20.00")] * (WINDOW - OBSERVED), [Decimal("120.00")] * OBSERVED)
    totals = [a + b for a, b in zip(flat, culprit, strict=True)]
    series = _series(totals)
    breakdown = _breakdown(_component("steady", flat), _component("sql", culprit))

    first = attribute(_anomaly(), series, breakdown)
    second = attribute(_anomaly(), series, breakdown)
    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# Windows and helpers
# ---------------------------------------------------------------------------


def test_the_baseline_window_is_the_days_immediately_before_the_change() -> None:
    """So the "before" in an attribution is the same "before" as in the alert."""
    anomaly = _anomaly()
    start, end = baseline_window(anomaly)
    assert end == anomaly.observed_from - timedelta(days=1)
    assert (end - start).days + 1 == BASELINE_DAYS


def test_day_counts_come_from_the_series_not_the_calendar() -> None:
    """A day the provider never reported is not a day to divide by."""
    days = _days()
    gapped = _series([Decimal("10.00")] * 10, days[:10])
    assert days_reported(gapped, days[0], days[-1]) == 10
    assert days_reported(gapped, days[20], days[-1]) == 0


def test_the_thresholds_are_stated_as_judgements() -> None:
    """Pinned so a change to one is a deliberate change, and documented as uncalibrated.

    None of these is derived from anything; they are product judgements chosen against
    synthetic shapes, which is what the module docstring says and what Phase 9b will
    replace with numbers real changes support.
    """
    assert Decimal("0.60") == MIN_EXPLAINED_SHARE
    assert Decimal("0.10") == MIN_DISPROPORTION
    assert MAX_NAMED_COMPONENTS == 3
    assert MIN_WINDOW_DAYS == 3


# ---------------------------------------------------------------------------
# Against the committed estate
# ---------------------------------------------------------------------------


def test_every_committed_breakdown_reconciles_with_its_series() -> None:
    """The property that makes attribution's arithmetic mean anything.

    A breakdown short of its total by a few percent would still rank its components
    plausibly and attribute a change to whichever happened to be largest, and nothing
    downstream could tell.
    """
    provider = MockAzureProvider()
    checked = 0
    for subscription in provider.list_subscriptions():
        breakdown = provider.get_cost_breakdown(subscription.subscription_id)
        if breakdown is None:
            continue
        series = provider.get_cost_series(subscription.subscription_id)
        assert breakdown.reconciles_with(series), subscription.display_name
        checked += 1
    assert checked >= 3, "too few breakdowns to be asserting anything"


def test_the_committed_estate_holds_an_absent_breakdown() -> None:
    """Unknown has to be reachable from the demo estate, or it is only ever a unit test."""
    provider = MockAzureProvider()
    absent = [
        s.display_name
        for s in provider.list_subscriptions()
        if provider.get_cost_breakdown(s.subscription_id) is None
    ]
    assert absent, "no subscription exercises the no-breakdown path"


def test_a_concentrated_subscription_attributes_and_a_diffuse_one_does_not() -> None:
    """Both committed shapes, end to end through the real detector and attributor."""
    from costsentinel.agents.anomalies import assess_estate

    provider = MockAzureProvider()
    subscriptions = provider.list_subscriptions()
    series = [provider.get_cost_series(s.subscription_id) for s in subscriptions]
    assessments = assess_estate(series, client=CLIENT)

    outcomes: dict[str, CostAttribution] = {}
    for assessment in assessments:
        total = next(s for s in series if s.subscription_id == assessment.scope)
        breakdown = provider.get_cost_breakdown(assessment.scope)
        for anomaly in assessment.anomalies:
            outcomes[assessment.scope] = attribute(anomaly, total, breakdown)

    attributed = [one for one in outcomes.values() if one.is_attributed]
    diffuse = [one for one in outcomes.values() if one.reason is UnattributedReason.DIFFUSE]
    assert attributed, "the concentrated subscription produced no attribution"
    assert diffuse, "the diffuse subscription was attributed, which is the failure mode"
    assert attributed[0].components[0].key == "sql-database-vcore"


def test_a_synthetic_breakdown_reconciles_for_every_shape() -> None:
    """Including the shapes the demo estate does not happen to use."""
    days = _days()
    dailies = [Decimal("100.00") + Decimal(index) for index in range(WINDOW)]
    series = _series(dailies, days)
    levels = wobbled_baseline(Decimal("100.00"), WINDOW)

    for shape in (BreakdownShape.CONCENTRATED, BreakdownShape.DIFFUSE):
        breakdown = synthetic_breakdown(
            series=series, baseline_levels=levels, shape=shape, provenance=_provenance()
        )
        assert breakdown is not None
        assert breakdown.reconciles_with(series), shape

    assert (
        synthetic_breakdown(
            series=series,
            baseline_levels=levels,
            shape=BreakdownShape.ABSENT,
            provenance=_provenance(),
        )
        is None
    )


def test_attribute_all_skips_an_anomaly_whose_scope_has_no_series() -> None:
    """An anomaly with no series is skipped rather than attributed from nothing.

    Without the total there is no reconciliation target, and a breakdown nothing checks
    is exactly what this module refuses to reason from.
    """
    assert attribute_all([_anomaly()], [], []) == ()
    assert len(attribute_all([_anomaly()], [_series([Decimal("100.00")] * WINDOW)], [])) == 1
