"""Deterministic spend-anomaly detection. No language model is imported here.

Like :mod:`costsentinel.agents.savings`, this module is separate so the property is
checkable in one glance at the imports: an anomaly in CostSentinel is arithmetic over
a provider-reported series, and there is no code path by which a model could produce
one. A test asserts the absent import rather than trusting the convention.

The question this answers is **"did spend change?"**, which is a different question
from "is this resource wasteful?". The answer routes to a report and an
investigation, never to a remediation (ARCHITECTURE.md D106).

Why robust statistics
---------------------

The obvious method -- a mean and a standard deviation, flag anything beyond three
sigma -- is partly blinded by the event it is looking for. A single large spike pulls
the mean up and inflates the standard deviation, so the spike reduces its own z-score
and a second comparable spike is hidden entirely. A median and a **median absolute
deviation** are not moved by a minority of extreme points, which is exactly the
property wanted: the baseline should describe normal days, and an anomalous day should
not get a vote on what normal looks like.

So the statistic is the *modified z-score* of Iglewicz and Hoaglin:

    z = 0.6745 x (value - median) / MAD

:data:`MAD_TO_SIGMA` is the constant that makes MAD a consistent estimator of the
standard deviation for normally distributed data, so a z here means roughly what a
z means anywhere else. Both are two medians and a subtraction, which matters as much
as the robustness: a client can recompute the whole thing from the numbers in the
alert.

Why a weekday profile
---------------------

A weekly batch window is the case a naive detector gets wrong, and it gets it wrong
*badly*: a Sunday that is five times a Tuesday looks like a spike every single week.
The fix is to compare like with like. Every day's value is reduced by the median of
its **own weekday** across the baseline, and the statistics run on the residual. A
weekly pattern then has a residual near zero and contributes no alert, while a genuine
change still shows up because it moves every weekday together.

That is the "documented seasonal-aware variant", and it is deliberately the simplest
one that works. A Fourier or STL decomposition would handle more shapes and would stop
a client being able to check the arithmetic by hand, which is a bad trade for a figure
that drives spend decisions.

Why two conditions
------------------

An alert needs to be both **statistically** and **materially** significant, and only
requiring the first produces alerts nobody wants. A series sitting at 40.00 with a
cent of daily variation has a residual MAD near zero, so a day at 40.50 scores a z in
the dozens -- statistically unmistakable and 1.25% of spend. Reporting it would be
correct and useless, and a feed of correct, useless alerts is how an alerting system
gets ignored.

So a deviation must also exceed :data:`MIN_RELATIVE_CHANGE` of the baseline. That
constant is a product judgement rather than a derived quantity, and it is the one knob
here that an operator might reasonably want to move.

What this does not do
---------------------

**It does not attribute.** Telling a client *which resource* caused a spike needs a
cost series broken down by resource, which the provider seam does not expose: Azure
Cost Management can produce it, the mock provider cannot usefully fake it, and the
whole point of attribution is matching a real spike to a real resource. That is
Phase 8 work. :func:`assess_series` takes a bare series and a scope label precisely
so a per-resource series needs no new detector when one arrives.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import CostPoint, CostSeries
from costsentinel.domain.signals import (
    AnomalyAssessment,
    AnomalyDirection,
    AnomalyKind,
    AnomalyVerdict,
    CostAnomaly,
    Observation,
)

# --- method constants ------------------------------------------------------
# Explicit, documented policy. Each one is justified above or below; none is a
# derived quantity, and the evaluation harness measures what they cost in precision
# and recall.

#: Makes the median absolute deviation a consistent estimator of the standard
#: deviation for normally distributed data, so a score here means what a z-score
#: means elsewhere. Not a tunable: it is the constant 0.6745 from the modified
#: z-score of Iglewicz and Hoaglin (1993).
MAD_TO_SIGMA = Decimal("0.6745")

#: The window being asked about. Exactly one week, so that a weekly pattern
#: contributes precisely one of each weekday to the observation and the comparison
#: against the weekday profile is balanced. A shorter window would weight whichever
#: weekdays it happened to contain.
OBSERVATION_DAYS = 7

#: Fewest baseline days from which a conclusion may be drawn. Two full weeks, because
#: that is the minimum from which a weekday profile can be estimated at all -- one
#: week gives a single sample per weekday, which is a value rather than a median.
MIN_BASELINE_DAYS = 14

#: Score a *week's* median must beat to be called a level shift. 3.5 is the
#: conventional cut for the modified z-score (Iglewicz and Hoaglin), and a claim about
#: seven days' median is supported by seven observations.
LEVEL_SHIFT_SCORE = Decimal("3.5")

#: Score a *single day* must beat to be called a spike. Higher than the level-shift
#: cut on purpose: one day is one observation, and a claim resting on one point should
#: need more evidence than a claim resting on seven.
SPIKE_SCORE = Decimal("5.0")

#: Smallest change worth reporting, as a share of the baseline. The material-
#: significance half of the test, and a product judgement rather than a derived
#: quantity: below this a client's own week-to-week variation swamps the finding, and
#: an alert that is right but immaterial trains people to ignore alerts.
MIN_RELATIVE_CHANGE = Decimal("0.15")

#: Share of the series' own date range that may be missing before the detector
#: declines to conclude. One day in a month is a late-arriving bill; a quarter of the
#: month missing is not a series.
MAX_MISSING_SHARE = Decimal("0.10")

# --- confidences -----------------------------------------------------------
#: A level shift is supported by a week of observations agreeing with each other.
_CONFIDENCE_LEVEL_SHIFT = Decimal("0.85")
#: A spike rests on a single day, so it is scored lower however large it is. The day
#: itself is a provider fact; what is less certain is whether it means anything.
_CONFIDENCE_SPIKE = Decimal("0.70")

_CENTS = Decimal("0.01")
_PLACES = Decimal("0.01")


def anomaly_id_for(scope: str, kind: AnomalyKind, day: date) -> str:
    """A stable, deterministic anomaly id.

    SHA-256 rather than :func:`hash`, whose string hashing is salted per process: an
    id that changed between runs would break both checkpoint resume and any future
    episodic lookup, exactly as it would for a waste signal.
    """
    digest = hashlib.sha256(f"{kind.value}:{scope}:{day.isoformat()}".encode()).hexdigest()[:10]
    return f"ca-{kind.value.replace('_', '')}-{digest}"


def median(values: Sequence[Decimal]) -> Decimal:
    """The middle value, averaging the two middle ones for an even count.

    Written out rather than taken from ``statistics`` so it stays exact on
    :class:`~decimal.Decimal` inputs. ``statistics.median`` is fine on Decimals, but
    the even case divides by two and this keeps every quantisation in one place.
    """
    if not values:
        msg = "the median of an empty series is undefined"
        raise ValueError(msg)
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def median_absolute_deviation(values: Sequence[Decimal]) -> Decimal:
    """Median of the absolute deviations from the median.

    Zero is a legitimate answer and means the series has no variability at all, which
    the caller must handle rather than divide by: a perfectly flat series cannot
    support a statistical claim about how unusual a day is, so the material-
    significance test carries the whole decision there.
    """
    if not values:
        msg = "the deviation of an empty series is undefined"
        raise ValueError(msg)
    centre = median(values)
    return median([abs(value - centre) for value in values])


def robust_score(value: Decimal, *, centre: Decimal, deviation: Decimal) -> Decimal | None:
    """The modified z-score of one value, or ``None`` when there is no variability.

    ``None`` rather than infinity. A flat series genuinely cannot say how many
    standard deviations away something is, and returning a large number would dress a
    missing statistic up as a strong one.
    """
    if deviation <= 0:
        return None
    return ((MAD_TO_SIGMA * (value - centre)) / deviation).quantize(_PLACES, rounding=ROUND_HALF_UP)


def weekday_profile(points: Sequence[CostPoint]) -> dict[int, Decimal]:
    """The median spend for each weekday present in the given points.

    This is the seasonal correction: a Sunday is compared with Sundays. Weekdays with
    no data simply do not appear, and :func:`residuals` falls back to the overall
    median for them, which is the conservative choice -- an unknown weekday profile
    should not manufacture a residual.
    """
    by_weekday: dict[int, list[Decimal]] = {}
    for point in points:
        by_weekday.setdefault(point.day.weekday(), []).append(point.amount)
    return {weekday: median(values) for weekday, values in by_weekday.items()}


def residuals(
    points: Sequence[CostPoint], profile: dict[int, Decimal], fallback: Decimal
) -> list[Decimal]:
    """Each point with its weekday's typical level removed.

    What remains is the part of the day's spend the weekly rhythm does not explain,
    which is what the statistics should run on. A perfectly periodic series reduces to
    residuals near zero and produces no alert; a genuine change survives, because it
    moves every weekday together.
    """
    return [point.amount - profile.get(point.day.weekday(), fallback) for point in points]


def missing_days(points: Sequence[CostPoint]) -> tuple[date, ...]:
    """Days inside the series' own date range with no data point.

    Reported rather than filled. A missing day is missing data: read as a zero it
    would look like spend collapsing to nothing, which is the most alarming false
    positive this detector could produce and the easiest to create by accident.
    """
    if not points:
        return ()
    present = {point.day for point in points}
    first, last = min(present), max(present)
    span = (last - first).days + 1
    return tuple(
        first + timedelta(days=offset)
        for offset in range(span)
        if (first + timedelta(days=offset)) not in present
    )


def _observation(name: str, value: str, source: Provenance) -> Observation:
    return Observation(name=name, value=value, provenance=source)


def _money(amount: Decimal, *, series: CostSeries, basis: str) -> MoneyAmount:
    return MoneyAmount.of(
        amount.quantize(_CENTS, rounding=ROUND_HALF_UP),
        currency=series.currency,
        provenance=Provenance(
            source=ProvenanceSource.CALCULATION,
            retrieved_at=series.provenance.retrieved_at,
            reference=basis,
            verification=Verification.VERIFIED,
        ),
    )


def _detector_provenance(detector: str, series: CostSeries) -> Provenance:
    return Provenance(
        source=ProvenanceSource.CALCULATION,
        retrieved_at=series.provenance.retrieved_at,
        reference=f"detector:{detector} over {series.provenance.source.value}",
        verification=Verification.VERIFIED,
    )


def _is_material(delta: Decimal, baseline: Decimal) -> bool:
    """Whether a change is large enough relative to the baseline to be worth an alert.

    A zero baseline is treated as material for any non-zero change: spend appearing
    where there was none is a real event, and there is no denominator to measure it
    against.
    """
    if baseline <= 0:
        return delta != 0
    return abs(delta) >= MIN_RELATIVE_CHANGE * baseline


def _direction(delta: Decimal) -> AnomalyDirection:
    return AnomalyDirection.INCREASE if delta > 0 else AnomalyDirection.DECREASE


def assess_series(
    series: CostSeries,
    *,
    client: str,
    scope: str | None = None,
    service: str | None = None,
) -> AnomalyAssessment:
    """Ask one daily series whether spend changed, and say what was concluded.

    Args:
        series: Daily actual spend. Points need not be sorted.
        client: The client the series belongs to, for the emitted anomaly.
        scope: What is being assessed. Defaults to the series' subscription id; a
            future per-resource series would pass a resource id instead.
        service: An Azure service name, when the series is narrowed to one.

    Returns:
        An assessment, always. A series with nothing wrong produces a clean verdict
        with its reasoning, not an empty tuple -- "we looked and spend is steady" is
        worth saying, and so is "we could not tell".
    """
    target = scope or series.subscription_id
    ordered = sorted(series.points, key=lambda point: point.day)
    gaps = missing_days(ordered)
    observed_count = len(ordered)

    if observed_count < MIN_BASELINE_DAYS + OBSERVATION_DAYS:
        return AnomalyAssessment(
            scope=target,
            verdict=AnomalyVerdict.INSUFFICIENT_HISTORY,
            detail=(
                f"{observed_count} day(s) of history; a conclusion needs "
                f"{MIN_BASELINE_DAYS} baseline day(s) plus a {OBSERVATION_DAYS}-day "
                f"observation window. Too short to say whether spend changed."
            ),
            days_observed=observed_count,
            days_missing=len(gaps),
        )

    span = observed_count + len(gaps)
    if len(gaps) > MAX_MISSING_SHARE * Decimal(span):
        return AnomalyAssessment(
            scope=target,
            verdict=AnomalyVerdict.INCOMPLETE_SERIES,
            detail=(
                f"{len(gaps)} of {span} day(s) in the series have no data, which is "
                f"more than the {MAX_MISSING_SHARE:.0%} this detector will work "
                f"around. Those days are unknown, not zero, so no conclusion is drawn "
                f"rather than inferring a change from absent data."
            ),
            days_observed=observed_count,
            days_missing=len(gaps),
        )

    baseline_points = ordered[:-OBSERVATION_DAYS]
    observed_points = ordered[-OBSERVATION_DAYS:]

    profile = weekday_profile(baseline_points)
    overall = median([point.amount for point in baseline_points])
    baseline_residuals = residuals(baseline_points, profile, overall)
    observed_residuals = residuals(observed_points, profile, overall)

    centre = median(baseline_residuals)
    spread = median_absolute_deviation(baseline_residuals)
    baseline_level = median([point.amount for point in baseline_points])

    anomaly = _level_shift(
        series=series,
        client=client,
        scope=target,
        service=service,
        observed_points=observed_points,
        observed_residuals=observed_residuals,
        centre=centre,
        spread=spread,
        baseline_level=baseline_level,
        baseline_days=len(baseline_points),
        profile=profile,
    ) or _spike(
        series=series,
        client=client,
        scope=target,
        service=service,
        observed_points=observed_points,
        observed_residuals=observed_residuals,
        centre=centre,
        spread=spread,
        baseline_level=baseline_level,
        baseline_days=len(baseline_points),
        profile=profile,
    )

    if anomaly is None:
        return AnomalyAssessment(
            scope=target,
            verdict=AnomalyVerdict.ASSESSED,
            detail=(
                f"Spend over the last {OBSERVATION_DAYS} day(s) is consistent with the "
                f"{len(baseline_points)}-day baseline of "
                f"{baseline_level.quantize(_CENTS)} {series.currency.value} per day, "
                f"after allowing for the weekly pattern. No level shift or spike."
            ),
            days_observed=observed_count,
            days_missing=len(gaps),
        )

    return AnomalyAssessment(
        scope=target,
        verdict=AnomalyVerdict.ASSESSED,
        detail=anomaly.statement(),
        anomalies=(anomaly,),
        days_observed=observed_count,
        days_missing=len(gaps),
    )


def _evidence(
    *,
    series: CostSeries,
    baseline_level: Decimal,
    baseline_days: int,
    profile: dict[int, Decimal],
    spread: Decimal,
    score: Decimal | None,
    threshold: Decimal,
) -> list[Observation]:
    """The numbers behind a conclusion, as citable facts.

    Includes the weekday profile, because a client asking "isn't that just our Sunday
    batch?" should be able to see that the Sunday batch was already accounted for.
    """
    source = series.provenance
    found = [
        _observation("baseline_daily_spend", str(baseline_level.quantize(_CENTS)), source),
        _observation("baseline_days", str(baseline_days), source),
        _observation("observation_days", str(OBSERVATION_DAYS), source),
        _observation("residual_mad", str(spread.quantize(_PLACES)), source),
        _observation(
            "deviation_score",
            "unavailable (flat baseline)" if score is None else str(score),
            source,
        ),
        _observation("score_threshold", str(threshold), source),
        _observation("minimum_relative_change", f"{MIN_RELATIVE_CHANGE:.0%}", source),
        _observation("method", "modified z-score over median/MAD, weekday-adjusted", source),
    ]
    found += [
        _observation(
            f"weekday_median.{_WEEKDAY_NAMES[weekday]}",
            str(level.quantize(_CENTS)),
            source,
        )
        for weekday, level in sorted(profile.items())
    ]
    return found


_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _level_shift(  # noqa: PLR0913 -- every argument is a number the conclusion rests on
    *,
    series: CostSeries,
    client: str,
    scope: str,
    service: str | None,
    observed_points: Sequence[CostPoint],
    observed_residuals: Sequence[Decimal],
    centre: Decimal,
    spread: Decimal,
    baseline_level: Decimal,
    baseline_days: int,
    profile: dict[int, Decimal],
) -> CostAnomaly | None:
    """Has the baseline itself moved and stayed moved?

    Asked first, and reported in preference to a spike. A week whose median has moved
    is a different and more important finding than any one of the days inside it, and
    reporting both would tell a client the same thing twice.
    """
    observed_median = median(list(observed_residuals))
    delta = observed_median - centre
    score = robust_score(observed_median, centre=centre, deviation=spread)
    observed_level = median([point.amount for point in observed_points])

    statistically = score is None or abs(score) >= LEVEL_SHIFT_SCORE
    if not statistically or not _is_material(delta, baseline_level):
        return None

    return _anomaly(
        series=series,
        client=client,
        scope=scope,
        service=service,
        kind=AnomalyKind.LEVEL_SHIFT,
        observed_from=observed_points[0].day,
        observed_to=observed_points[-1].day,
        baseline_level=baseline_level,
        observed_level=observed_level,
        delta=delta,
        score=score,
        threshold=LEVEL_SHIFT_SCORE,
        spread=spread,
        baseline_days=baseline_days,
        profile=profile,
        confidence=_CONFIDENCE_LEVEL_SHIFT,
        detector="spend_level_shift/v1",
    )


def _spike(  # noqa: PLR0913 -- every argument is a number the conclusion rests on
    *,
    series: CostSeries,
    client: str,
    scope: str,
    service: str | None,
    observed_points: Sequence[CostPoint],
    observed_residuals: Sequence[Decimal],
    centre: Decimal,
    spread: Decimal,
    baseline_level: Decimal,
    baseline_days: int,
    profile: dict[int, Decimal],
) -> CostAnomaly | None:
    """Did one day stand far outside normal variation?

    The worst single day in the observation window, which is the one a client would
    want named. Asked only after the level-shift question has come back negative, so a
    spike is always "a day, against a baseline that has not moved".
    """
    worst = max(
        zip(observed_points, observed_residuals, strict=True),
        key=lambda pair: abs(pair[1] - centre),
    )
    point, residual = worst
    delta = residual - centre
    score = robust_score(residual, centre=centre, deviation=spread)

    statistically = score is None or abs(score) >= SPIKE_SCORE
    if not statistically or not _is_material(delta, baseline_level):
        return None

    return _anomaly(
        series=series,
        client=client,
        scope=scope,
        service=service,
        kind=AnomalyKind.SPIKE,
        observed_from=point.day,
        observed_to=point.day,
        baseline_level=baseline_level,
        observed_level=point.amount,
        delta=delta,
        score=score,
        threshold=SPIKE_SCORE,
        spread=spread,
        baseline_days=baseline_days,
        profile=profile,
        confidence=_CONFIDENCE_SPIKE,
        detector="spend_spike/v1",
    )


def _anomaly(  # noqa: PLR0913 -- a constructor over the facts of one conclusion
    *,
    series: CostSeries,
    client: str,
    scope: str,
    service: str | None,
    kind: AnomalyKind,
    observed_from: date,
    observed_to: date,
    baseline_level: Decimal,
    observed_level: Decimal,
    delta: Decimal,
    score: Decimal | None,
    threshold: Decimal,
    spread: Decimal,
    baseline_days: int,
    profile: dict[int, Decimal],
    confidence: Decimal,
    detector: str,
) -> CostAnomaly:
    return CostAnomaly(
        anomaly_id=anomaly_id_for(scope, kind, observed_from),
        client=client,
        subscription_id=series.subscription_id,
        service=service,
        kind=kind,
        direction=_direction(delta),
        baseline=_money(
            baseline_level,
            series=series,
            basis=f"median daily spend over the {baseline_days}-day baseline",
        ),
        observed=_money(
            observed_level,
            series=series,
            basis=(
                f"spend on {observed_from.isoformat()}"
                if kind is AnomalyKind.SPIKE
                else (
                    f"median daily spend over {observed_from.isoformat()} to "
                    f"{observed_to.isoformat()}"
                )
            ),
        ),
        # The magnitude, because ``MoneyAmount`` refuses a negative -- an amount of
        # money is not a signed quantity, and ``direction`` already carries the sign.
        # A decrease reads as "fell by 60.00", not "rose by -60.00".
        delta=_money(
            abs(delta),
            series=series,
            basis=(
                "absolute difference between observed and baseline, after removing "
                "the weekday pattern"
            ),
        ),
        observed_from=observed_from,
        observed_to=observed_to,
        window_days=(observed_to - observed_from).days + 1,
        baseline_days=baseline_days,
        # Absolute: a decrease is as far from normal as an increase of the same size,
        # and `direction` already carries the sign.
        deviation_score=abs(score) if score is not None else Decimal(0),
        threshold_score=threshold,
        currency=series.currency,
        confidence=confidence,
        detector=detector,
        evidence=tuple(
            _evidence(
                series=series,
                baseline_level=baseline_level,
                baseline_days=baseline_days,
                profile=profile,
                spread=spread,
                score=score,
                threshold=threshold,
            )
        ),
        detected_at=series.provenance.retrieved_at,
        provenance=_detector_provenance(detector, series),
    )


def assess_estate(series: Sequence[CostSeries], *, client: str) -> tuple[AnomalyAssessment, ...]:
    """Assess every subscription's series, in a deterministic order.

    One assessment per series, including the clean ones. Sorted by scope so a
    checkpoint resume and a re-run agree, exactly as the waste signals are.
    """
    assessments = [assess_series(one, client=client) for one in series]
    return tuple(sorted(assessments, key=lambda assessment: assessment.scope))
