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

from pydantic import Field, model_validator

from costsentinel.domain.common import (
    Confidence,
    Currency,
    Frozen,
    MoneyAmount,
    Provenance,
    utc_now,
)
from costsentinel.domain.estate import CostDimension


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


class AttributionOutcome(StrEnum):
    """Whether a spend change could be traced to something.

    Two values and no third. "Partially attributed" was considered and rejected: a
    client reading one would have to decide for themselves whether the named component
    was the cause, which is the judgement the attribution exists to make.
    """

    ATTRIBUTED = "attributed"
    UNATTRIBUTED = "unattributed"


class UnattributedReason(StrEnum):
    """Why a change could not be traced. Each is a different thing to tell a client.

    The distinction matters operationally. ``NO_BREAKDOWN`` is a gap in what we
    retrieved and may close tomorrow. ``DIFFUSE`` is a real finding about the spend --
    nothing single caused it -- and will not change however good the data gets.
    ``DOES_NOT_RECONCILE`` is a fault in the data itself, and reporting a cause from it
    would be worse than reporting none.
    """

    #: No breakdown was retrievable for this scope.
    NO_BREAKDOWN = "no_breakdown"

    #: The components did not sum to the total they claim to explain.
    DOES_NOT_RECONCILE = "does_not_reconcile"

    #: The change is spread across components; none accounts for enough of it.
    DIFFUSE = "diffuse"

    #: Not enough days on one side of the change to compare against.
    INSUFFICIENT_HISTORY = "insufficient_history"


class AttributedComponent(Frozen):
    """One component's part in a spend change, in plain numbers.

    Everything a client needs in order to check the claim themselves: what the
    component cost per day before, what it costs per day now, the difference, and how
    much of the whole change that difference accounts for.
    """

    key: str
    label: str
    baseline: MoneyAmount = Field(description="This component's mean daily spend before.")
    observed: MoneyAmount = Field(description="This component's mean daily spend during.")
    contribution: MoneyAmount = Field(
        description=(
            "Magnitude of this component's change, per day. A magnitude because "
            "MoneyAmount refuses a negative; `direction` carries the sign, exactly as "
            "CostAnomaly.delta does."
        )
    )
    direction: AnomalyDirection
    share: Decimal = Field(
        ge=Decimal(0),
        description=(
            "Fraction of the whole change this component accounts for. May exceed 1 "
            "when another component moved the other way, which is a real pattern and "
            "not an error -- one meter rising 100 while another falls 20 is a net 80."
        ),
    )


class CostAttribution(Frozen):
    """What accounted for a spend change, or why we cannot say.

    Deterministic arithmetic over a :class:`~costsentinel.domain.estate.CostBreakdown`.
    A model may explain an attribution in prose and cannot choose the component, set a
    figure or change the outcome -- the same separation that keeps an anomaly from
    acquiring an action (ARCHITECTURE.md D106).

    Carries no action, no action class and no saving, for the same reason its anomaly
    does not: knowing which meter rose is a better investigation, not a remediation.
    """

    anomaly_id: str
    scope: str
    dimension: CostDimension | None = Field(
        default=None,
        description="What the spend was broken down by. ``None`` when there was no breakdown.",
    )
    outcome: AttributionOutcome
    components: tuple[AttributedComponent, ...] = Field(
        default=(),
        description=(
            "The components that account for the change, largest first. Empty when "
            "unattributed -- deliberately, because a ranked list of components that "
            "explain nothing in particular reads as an answer."
        ),
    )
    reason: UnattributedReason | None = Field(
        default=None, description="Why not, when unattributed. ``None`` when attributed."
    )
    considered: int = Field(
        default=0, ge=0, description="How many components were examined, attributed or not."
    )
    detector: str
    provenance: Provenance

    @model_validator(mode="after")
    def _outcome_and_evidence_agree(self) -> CostAttribution:
        """An outcome must match what it carries.

        An attributed outcome with no components, or an unattributed one with no
        reason, would each be a claim the type cannot support. Refused here rather than
        guarded at every read site.
        """
        if self.outcome is AttributionOutcome.ATTRIBUTED:
            if not self.components:
                msg = "an attributed outcome must name at least one component"
                raise ValueError(msg)
            if self.reason is not None:
                msg = "an attributed outcome cannot also carry a reason for not attributing"
                raise ValueError(msg)
        else:
            if self.reason is None:
                msg = "an unattributed outcome must say why"
                raise ValueError(msg)
            if self.components:
                msg = (
                    "an unattributed outcome must name no components: a ranked list "
                    "that explains nothing in particular reads as an answer"
                )
                raise ValueError(msg)
        return self

    @property
    def is_attributed(self) -> bool:
        """Whether a cause was named."""
        return self.outcome is AttributionOutcome.ATTRIBUTED

    def statement(self) -> str:
        """The attribution in plain numbers, composed here rather than by a model."""
        if not self.is_attributed:
            return _UNATTRIBUTED_STATEMENTS[self.reason or UnattributedReason.NO_BREAKDOWN]
        parts: list[str] = []
        for part in self.components:
            moved = "up" if part.direction is AnomalyDirection.INCREASE else "down"
            parts.append(
                f"{part.label} ({part.baseline.display()} to {part.observed.display()} "
                f"per day, {moved} {part.contribution.display()}, "
                f"{part.share * Decimal(100):.0f}% of the change)"
            )
        named = "; ".join(parts)
        lead = "Attributed to" if len(parts) == 1 else "Attributed mostly to"
        return f"{lead} {named}."


#: What to tell a client when nothing could be named. Written out here rather than
#: composed, because each of these is a different thing to say and a generic "cause
#: unknown" would collapse a real finding (diffuse) into a data gap (no breakdown).
_UNATTRIBUTED_STATEMENTS: dict[UnattributedReason, str] = {
    UnattributedReason.NO_BREAKDOWN: (
        "No cost breakdown was available for this scope, so what changed cannot be "
        "identified from the data we have. This is a gap in what we retrieved, not a "
        "finding that the change had no cause."
    ),
    UnattributedReason.DOES_NOT_RECONCILE: (
        "The cost breakdown for this scope does not sum to the total it should explain, "
        "so no component can be named from it. Naming one would be a guess dressed as "
        "an answer."
    ),
    UnattributedReason.DIFFUSE: (
        "The change is spread across the meters rather than concentrated in one, so no "
        "single component accounts for it. That is itself a finding: look for something "
        "affecting everything at once -- a traffic change, a price change, or a change "
        "in how the whole environment is used -- rather than for one resource."
    ),
    UnattributedReason.INSUFFICIENT_HISTORY: (
        "There is not enough history on one side of this change to compare against, so "
        "no component can be named yet."
    ),
}


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
