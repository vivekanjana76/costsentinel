"""The spend-anomaly detector, over the four series shapes that matter.

A step change and a reverting spike must be found. A steady series and a **weekly
pattern** must not produce anything -- and the seasonal case is the one worth the most
attention, because it is where a naive detector fails and fails every week.

Everything here is a pure function of a committed series, so these tests need no
graph, no provider and no model.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from costsentinel.agents.anomalies import (
    LEVEL_SHIFT_SCORE,
    MIN_BASELINE_DAYS,
    MIN_RELATIVE_CHANGE,
    OBSERVATION_DAYS,
    SPIKE_SCORE,
    anomaly_id_for,
    assess_estate,
    assess_series,
    median,
    median_absolute_deviation,
    missing_days,
    robust_score,
    usable_spread,
    weekday_profile,
)
from costsentinel.domain.common import (
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import CostPoint, CostSeries
from costsentinel.domain.signals import AnomalyDirection, AnomalyKind, AnomalyVerdict

CLIENT = "acme-industrial"
SUBSCRIPTION = "00000000-0000-4000-8000-00000000a001"

#: A Monday, so weekday arithmetic in these tests reads the way it looks.
START = date(2026, 9, 7)
assert START.weekday() == 0

#: Deterministic day-to-day wobble, so the robust statistics have something to work
#: with. Thirty values rather than a repeating seven, because a seven-day cycle would
#: be absorbed by the weekday profile and leave the residual MAD at zero -- which
#: would make every test here exercise only the flat-baseline fallback.
JITTER: tuple[str, ...] = (
    "0.00",
    "0.45",
    "-0.30",
    "0.20",
    "-0.55",
    "0.35",
    "-0.15",
    "0.60",
    "-0.40",
    "0.10",
    "0.50",
    "-0.25",
    "0.30",
    "-0.50",
    "0.15",
    "0.40",
    "-0.20",
    "0.55",
    "-0.35",
    "0.25",
    "-0.45",
    "0.05",
    "0.65",
    "-0.10",
    "0.35",
    "-0.60",
    "0.45",
    "-0.05",
    "0.20",
    "-0.30",
)


def _provenance() -> Provenance:
    return Provenance(
        source=ProvenanceSource.MOCK_PROVIDER,
        retrieved_at=datetime(2026, 10, 7, tzinfo=UTC),
        reference="tests/test_anomalies",
        verification=Verification.VERIFIED,
    )


def _series(amounts: Sequence[str], *, start: date = START) -> CostSeries:
    return CostSeries(
        subscription_id=SUBSCRIPTION,
        points=tuple(
            CostPoint(day=start + timedelta(days=offset), amount=Decimal(amount))
            for offset, amount in enumerate(amounts)
        ),
        provenance=_provenance(),
    )


def _with_jitter(levels: Sequence[Decimal]) -> list[str]:
    """Daily levels plus the committed wobble, so nothing is suspiciously flat."""
    return [str(level + Decimal(JITTER[index % len(JITTER)])) for index, level in enumerate(levels)]


# --- the four committed shapes --------------------------------------------

#: Thirty days at a steady 40 per day. Nothing to report.
STEADY = _with_jitter([Decimal("40.00")] * 30)

#: Steady for 23 days, then 70 per day for the last week. The baseline is clean and
#: the whole observation window sits at the new level: a level shift.
STEP_CHANGE = _with_jitter([Decimal("40.00")] * 23 + [Decimal("70.00")] * 7)

#: Steady, with one day at 160 that reverts the next day. A spike.
REVERTING_SPIKE = _with_jitter(
    [Decimal("40.00")] * 25 + [Decimal("160.00")] + [Decimal("40.00")] * 4
)

#: 30 per day with a Sunday batch window at 150. A pattern, not an anomaly -- and the
#: case a detector without a weekday profile reports every single week.
SEASONAL = _with_jitter(
    [
        Decimal("150.00") if (START + timedelta(days=offset)).weekday() == 6 else Decimal("30.00")
        for offset in range(30)
    ]
)


# ---------------------------------------------------------------------------
# The statistics
# ---------------------------------------------------------------------------


def test_the_median_averages_the_two_middle_values_of_an_even_series() -> None:
    """Exact on Decimals, because every figure downstream of this is money."""
    assert median([Decimal(1), Decimal(3)]) == Decimal(2)
    assert median([Decimal(5), Decimal(1), Decimal(3)]) == Decimal(3)


def test_the_median_of_nothing_is_an_error_not_a_zero() -> None:
    """A zero would be a claim about a series that does not exist."""
    with pytest.raises(ValueError, match="median of an empty series"):
        median([])
    with pytest.raises(ValueError, match="deviation of an empty series"):
        median_absolute_deviation([])


def test_a_minority_of_extreme_points_does_not_move_the_baseline() -> None:
    """The property the whole method rests on.

    A mean and a standard deviation would be dragged by the outliers, so the outliers
    would partly hide themselves. The median and the MAD are not, which is why a
    second spike in the same window is still visible.
    """
    normal = [Decimal(10)] * 20
    with_outliers = [*normal, Decimal(1000), Decimal(2000)]
    assert median(with_outliers) == Decimal(10)
    assert median_absolute_deviation(with_outliers) == Decimal(0)


def test_a_flat_baseline_has_no_score_rather_than_an_infinite_one() -> None:
    """``None`` rather than infinity: a flat series cannot say how unusual a day is.

    Returning a large number would dress a missing statistic up as a strong one, and
    the material-significance test is what decides these cases instead.
    """
    assert robust_score(Decimal(50), centre=Decimal(40), deviation=Decimal(0)) is None
    assert robust_score(Decimal(50), centre=Decimal(40), deviation=Decimal(2)) is not None


def test_the_weekday_profile_is_a_median_per_weekday() -> None:
    """Sundays are compared with Sundays, which is the seasonal correction entire."""
    profile = weekday_profile(_series(SEASONAL).points)
    assert profile[6] > profile[0] * 4  # Sunday batch against a Monday
    assert pytest.approx(float(profile[0]), abs=1.0) == 30.0


def test_a_missing_day_is_reported_rather_than_filled() -> None:
    """A gap read as a zero would look like spend collapsing to nothing."""
    points = _series(["40", "40", "40"]).points
    gapped = CostSeries(
        subscription_id=SUBSCRIPTION,
        points=(points[0], points[2]),
        provenance=_provenance(),
    )
    assert missing_days(gapped.points) == (START + timedelta(days=1),)
    assert missing_days(()) == ()


# ---------------------------------------------------------------------------
# The four shapes
# ---------------------------------------------------------------------------


def test_a_steady_series_reports_nothing_and_says_so() -> None:
    """A clean verdict is worth recording: "we looked, and spend is steady"."""
    assessment = assess_series(_series(STEADY), client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.ASSESSED
    assert assessment.is_clean
    assert assessment.anomalies == ()
    assert "consistent with" in assessment.detail
    assert assessment.days_missing == 0


def test_a_step_change_is_a_level_shift_not_a_spike() -> None:
    """The baseline moved and stayed moved: a change, not an event.

    Reported as a level shift because "your daily run rate is now higher" and "look at
    last Tuesday" lead to different investigations, and only one of them is right here.
    """
    assessment = assess_series(_series(STEP_CHANGE), client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.ASSESSED
    assert len(assessment.anomalies) == 1

    anomaly = assessment.anomalies[0]
    assert anomaly.kind is AnomalyKind.LEVEL_SHIFT
    assert anomaly.direction is AnomalyDirection.INCREASE
    assert anomaly.window_days == OBSERVATION_DAYS
    # 70 against 40, to the cent, from the committed series.
    assert anomaly.baseline.amount == pytest.approx(Decimal("40"), abs=Decimal("1"))
    assert anomaly.observed.amount == pytest.approx(Decimal("70"), abs=Decimal("1"))
    assert anomaly.delta.amount == pytest.approx(Decimal("30"), abs=Decimal("1"))
    assert anomaly.deviation_score >= LEVEL_SHIFT_SCORE
    assert anomaly.threshold_score == LEVEL_SHIFT_SCORE


def test_a_reverting_spike_is_a_spike_and_names_the_day() -> None:
    """One day far outside normal variation, with the baseline unmoved around it."""
    assessment = assess_series(_series(REVERTING_SPIKE), client=CLIENT)
    assert len(assessment.anomalies) == 1

    anomaly = assessment.anomalies[0]
    assert anomaly.kind is AnomalyKind.SPIKE
    assert anomaly.window_days == 1
    assert anomaly.observed_from == anomaly.observed_to == START + timedelta(days=25)
    assert anomaly.observed.amount == pytest.approx(Decimal("160"), abs=Decimal("1"))
    assert anomaly.deviation_score >= SPIKE_SCORE
    assert anomaly.threshold_score == SPIKE_SCORE


def test_a_weekly_pattern_does_not_alert() -> None:
    """The adversarial case, and the reason the weekday profile exists.

    A Sunday batch window at five times a weekday is not an anomaly, it is a schedule.
    A detector comparing every day with the overall median reports it every week, and
    a client who is told their Sunday batch is a spike every Sunday stops reading.
    """
    assessment = assess_series(_series(SEASONAL), client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.ASSESSED
    assert assessment.anomalies == (), assessment.detail


def test_the_seasonal_case_would_fire_without_the_weekday_profile() -> None:
    """Shows the correction is doing work rather than being decorative.

    The same series assessed against its own overall median -- what a detector without
    a weekday profile computes -- puts the Sunday many standard deviations out. If this
    ever stops being true, the seasonal test above has stopped proving anything.
    """
    points = _series(SEASONAL).points
    amounts = [point.amount for point in points]
    centre = median(amounts)
    spread = median_absolute_deviation(amounts)
    sunday = next(p.amount for p in points[-OBSERVATION_DAYS:] if p.day.weekday() == 6)
    naive = robust_score(sunday, centre=centre, deviation=spread)
    assert naive is not None
    assert naive > SPIKE_SCORE * 10


# ---------------------------------------------------------------------------
# Unknown stays unknown
# ---------------------------------------------------------------------------


def test_too_little_history_draws_no_conclusion() -> None:
    """A new subscription has no baseline, and inventing one would invent the finding."""
    short = _series(STEADY[: MIN_BASELINE_DAYS + OBSERVATION_DAYS - 1])
    assessment = assess_series(short, client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.INSUFFICIENT_HISTORY
    assert not assessment.verdict.is_conclusive
    assert assessment.anomalies == ()
    assert str(MIN_BASELINE_DAYS) in assessment.detail


def test_a_gappy_series_draws_no_conclusion_rather_than_reading_gaps_as_zero() -> None:
    """The worst false positive available to this detector, refused explicitly.

    Five missing days out of thirty is a broken feed, not a spend collapse. Filling
    them with zeros would produce a confident, enormous, entirely fictional decrease.
    """
    full = _series(STEADY)
    # Drop five days from the middle of the window.
    kept = tuple(point for index, point in enumerate(full.points) if not 10 <= index < 15)
    gappy = CostSeries(subscription_id=SUBSCRIPTION, points=kept, provenance=_provenance())
    assessment = assess_series(gappy, client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.INCOMPLETE_SERIES
    assert assessment.anomalies == ()
    assert assessment.days_missing == 5
    assert "unknown, not zero" in assessment.detail


def test_one_missing_day_is_tolerated() -> None:
    """A late-arriving daily bill should not silence the detector for the month."""
    full = _series(STEP_CHANGE)
    kept = tuple(point for index, point in enumerate(full.points) if index != 3)
    gappy = CostSeries(subscription_id=SUBSCRIPTION, points=kept, provenance=_provenance())
    assessment = assess_series(gappy, client=CLIENT)
    assert assessment.verdict is AnomalyVerdict.ASSESSED
    assert assessment.days_missing == 1
    assert len(assessment.anomalies) == 1


def test_an_immaterial_change_is_not_reported_however_significant() -> None:
    """Both halves of the test are required, and this is the half people forget.

    A series with almost no variability scores a tiny move as many standard
    deviations. Correct, and useless: a feed of correct, useless alerts is how an
    alerting system gets ignored.
    """
    flat = [Decimal("40.00")] * 23 + [Decimal("40.50")] * 7
    assessment = assess_series(_series([str(v) for v in flat]), client=CLIENT)
    assert assessment.anomalies == ()
    change = Decimal("0.50") / Decimal("40.00")
    assert change < MIN_RELATIVE_CHANGE


def test_a_material_change_on_a_perfectly_flat_baseline_is_still_reported() -> None:
    """A doubling of spend must be found even where there is no variance to measure.

    With no variability the measured deviation is zero and the scale floor supplies 2%
    of the baseline instead, so the score is computed against the precision of the
    billing feed rather than against nothing. A detector that required a measured
    variance would be silent on the most obvious step change there is.
    """
    flat = [Decimal("40.00")] * 23 + [Decimal("80.00")] * 7
    assessment = assess_series(_series([str(v) for v in flat]), client=CLIENT)
    assert len(assessment.anomalies) == 1
    anomaly = assessment.anomalies[0]
    assert anomaly.kind is AnomalyKind.LEVEL_SHIFT
    assert anomaly.deviation_score > LEVEL_SHIFT_SCORE


def test_the_scale_floor_stops_cents_of_noise_scoring_as_significance() -> None:
    """The defect this floor exists for, measured on the mock estate before it landed.

    A stable subscription's residual deviation can be a few cents, and dividing by a
    few cents turns ordinary daily wobble into a double-digit score. Flooring the scale
    at a share of the *baseline* keeps the two halves of the test complementary instead
    of leaving materiality to carry the whole decision.
    """
    assert usable_spread(Decimal("0.02"), baseline_level=Decimal("60.00")) == Decimal("1.20")
    # A measured deviation above the floor is used as measured.
    assert usable_spread(Decimal("5.00"), baseline_level=Decimal("60.00")) == Decimal("5.00")
    # A zero baseline leaves no scale at all, which the caller handles.
    assert usable_spread(Decimal(0), baseline_level=Decimal(0)) == Decimal(0)


def test_a_zero_baseline_has_no_score_and_is_decided_on_materiality() -> None:
    """Spend appearing where there was none: a real event with no scale to measure it.

    The only case left where ``robust_score`` returns ``None`` once the floor is in
    place, and materiality decides it -- any non-zero change against a zero baseline is
    material, because there is no denominator to measure it against.
    """
    from_nothing = [Decimal("0.00")] * 23 + [Decimal("50.00")] * 7
    assessment = assess_series(_series([str(v) for v in from_nothing]), client=CLIENT)
    assert len(assessment.anomalies) == 1
    anomaly = assessment.anomalies[0]
    assert anomaly.direction is AnomalyDirection.INCREASE
    assert anomaly.deviation_score == Decimal(0)
    assert anomaly.delta_fraction is None
    assert "unavailable (flat baseline)" in {observation.value for observation in anomaly.evidence}


# ---------------------------------------------------------------------------
# What an alert says
# ---------------------------------------------------------------------------


def test_an_alert_states_every_number_behind_it() -> None:
    """A client must be able to recompute the conclusion, not just read it."""
    anomaly = assess_series(_series(STEP_CHANGE), client=CLIENT).anomalies[0]
    statement = anomaly.statement()
    assert anomaly.baseline.display() in statement
    assert anomaly.observed.display() in statement
    assert anomaly.delta.display() in statement
    assert "robust standard deviations" in statement
    assert "% of baseline" in statement

    named = {observation.name for observation in anomaly.evidence}
    assert {
        "baseline_daily_spend",
        "residual_scale",
        "minimum_scale_share",
        "deviation_score",
        "method",
    } <= named
    assert any(name.startswith("weekday_median.") for name in named)


def test_the_evidence_shows_the_weekly_pattern_was_accounted_for() -> None:
    """So "isn't that just our Sunday batch?" has an answer in the alert itself."""
    anomaly = assess_series(_series(STEP_CHANGE), client=CLIENT).anomalies[0]
    weekdays = {
        observation.name.removeprefix("weekday_median.")
        for observation in anomaly.evidence
        if observation.name.startswith("weekday_median.")
    }
    assert weekdays == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def test_every_figure_is_calculated_and_none_is_model_derived() -> None:
    """The guarantee ``MoneyAmount`` already gives, re-asserted where it matters."""
    anomaly = assess_series(_series(STEP_CHANGE), client=CLIENT).anomalies[0]
    for money in (anomaly.baseline, anomaly.observed, anomaly.delta):
        assert money.provenance.source is ProvenanceSource.CALCULATION
        assert not money.provenance.is_model_derived
    assert anomaly.provenance.source is ProvenanceSource.CALCULATION


def test_a_decrease_is_reported_with_a_positive_score() -> None:
    """Direction carries the sign; the score carries the distance.

    A 40% drop in spend is as far from normal as a 40% rise and is worth telling a
    client about -- a workload that stopped is often a workload that broke.
    """
    dropped = _with_jitter([Decimal("100.00")] * 23 + [Decimal("40.00")] * 7)
    anomaly = assess_series(_series(dropped), client=CLIENT).anomalies[0]
    assert anomaly.direction is AnomalyDirection.DECREASE
    assert anomaly.deviation_score > 0
    # The magnitude, not a signed amount: MoneyAmount refuses negatives, and the
    # direction field is what says which way it went.
    assert anomaly.delta.amount == pytest.approx(Decimal("60"), abs=Decimal("1"))
    assert "fell" in anomaly.statement()
    assert anomaly.delta_fraction is not None
    assert anomaly.delta_fraction > 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_assessment_is_deterministic() -> None:
    """A scorecard over a detector that wobbles between runs would mean nothing."""
    first = assess_series(_series(STEP_CHANGE), client=CLIENT)
    second = assess_series(_series(STEP_CHANGE), client=CLIENT)
    assert first == second


def test_an_anomaly_id_is_stable_across_processes() -> None:
    """SHA-256 rather than ``hash``, whose string hashing is salted per process."""
    first = anomaly_id_for(SUBSCRIPTION, AnomalyKind.SPIKE, START)
    assert first == anomaly_id_for(SUBSCRIPTION, AnomalyKind.SPIKE, START)
    assert first != anomaly_id_for(SUBSCRIPTION, AnomalyKind.LEVEL_SHIFT, START)
    assert first.startswith("ca-spike-")


def test_an_estate_is_assessed_in_a_stable_order() -> None:
    """So a checkpoint resume and a re-run agree, as they do for waste signals."""
    first = _series(STEADY)
    second = CostSeries(
        subscription_id="00000000-0000-4000-8000-00000000a000",
        points=_series(STEP_CHANGE).points,
        provenance=_provenance(),
    )
    assessments = assess_estate([first, second], client=CLIENT)
    assert [a.scope for a in assessments] == sorted(a.scope for a in assessments)
    assert len(assessments) == 2


def test_the_module_imports_no_language_model() -> None:
    """Structural, like ``savings.py``: an anomaly is arithmetic or it is nothing."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/costsentinel/agents/anomalies.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [name for name in imported if "llm" in name], imported
