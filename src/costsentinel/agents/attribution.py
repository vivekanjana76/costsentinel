"""Deterministic attribution of a spend change. No language model is imported here.

Like :mod:`costsentinel.agents.anomalies` and :mod:`costsentinel.agents.savings`, the
property is checkable in one glance at the imports: *what* accounted for a change in
spend is arithmetic over a provider-reported breakdown, and there is no code path by
which a model could produce it. A test asserts the absent import rather than trusting
the convention.

Phase 7 answered "did spend change?" and named a scope. This answers the obvious next
question -- "what changed?" -- and the answer still routes to an investigation, never
to a remediation. Knowing that the SQL vCore meter rose is a better place to start
looking; it is not an instruction to resize anything.

The method
----------

Two windows and a subtraction, which is as simple as it can honestly be:

1. the **observed** window is the anomaly's own, which the detector already decided;
2. the **baseline** window is the days immediately before it, up to
   :data:`BASELINE_DAYS`;
3. each component's mean daily spend is computed over each window, and its
   *contribution* is the difference;
4. a component is a *candidate cause* only if its share of the change exceeds its
   share of the baseline spend by :data:`MIN_DISPROPORTION`;
5. candidates are ranked by contribution, and the leading set -- at most
   :data:`MAX_NAMED_COMPONENTS` of them -- is named if together they account for
   :data:`MIN_EXPLAINED_SHARE` of the whole change.

Step 4 is the one that matters, and it was added after running the detector over the
committed estate rather than reasoned out in advance. Without it, "which components
account for most of the change" passes on a change that is spread perfectly evenly: in
a 40/25/20/10/5 meter mix the top two always sum to 65%, which clears any threshold low
enough to be useful. The arithmetic was right and the answer was wrong.

Disproportion fixes it because of what each case looks like arithmetically. In a
proportional rise every component's share of the change equals its share of the
baseline, so every lift is zero and nothing is a candidate. In a concentrated rise the
culprit's share of the change (100%) dwarfs its share of the baseline (20%). That gap
is the difference between "the SQL meter caused this" and "your biggest meter is your
biggest meter".

Means rather than medians, which is a deliberate departure from the detector. The
detector needs a robust *level* and must not let the event it is looking for define
normal. Attribution needs the opposite: the total change decomposed exactly, and a sum
of medians does not equal the median of sums, so a median-based split would not add up
to the change it claims to explain. The reconciliation check below is only possible
with means.

Why unknown stays unknown
-------------------------

Four ways this can honestly fail, and each is a different thing to tell a client
(:class:`~costsentinel.domain.signals.UnattributedReason`). The one worth dwelling on
is **diffuse**: when every component rises together, the largest one is still the
largest, and a detector that simply named it would be right about the arithmetic and
wrong about the world. "Your biggest meter is your biggest meter" is not a cause. So a
leading set that does not clear the share threshold produces no attribution at all,
and says that the change is spread -- which is itself an actionable finding, pointing
at something affecting the whole environment rather than at one resource.

The remaining three are a missing breakdown (a gap in what we retrieved), a breakdown
that does not sum to its own total (a fault in the data, where naming a component would
be a guess dressed as an answer), and too little history on one side to compare
against.

Why the thresholds are what they are
------------------------------------

:data:`MIN_EXPLAINED_SHARE` is 0.60 and :data:`MAX_NAMED_COMPONENTS` is 3. Both are
product judgements rather than derived quantities, and they are stated as such: a
client acting on "mostly these two meters" wants the list short enough to investigate
and the claim strong enough to be worth investigating. Phase 9b calibrates them
against real changes, which is the only thing that could.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import CostBreakdown, CostSeries
from costsentinel.domain.signals import (
    AnomalyDirection,
    AttributedComponent,
    AttributionOutcome,
    CostAnomaly,
    CostAttribution,
    UnattributedReason,
)

#: Detector name recorded on every attribution, for the audit trail and the report.
DETECTOR = "spend_attribution/v1"

#: How many days before the change to use as the comparison window. The same figure
#: the anomaly detector uses for its own baseline, so an attribution describes the same
#: "before" a client has already read in the alert.
BASELINE_DAYS = 14

#: The least history on either side that supports a comparison. Two days of "before"
#: can differ from each other by more than the change being explained.
MIN_WINDOW_DAYS = 3

#: How much of the whole change a named set of components must account for. A product
#: judgement: below this the change is spread, and naming its largest component would
#: be true arithmetic and a false cause.
MIN_EXPLAINED_SHARE = Decimal("0.60")

#: How many components may be named. A client investigating four meters at once is
#: not investigating.
MAX_NAMED_COMPONENTS = 3

#: How far a component's share of the change must exceed its share of the baseline
#: before it counts as a cause, in share points. The test that distinguishes a cause
#: from a component that merely rose along with everything else -- see the module
#: docstring, where the committed diffuse subscription is the case that forced it.
#:
#: A product judgement at 0.10, like the two above, and the one most worth calibrating
#: against real changes in Phase 9b: too low and a proportional rise acquires a cause,
#: too high and a real culprit sharing its rise with a second meter loses one.
MIN_DISPROPORTION = Decimal("0.10")

#: Changes below this per-day magnitude are treated as no change at all when working
#: out shares, so a near-zero denominator cannot manufacture a 900% contribution.
MIN_TOTAL_CHANGE = Decimal("0.05")

_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Round to cents, half up, like every other money figure in the system."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def days_reported(series: CostSeries, start: date, end: date) -> int:
    """How many days the *series* reports in an inclusive range.

    The denominator for every component's mean, including components that reported
    nothing on some of those days. Within a reconciling breakdown that is the right
    reading: the day itself was measured, and a component with no point on a measured
    day spent nothing on it. That is a different claim from a gap in the total, which
    is unknown and not zero -- and the difference is exactly that one has a measurement
    to be absent from.
    """
    return sum(1 for point in series.points if start <= point.day <= end)


def baseline_window(anomaly: CostAnomaly) -> tuple[date, date]:
    """The days immediately before the change, up to :data:`BASELINE_DAYS` of them."""
    end = anomaly.observed_from - timedelta(days=1)
    return end - timedelta(days=BASELINE_DAYS - 1), end


def _mean_daily(component_total: Decimal, days: int) -> Decimal | None:
    """Mean daily spend, or ``None`` when there were no days to average over."""
    if days <= 0:
        return None
    return _money(component_total / Decimal(days))


def _attribution_provenance(
    anomaly: CostAnomaly, series: CostSeries, breakdown: CostBreakdown | None
) -> Provenance:
    """Where an attribution came from, dated by the data rather than the clock.

    ``retrieved_at`` is inherited from the breakdown it was computed over, falling back
    to the series, exactly as the anomaly detector's own provenance is. Two reasons, and
    the second is the one that caught this: an attribution is as fresh as the data
    behind it and not as fresh as the moment it was computed -- and a wall-clock stamp
    makes the result differ between two runs over identical inputs, which breaks the
    determinism a checkpoint resume and a committed scorecard both rely on.
    """
    upstream = breakdown.provenance if breakdown is not None else series.provenance
    dimension = breakdown.dimension.value if breakdown is not None else "no"
    return Provenance(
        source=ProvenanceSource.CALCULATION,
        retrieved_at=upstream.retrieved_at,
        reference=(
            f"{DETECTOR} over {dimension} breakdown for {anomaly.subscription_id} "
            f"({upstream.source.value})"
        ),
        verification=Verification.VERIFIED,
    )


def attribute(
    anomaly: CostAnomaly,
    series: CostSeries,
    breakdown: CostBreakdown | None,
    *,
    provenance: Provenance | None = None,
) -> CostAttribution:
    """What accounted for one anomaly, or why we cannot say.

    Args:
        anomaly: The change to explain. Its own observed window is used.
        series: The total series the anomaly was detected in, which supplies the day
            counts and the reconciliation target.
        breakdown: The same window split along a dimension, or ``None`` when the
            provider could not retrieve one.
        provenance: Where this attribution came from. Defaults to a calculation over
            the breakdown's own provenance, so the chain back to the provider survives.

    Returns:
        A :class:`CostAttribution`. Always one: an anomaly with no attributable cause
        gets an explicit unattributed outcome with a reason, never a missing field that
        a reader could take for "nothing changed".
    """
    source = provenance or _attribution_provenance(anomaly, series, breakdown)

    def unattributed(reason: UnattributedReason, considered: int = 0) -> CostAttribution:
        return CostAttribution(
            anomaly_id=anomaly.anomaly_id,
            scope=anomaly.subscription_id,
            dimension=breakdown.dimension if breakdown else None,
            outcome=AttributionOutcome.UNATTRIBUTED,
            reason=reason,
            considered=considered,
            detector=DETECTOR,
            provenance=source,
        )

    if breakdown is None or not breakdown.components:
        return unattributed(UnattributedReason.NO_BREAKDOWN)

    if not breakdown.reconciles_with(series):
        return unattributed(UnattributedReason.DOES_NOT_RECONCILE, len(breakdown.components))

    base_from, base_to = baseline_window(anomaly)
    base_days = days_reported(series, base_from, base_to)
    observed_days = days_reported(series, anomaly.observed_from, anomaly.observed_to)
    if base_days < MIN_WINDOW_DAYS or observed_days < 1:
        return unattributed(UnattributedReason.INSUFFICIENT_HISTORY, len(breakdown.components))

    # One pass: each component's two means and the difference between them.
    measured: list[tuple[str, str, Decimal, Decimal, Decimal]] = []
    for component in breakdown.components:
        before = _mean_daily(component.total_between(base_from, base_to), base_days)
        during = _mean_daily(
            component.total_between(anomaly.observed_from, anomaly.observed_to), observed_days
        )
        if before is None or during is None:  # pragma: no cover -- day counts checked above
            continue
        measured.append((component.key, component.label, before, during, during - before))

    total_change = sum((change for *_, change in measured), start=Decimal(0))
    if abs(total_change) < MIN_TOTAL_CHANGE:
        return unattributed(UnattributedReason.DIFFUSE, len(measured))

    # A component moving the other way is not a candidate cause of a rise, however
    # large it is.
    aligned = [one for one in measured if _signum(one[4]) == _signum(total_change)]

    # The disproportion test. A component whose share of the change is no larger than
    # its share of the spend rose along with everything else, which is not a cause.
    total_baseline = sum((before for _, _, before, _, _ in measured), start=Decimal(0))
    candidates = [
        one
        for one in aligned
        if total_baseline <= 0
        or abs(one[4] / total_change) - (one[2] / total_baseline) >= MIN_DISPROPORTION
    ]
    ranked = sorted(candidates, key=lambda one: (-abs(one[4]), one[0]))

    named: list[AttributedComponent] = []
    explained = Decimal(0)
    for key, label, before, during, change in ranked[:MAX_NAMED_COMPONENTS]:
        named.append(
            AttributedComponent(
                key=key,
                label=label,
                baseline=MoneyAmount.of(before, currency=breakdown.currency, provenance=source),
                observed=MoneyAmount.of(during, currency=breakdown.currency, provenance=source),
                contribution=MoneyAmount.of(
                    abs(change), currency=breakdown.currency, provenance=source
                ),
                direction=(AnomalyDirection.INCREASE if change > 0 else AnomalyDirection.DECREASE),
                share=abs(change / total_change),
            )
        )
        explained += abs(change / total_change)
        if explained >= MIN_EXPLAINED_SHARE:
            break

    if explained < MIN_EXPLAINED_SHARE:
        return unattributed(UnattributedReason.DIFFUSE, len(measured))

    return CostAttribution(
        anomaly_id=anomaly.anomaly_id,
        scope=anomaly.subscription_id,
        dimension=breakdown.dimension,
        outcome=AttributionOutcome.ATTRIBUTED,
        components=tuple(named),
        considered=len(measured),
        detector=DETECTOR,
        provenance=source,
    )


def _signum(value: Decimal) -> int:
    """-1, 0 or 1. Used to keep a falling component out of a rise's cause list."""
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def attribute_all(
    anomalies: Sequence[CostAnomaly],
    series: Sequence[CostSeries],
    breakdowns: Sequence[CostBreakdown],
) -> tuple[CostAttribution, ...]:
    """Attribute every anomaly, in the order the anomalies were given.

    An anomaly whose scope has no series is skipped rather than attributed from
    nothing: without the total there is no reconciliation target, and a breakdown
    nothing checks is the case this module refuses to reason from.
    """
    by_scope = {one.subscription_id: one for one in series}
    parts = {one.subscription_id: one for one in breakdowns}
    attributions: list[CostAttribution] = []
    for anomaly in anomalies:
        total = by_scope.get(anomaly.subscription_id)
        if total is None:
            continue
        attributions.append(attribute(anomaly, total, parts.get(anomaly.subscription_id)))
    return tuple(attributions)
