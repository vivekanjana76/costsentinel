"""Detection output: waste signals and cost anomalies.

Signals are produced by deterministic detectors, never by a language model
(ARCHITECTURE.md D1). Each one carries the evidence that produced it, so the
Root-Cause Analyst and the report can cite observations rather than restate
conclusions.

**A waste signal and a cost anomaly are different claims and deliberately different
types.** A :class:`WasteSignal` says *this resource is wasteful -- act*: it names a
resource, carries a monthly cost, and a remediation can be proposed for it. A
:class:`CostAnomaly` says *spend changed -- investigate*: it names a scope rather
than a wasteful resource, carries no saving, and has nothing to remediate. Keeping
them apart in the type system is what stops the second quietly acquiring the first's
execution path (ARCHITECTURE.md D106).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import (
    Confidence,
    Currency,
    Frozen,
    MoneyAmount,
    Provenance,
    utc_now,
)


class WasteKind(StrEnum):
    """The categories of waste CostSentinel detects.

    Phase 2 implements eight of these. The remainder are declared here because the
    report, the policy store and the eval dataset schema all key off this enum, and
    adding a member later should not be a breaking change for them.
    """

    ORPHANED_MANAGED_DISK = "orphaned_managed_disk"
    UNATTACHED_PUBLIC_IP = "unattached_public_ip"
    STALE_SNAPSHOT = "stale_snapshot"
    IDLE_VIRTUAL_MACHINE = "idle_virtual_machine"
    OVERSIZED_VIRTUAL_MACHINE = "oversized_virtual_machine"
    STALE_NON_PRODUCTION_RESOURCE = "stale_non_production_resource"
    UNUSED_RESERVATION = "unused_reservation"
    IDLE_SQL_DATABASE = "idle_sql_database"
    OVERSIZED_APP_SERVICE_PLAN = "oversized_app_service_plan"
    UNEXPECTED_EGRESS = "unexpected_egress"


class Observation(Frozen):
    """One piece of evidence behind a signal.

    Deliberately a name/value pair with its own provenance rather than free prose:
    evidence is data that can be rendered, cited and asserted on in an eval, and a
    resource name inside it is never in an instruction position.
    """

    name: str
    value: str
    provenance: Provenance


class WasteSignal(Frozen):
    """A detected instance of waste on a specific resource."""

    signal_id: str
    kind: WasteKind
    client: str
    subscription_id: str
    resource_id: str
    resource_name: str
    resource_kind: str
    evidence: tuple[Observation, ...]
    monthly_cost: MoneyAmount
    confidence: Confidence
    detector: str = Field(description="Identifier of the deterministic rule that fired.")
    detected_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    def evidence_value(self, name: str) -> str | None:
        """Look up one observation's value by name, or ``None``."""
        return next((o.value for o in self.evidence if o.name == name), None)


class AnomalyDirection(StrEnum):
    """Whether observed spend rose or fell against its baseline."""

    INCREASE = "increase"
    DECREASE = "decrease"


class AnomalyKind(StrEnum):
    """What shape the deviation has.

    Two shapes, and they are different findings rather than two sizes of the same
    one. A **spike** is one or a few days far above the baseline which then reverts:
    something ran. A **level shift** is the baseline itself having moved and stayed
    moved: something changed. Conflating them produces an alert a client cannot act
    on, because "look at last Tuesday" and "your monthly run rate is now higher" lead
    to completely different investigations.
    """

    SPIKE = "spike"
    LEVEL_SHIFT = "level_shift"


class AnomalyVerdict(StrEnum):
    """The outcome of asking whether a series contains an anomaly.

    ``ASSESSED`` is the only verdict that can carry anomalies. The other two are
    first-class rather than an empty result, because "no anomaly found" and "we could
    not tell" are different things to put in front of a client -- and because the
    second is what a gap in the data must produce. A missing day is missing data; read
    as a zero it would look like a spend collapse, which would be the worst false
    positive this detector could produce.
    """

    ASSESSED = "assessed"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPLETE_SERIES = "incomplete_series"

    @property
    def is_conclusive(self) -> bool:
        """Whether the series was actually assessed."""
        return self is AnomalyVerdict.ASSESSED


class CostAnomaly(Frozen):
    """A statistically significant deviation in spend for a scope.

    Modelled in Phase 1 so the state, report and eval schemas were stable; emitted
    from Phase 7.

    Deliberately carries no saving and no action. An anomaly is a change in spend, not
    a recoverable amount: ``delta`` is what the client is now paying differently, and
    presenting it as a saving would promise money nobody has identified a way to
    recover. There is no ``ActionType`` field and no ``ActionClass`` field either,
    which is what makes "an anomaly can never become an automatable action"
    structural rather than a convention (ARCHITECTURE.md D106).

    ``delta`` is a **magnitude**, and ``direction`` carries the sign.
    :class:`~costsentinel.domain.common.MoneyAmount` refuses a negative amount, which
    is the right constraint -- an amount of money is not a signed quantity -- and it
    reads better anyway: "fell by 60.00" rather than "rose by -60.00".
    """

    anomaly_id: str
    client: str
    subscription_id: str
    service: str | None = None
    kind: AnomalyKind
    direction: AnomalyDirection
    baseline: MoneyAmount
    observed: MoneyAmount
    delta: MoneyAmount
    observed_from: date
    observed_to: date
    window_days: int = Field(ge=1)
    baseline_days: int = Field(ge=1, description="Days of history the baseline was taken from.")
    deviation_score: Decimal = Field(
        ge=Decimal(0),
        description=(
            "How far the observation sits from the baseline, in robust standard "
            "deviations. Stated so a client can check the conclusion rather than "
            "accept it."
        ),
    )
    threshold_score: Decimal = Field(
        ge=Decimal(0), description="The score this deviation had to beat to be reported."
    )
    currency: Currency = Currency.USD
    confidence: Confidence
    detector: str
    evidence: tuple[Observation, ...] = ()
    detected_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    @property
    def delta_is_known(self) -> bool:
        """Whether the size of the deviation was determined."""
        return self.delta.is_known

    @property
    def delta_fraction(self) -> Decimal | None:
        """Deviation as a fraction of baseline, or ``None`` if either side is unknown.

        A magnitude, like ``delta`` itself. ``None`` rather than infinity against a
        zero baseline: spend appearing where there was none is a real event with no
        meaningful percentage attached to it.
        """
        if self.baseline.amount is None or self.delta.amount is None:
            return None
        if self.baseline.amount == 0:
            return None
        return self.delta.amount / self.baseline.amount

    def statement(self) -> str:
        """The finding in plain numbers, composed here rather than by a model.

        Every quantity a client needs in order to disagree: the baseline, the observed
        value, the difference, the window it was measured over and how far outside
        normal variation it sits.
        """
        shape = "rose" if self.direction is AnomalyDirection.INCREASE else "fell"
        share = (
            f" ({self.delta_fraction * Decimal(100):.1f}% of baseline)"
            if self.delta_fraction is not None
            else ""
        )
        window = (
            f"on {self.observed_from.isoformat()}"
            if self.observed_from == self.observed_to
            else f"over {self.observed_from.isoformat()} to {self.observed_to.isoformat()}"
        )
        return (
            f"Daily spend {shape} from a baseline of {self.baseline.display()} to "
            f"{self.observed.display()} {window}, a difference of "
            f"{self.delta.display()}{share}. That is "
            f"{self.deviation_score:.1f} robust standard deviations from the "
            f"{self.baseline_days}-day baseline, against a threshold of "
            f"{self.threshold_score:.1f}."
        )


class AnomalyAssessment(Frozen):
    """The result of asking one series whether it contains an anomaly.

    An assessment is produced for every scope examined, including the ones with
    nothing to report, so "we looked at this subscription and its spend is steady" is
    a recorded fact rather than an absence. That matters most in the inconclusive
    cases: a client whose series had nine days missing should be told so, not shown a
    clean report.
    """

    scope: str = Field(description="What was assessed: a subscription id, or a resource id.")
    verdict: AnomalyVerdict
    detail: str = Field(
        min_length=1,
        description="Why this verdict, in words. Mandatory, including when nothing was found.",
    )
    anomalies: tuple[CostAnomaly, ...] = ()
    days_observed: int = Field(default=0, ge=0)
    days_missing: int = Field(
        default=0,
        ge=0,
        description=(
            "Days inside the series' own date range with no data point. Counted and "
            "reported rather than filled with zeros."
        ),
    )

    @property
    def is_clean(self) -> bool:
        """Whether the series was assessed and nothing was found."""
        return self.verdict.is_conclusive and not self.anomalies
