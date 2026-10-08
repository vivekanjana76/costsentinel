"""A deterministic, seeded synthetic Azure estate.

This is not a stub (ARCHITECTURE.md D5). It is a faithful mirror of
:class:`~costsentinel.providers.base.AzureProvider` with realistic shapes, so the
whole pipeline -- including the Phase 5 eval harness -- exercises real code paths
with zero credentials.

The estate deliberately contains clearly wasteful resources alongside healthy ones,
so both recall *and* precision are testable:

* two orphaned (unattached) managed disks,
* one idle, oversized virtual machine,
* one unattached public IP,
* one oversized-but-active virtual machine,
* and eleven resources that are behaving correctly and must *not* be flagged.

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
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
    Subscription,
)

#: Fixed observation anchor. The mock's contract is determinism, so it does not read
#: the wall clock. Override via the ``as_of`` constructor argument.
MOCK_AS_OF = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)

#: Default cost-series window.
DEFAULT_WINDOW_DAYS = 30

_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Round to whole cents, the way a billing system would."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Price catalogue
# ---------------------------------------------------------------------------


class _SkuSpec(NamedTuple):
    """A SKU and its base (``eastus``) monthly list price."""

    sku: str
    family: str
    vcpu: int
    memory_gb: str
    base_monthly: str


#: Synthetic but plausible pay-as-you-go Linux monthly list prices (730 hours).
#: These are mock catalogue data: the point is that a savings figure traces to a
#: catalogue lookup rather than to a guess.
_SKU_CATALOGUE: tuple[_SkuSpec, ...] = (
    _SkuSpec("Standard_D2s_v5", "Dsv5", 2, "8", "70.08"),
    _SkuSpec("Standard_D4s_v5", "Dsv5", 4, "16", "140.16"),
    _SkuSpec("Standard_D8s_v5", "Dsv5", 8, "32", "280.32"),
    _SkuSpec("Standard_D16s_v5", "Dsv5", 16, "64", "560.64"),
    _SkuSpec("Standard_D32s_v5", "Dsv5", 32, "128", "1121.28"),
    _SkuSpec("Standard_E2s_v5", "Esv5", 2, "16", "91.98"),
    _SkuSpec("Standard_E4s_v5", "Esv5", 4, "32", "183.96"),
    _SkuSpec("Standard_E8s_v5", "Esv5", 8, "64", "367.92"),
    _SkuSpec("Standard_E16s_v5", "Esv5", 16, "128", "735.84"),
    _SkuSpec("Standard_E32s_v5", "Esv5", 32, "256", "1471.68"),
)

#: Regional price multipliers against the ``eastus`` base.
_REGION_MULTIPLIER: dict[str, str] = {
    "eastus": "1.00",
    "qatarcentral": "1.12",
}


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

    ``base_monthly`` is the ``eastus`` list price; the regional multiplier is applied
    when the resource is built. For a virtual machine it is ``None`` and the price
    comes from the SKU catalogue instead, so a VM's cost and its catalogue price can
    never drift apart.
    """

    name: str
    kind: ResourceKind
    subscription_index: int
    resource_group: str
    state: ResourceState
    sku: str | None = None
    base_monthly: str | None = None
    attached_to_name: str | None = None
    tags: tuple[tuple[str, str], ...] = ()
    age_days: int = 0
    cpu_avg_pct: str | None = None
    cpu_max_pct: str | None = None
    network_in_gb: str | None = None


CLIENT_NORTHWIND = "northwind-energy"
CLIENT_MINISTRY = "ministry-of-transport"

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
        base_monthly="19.71",
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
        base_monthly="3.65",
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
        base_monthly="48.20",
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
        base_monthly="19.71",
        attached_to_name="vm-batch-02",
        tags=(("env", "prod"),),
        age_days=430,
    ),
    _ResSpec(
        name="sql-core-reporting",
        kind=ResourceKind.SQL_DATABASE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.AVAILABLE,
        sku="GP_Gen5_4",
        base_monthly="147.30",
        tags=(("env", "prod"), ("cost-centre", "NW-1002")),
        age_days=548,
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
        base_monthly="19.71",
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
        base_monthly="38.42",
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
        base_monthly="3.65",
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
        base_monthly="11.40",
        tags=(("env", "dev"),),
        age_days=287,
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
        base_monthly="19.71",
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
        base_monthly="76.84",
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
        base_monthly="3.65",
        attached_to_name="vm-portal-01",
        tags=(("env", "prod"),),
        age_days=734,
    ),
    _ResSpec(
        name="app-portal-plan",
        kind=ResourceKind.APP_SERVICE_PLAN,
        subscription_index=2,
        resource_group="rg-portal-prod",
        state=ResourceState.AVAILABLE,
        sku="P1v3",
        base_monthly="219.00",
        tags=(("env", "prod"),),
        age_days=734,
    ),
)

_ARM_PROVIDER_PATH: dict[ResourceKind, str] = {
    ResourceKind.VIRTUAL_MACHINE: "Microsoft.Compute/virtualMachines",
    ResourceKind.MANAGED_DISK: "Microsoft.Compute/disks",
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

    def __init__(self, *, seed: int = 1337, as_of: datetime | None = None) -> None:
        """Build the whole estate up front, so every later call is a lookup.

        Args:
            seed: Drives the cost-series day-to-day variation. The same seed always
                produces the same series. The estate topology is fixed in Phase 1
                (ARCHITECTURE.md D25); Phase 5 varies it by seed for eval datasets.
            as_of: Observation anchor for every timestamp and cost-series date.
                Defaults to :data:`MOCK_AS_OF` so output is identical across
                processes.
        """
        self._seed = seed
        self._as_of = as_of or MOCK_AS_OF
        self._subscriptions: tuple[Subscription, ...] = tuple(
            self._build_subscription(spec) for spec in _SUBSCRIPTIONS
        )
        self._resources: tuple[Resource, ...] = tuple(
            self._build_resource(spec) for spec in _RESOURCES
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

    def _region_multiplier(self, region: str) -> Decimal:
        return Decimal(_REGION_MULTIPLIER.get(region, "1.00"))

    def _sku_base_monthly(self, sku: str) -> Decimal | None:
        spec = next((s for s in _SKU_CATALOGUE if s.sku == sku), None)
        return Decimal(spec.base_monthly) if spec else None

    def _resource_monthly(self, spec: _ResSpec, region: str) -> MoneyAmount:
        """Price a resource, preferring the SKU catalogue for compute.

        A virtual machine with a SKU that is not in the catalogue is priced as
        *unknown* rather than guessed -- the honest answer, and the behaviour the
        downstream nodes are built to tolerate.
        """
        base = (
            self._sku_base_monthly(spec.sku)
            if spec.kind is ResourceKind.VIRTUAL_MACHINE and spec.sku
            else (Decimal(spec.base_monthly) if spec.base_monthly else None)
        )
        if base is None:
            return MoneyAmount.undetermined(
                f"no catalogue price for sku {spec.sku!r} in region {region}"
            )
        return MoneyAmount.of(
            _money(base * self._region_multiplier(region)),
            currency=Currency.USD,
            provenance=self._provenance(f"mock:costmanagement.resourceMonthly/{spec.name}"),
        )

    def _build_resource(self, spec: _ResSpec) -> Resource:
        sub = _SUBSCRIPTIONS[spec.subscription_index]
        attached_to = (
            _arm_id(
                sub.subscription_id,
                spec.resource_group,
                ResourceKind.VIRTUAL_MACHINE,
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

    def _build_metrics(self, spec: _ResSpec) -> ResourceMetrics | None:
        if spec.cpu_avg_pct is None and spec.cpu_max_pct is None:
            return None
        sub = _SUBSCRIPTIONS[spec.subscription_index]
        return ResourceMetrics(
            resource_id=_arm_id(sub.subscription_id, spec.resource_group, spec.kind, spec.name),
            observation_days=DEFAULT_WINDOW_DAYS,
            cpu_avg_pct=Decimal(spec.cpu_avg_pct) if spec.cpu_avg_pct else None,
            cpu_max_pct=Decimal(spec.cpu_max_pct) if spec.cpu_max_pct else None,
            network_in_gb=Decimal(spec.network_in_gb) if spec.network_in_gb else None,
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
        multiplier = self._region_multiplier(region)
        return tuple(
            SkuPrice(
                sku=spec.sku,
                family=spec.family,
                vcpu=spec.vcpu,
                memory_gb=Decimal(spec.memory_gb),
                region=region,
                monthly_cost=MoneyAmount.of(
                    _money(Decimal(spec.base_monthly) * multiplier),
                    currency=Currency.USD,
                    provenance=self._provenance(f"mock:retailprices/{spec.sku}/{region}"),
                ),
            )
            for spec in _SKU_CATALOGUE
        )
