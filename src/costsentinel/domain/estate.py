"""The estate: subscriptions, resources, cost series, metrics and SKU prices.

These are the facts a provider supplies. Nothing in this module is derived by
reasoning -- it is the ground truth that everything downstream must trace back to.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import (
    Currency,
    Frozen,
    MoneyAmount,
    Percentage,
    Provenance,
    utc_now,
)


class Environment(StrEnum):
    """The lifecycle environment a subscription or resource belongs to.

    Drives policy: a change that is ``review`` in development may be ``block`` in a
    government production subscription.
    """

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    TEST = "test"
    SANDBOX = "sandbox"
    UNKNOWN = "unknown"


class ResourceKind(StrEnum):
    """The kinds of Azure resource CostSentinel reasons about."""

    VIRTUAL_MACHINE = "virtual_machine"
    MANAGED_DISK = "managed_disk"
    PUBLIC_IP = "public_ip"
    NETWORK_INTERFACE = "network_interface"
    STORAGE_ACCOUNT = "storage_account"
    SQL_DATABASE = "sql_database"
    APP_SERVICE_PLAN = "app_service_plan"


class ResourceState(StrEnum):
    """The operational state of a resource, as reported by the provider."""

    RUNNING = "running"
    STOPPED = "stopped"
    DEALLOCATED = "deallocated"
    ATTACHED = "attached"
    UNATTACHED = "unattached"
    AVAILABLE = "available"
    UNKNOWN = "unknown"


class Subscription(Frozen):
    """An Azure subscription in scope for a scan.

    ``client`` is mandatory and is the multi-tenancy boundary: there is no code path
    in CostSentinel where a query, a memory retrieval or a report spans clients
    (ARCHITECTURE.md D12).
    """

    subscription_id: str
    display_name: str
    client: str
    environment: Environment = Environment.UNKNOWN
    tags: Mapping[str, str] = Field(default_factory=dict)
    provenance: Provenance


class Resource(Frozen):
    """A single billable resource.

    ``attached_to`` is the resource id this resource is attached to, or ``None`` when
    it is standing alone. For a disk or a public IP, ``None`` is precisely the
    orphan condition the detectors look for -- which is why it is a first-class
    field rather than something inferred from a tag.
    """

    resource_id: str
    name: str
    kind: ResourceKind
    subscription_id: str
    resource_group: str
    region: str
    sku: str | None = None
    state: ResourceState = ResourceState.UNKNOWN
    attached_to: str | None = None
    tags: Mapping[str, str] = Field(default_factory=dict)
    created_at: datetime | None = None
    monthly_cost: MoneyAmount
    provenance: Provenance


class CostPoint(Frozen):
    """One day of actual spend."""

    day: date
    amount: Decimal = Field(ge=Decimal(0))


class CostSeries(Frozen):
    """A daily actual-cost series for one subscription over an observation window."""

    subscription_id: str
    currency: Currency = Currency.USD
    points: tuple[CostPoint, ...]
    provenance: Provenance

    @property
    def window_days(self) -> int:
        """Number of days covered by the series."""
        return len(self.points)

    def observed_spend(self) -> MoneyAmount:
        """Total actual spend over the window, carrying the series' own provenance.

        This is a sum of provider-reported dailies, so it is a calculation over
        verified inputs -- never an estimate.
        """
        total = sum((point.amount for point in self.points), start=Decimal(0))
        return MoneyAmount.of(
            total,
            currency=self.currency,
            provenance=Provenance.calculated(
                f"sum of {len(self.points)} daily actuals for {self.subscription_id}"
            ),
        )


class ResourceMetrics(Frozen):
    """Observed utilisation for one resource.

    Any field may be ``None``: a metric that was not collected is unknown, and the
    detectors treat unknown as "cannot conclude", never as zero.
    """

    resource_id: str
    observation_days: int = Field(ge=1)
    cpu_avg_pct: Percentage | None = None
    cpu_max_pct: Percentage | None = None
    network_in_gb: Decimal | None = Field(default=None, ge=Decimal(0))
    provenance: Provenance


class SkuPrice(Frozen):
    """A priced SKU from the provider's catalogue.

    Rightsizing needs two prices -- the current SKU and a candidate -- and both must
    come from here. Holding ``vcpu`` and ``memory_gb`` alongside the price is what
    lets the planner pick a candidate arithmetically instead of guessing a size.
    """

    sku: str
    family: str
    vcpu: int = Field(ge=1)
    memory_gb: Decimal = Field(gt=Decimal(0))
    region: str
    monthly_cost: MoneyAmount


class Estate(Frozen):
    """Everything a scan read about one client's estate.

    Assembled by the Anomaly Scout, which is the only node that talks to a provider
    for discovery, and then carried in state so downstream nodes reason over a
    single consistent snapshot rather than re-querying.
    """

    client: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    subscriptions: tuple[Subscription, ...]
    resources: tuple[Resource, ...]
    cost_series: tuple[CostSeries, ...] = ()
    metrics: tuple[ResourceMetrics, ...] = ()

    def resources_in(self, subscription_id: str) -> Sequence[Resource]:
        """Resources belonging to one subscription."""
        return tuple(r for r in self.resources if r.subscription_id == subscription_id)

    def metrics_for(self, resource_id: str) -> ResourceMetrics | None:
        """Metrics for one resource, or ``None`` if none were collected."""
        return next((m for m in self.metrics if m.resource_id == resource_id), None)

    def resource(self, resource_id: str) -> Resource | None:
        """One resource by id, or ``None``."""
        return next((r for r in self.resources if r.resource_id == resource_id), None)

    def observed_spend(self) -> MoneyAmount:
        """Total actual spend across every in-scope subscription.

        Unknown when no cost series was retrieved -- reported as unknown rather than
        as zero, so a report never understates an estate's spend.
        """
        if not self.cost_series:
            return MoneyAmount.undetermined(f"no cost series retrieved for client {self.client}")
        currency = self.cost_series[0].currency
        total = sum(
            (series.observed_spend().amount or Decimal(0) for series in self.cost_series),
            start=Decimal(0),
        )
        return MoneyAmount.of(
            total,
            currency=currency,
            provenance=Provenance.calculated(
                f"sum of actual spend across {len(self.cost_series)} subscription(s)"
            ),
        )
