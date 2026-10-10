"""A deterministic, seeded synthetic Azure estate.

This is not a stub (ARCHITECTURE.md D5). It is a faithful mirror of
:class:`~costsentinel.providers.base.AzureProvider` with realistic shapes, so the
whole pipeline -- including the Phase 5 eval harness -- exercises real code paths
with zero credentials.

Every price comes from the committed catalogue snapshot
(:mod:`costsentinel.providers.catalogue`), so a resource's reported cost and the
catalogue price a rightsizing decision quotes can never drift apart.

The estate deliberately contains clearly wasteful resources alongside healthy ones,
so both recall *and* precision are testable. Wasteful:

* two orphaned (unattached) managed disks,
* one idle, oversized virtual machine,
* one oversized-but-active virtual machine,
* one unattached public IP,
* two stale snapshots,
* one idle SQL database,
* one oversized App Service plan,
* one badly under-used reservation.

Healthy counter-examples for each: attached disks and addresses, a busy VM, a recent
snapshot, a well-used SQL database, and two well-utilised reservations.

Determinism: identical output on every call and in every process. Timestamps derive
from a fixed ``as_of`` rather than the wall clock, and the cost-series jitter is
driven by ``random.Random`` seeded with a string, which is stable across processes
(unlike :func:`hash`, which is salted per process).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from costsentinel.domain.common import (
    Currency,
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    CostPoint,
    CostSeries,
    Environment,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
    Subscription,
)
from costsentinel.providers.catalogue import PriceCatalogue, default_catalogue

#: Fixed observation anchor. The mock's contract is determinism, so it does not read
#: the wall clock. Override via the ``as_of`` constructor argument.
MOCK_AS_OF = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)

#: Default cost-series window.
DEFAULT_WINDOW_DAYS = 30

_CENTS = Decimal("0.01")

CLIENT_NORTHWIND = "northwind-energy"
CLIENT_MINISTRY = "ministry-of-transport"


def _money(value: Decimal) -> Decimal:
    """Round to whole cents, the way a billing system would."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Estate topology
# ---------------------------------------------------------------------------


class _SubSpec(NamedTuple):
    """One synthetic subscription."""

    subscription_id: str
    display_name: str
    client: str
    environment: Environment
    region: str


class _ResSpec(NamedTuple):
    """One synthetic resource.

    There is no price field: the cost is looked up in the catalogue by ``(sku,
    kind)`` and scaled by the region multiplier. A SKU absent from the catalogue
    yields an explicitly unknown cost rather than a guess.
    """

    name: str
    kind: ResourceKind
    subscription_index: int
    resource_group: str
    state: ResourceState
    sku: str | None = None
    attached_to_name: str | None = None
    attached_to_kind: ResourceKind = ResourceKind.VIRTUAL_MACHINE
    tags: tuple[tuple[str, str], ...] = ()
    age_days: int = 0
    cpu_avg_pct: str | None = None
    cpu_max_pct: str | None = None
    network_in_gb: str | None = None
    connection_count: int | None = None
    utilisation_pct: str | None = None


class _RsvSpec(NamedTuple):
    """One synthetic reservation."""

    reservation_id: str
    name: str
    client: str
    subscription_index: int | None
    reserved_sku: str
    reserved_kind: ResourceKind
    term_months: int
    quantity: int
    monthly_amortised: str
    utilisation_pct: str | None
    expires_in_days: int


_SUBSCRIPTIONS: tuple[_SubSpec, ...] = (
    _SubSpec(
        "00000000-0000-4000-8000-00000000a001",
        "NW-PROD-Core",
        CLIENT_NORTHWIND,
        Environment.PRODUCTION,
        "eastus",
    ),
    _SubSpec(
        "00000000-0000-4000-8000-00000000a002",
        "NW-DEV-Sandbox",
        CLIENT_NORTHWIND,
        Environment.DEVELOPMENT,
        "eastus",
    ),
    _SubSpec(
        "00000000-0000-4000-8000-00000000b001",
        "MOT-PROD-Gov",
        CLIENT_MINISTRY,
        Environment.PRODUCTION,
        "qatarcentral",
    ),
)

_RESOURCES: tuple[_ResSpec, ...] = (
    # --- NW-PROD-Core -----------------------------------------------------
    _ResSpec(
        name="vm-web-01",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        tags=(("env", "prod"), ("owner", "platform"), ("cost-centre", "NW-1001")),
        age_days=612,
        cpu_avg_pct="41.8",
        cpu_max_pct="78.3",
        network_in_gb="1842.5",
    ),
    _ResSpec(
        name="disk-web-01-os",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.ATTACHED,
        sku="Premium_LRS_P10",
        attached_to_name="vm-web-01",
        tags=(("env", "prod"), ("cost-centre", "NW-1001")),
        age_days=612,
    ),
    _ResSpec(
        name="pip-web-01",
        kind=ResourceKind.PUBLIC_IP,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.ATTACHED,
        sku="Standard_Static",
        attached_to_name="vm-web-01",
        tags=(("env", "prod"),),
        age_days=612,
    ),
    _ResSpec(
        name="st-core-archive",
        kind=ResourceKind.STORAGE_ACCOUNT,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.AVAILABLE,
        sku="Standard_GRS_Cool",
        tags=(("env", "prod"), ("retention", "7y")),
        age_days=901,
    ),
    _ResSpec(
        # Oversized but genuinely in use: CPU peaks well under half its capacity.
        name="vm-batch-02",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_E16s_v5",
        tags=(("env", "prod"), ("owner", "data-eng"), ("cost-centre", "NW-1004")),
        age_days=430,
        cpu_avg_pct="6.1",
        cpu_max_pct="18.5",
        network_in_gb="214.8",
    ),
    _ResSpec(
        name="disk-batch-02-os",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.ATTACHED,
        sku="Premium_LRS_P10",
        attached_to_name="vm-batch-02",
        tags=(("env", "prod"),),
        age_days=430,
    ),
    _ResSpec(
        # Busy database: a well-used counter-example for the idle-SQL detector.
        name="sql-core-reporting",
        kind=ResourceKind.SQL_DATABASE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.AVAILABLE,
        sku="GP_Gen5_4",
        tags=(("env", "prod"), ("cost-centre", "NW-1002")),
        age_days=548,
        cpu_avg_pct="38.4",
        cpu_max_pct="81.2",
        connection_count=14820,
    ),
    _ResSpec(
        # Stale snapshot in production: kept long past any restore window.
        name="snap-web-01-pre-upgrade",
        kind=ResourceKind.SNAPSHOT,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.AVAILABLE,
        sku="Standard_LRS_Snapshot_512",
        tags=(("env", "prod"), ("note", "taken before the 2025 platform upgrade")),
        age_days=603,
    ),
    # --- NW-DEV-Sandbox ---------------------------------------------------
    _ResSpec(
        # Idle *and* oversized. The idle detector takes precedence: deallocating
        # saves the whole compute charge, which strictly dominates a resize.
        name="vm-analytics-01",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.RUNNING,
        sku="Standard_D16s_v5",
        tags=(("env", "dev"), ("owner", "analytics"), ("project", "churn-model-poc")),
        age_days=287,
        cpu_avg_pct="1.2",
        cpu_max_pct="3.8",
        network_in_gb="4.1",
    ),
    _ResSpec(
        name="disk-analytics-01-os",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.ATTACHED,
        sku="Premium_LRS_P10",
        attached_to_name="vm-analytics-01",
        tags=(("env", "dev"),),
        age_days=287,
    ),
    _ResSpec(
        # Orphan: a data disk detached when the original VM was rebuilt.
        name="disk-analytics-01-data",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.UNATTACHED,
        sku="Premium_LRS_P15",
        tags=(("env", "dev"), ("project", "churn-model-poc")),
        age_days=241,
    ),
    _ResSpec(
        # Orphan: a reserved static IP left behind by a retired API.
        name="pip-legacy-api",
        kind=ResourceKind.PUBLIC_IP,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.UNATTACHED,
        sku="Standard_Static",
        tags=(("env", "dev"),),
        age_days=398,
    ),
    _ResSpec(
        name="st-dev-scratch",
        kind=ResourceKind.STORAGE_ACCOUNT,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.AVAILABLE,
        sku="Standard_LRS_Hot",
        tags=(("env", "dev"),),
        age_days=287,
    ),
    _ResSpec(
        # Stale snapshot in development.
        name="snap-analytics-baseline",
        kind=ResourceKind.SNAPSHOT,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.AVAILABLE,
        sku="Standard_LRS_Snapshot_128",
        tags=(("env", "dev"), ("project", "churn-model-poc")),
        age_days=412,
    ),
    _ResSpec(
        # Recent snapshot: must NOT be flagged. Precision counter-example.
        name="snap-analytics-nightly",
        kind=ResourceKind.SNAPSHOT,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.AVAILABLE,
        sku="Standard_LRS_Snapshot_128",
        tags=(("env", "dev"), ("schedule", "nightly")),
        age_days=12,
    ),
    _ResSpec(
        # Idle database: provisioned for a proof of concept, never connected to.
        name="sql-dev-sandbox",
        kind=ResourceKind.SQL_DATABASE,
        subscription_index=1,
        resource_group="rg-analytics-dev",
        state=ResourceState.AVAILABLE,
        sku="GP_Gen5_4",
        tags=(("env", "dev"), ("project", "churn-model-poc")),
        age_days=263,
        cpu_avg_pct="0.4",
        cpu_max_pct="2.1",
        connection_count=0,
    ),
    # --- MOT-PROD-Gov -----------------------------------------------------
    _ResSpec(
        name="vm-portal-01",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        tags=(("env", "prod"), ("classification", "official"), ("owner", "digital-services")),
        age_days=734,
        cpu_avg_pct="55.4",
        cpu_max_pct="91.2",
        network_in_gb="3290.7",
    ),
    _ResSpec(
        name="disk-portal-01-os",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.ATTACHED,
        sku="Premium_LRS_P10",
        attached_to_name="vm-portal-01",
        tags=(("env", "prod"),),
        age_days=734,
    ),
    _ResSpec(
        # Orphan: a migration-era disk nobody deleted. Larger, and so costlier,
        # than the development orphan -- which is why ranking matters.
        name="disk-portal-legacy-snapshot",
        kind=ResourceKind.MANAGED_DISK,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.UNATTACHED,
        sku="Premium_LRS_P20",
        tags=(("env", "prod"), ("note", "pre-migration copy")),
        age_days=516,
    ),
    _ResSpec(
        name="pip-portal-01",
        kind=ResourceKind.PUBLIC_IP,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.ATTACHED,
        sku="Standard_Static",
        attached_to_name="vm-portal-01",
        tags=(("env", "prod"),),
        age_days=734,
    ),
    _ResSpec(
        # Oversized plan: sized for a launch peak that never recurred.
        name="app-portal-plan",
        kind=ResourceKind.APP_SERVICE_PLAN,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.AVAILABLE,
        sku="P1v3",
        tags=(("env", "prod"),),
        age_days=734,
        cpu_avg_pct="8.2",
        cpu_max_pct="19.6",
        utilisation_pct="11.4",
    ),
)

_RESERVATIONS: tuple[_RsvSpec, ...] = (
    _RsvSpec(
        # Badly under-used: bought for a workload that was later re-platformed.
        reservation_id="rsv-00000000-0000-4000-8000-0000000000c1",
        name="nw-compute-dsv5-3y",
        client=CLIENT_NORTHWIND,
        subscription_index=0,
        reserved_sku="Standard_D4s_v5",
        reserved_kind=ResourceKind.VIRTUAL_MACHINE,
        term_months=36,
        quantity=4,
        monthly_amortised="420.48",
        utilisation_pct="34.5",
        expires_in_days=488,
    ),
    _RsvSpec(
        # Well used: a precision counter-example.
        reservation_id="rsv-00000000-0000-4000-8000-0000000000c2",
        name="nw-sql-gen5-1y",
        client=CLIENT_NORTHWIND,
        subscription_index=0,
        reserved_sku="GP_Gen5_4",
        reserved_kind=ResourceKind.SQL_DATABASE,
        term_months=12,
        quantity=1,
        monthly_amortised="110.48",
        utilisation_pct="96.2",
        expires_in_days=211,
    ),
    _RsvSpec(
        reservation_id="rsv-00000000-0000-4000-8000-0000000000d1",
        name="mot-compute-dsv5-1y",
        client=CLIENT_MINISTRY,
        subscription_index=2,
        reserved_sku="Standard_D4s_v5",
        reserved_kind=ResourceKind.VIRTUAL_MACHINE,
        term_months=12,
        quantity=2,
        monthly_amortised="236.80",
        utilisation_pct="88.0",
        expires_in_days=96,
    ),
)

_ARM_PROVIDER_PATH: dict[ResourceKind, str] = {
    ResourceKind.VIRTUAL_MACHINE: "Microsoft.Compute/virtualMachines",
    ResourceKind.MANAGED_DISK: "Microsoft.Compute/disks",
    ResourceKind.SNAPSHOT: "Microsoft.Compute/snapshots",
    ResourceKind.PUBLIC_IP: "Microsoft.Network/publicIPAddresses",
    ResourceKind.NETWORK_INTERFACE: "Microsoft.Network/networkInterfaces",
    ResourceKind.STORAGE_ACCOUNT: "Microsoft.Storage/storageAccounts",
    ResourceKind.SQL_DATABASE: "Microsoft.Sql/servers/databases",
    ResourceKind.APP_SERVICE_PLAN: "Microsoft.Web/serverfarms",
}


def _arm_id(subscription_id: str, resource_group: str, kind: ResourceKind, name: str) -> str:
    """Build an ARM-shaped resource id, so ids look like the real thing."""
    return (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_ARM_PROVIDER_PATH[kind]}/{name}"
    )


class MockAzureProvider:
    """A deterministic synthetic estate satisfying :class:`AzureProvider`."""

    def __init__(
        self,
        *,
        seed: int = 1337,
        as_of: datetime | None = None,
        catalogue: PriceCatalogue | None = None,
    ) -> None:
        """Build the whole estate up front, so every later call is a lookup.

        Args:
            seed: Drives the cost-series day-to-day variation. The same seed always
                produces the same series. The estate topology is fixed
                (ARCHITECTURE.md D25); Phase 5 varies it by seed for eval datasets.
            as_of: Observation anchor for every timestamp and cost-series date.
                Defaults to :data:`MOCK_AS_OF` so output is identical across
                processes.
            catalogue: Price snapshot to price the estate from. Defaults to the
                committed one, which is what makes the figures reproducible.
        """
        self._seed = seed
        self._as_of = as_of or MOCK_AS_OF
        self._catalogue = catalogue or default_catalogue()
        self._subscriptions: tuple[Subscription, ...] = tuple(
            self._build_subscription(spec) for spec in _SUBSCRIPTIONS
        )
        self._resources: tuple[Resource, ...] = tuple(
            self._build_resource(spec) for spec in _RESOURCES
        )
        self._reservations: tuple[Reservation, ...] = tuple(
            self._build_reservation(spec) for spec in _RESERVATIONS
        )
        self._metrics: dict[str, ResourceMetrics] = {
            metric.resource_id: metric
            for metric in (self._build_metrics(spec) for spec in _RESOURCES)
            if metric is not None
        }

    # --- identity --------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier used in logs, traces and provenance references."""
        return "mock-azure"

    @property
    def seed(self) -> int:
        """The seed this provider was built with."""
        return self._seed

    @property
    def as_of(self) -> datetime:
        """The observation anchor for every timestamp this provider emits."""
        return self._as_of

    @property
    def catalogue(self) -> PriceCatalogue:
        """The price snapshot this estate is priced from."""
        return self._catalogue

    # --- provenance helpers ----------------------------------------------

    def _provenance(self, reference: str) -> Provenance:
        return Provenance(
            source=ProvenanceSource.MOCK_PROVIDER,
            retrieved_at=self._as_of,
            reference=reference,
            verification=Verification.VERIFIED,
        )

    # --- construction ----------------------------------------------------

    def _build_subscription(self, spec: _SubSpec) -> Subscription:
        return Subscription(
            subscription_id=spec.subscription_id,
            display_name=spec.display_name,
            client=spec.client,
            environment=spec.environment,
            tags={"region": spec.region, "client": spec.client},
            provenance=self._provenance("mock:subscriptions.list"),
        )

    def _resource_monthly(self, spec: _ResSpec, region: str) -> MoneyAmount:
        """Price a resource from the catalogue.

        A SKU the catalogue does not price yields an explicitly unknown cost rather
        than a guess -- the honest answer, and the behaviour downstream nodes are
        built to tolerate.
        """
        if spec.sku is None:
            return MoneyAmount.undetermined(f"{spec.name} has no SKU to price")
        priced = self._catalogue.price_for(spec.sku, spec.kind, region)
        if priced is None:
            return MoneyAmount.undetermined(
                f"no catalogue price for sku {spec.sku!r} as {spec.kind.value} in {region}"
            )
        return priced.monthly_cost

    def _build_resource(self, spec: _ResSpec) -> Resource:
        sub = _SUBSCRIPTIONS[spec.subscription_index]
        attached_to = (
            _arm_id(
                sub.subscription_id,
                spec.resource_group,
                spec.attached_to_kind,
                spec.attached_to_name,
            )
            if spec.attached_to_name
            else None
        )
        return Resource(
            resource_id=_arm_id(sub.subscription_id, spec.resource_group, spec.kind, spec.name),
            name=spec.name,
            kind=spec.kind,
            subscription_id=sub.subscription_id,
            resource_group=spec.resource_group,
            region=sub.region,
            sku=spec.sku,
            state=spec.state,
            attached_to=attached_to,
            tags=dict(spec.tags),
            created_at=self._as_of - timedelta(days=spec.age_days),
            monthly_cost=self._resource_monthly(spec, sub.region),
            provenance=self._provenance("mock:resourcegraph.resources"),
        )

    def _build_reservation(self, spec: _RsvSpec) -> Reservation:
        sub = (
            _SUBSCRIPTIONS[spec.subscription_index] if spec.subscription_index is not None else None
        )
        return Reservation(
            reservation_id=spec.reservation_id,
            name=spec.name,
            client=spec.client,
            scope_subscription_id=sub.subscription_id if sub else None,
            reserved_sku=spec.reserved_sku,
            reserved_kind=spec.reserved_kind,
            region=sub.region if sub else self._catalogue.base_region,
            term_months=spec.term_months,
            quantity=spec.quantity,
            monthly_amortised_cost=MoneyAmount.of(
                _money(Decimal(spec.monthly_amortised)),
                currency=Currency.USD,
                provenance=self._provenance(f"mock:reservations.detail/{spec.name}"),
            ),
            utilisation_pct=(
                Decimal(spec.utilisation_pct) if spec.utilisation_pct is not None else None
            ),
            expires_on=(self._as_of + timedelta(days=spec.expires_in_days)).date(),
            provenance=self._provenance("mock:reservations.list"),
        )

    def _build_metrics(self, spec: _ResSpec) -> ResourceMetrics | None:
        measured = (
            spec.cpu_avg_pct,
            spec.cpu_max_pct,
            spec.connection_count,
            spec.utilisation_pct,
        )
        if all(value is None for value in measured):
            return None
        sub = _SUBSCRIPTIONS[spec.subscription_index]
        return ResourceMetrics(
            resource_id=_arm_id(sub.subscription_id, spec.resource_group, spec.kind, spec.name),
            observation_days=DEFAULT_WINDOW_DAYS,
            cpu_avg_pct=Decimal(spec.cpu_avg_pct) if spec.cpu_avg_pct else None,
            cpu_max_pct=Decimal(spec.cpu_max_pct) if spec.cpu_max_pct else None,
            network_in_gb=Decimal(spec.network_in_gb) if spec.network_in_gb else None,
            connection_count=spec.connection_count,
            utilisation_pct=(
                Decimal(spec.utilisation_pct) if spec.utilisation_pct is not None else None
            ),
            provenance=self._provenance(f"mock:monitor.metrics/{spec.name}"),
        )

    # --- AzureProvider ---------------------------------------------------

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        """Subscriptions in scope, optionally narrowed to one client."""
        if client is None:
            return self._subscriptions
        return tuple(s for s in self._subscriptions if s.client == client)

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        """Every billable resource in one subscription."""
        return tuple(r for r in self._resources if r.subscription_id == subscription_id)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        """Capacity commitments in scope, optionally narrowed to one client."""
        if client is None:
            return self._reservations
        return tuple(r for r in self._reservations if r.client == client)

    def get_cost_series(
        self, subscription_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS
    ) -> CostSeries:
        """Daily actual spend for one subscription over the trailing window.

        Dailies are the subscription's total monthly list cost spread over the
        window with a deterministic day-to-day variation, which is why the summed
        actual does not exactly equal the sum of list prices -- as in a real estate.
        """
        monthly_total = sum(
            (
                r.monthly_cost.amount
                for r in self._resources
                if r.subscription_id == subscription_id and r.monthly_cost.amount is not None
            ),
            start=Decimal(0),
        )
        rng = random.Random(f"{self._seed}:{subscription_id}:{window_days}")  # noqa: S311
        daily_base = monthly_total / Decimal(window_days)
        end = self._as_of.date()
        points = tuple(
            CostPoint(
                day=end - timedelta(days=window_days - offset),
                amount=_money(daily_base * Decimal(str(round(rng.uniform(0.92, 1.08), 4)))),
            )
            for offset in range(window_days)
        )
        return CostSeries(
            subscription_id=subscription_id,
            currency=Currency.USD,
            points=points,
            provenance=self._provenance(
                f"mock:costmanagement.query/daily/{subscription_id}/{window_days}d"
            ),
        )

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Observed utilisation for one resource, or ``None`` if not measured."""
        return self._metrics.get(resource_id)

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """The priced SKU catalogue for one region."""
        return self._catalogue.for_region(region)
