"""A deterministic, seeded synthetic Azure estate.

This is not a stub (ARCHITECTURE.md D5). It is a faithful mirror of
:class:`~costsentinel.providers.base.AzureProvider` with realistic shapes, so the
whole pipeline -- including the Phase 5 eval harness -- exercises real code paths
with zero credentials.

Every price comes from the committed catalogue snapshot
(:mod:`costsentinel.providers.catalogue`), which holds **real** Azure Retail Prices
figures with the meter each came from. So a resource's reported cost and the
catalogue price a rightsizing decision quotes can never drift apart, and both are
checkable against Azure by hand.

The estate itself is synthetic -- the resource names, ids, tags, utilisation and
capacities are invented, and deliberately so, since this is a public repository. What
is real is the *rates*: the quantities come from here and the prices come from
Azure.

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

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
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


class SeriesShape(StrEnum):
    """What a subscription's daily spend does over the window.

    Four shapes, because a detector with nothing to detect over is untested and a
    detector tested only on the easy shapes is worse than that. Three of these should
    produce a finding or no finding unambiguously; the fourth, :attr:`SEASONAL`, is the
    one a naive detector gets wrong, and it is in the committed estate rather than only
    in a unit test for exactly that reason.
    """

    #: Flat, within the daily wobble. Nothing to report.
    STEADY = "steady"

    #: Flat, then a new level for the last week and staying there. A level shift.
    STEP_CHANGE = "step_change"

    #: Flat, with one day far above and the next day back to normal. A spike.
    REVERTING_SPIKE = "reverting_spike"

    #: A weekly batch window: one weekday several times the others, every week. A
    #: pattern rather than an anomaly, and it must produce no alert.
    SEASONAL = "seasonal"


#: Day-to-day wobble applied to every series, as a multiplier on the daily baseline.
#:
#: Committed rather than drawn from a seeded generator, and thirty values rather than a
#: repeating seven. A seven-day cycle would be absorbed entirely by the detector's
#: weekday profile, leaving the residual deviation at zero -- so every shape would
#: exercise only the flat-baseline fallback and the robust statistics would never run.
_DAILY_WOBBLE: tuple[str, ...] = (
    "1.000",
    "1.011",
    "0.993",
    "1.005",
    "0.986",
    "1.009",
    "0.996",
    "1.015",
    "0.990",
    "1.002",
    "1.012",
    "0.994",
    "1.007",
    "0.988",
    "1.004",
    "1.010",
    "0.995",
    "1.013",
    "0.991",
    "1.006",
    "0.989",
    "1.001",
    "1.016",
    "0.997",
    "1.008",
    "0.985",
    "1.011",
    "0.999",
    "1.005",
    "0.992",
)

#: How many days from the end of the window a step change covers. Seven, to place the
#: whole change inside the detector's observation window -- a step that straddled the
#: boundary would put the new level into the baseline and partly hide itself.
#:
#: Deliberately a constant here rather than an import of
#: ``agents.anomalies.OBSERVATION_DAYS``: a provider must not depend on an agent. The
#: two are coupled, so ``tests/test_anomaly_series.py`` asserts they agree, which is
#: where a coupling between a detector's policy and a fixture's data belongs.
_STEP_DAYS_FROM_END = 7

#: Which day of the last week the spike lands on, counted from the end of the window.
#: Not the final day: a spike on the last day of the series cannot be seen to revert,
#: and "it spiked and came back" is the claim this shape exists to make.
_SPIKE_OFFSET_FROM_END = 4

#: How many times the baseline a spike reaches. Large enough to be unmistakable and
#: not so large it would be the only thing a test could detect.
_SPIKE_MULTIPLE = Decimal("4.0")

#: How much higher the new level sits after a step change. 1.75 is a workload landing,
#: not a billing error -- the detector should catch a realistic change, not only an
#: enormous one.
_STEP_MULTIPLE = Decimal("1.75")

#: The batch weekday, and how many times a normal day it costs. Sunday night, which is
#: where a real estate puts a weekly job.
_BATCH_WEEKDAY = 6
_BATCH_MULTIPLE = Decimal("5.0")


def shape_multiple(shape: SeriesShape, *, offset: int, window_days: int, weekday: int) -> Decimal:
    """The shape's multiplier for one day of the window.

    ``offset`` counts from the start of the window, so ``window_days - 1`` is the most
    recent day. Deterministic arithmetic over committed constants: no randomness, and
    no dependence on the wall clock beyond the caller's own anchor date.
    """
    days_from_end = window_days - 1 - offset
    if shape is SeriesShape.STEP_CHANGE:
        return _STEP_MULTIPLE if days_from_end < _STEP_DAYS_FROM_END else Decimal(1)
    if shape is SeriesShape.REVERTING_SPIKE:
        return _SPIKE_MULTIPLE if days_from_end == _SPIKE_OFFSET_FROM_END else Decimal(1)
    if shape is SeriesShape.SEASONAL:
        return _BATCH_MULTIPLE if weekday == _BATCH_WEEKDAY else Decimal(1)
    return Decimal(1)


def synthetic_series(
    *,
    subscription_id: str,
    daily_baseline: Decimal,
    shape: SeriesShape,
    end: date,
    window_days: int,
    provenance: Provenance,
    currency: Currency = Currency.USD,
) -> CostSeries:
    """A committed daily series of the requested shape, ending on ``end``.

    Public, and shared by two callers that would otherwise each grow their own copy:
    :class:`MockAzureProvider` for the demo estate, and the evaluation harness's
    dataset provider for the labelled anomaly cases. One definition reviewed once,
    rather than two fixtures whose idea of "a step change" could drift apart while
    both looked right.

    Entirely deterministic -- a committed baseline times a committed shape times a
    committed wobble -- so two runs of the same request produce byte-identical points
    and therefore byte-identical anomaly verdicts.
    """
    first = end - timedelta(days=window_days - 1)
    points: list[CostPoint] = []
    for offset in range(window_days):
        day = first + timedelta(days=offset)
        wobble = Decimal(_DAILY_WOBBLE[offset % len(_DAILY_WOBBLE)])
        multiple = shape_multiple(
            shape, offset=offset, window_days=window_days, weekday=day.weekday()
        )
        points.append(CostPoint(day=day, amount=_money(daily_baseline * multiple * wobble)))
    return CostSeries(
        subscription_id=subscription_id,
        currency=currency,
        points=tuple(points),
        provenance=provenance,
    )


class _SubSpec(NamedTuple):
    """One synthetic subscription."""

    subscription_id: str
    display_name: str
    client: str
    environment: Environment
    region: str
    daily_baseline: str
    shape: SeriesShape


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
    capacity_gb: str | None = None
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
    utilisation_pct: str | None
    expires_in_days: int


#: The synthetic estate's subscriptions, one per series shape.
#:
#: Each shape is placed where it would plausibly occur: a workload landing in a
#: production estate, a one-off load test in a sandbox, a weekly overnight batch on a
#: government portal, and a quiet subscription where nothing is happening.
#:
#: ``daily_baseline`` is committed per subscription rather than derived from the
#: resource catalogue, which it used to be. Deriving it meant that refreshing Azure's
#: prices silently changed every cost series and therefore every anomaly verdict, and
#: it is also the less realistic model: a real subscription's bill includes bandwidth,
#: support and marketplace charges that no inventory query returns.
_SUBSCRIPTIONS: tuple[_SubSpec, ...] = (
    _SubSpec(
        "00000000-0000-4000-8000-00000000a001",
        "NW-PROD-Core",
        CLIENT_NORTHWIND,
        Environment.PRODUCTION,
        "eastus",
        daily_baseline="120.00",
        shape=SeriesShape.STEP_CHANGE,
    ),
    _SubSpec(
        "00000000-0000-4000-8000-00000000a002",
        "NW-DEV-Sandbox",
        CLIENT_NORTHWIND,
        Environment.DEVELOPMENT,
        "eastus",
        daily_baseline="35.00",
        shape=SeriesShape.REVERTING_SPIKE,
    ),
    _SubSpec(
        "00000000-0000-4000-8000-00000000b001",
        "MOT-PROD-Gov",
        CLIENT_MINISTRY,
        Environment.PRODUCTION,
        "qatarcentral",
        daily_baseline="60.00",
        shape=SeriesShape.SEASONAL,
    ),
    _SubSpec(
        "00000000-0000-4000-8000-00000000b002",
        "MOT-DEV-Portal",
        CLIENT_MINISTRY,
        Environment.DEVELOPMENT,
        "qatarcentral",
        daily_baseline="18.00",
        shape=SeriesShape.STEADY,
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
        capacity_gb="1024",
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
        sku="Premium_LRS_Snapshot",
        capacity_gb="512",
        tags=(("env", "prod"), ("note", "taken before the 2025 platform upgrade")),
        age_days=603,
    ),
    _ResSpec(
        # Idle on purpose, and tagged to say so. Added in Phase 9 because the estate
        # had no resource that a detector flags *and* a tag exempts, so the
        # "flagged but exempt" path shipped in Phase 6 appeared in no rendered
        # report -- the one place a client would ever see it.
        name="vm-core-dr-standby",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        tags=(
            ("env", "prod"),
            ("role", "disaster-recovery-standby"),
            ("owner", "platform"),
            ("cost-centre", "NW-1007"),
        ),
        age_days=512,
        cpu_avg_pct="0.8",
        cpu_max_pct="2.6",
        network_in_gb="0.9",
    ),
    _ResSpec(
        # Oversized, in the band where no same-family SKU fits the peak with 2.0x
        # headroom: 8 vCPU x 37.6% x 2.0 = 6.02 required, and the next step down is
        # 4. So the finding is real and its saving is honestly unknown, which is the
        # case the Phase 8 renderer has a whole section for and the mock estate could
        # not produce.
        name="vm-core-api-02",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_D8s_v5",
        tags=(("env", "prod"), ("owner", "platform"), ("cost-centre", "NW-1003")),
        age_days=366,
        cpu_avg_pct="14.2",
        cpu_max_pct="37.6",
        network_in_gb="88.4",
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
        capacity_gb="256",
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
        sku="Premium_LRS_Snapshot",
        capacity_gb="128",
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
        sku="Premium_LRS_Snapshot",
        capacity_gb="128",
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
        utilisation_pct="34.5",
        expires_in_days=488,
    ),
    _RsvSpec(
        # Well used: a precision counter-example.
        reservation_id="rsv-00000000-0000-4000-8000-0000000000c2",
        name="nw-compute-dsv5-1y",
        client=CLIENT_NORTHWIND,
        subscription_index=0,
        reserved_sku="Standard_D4s_v5",
        reserved_kind=ResourceKind.VIRTUAL_MACHINE,
        term_months=12,
        quantity=2,
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
        """Price a resource from the catalogue, in its own region.

        A SKU the catalogue does not price *in that region* yields an explicitly
        unknown cost rather than a price borrowed from somewhere else. Not every SKU
        is sold in every region, and pretending otherwise would quietly invent a
        figure.

        For a per-GB SKU the rate comes from the catalogue and the capacity from this
        provider, which is the division of labour the whole pricing model rests on.
        """
        if spec.sku is None:
            return MoneyAmount.undetermined(f"{spec.name} has no SKU to price")
        priced = self._catalogue.price_for(spec.sku, spec.kind, region)
        if priced is None:
            return MoneyAmount.undetermined(
                f"no catalogue price for sku {spec.sku!r} as {spec.kind.value} in {region}"
            )
        return priced.monthly_for(
            Decimal(spec.capacity_gb) if spec.capacity_gb is not None else None
        )

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
            capacity_gb=Decimal(spec.capacity_gb) if spec.capacity_gb is not None else None,
            monthly_cost=self._resource_monthly(spec, sub.region),
            provenance=self._provenance("mock:resourcegraph.resources"),
        )

    def _reservation_monthly(self, spec: _RsvSpec, sub: _SubSpec | None) -> MoneyAmount:
        """Amortised monthly cost of a commitment, from the real reserved meter.

        What a given customer actually paid is contract data with no public API, but
        a reservation bought at list price costs the published reserved rate. So the
        rate comes from the catalogue and the quantity from this provider, exactly as
        for a per-GB resource -- which keeps the last invented price out of the
        repository.
        """
        region = sub.region if sub else self._catalogue.regions[0]
        priced = self._catalogue.reserved_price_for(spec.reserved_sku, spec.term_months, region)
        if priced is None or priced.monthly_cost.amount is None:
            return MoneyAmount.undetermined(
                f"no reserved price for {spec.reserved_sku} over {spec.term_months} "
                f"months in {region}"
            )
        rate = priced.rate_provenance or priced.monthly_cost.provenance
        return MoneyAmount.of(
            _money(priced.monthly_cost.amount * spec.quantity),
            currency=priced.monthly_cost.currency,
            provenance=Provenance(
                source=ProvenanceSource.AZURE_RETAIL_PRICES,
                retrieved_at=rate.retrieved_at,
                reference=(
                    f"{priced.monthly_cost.display()} per instance per month over a "
                    f"{spec.term_months}-month term x {spec.quantity} instance(s); "
                    f"{priced.meter.describe() if priced.meter else 'no meter'}"
                ),
                verification=rate.verification,
            ),
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
            region=sub.region if sub else self._catalogue.regions[0],
            term_months=spec.term_months,
            quantity=spec.quantity,
            monthly_amortised_cost=self._reservation_monthly(spec, sub),
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

    def series_shape(self, subscription_id: str) -> SeriesShape:
        """What this subscription's spend does over the window.

        A subscription the estate does not know about is steady: inventing an anomaly
        for an unrecognised id would be inventing a finding.
        """
        spec = next((s for s in _SUBSCRIPTIONS if s.subscription_id == subscription_id), None)
        return spec.shape if spec else SeriesShape.STEADY

    def get_cost_series(
        self, subscription_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS
    ) -> CostSeries:
        """Daily actual spend for one subscription over the trailing window.

        A committed daily baseline, multiplied by the subscription's committed
        :class:`SeriesShape` and by a committed day-to-day wobble. Entirely
        deterministic arithmetic: no generator, no seed and no wall clock, so two runs
        of the same estate produce byte-identical series and therefore byte-identical
        anomaly verdicts. A scorecard over a randomised series would mean nothing.

        The baseline is the subscription's own figure rather than a share of its
        resource costs, which is how this used to work. Deriving it meant refreshing
        Azure's prices silently changed every cost series, and it is the less realistic
        model besides -- a real bill includes bandwidth, support and marketplace
        charges no inventory query returns, which is why the observed spend never
        matches the sum of list prices.
        """
        spec = next((s for s in _SUBSCRIPTIONS if s.subscription_id == subscription_id), None)
        shape = spec.shape if spec else SeriesShape.STEADY
        return synthetic_series(
            subscription_id=subscription_id,
            daily_baseline=Decimal(spec.daily_baseline) if spec else Decimal(0),
            shape=shape,
            end=self._as_of.date(),
            window_days=window_days,
            provenance=self._provenance(
                f"mock:costmanagement.query/daily/{subscription_id}/{window_days}d"
                f"?shape={shape.value}"
            ),
        )

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Observed utilisation for one resource, or ``None`` if not measured."""
        return self._metrics.get(resource_id)

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """The priced SKU catalogue for one region."""
        return self._catalogue.for_region(region)
