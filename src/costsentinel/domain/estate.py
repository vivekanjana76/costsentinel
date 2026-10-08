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
    SNAPSHOT = "snapshot"


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
    connection_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Distinct client connections observed over the window. Zero is a "
            "measured fact and means idle; None means not measured."
        ),
    )
    utilisation_pct: Percentage | None = Field(
        default=None,
        description=(
            "Generic utilisation for resources whose waste is not CPU-shaped, such "
            "as an App Service plan's instance utilisation."
        ),
    )
    provenance: Provenance


class SkuPrice(Frozen):
    """A priced SKU from the provider's catalogue.

    Rightsizing needs two prices -- the current SKU and a candidate -- and both must
    come from here. Holding ``vcpu`` and ``memory_gb`` alongside the price is what
    lets the planner pick a candidate arithmetically instead of guessing a size.

    ``applies_to`` keeps the search honest across resource kinds: without it a
    rightsizing pass could offer a SQL tier as a candidate for a virtual machine,
    because both are priced per vCPU.

    ``vcpu`` and ``memory_gb`` are optional, because a disk, a snapshot or a public
    IP has neither. Absent capacity simply means that SKU cannot participate in a
    capacity-based rightsizing decision.
    """

    sku: str
    applies_to: ResourceKind
    family: str
    region: str
    vcpu: int | None = Field(default=None, ge=1)
    memory_gb: Decimal | None = Field(default=None, gt=Decimal(0))
    monthly_cost: MoneyAmount

    @property
    def has_capacity(self) -> bool:
        """Whether this SKU can take part in capacity-based rightsizing."""
        return self.vcpu is not None


class Reservation(Frozen):
    """A purchased capacity commitment.

    Modelled separately from :class:`Resource` rather than squeezed into it, because
    a reservation has structure a resource does not -- a term, a quantity, an expiry
    and a utilisation against the commitment -- and because the real Azure
    Reservations API is a different surface from Resource Graph.

    The waste here is unlike other waste: the money is *already spent*. Low
    utilisation means commitment that is being paid for and not consumed, which is
    recoverable only by exchanging or re-scoping the reservation, never by deleting
    something.
    """

    reservation_id: str
    name: str
    client: str
    scope_subscription_id: str | None = Field(
        default=None,
        description="None for a shared-scope reservation spanning the enrolment.",
    )
    reserved_sku: str
    reserved_kind: ResourceKind
    region: str
    term_months: int = Field(ge=1)
    quantity: int = Field(ge=1)
    monthly_amortised_cost: MoneyAmount
    utilisation_pct: Percentage | None = Field(
        default=None,
        description="Observed utilisation of the commitment. None means not measured.",
    )
    expires_on: date | None = None
    provenance: Provenance

    @property
    def is_measured(self) -> bool:
        """Whether utilisation was reported, so a conclusion can be drawn."""
        return self.utilisation_pct is not None


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
    reservations: tuple[Reservation, ...] = ()

    def resources_in(self, subscription_id: str) -> Sequence[Resource]:
        """Resources belonging to one subscription."""
        return tuple(r for r in self.resources if r.subscription_id == subscription_id)

    def metrics_for(self, resource_id: str) -> ResourceMetrics | None:
        """Metrics for one resource, or ``None`` if none were collected."""
        return next((m for m in self.metrics if m.resource_id == resource_id), None)

    def resource(self, resource_id: str) -> Resource | None:
        """One resource by id, or ``None``."""
        return next((r for r in self.resources if r.resource_id == resource_id), None)

    def reservation(self, reservation_id: str) -> Reservation | None:
        """One reservation by id, or ``None``."""
        return next((r for r in self.reservations if r.reservation_id == reservation_id), None)

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
