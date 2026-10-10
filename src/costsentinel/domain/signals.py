"""Detection output: waste signals and cost anomalies.

Signals are produced by deterministic detectors, never by a language model
(ARCHITECTURE.md D1). Each one carries the evidence that produced it, so the
Root-Cause Analyst and the report can cite observations rather than restate
conclusions.
"""

from __future__ import annotations

from datetime import datetime
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


class CostAnomaly(Frozen):
    """A statistically significant deviation in spend for a scope.

    Modelled in Phase 1 so the state, report and eval schemas are stable; the
    detector that emits these arrives in Phase 3.
    """

    anomaly_id: str
    client: str
    subscription_id: str
    service: str | None = None
    direction: AnomalyDirection
    baseline: MoneyAmount
    observed: MoneyAmount
    delta: MoneyAmount
    window_days: int = Field(ge=1)
    currency: Currency = Currency.USD
    confidence: Confidence
    detector: str
    detected_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    @property
    def delta_is_known(self) -> bool:
        """Whether the size of the deviation was determined."""
        return self.delta.is_known

    @property
    def delta_fraction(self) -> Decimal | None:
        """Deviation as a fraction of baseline, or ``None`` if either side is unknown."""
        if self.baseline.amount is None or self.delta.amount is None:
            return None
        if self.baseline.amount == 0:
            return None
        return self.delta.amount / self.baseline.amount
