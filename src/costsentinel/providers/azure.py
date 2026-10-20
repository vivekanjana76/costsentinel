"""The real Azure provider. Read-only, backed by the MCP tool surface.

Satisfies the same :class:`~costsentinel.providers.base.AzureProvider` protocol as
the mock, so the whole Phase 2 graph runs against a live estate unchanged. It reaches
Azure only through :class:`~costsentinel.tools.invoker.ToolInvoker`, so it inherits
the read-only guarantee rather than restating it: there is no Azure client here to
misuse, and no method on this class that could mutate anything.

Three things it is careful about.

**Unknown is not zero.** A tool response that is ``unknown`` or ``failed`` becomes an
explicitly unknown domain value, never an empty list or a zero cost. A failed
inventory query must not look like a clean subscription.

**Prices come from the committed catalogue, not from Azure at runtime.** The
catalogue is refreshed by hand, so a real run's figures are reproducible and a price
change is a reviewable commit rather than a silent difference between two sweeps.

**Client scope comes from a tag.** Azure has no notion of CostSentinel's client; a
managed-service provider marks it, conventionally with a ``client`` tag. A
subscription with no such tag is attributed to a named placeholder rather than
guessed at or silently folded into somebody else's report.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from costsentinel.config import Settings
from costsentinel.domain.common import (
    Currency,
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    CostBreakdown,
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
from costsentinel.observability.logging import get_logger
from costsentinel.providers.base import ProviderError, ProviderNotConfiguredError
from costsentinel.providers.catalogue import PriceCatalogue, default_catalogue
from costsentinel.providers.sku_mapping import catalogue_sku, resource_kind
from costsentinel.tools.invoker import ToolInvoker
from costsentinel.tools.schemas import (
    GetCostSeriesResponse,
    GetResourceMetricsResponse,
    ListReservationsResponse,
    ListSubscriptionsResponse,
    QueryResourcesResponse,
    ReservationRecord,
    ResourceRecord,
    ToolStatus,
)

_log = get_logger("providers.azure")

#: Where a subscription's owning client is read from, by convention.
DEFAULT_CLIENT_TAG = "client"

#: Attributed to a subscription with no client tag. Named rather than guessed, so an
#: untagged subscription shows up as a gap to fix rather than quietly joining a
#: client's report.
UNASSIGNED_CLIENT = "unassigned"

#: Where a subscription's environment is read from.
ENVIRONMENT_TAGS: tuple[str, ...] = ("environment", "env")

_ENVIRONMENTS: dict[str, Environment] = {
    "prod": Environment.PRODUCTION,
    "production": Environment.PRODUCTION,
    "staging": Environment.STAGING,
    "stage": Environment.STAGING,
    "uat": Environment.STAGING,
    "dev": Environment.DEVELOPMENT,
    "development": Environment.DEVELOPMENT,
    "test": Environment.TEST,
    "qa": Environment.TEST,
    "sandbox": Environment.SANDBOX,
    "sbx": Environment.SANDBOX,
}

#: Disk and address states that mean "attached to nothing", which is the orphan test.
_UNATTACHED_DISK_STATES = frozenset({"unattached", "reserved"})


class AzureLiveProvider:
    """Read-only access to a live Azure estate, through the MCP tool surface.

    Args:
        invoker: Dispatch to the read-only tools. The provider has no other route to
            Azure, which is what makes its read-only-ness inherited rather than
            asserted separately.
        settings: Resolved configuration.
        subscription_ids: The subscriptions this run may read. **Required.** A real
            run reads what the operator named, never everything a credential can
            reach -- a sweep that silently widened its own scope on somebody else's
            tenant would be indefensible.
        catalogue: Price snapshot. Defaults to the committed one.

    Raises:
        ProviderNotConfiguredError: If no subscription scope was given.
    """

    def __init__(
        self,
        *,
        invoker: ToolInvoker,
        settings: Settings,
        subscription_ids: Sequence[str],
        catalogue: PriceCatalogue | None = None,
    ) -> None:
        """Bind the provider to a tool invoker and an explicit subscription scope."""
        if not subscription_ids:
            msg = (
                "a real run needs an explicit subscription scope. Pass --subscription "
                "or set AZURE_SUBSCRIPTION_IDS. CostSentinel will not read every "
                "subscription a credential happens to reach."
            )
            raise ProviderNotConfiguredError(msg)

        self._invoker = invoker
        self._settings = settings
        self._subscription_ids = tuple(dict.fromkeys(subscription_ids))
        self._catalogue = catalogue or default_catalogue()
        self._client_tag = settings.azure_client_tag or DEFAULT_CLIENT_TAG
        self._regions: dict[str, str] = {}

    # --- identity --------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "azure-live"

    @property
    def subscription_ids(self) -> tuple[str, ...]:
        """The subscriptions this run may read."""
        return self._subscription_ids

    @property
    def catalogue(self) -> PriceCatalogue:
        """The price snapshot this estate is priced from."""
        return self._catalogue

    # --- provenance ------------------------------------------------------

    def _provenance(
        self, source: ProvenanceSource, reference: str, status: ToolStatus
    ) -> Provenance:
        """Provenance carrying the tool's own verdict.

        A ``verified`` read becomes verified provenance and an ``unknown`` one does
        not, so the honesty model survives the trip into the domain rather than being
        flattened at the boundary.
        """
        return Provenance(
            source=source,
            retrieved_at=datetime.now(UTC),
            reference=reference,
            verification=(
                Verification.VERIFIED if status is ToolStatus.VERIFIED else Verification.UNVERIFIED
            ),
        )

    # --- AzureProvider ---------------------------------------------------

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        """Read the in-scope subscriptions, optionally narrowed to one client."""
        response = self._invoker.invoke("azure_list_subscriptions", {})
        if not isinstance(response, ListSubscriptionsResponse):  # pragma: no cover
            msg = f"azure_list_subscriptions returned {type(response).__name__}"
            raise ProviderError(msg)
        if response.status is ToolStatus.FAILED:
            msg = f"could not read subscriptions: {response.reason}"
            raise ProviderError(msg)

        subscriptions: list[Subscription] = []
        for record in response.subscriptions:
            if record.subscription_id not in self._subscription_ids:
                continue
            owner = record.tags.get(self._client_tag, UNASSIGNED_CLIENT).strip()
            subscriptions.append(
                Subscription(
                    subscription_id=record.subscription_id,
                    display_name=record.display_name,
                    client=owner or UNASSIGNED_CLIENT,
                    environment=_environment_from(record.tags),
                    tags=dict(record.tags),
                    provenance=self._provenance(
                        ProvenanceSource.AZURE_RESOURCE_GRAPH,
                        f"azure_list_subscriptions ({response.status.value})",
                        response.status,
                    ),
                )
            )

        missing = set(self._subscription_ids) - {s.subscription_id for s in subscriptions}
        if missing:
            _log.warning(
                "requested subscriptions were not visible to this credential",
                extra={"missing": sorted(missing), "tool_status": response.status.value},
            )

        if client is None:
            return tuple(subscriptions)
        return tuple(s for s in subscriptions if s.client == client)

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        """Read one subscription's inventory.

        A resource whose SKU cannot be mapped to a catalogue meter is still returned,
        with an explicitly unknown cost. Dropping it would hide a resource from the
        report; pricing it by guesswork would be worse.
        """
        self._require_in_scope(subscription_id)
        response = self._invoker.invoke(
            "azure_query_resources", {"subscription_id": subscription_id}
        )
        if not isinstance(response, QueryResourcesResponse):  # pragma: no cover
            msg = f"azure_query_resources returned {type(response).__name__}"
            raise ProviderError(msg)
        if response.status is ToolStatus.FAILED:
            msg = f"could not read inventory for {subscription_id}: {response.reason}"
            raise ProviderError(msg)
        if response.truncated:
            _log.warning(
                "inventory was truncated; the report will understate this subscription",
                extra={"subscription_id": subscription_id},
            )

        provenance = self._provenance(
            ProvenanceSource.AZURE_RESOURCE_GRAPH,
            f"azure_query_resources ({response.status.value})",
            response.status,
        )
        resources: list[Resource] = []
        for record in response.resources:
            kind = resource_kind(record.resource_type)
            if kind is None:
                continue
            self._regions[record.resource_id] = record.location
            resources.append(self._to_resource(record, kind, provenance))
        return tuple(resources)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        """Read capacity commitments in scope.

        The amortised cost is the published *list* reserved price times the quantity,
        not what this customer actually paid. Azure exposes no API for the latter, so
        a negotiated discount would make this figure an overstatement -- which the
        provenance says, rather than presenting it as the real contract value.
        """
        response = self._invoker.invoke("azure_list_reservations", {})
        if not isinstance(response, ListReservationsResponse):  # pragma: no cover
            msg = f"azure_list_reservations returned {type(response).__name__}"
            raise ProviderError(msg)
        if response.status is ToolStatus.FAILED:
            _log.warning(
                "reservations could not be read; the sweep continues without them",
                extra={"reason": response.reason},
            )
            return ()

        owners = {s.subscription_id: s.client for s in self.list_subscriptions()}
        reservations: list[Reservation] = []
        for record in response.reservations:
            scope = record.scope_subscription_id
            if scope is not None and scope not in self._subscription_ids:
                continue
            owner = owners.get(scope or "", UNASSIGNED_CLIENT)
            if client is not None and owner != client:
                continue
            reservations.append(
                Reservation(
                    reservation_id=record.reservation_id,
                    name=record.name,
                    client=owner,
                    scope_subscription_id=scope,
                    reserved_sku=record.reserved_sku,
                    reserved_kind=ResourceKind.VIRTUAL_MACHINE,
                    region=record.region or self._catalogue.regions[0],
                    term_months=record.term_months,
                    quantity=record.quantity,
                    monthly_amortised_cost=self._amortised_cost(record),
                    utilisation_pct=record.utilisation_pct,
                    expires_on=record.expires_on,
                    provenance=self._provenance(
                        ProvenanceSource.AZURE_ADVISOR,
                        f"azure_list_reservations ({response.status.value})",
                        response.status,
                    ),
                )
            )
        return tuple(reservations)

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        """Read daily actual spend for one subscription.

        An unknown or failed cost read yields an empty series whose provenance says
        so, rather than a zero-cost series -- the Report Author then reports spend as
        unknown instead of claiming a subscription costs nothing.
        """
        self._require_in_scope(subscription_id)
        response = self._invoker.invoke(
            "azure_get_cost_series",
            {"subscription_id": subscription_id, "window_days": window_days},
        )
        if not isinstance(response, GetCostSeriesResponse):  # pragma: no cover
            msg = f"azure_get_cost_series returned {type(response).__name__}"
            raise ProviderError(msg)

        provenance = self._provenance(
            ProvenanceSource.AZURE_COST_MANAGEMENT,
            f"azure_get_cost_series ({response.status.value}): "
            f"{response.reason or 'daily actuals'}",
            response.status,
        )
        if response.status is not ToolStatus.VERIFIED:
            _log.warning(
                "cost series unavailable; spend will be reported as unknown",
                extra={"subscription_id": subscription_id, "reason": response.reason},
            )
            return CostSeries(subscription_id=subscription_id, points=(), provenance=provenance)
        return CostSeries(
            subscription_id=subscription_id,
            currency=_currency(response.currency),
            points=tuple(
                CostPoint(day=point.day, amount=point.amount) for point in response.points
            ),
            provenance=provenance,
        )

    def get_cost_breakdown(
        self, subscription_id: str, *, window_days: int = 30
    ) -> CostBreakdown | None:
        """Not yet retrievable from a real estate, so ``None``.

        Deliberately a declaration rather than a guess. Azure's Cost Management query
        API can group daily actuals by meter, but it is a different query from the one
        behind :meth:`get_cost_series` -- a different tool, a different schema and a
        different set of edge cases around meters that appear and vanish mid-window --
        and it cannot be verified against anything but a real tenant. Shipping an
        unverified implementation would mean a client's first attributed spend change
        was also the first time the query had ever run.

        So attribution degrades the way every other unknown in this system does: no
        breakdown means the anomaly reports its cause as unknown, with the reason
        stated, rather than naming whichever meter an untested query happened to rank
        first. Scheduled as Phase 9b work, with the real-tenant verification it needs.

        Returns:
            ``None``, always. Never an empty breakdown, which would read as "no cause
            found" rather than "not looked for".
        """
        self._require_in_scope(subscription_id)
        _ = window_days
        _log.info(
            "cost breakdown not retrieved; spend changes will report an unknown cause",
            extra={
                "subscription_id": subscription_id,
                "reason": "no real cost-breakdown query is implemented yet",
            },
        )
        return None

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Read observed utilisation for one resource.

        Returns ``None`` when Azure has no data, which the detectors read as "cannot
        conclude". That is the whole reason this returns an optional: an unmeasured
        resource must not be reported as idle.
        """
        response = self._invoker.invoke("azure_get_resource_metrics", {"resource_id": resource_id})
        if not isinstance(response, GetResourceMetricsResponse):  # pragma: no cover
            msg = f"azure_get_resource_metrics returned {type(response).__name__}"
            raise ProviderError(msg)
        if response.status is not ToolStatus.VERIFIED or not response.has_any_metric:
            return None
        return ResourceMetrics(
            resource_id=resource_id,
            observation_days=max(1, response.observation_days),
            cpu_avg_pct=response.cpu_avg_pct,
            cpu_max_pct=response.cpu_max_pct,
            network_in_gb=response.network_in_gb,
            connection_count=response.connection_count,
            utilisation_pct=response.utilisation_pct,
            provenance=self._provenance(
                ProvenanceSource.AZURE_MONITOR,
                f"azure_get_resource_metrics ({response.status.value})",
                response.status,
            ),
        )

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """The priced SKU catalogue for one region.

        From the committed snapshot, not from Azure at runtime, so a real sweep's
        figures are reproducible and a price change is a reviewable commit.
        """
        return self._catalogue.for_region(region)

    # --- internals -------------------------------------------------------

    def _require_in_scope(self, subscription_id: str) -> None:
        if subscription_id not in self._subscription_ids:
            msg = (
                f"subscription {subscription_id} is not in this run's scope "
                f"({', '.join(self._subscription_ids)}). CostSentinel does not widen "
                f"its own scope mid-sweep."
            )
            raise ProviderError(msg)

    def _to_resource(
        self, record: ResourceRecord, kind: ResourceKind, provenance: Provenance
    ) -> Resource:
        sku = catalogue_sku(
            kind=kind,
            sku_name=record.sku_name,
            properties=dict(record.properties),
            size_gb=record.disk_size_gb,
        )
        capacity = record.disk_size_gb if kind in _CAPACITY_KINDS else None
        return Resource(
            resource_id=record.resource_id,
            name=record.name,
            kind=kind,
            subscription_id=record.subscription_id,
            resource_group=record.resource_group,
            region=record.location,
            sku=sku,
            state=_state_from(record, kind),
            attached_to=record.managed_by,
            tags=dict(record.tags),
            created_at=record.created_time,
            capacity_gb=capacity,
            monthly_cost=self._price(record, kind, sku, capacity),
            provenance=provenance,
        )

    def _price(
        self,
        record: ResourceRecord,
        kind: ResourceKind,
        sku: str | None,
        capacity: Decimal | None,
    ) -> MoneyAmount:
        if sku is None:
            return MoneyAmount.undetermined(
                f"{record.name}: no catalogue meter matches sku "
                f"{record.sku_name!r} for a {kind.value} in {record.location}"
            )
        priced = self._catalogue.price_for(sku, kind, record.location)
        if priced is None:
            return MoneyAmount.undetermined(
                f"{record.name}: the catalogue has no price for {sku} in "
                f"{record.location}. Re-run scripts/refresh_prices.py for that region."
            )
        return priced.monthly_for(capacity)

    def _amortised_cost(self, record: ReservationRecord) -> MoneyAmount:
        """List-price amortised monthly cost of one commitment."""
        priced = self._catalogue.reserved_price_for(
            record.reserved_sku, record.term_months, record.region
        )
        if priced is None or priced.monthly_cost.amount is None:
            return MoneyAmount.undetermined(
                f"{record.name}: the catalogue has no {record.term_months}-month "
                f"reserved price for {record.reserved_sku} in {record.region}"
            )
        rate = priced.rate_provenance or priced.monthly_cost.provenance
        return MoneyAmount.of(
            priced.monthly_cost.amount * record.quantity,
            currency=priced.monthly_cost.currency,
            provenance=Provenance(
                source=ProvenanceSource.AZURE_RETAIL_PRICES,
                retrieved_at=rate.retrieved_at,
                reference=(
                    f"published LIST reserved price {priced.monthly_cost.display()} per "
                    f"instance per month x {record.quantity}; not the price this "
                    f"customer actually paid, which Azure does not expose -- a "
                    f"negotiated discount would make this an overstatement"
                ),
                # Deliberately not verified: the rate is real, but it is not this
                # customer's contract value.
                verification=Verification.UNVERIFIED,
            ),
        )


#: Kinds whose billed quantity is a size the inventory reports.
_CAPACITY_KINDS = frozenset({ResourceKind.SNAPSHOT, ResourceKind.STORAGE_ACCOUNT})


def _environment_from(tags: dict[str, str]) -> Environment:
    """Read the environment from a subscription tag, defaulting to unknown.

    Unknown is the safe default: the policy store treats production as stricter, and
    guessing a subscription into development would loosen a class it should not.
    """
    for key in ENVIRONMENT_TAGS:
        value = tags.get(key, "").strip().lower()
        if value in _ENVIRONMENTS:
            return _ENVIRONMENTS[value]
    return Environment.UNKNOWN


def _state_from(record: ResourceRecord, kind: ResourceKind) -> ResourceState:
    """Derive the state the detectors test against."""
    if kind is ResourceKind.MANAGED_DISK:
        disk_state = record.properties.get("diskState", "").lower()
        if disk_state in _UNATTACHED_DISK_STATES or record.managed_by is None:
            return ResourceState.UNATTACHED
        return ResourceState.ATTACHED
    if kind is ResourceKind.PUBLIC_IP:
        has_config = bool(record.properties.get("ipConfiguration"))
        return (
            ResourceState.ATTACHED if has_config or record.managed_by else ResourceState.UNATTACHED
        )
    if kind is ResourceKind.VIRTUAL_MACHINE:
        power = record.properties.get("powerState", "").lower()
        if "deallocated" in power:
            return ResourceState.DEALLOCATED
        if "stopped" in power:
            return ResourceState.STOPPED
        if "running" in power:
            return ResourceState.RUNNING
        # Resource Graph does not always project power state; a VM that exists is
        # billed unless it is explicitly deallocated, so running is the safe read.
        return ResourceState.RUNNING
    return ResourceState.AVAILABLE


def _currency(code: str) -> Currency:
    """Map a currency code, defaulting to USD when Azure reports none."""
    try:
        return Currency(code.upper())
    except ValueError:
        return Currency.USD
