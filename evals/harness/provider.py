"""Presenting a dataset to the pipeline as an ordinary estate.

``DatasetProvider`` satisfies the existing
:class:`~costsentinel.providers.base.AzureProvider` protocol, so the graph cannot
tell a labelled dataset from a mock estate or a live one. That is the whole point:
the harness must run the *real* pipeline, and the cheapest way to guarantee it is to
give the pipeline nothing to notice.

Prices come from the committed catalogue by ``(sku, kind, region)`` — the same path
the mock and the real provider use. A dataset therefore cannot assert a price Azure
does not charge, and a resource whose SKU the catalogue does not cover is reported as
an explicitly unknown cost rather than priced by guesswork.

The cost series is flat: ``daily_spend`` repeated across the window. A varied series
would make the observed-spend figure depend on a random seed, and this phase scores
figures exactly rather than approximately.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    CostPoint,
    CostSeries,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    SkuPrice,
    Subscription,
)
from costsentinel.providers.catalogue import PriceCatalogue, catalogue_provenance, default_catalogue
from evals.harness.dataset import (
    EstateSpec,
    LabelledEstate,
    ReservationSpec,
    ResourceSpec,
    SubscriptionSpec,
)

#: Observation window for metrics and the cost series. Matches the mock provider's,
#: so a dataset and the demo estate are scored over the same period.
WINDOW_DAYS = 30

#: ARM provider path per resource kind, so dataset resource ids look like real ones.
#: A detector keyed on an id shape would otherwise behave differently here, which is
#: exactly the sort of difference this module exists to prevent.
_ARM_PATH: dict[ResourceKind, str] = {
    ResourceKind.VIRTUAL_MACHINE: "Microsoft.Compute/virtualMachines",
    ResourceKind.MANAGED_DISK: "Microsoft.Compute/disks",
    ResourceKind.SNAPSHOT: "Microsoft.Compute/snapshots",
    ResourceKind.PUBLIC_IP: "Microsoft.Network/publicIPAddresses",
    ResourceKind.NETWORK_INTERFACE: "Microsoft.Network/networkInterfaces",
    ResourceKind.STORAGE_ACCOUNT: "Microsoft.Storage/storageAccounts",
    ResourceKind.SQL_DATABASE: "Microsoft.Sql/servers/databases",
    ResourceKind.APP_SERVICE_PLAN: "Microsoft.Web/serverfarms",
}


def arm_id(subscription_id: str, resource_group: str, kind: ResourceKind, name: str) -> str:
    """An ARM-shaped resource id."""
    return (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_ARM_PATH[kind]}/{name}"
    )


class DatasetProvider:
    """Read-only access to one labelled dataset's estate."""

    def __init__(
        self,
        dataset: LabelledEstate,
        *,
        as_of: datetime | None = None,
        catalogue: PriceCatalogue | None = None,
    ) -> None:
        """Build the estate up front, so every later call is a lookup.

        Args:
            dataset: The labelled estate to present.
            as_of: The run's reference time. Fixed by default, because a dataset
                whose resource ages drift with the wall clock would move in and out
                of a detector's threshold depending on when the eval ran.
            catalogue: Price snapshot. Defaults to the committed one.
        """
        self._dataset = dataset
        self._spec: EstateSpec = dataset.estate
        self._as_of = as_of or datetime(2026, 10, 1, tzinfo=UTC)
        self._catalogue = catalogue or default_catalogue()

        self._subscriptions = tuple(self._subscription(spec) for spec in self._spec.subscriptions)
        self._resources = tuple(self._resource(spec) for spec in self._spec.resources)
        self._metrics = tuple(
            metrics
            for metrics in (self._resource_metrics(spec) for spec in self._spec.resources)
            if metrics is not None
        )
        self._reservations = tuple(self._reservation(spec) for spec in self._spec.reservations)

    # --- identity --------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return f"eval-dataset:{self._dataset.dataset_id}"

    @property
    def dataset(self) -> LabelledEstate:
        """The dataset this provider presents."""
        return self._dataset

    @property
    def catalogue(self) -> PriceCatalogue:
        """The price snapshot the estate is priced from."""
        return self._catalogue

    def resource_id_for(self, resource_name: str) -> str | None:
        """The ARM id of one dataset resource, for matching a label to a finding."""
        return next(
            (r.resource_id for r in self._resources if r.name == resource_name),
            None,
        )

    # --- AzureProvider ---------------------------------------------------

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        """The dataset's subscriptions, optionally narrowed to one client."""
        if client is None:
            return self._subscriptions
        return tuple(s for s in self._subscriptions if s.client == client)

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        """One subscription's resources."""
        return tuple(r for r in self._resources if r.subscription_id == subscription_id)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        """The dataset's capacity commitments."""
        if client is None:
            return self._reservations
        return tuple(r for r in self._reservations if r.client == client)

    def get_cost_series(
        self, subscription_id: str, *, window_days: int = WINDOW_DAYS
    ) -> CostSeries:
        """A flat daily series, so observed spend is exact rather than sampled."""
        days = max(1, window_days)
        end = self._as_of.date()
        start = end - timedelta(days=days - 1)
        points = tuple(
            CostPoint(day=start + timedelta(days=offset), amount=self._spec.daily_spend)
            for offset in range(days)
        )
        return CostSeries(
            subscription_id=subscription_id,
            points=points,
            provenance=self._provenance(
                f"dataset:{self._dataset.dataset_id}/cost/{subscription_id}/{days}d"
            ),
        )

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Observed utilisation, or ``None`` when the dataset measured nothing.

        ``None`` is a label in its own right: a dataset can state that a resource was
        never measured, and a detector that flags it anyway has invented a fact.
        """
        return next((m for m in self._metrics if m.resource_id == resource_id), None)

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """The priced SKU catalogue for one region."""
        return self._catalogue.for_region(region)

    # --- internals -------------------------------------------------------

    def _provenance(self, reference: str) -> Provenance:
        """Provenance for a dataset-derived fact.

        ``MOCK_PROVIDER`` rather than a new source: a dataset *is* synthetic provider
        data, and adding an ``EVAL_DATASET`` member would mean the domain model knew
        it was sometimes being evaluated — which is the one thing this phase must not
        do.
        """
        return Provenance(
            source=ProvenanceSource.MOCK_PROVIDER,
            retrieved_at=self._as_of,
            reference=reference,
            verification=Verification.VERIFIED,
        )

    def _subscription(self, spec: SubscriptionSpec) -> Subscription:
        return Subscription(
            subscription_id=spec.subscription_id,
            display_name=spec.display_name,
            client=self._spec.client,
            environment=spec.environment,
            tags={"region": spec.region, "client": self._spec.client},
            provenance=self._provenance(f"dataset:{self._dataset.dataset_id}/subscriptions"),
        )

    def _region_of(self, index: int) -> str:
        return self._spec.subscriptions[index].region

    def _monthly_cost(self, spec: ResourceSpec, region: str) -> MoneyAmount:
        """Price one resource from the catalogue, in its own region."""
        if spec.sku is None:
            return MoneyAmount.undetermined(
                f"{spec.name} has no SKU, so no catalogue price applies"
            )
        priced = self._catalogue.price_for(spec.sku, spec.kind, region)
        if priced is None:
            return MoneyAmount.undetermined(
                f"the catalogue has no price for {spec.sku} as a {spec.kind.value} in {region}"
            )
        return priced.monthly_for(spec.capacity_gb)

    def _resource(self, spec: ResourceSpec) -> Resource:
        subscription = self._spec.subscriptions[spec.subscription]
        attached_to = (
            arm_id(
                subscription.subscription_id,
                spec.resource_group,
                spec.attached_to_kind,
                spec.attached_to,
            )
            if spec.attached_to
            else None
        )
        return Resource(
            resource_id=arm_id(
                subscription.subscription_id, spec.resource_group, spec.kind, spec.name
            ),
            name=spec.name,
            kind=spec.kind,
            subscription_id=subscription.subscription_id,
            resource_group=spec.resource_group,
            region=subscription.region,
            sku=spec.sku,
            state=spec.state,
            attached_to=attached_to,
            tags=dict(spec.tags),
            created_at=self._as_of - timedelta(days=spec.age_days),
            capacity_gb=spec.capacity_gb,
            monthly_cost=self._monthly_cost(spec, subscription.region),
            provenance=self._provenance(
                f"dataset:{self._dataset.dataset_id}/resources/{spec.name}"
            ),
        )

    def _resource_metrics(self, spec: ResourceSpec) -> ResourceMetrics | None:
        if not spec.has_metrics:
            return None
        subscription = self._spec.subscriptions[spec.subscription]
        return ResourceMetrics(
            resource_id=arm_id(
                subscription.subscription_id, spec.resource_group, spec.kind, spec.name
            ),
            observation_days=WINDOW_DAYS,
            cpu_avg_pct=spec.cpu_avg_pct,
            cpu_max_pct=spec.cpu_max_pct,
            network_in_gb=spec.network_in_gb,
            connection_count=spec.connection_count,
            utilisation_pct=spec.utilisation_pct,
            provenance=self._provenance(f"dataset:{self._dataset.dataset_id}/metrics/{spec.name}"),
        )

    def _reservation(self, spec: ReservationSpec) -> Reservation:
        subscription = (
            self._spec.subscriptions[spec.subscription] if spec.subscription is not None else None
        )
        region = subscription.region if subscription else self._catalogue.regions[0]
        priced = self._catalogue.reserved_price_for(spec.reserved_sku, spec.term_months, region)
        if priced is not None and priced.monthly_cost.amount is not None:
            amortised = MoneyAmount.of(
                priced.monthly_cost.amount * Decimal(spec.quantity),
                currency=priced.monthly_cost.currency,
                provenance=catalogue_provenance(
                    self._catalogue,
                    reference_suffix=(
                        f"{spec.term_months}-month reserved rate for "
                        f"{spec.reserved_sku} x {spec.quantity}"
                    ),
                    meter=priced.meter,
                ),
            )
        else:
            amortised = MoneyAmount.undetermined(
                f"the catalogue has no {spec.term_months}-month reserved price for "
                f"{spec.reserved_sku} in {region}"
            )
        return Reservation(
            reservation_id=f"/providers/Microsoft.Capacity/reservations/{spec.name}",
            name=spec.name,
            client=self._spec.client,
            scope_subscription_id=(
                subscription.subscription_id if subscription is not None else None
            ),
            reserved_sku=spec.reserved_sku,
            reserved_kind=spec.reserved_kind,
            region=region,
            term_months=spec.term_months,
            quantity=spec.quantity,
            monthly_amortised_cost=amortised,
            utilisation_pct=spec.utilisation_pct,
            expires_on=self._expiry(spec.expires_in_days),
            provenance=self._provenance(
                f"dataset:{self._dataset.dataset_id}/reservations/{spec.name}"
            ),
        )

    def _expiry(self, days: int) -> date:
        return (self._as_of + timedelta(days=days)).date()
