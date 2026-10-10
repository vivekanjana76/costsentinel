"""Azure tool handlers. Read-only, every one of them.

Each handler takes a typed request, calls one Azure read API, and returns a typed
response carrying ``verified``, ``unknown`` or ``failed``. None of them writes,
deletes or mutates anything, and no mutating client is imported anywhere in this
module -- the dependency is absent, not merely unused.

The Azure SDKs are imported **lazily**, inside the function that needs them, behind
the ``azure`` optional extra. Three reasons: the default install stays small, CI
never has to install them, and the whole test suite drives these handlers through an
injected :class:`AzureClients` double rather than the real thing. The only code paths
not covered offline are the three lines that construct a real SDK client.

Resilience lives here rather than in the provider: every handler converts an SDK
exception into a ``failed`` response with the reason, so one unreachable API degrades
one signal rather than the sweep.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from costsentinel.observability.logging import get_logger
from costsentinel.tools.schemas import (
    AdvisorRecommendationRecord,
    CostPointRecord,
    GetCostSeriesRequest,
    GetCostSeriesResponse,
    GetResourceMetricsRequest,
    GetResourceMetricsResponse,
    ListAdvisorRecommendationsRequest,
    ListAdvisorRecommendationsResponse,
    ListReservationsRequest,
    ListReservationsResponse,
    ListSubscriptionsRequest,
    ListSubscriptionsResponse,
    QueryResourcesRequest,
    QueryResourcesResponse,
    ReservationRecord,
    ResourceRecord,
    SubscriptionRecord,
    ToolStatus,
)

_log = get_logger("tools.azure")

#: API versions pinned so a response shape cannot change under us silently.
RESOURCE_GRAPH_API_VERSION = "2022-10-01"
COST_MANAGEMENT_API_VERSION = "2023-03-01"
MONITOR_API_VERSION = "2019-07-01"
ADVISOR_API_VERSION = "2020-01-01"
RESERVATIONS_API_VERSION = "2022-11-01"

#: How many inventory rows to pull before declaring the result truncated. A real
#: estate can be large, and a partial inventory must be *labelled* partial rather
#: than quietly reported as the whole picture.
MAX_INVENTORY_ROWS = 5000


class AzureClients(Protocol):
    """The Azure read clients a handler may use.

    A protocol rather than a concrete bundle, so the tests inject a double and the
    handlers never know the difference. Every method here is a read; there is no
    member through which a mutation could be requested.
    """

    def resource_graph_query(
        self, query: str, subscriptions: Sequence[str]
    ) -> Iterable[Mapping[str, Any]]:
        """Run a read-only Resource Graph KQL query."""
        ...

    def cost_query(
        self, subscription_id: str, start: date, end: date
    ) -> Iterable[Mapping[str, Any]]:
        """Read daily actual cost rows for one subscription."""
        ...

    def metric_values(
        self, resource_id: str, metric_names: Sequence[str], start: datetime, end: datetime
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Read aggregated metric values for one resource."""
        ...

    def advisor_recommendations(self, subscription_id: str) -> Iterable[Mapping[str, Any]]:
        """Read Advisor recommendations for one subscription."""
        ...

    def reservation_orders(self) -> Iterable[Mapping[str, Any]]:
        """Read reservation orders and their utilisation."""
        ...


class AzureToolError(RuntimeError):
    """An Azure read could not be completed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _nested(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One nested object from an Azure payload, or an empty mapping.

    Azure returns deeply nested, optional structures. Going through a typed helper
    rather than ``payload.get(key) or {}`` keeps the handlers checkable: the type of
    what comes back is a mapping, not whatever the SDK felt like returning.

    The cast asserts the key type of data Azure controls, which no static check can
    establish. It is safe because nothing is trusted on the strength of it: every
    value read out still goes through :func:`_text` or :func:`_decimal`, and the
    whole record is then validated against a Pydantic model.
    """
    value = payload.get(key)
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


def _items(payload: Mapping[str, Any], key: str) -> Sequence[Any]:
    """One nested list of scalars from an Azure payload, or an empty sequence."""
    value = payload.get(key)
    if isinstance(value, list | tuple):
        return cast("Sequence[Any]", value)
    return ()


def _mappings(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    """One nested list of objects, with non-objects dropped.

    Separate from :func:`_items` because a list of strings and a list of records
    want different handling, and conflating them is how an index error reaches a
    report.
    """
    return tuple(
        cast("Mapping[str, Any]", item)
        for item in _items(payload, key)
        if isinstance(item, Mapping)
    )


def _text(payload: Mapping[str, Any], *keys: str) -> str:
    """The first present value among ``keys``, as a string.

    Azure spells the same field differently across API versions -- ``date`` and
    ``UsageDate``, ``cost`` and ``PreTaxCost`` -- so a handler asks for all the
    spellings it knows and takes the first that is there.
    """
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _decimal(value: Any) -> Decimal | None:
    """Parse a number from an Azure payload, or ``None`` if it is not one.

    Returning ``None`` rather than zero matters: an unparseable cost is unknown, and
    a zero would understate a client's spend.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _flatten(properties: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep the scalar properties, drop the nested ones.

    Azure resource properties are arbitrarily deep. Flattening to scalars keeps the
    boundary a flat string map -- which serialises predictably, is safe to render in
    a client-facing audit trail, and cannot smuggle an arbitrary object into a
    validated response.
    """
    if not properties:
        return {}
    return {
        key: str(value)
        for key, value in properties.items()
        if isinstance(value, str | int | float | bool)
    }


def _guard[ResponseT](
    response_type: Callable[..., ResponseT], label: str
) -> Callable[[Callable[[], ResponseT]], ResponseT]:
    """Run a handler body, turning any failure into a ``failed`` response.

    One unreachable API should cost one signal, not the sweep.
    """

    def run(body: Callable[[], ResponseT]) -> ResponseT:
        try:
            return body()
        except Exception as exc:
            _log.warning("azure read failed", extra={"tool": label, "error": str(exc)})
            return response_type(
                status=ToolStatus.FAILED,
                reason=f"{label} failed: {type(exc).__name__}: {exc}",
            )

    return run


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

_SUBSCRIPTIONS_QUERY = (
    "ResourceContainers "
    "| where type =~ 'microsoft.resources/subscriptions' "
    "| project subscriptionId, name, properties, tenantId, tags"
)


def list_subscriptions(
    request: ListSubscriptionsRequest, clients: AzureClients
) -> ListSubscriptionsResponse:
    """Read the subscriptions the caller's credential can see.

    Read-only: a Resource Graph projection, nothing else.
    """

    @_guard(ListSubscriptionsResponse, "azure_list_subscriptions")
    def body() -> ListSubscriptionsResponse:
        rows = list(clients.resource_graph_query(_SUBSCRIPTIONS_QUERY, []))
        records = tuple(
            SubscriptionRecord(
                subscription_id=_text(row, "subscriptionId"),
                display_name=_text(row, "name"),
                state=_text(_nested(row, "properties"), "state"),
                tenant_id=_text(row, "tenantId"),
                tags=_flatten(row.get("tags")),
            )
            for row in rows
            if row.get("subscriptionId")
        )
        if request.tenant_id:
            records = tuple(r for r in records if r.tenant_id == request.tenant_id)
        if not records:
            return ListSubscriptionsResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    "Resource Graph returned no subscriptions. The credential may "
                    "have no Reader assignment, or the tenant filter matched nothing."
                ),
                api_version=RESOURCE_GRAPH_API_VERSION,
                retrieved_at=_now(),
            )
        return ListSubscriptionsResponse(
            status=ToolStatus.VERIFIED,
            api_version=RESOURCE_GRAPH_API_VERSION,
            retrieved_at=_now(),
            subscriptions=records,
        )

    return body


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _inventory_query(resource_types: Sequence[str]) -> str:
    """A read-only Resource Graph projection over one subscription's resources."""
    clauses = ["Resources"]
    if resource_types:
        quoted = ", ".join(f"'{rtype.lower()}'" for rtype in resource_types)
        clauses.append(f"| where tolower(type) in ({quoted})")
    clauses.append(
        "| project id, name, type, location, resourceGroup, subscriptionId, "
        "sku, tags, properties, managedBy"
    )
    return " ".join(clauses)


def query_resources(
    request: QueryResourcesRequest, clients: AzureClients
) -> QueryResourcesResponse:
    """Read the resource inventory of one subscription.

    Read-only: a Resource Graph projection scoped to the subscription the caller
    named.
    """

    @_guard(QueryResourcesResponse, "azure_query_resources")
    def body() -> QueryResourcesResponse:
        rows = list(
            clients.resource_graph_query(
                _inventory_query(request.resource_types), [request.subscription_id]
            )
        )
        truncated = len(rows) > MAX_INVENTORY_ROWS
        records = tuple(
            _resource_record(row, request.subscription_id)
            for row in rows[:MAX_INVENTORY_ROWS]
            if row.get("id")
        )
        if not records:
            return QueryResourcesResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    f"Resource Graph returned no resources for "
                    f"{request.subscription_id}. The subscription may be empty, or "
                    f"the credential may lack Reader on it."
                ),
                api_version=RESOURCE_GRAPH_API_VERSION,
                retrieved_at=_now(),
            )
        return QueryResourcesResponse(
            status=ToolStatus.VERIFIED,
            api_version=RESOURCE_GRAPH_API_VERSION,
            retrieved_at=_now(),
            resources=records,
            truncated=truncated,
        )

    return body


def _resource_record(row: Mapping[str, Any], subscription_id: str) -> ResourceRecord:
    sku = _nested(row, "sku")
    properties = _nested(row, "properties")
    return ResourceRecord(
        resource_id=_text(row, "id"),
        name=_text(row, "name"),
        resource_type=_text(row, "type"),
        location=_text(row, "location"),
        resource_group=_text(row, "resourceGroup"),
        subscription_id=_text(row, "subscriptionId") or subscription_id,
        sku_name=_text(sku, "name") or None,
        sku_tier=_text(sku, "tier") or None,
        tags=_flatten(row.get("tags")),
        properties=_flatten(properties),
        created_time=_parse_datetime(properties.get("timeCreated")),
        disk_size_gb=_decimal(properties.get("diskSizeGB")),
        managed_by=_text(row, "managedBy") or None,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

_TERM_MONTHS: dict[str, int] = {"P1Y": 12, "P3Y": 36, "P5Y": 60}


def list_reservations(
    request: ListReservationsRequest, clients: AzureClients
) -> ListReservationsResponse:
    """Read capacity commitments and their utilisation.

    Read-only: the Reservations list API. Purchase, exchange and refund operations
    are not reachable from here.
    """

    @_guard(ListReservationsResponse, "azure_list_reservations")
    def body() -> ListReservationsResponse:
        rows = list(clients.reservation_orders())
        records: list[ReservationRecord] = []
        for row in rows:
            properties = _nested(row, "properties")
            scope = _items(properties, "appliedScopes")
            scope_subscription = _subscription_from_scope(scope)
            if request.subscription_id and scope_subscription not in (
                None,
                request.subscription_id,
            ):
                continue
            term = _text(properties, "term")
            records.append(
                ReservationRecord(
                    reservation_id=_text(row, "id", "name"),
                    name=_text(properties, "displayName") or _text(row, "name"),
                    reserved_sku=_text(_nested(row, "sku"), "name"),
                    reserved_type=_text(properties, "reservedResourceType"),
                    region=_text(properties, "location"),
                    scope_subscription_id=scope_subscription,
                    term_months=_TERM_MONTHS.get(term, 12),
                    quantity=max(1, int(properties.get("quantity", 1) or 1)),
                    utilisation_pct=_utilisation(properties),
                    expires_on=_parse_date(properties.get("expiryDate")),
                )
            )
        if not records:
            return ListReservationsResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    "No reservations were returned. The enrolment may hold none, or "
                    "the credential may lack reservation reader rights -- which are "
                    "granted separately from subscription Reader."
                ),
                api_version=RESERVATIONS_API_VERSION,
                retrieved_at=_now(),
            )
        return ListReservationsResponse(
            status=ToolStatus.VERIFIED,
            api_version=RESERVATIONS_API_VERSION,
            retrieved_at=_now(),
            reservations=tuple(records),
        )

    return body


def _subscription_from_scope(scopes: Sequence[Any]) -> str | None:
    for scope in scopes:
        text = str(scope)
        if "/subscriptions/" in text:
            return text.split("/subscriptions/", 1)[1].split("/", 1)[0]
    return None


def _utilisation(properties: Mapping[str, Any]) -> Decimal | None:
    aggregates = _mappings(_nested(properties, "utilization"), "aggregates")
    for aggregate in aggregates:
        if _text(aggregate, "grain") in {"30", "30.0", "monthly"}:
            return _decimal(aggregate.get("value"))
    # No monthly grain reported; the first aggregate is the next best answer.
    for aggregate in aggregates[:1]:
        return _decimal(aggregate.get("value"))
    return None


def _parse_date(value: Any) -> date | None:
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed.date()
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cost Management
# ---------------------------------------------------------------------------


def get_cost_series(request: GetCostSeriesRequest, clients: AzureClients) -> GetCostSeriesResponse:
    """Read daily actual spend for one subscription.

    Read-only: a Cost Management usage query. Budgets and exports are not touched.
    """

    @_guard(GetCostSeriesResponse, "azure_get_cost_series")
    def body() -> GetCostSeriesResponse:
        end = _now().date()
        start = end - timedelta(days=request.window_days)
        rows = list(clients.cost_query(request.subscription_id, start, end))

        points: list[CostPointRecord] = []
        currency = ""
        for row in rows:
            day = _parse_date(_text(row, "date", "UsageDate"))
            amount = _decimal(_text(row, "cost", "Cost", "PreTaxCost"))
            currency = currency or _text(row, "currency", "Currency")
            if day is None or amount is None:
                continue
            points.append(CostPointRecord(day=day, amount=max(Decimal(0), amount)))

        if not points:
            return GetCostSeriesResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    f"Cost Management returned no usable daily rows for "
                    f"{request.subscription_id}. Actuals can lag by up to 48 hours, "
                    f"and the credential needs Cost Management Reader specifically."
                ),
                api_version=COST_MANAGEMENT_API_VERSION,
                retrieved_at=_now(),
                subscription_id=request.subscription_id,
            )
        return GetCostSeriesResponse(
            status=ToolStatus.VERIFIED,
            api_version=COST_MANAGEMENT_API_VERSION,
            retrieved_at=_now(),
            subscription_id=request.subscription_id,
            currency=currency or "USD",
            points=tuple(sorted(points, key=lambda point: point.day)),
        )

    return body


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

#: Metrics read per resource. CPU for compute, connections for SQL, and the generic
#: utilisation an App Service plan is sized against.
METRIC_NAMES: tuple[str, ...] = (
    "Percentage CPU",
    "cpu_percent",
    "connection_successful",
    "NetworkIn",
    "CpuPercentage",
)


def _metric(
    values: Mapping[str, Mapping[str, float | None]], *names: str
) -> Mapping[str, float | None]:
    """The first metric present among ``names``, or an empty mapping.

    Azure names the same measurement differently per resource type: a virtual
    machine reports ``Percentage CPU`` and a SQL database ``cpu_percent``.
    """
    for name in names:
        found = values.get(name)
        if found:
            return found
    return {}


def get_resource_metrics(
    request: GetResourceMetricsRequest, clients: AzureClients
) -> GetResourceMetricsResponse:
    """Read observed utilisation for one resource.

    Read-only: a Monitor metrics query. Returns ``unknown`` rather than zeroes when
    Azure has no data, because an unmeasured resource is not an idle one.
    """

    @_guard(GetResourceMetricsResponse, "azure_get_resource_metrics")
    def body() -> GetResourceMetricsResponse:
        end = _now()
        start = end - timedelta(days=request.window_days)
        values = clients.metric_values(request.resource_id, METRIC_NAMES, start, end)

        cpu = _metric(values, "Percentage CPU", "cpu_percent")
        plan_cpu = _metric(values, "CpuPercentage")
        network = _metric(values, "NetworkIn")
        connections = _metric(values, "connection_successful")

        response = GetResourceMetricsResponse(
            status=ToolStatus.VERIFIED,
            api_version=MONITOR_API_VERSION,
            retrieved_at=end,
            resource_id=request.resource_id,
            observation_days=request.window_days,
            cpu_avg_pct=_pct(cpu.get("average")),
            cpu_max_pct=_pct(cpu.get("maximum")),
            network_in_gb=_bytes_to_gb(network.get("total")),
            connection_count=_count(connections.get("total")),
            utilisation_pct=_pct(plan_cpu.get("average")),
        )
        if not response.has_any_metric:
            return GetResourceMetricsResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    f"Monitor returned no metric data for {request.resource_id} over "
                    f"{request.window_days} days. The resource may never have been "
                    f"started, or may not emit these metrics."
                ),
                api_version=MONITOR_API_VERSION,
                retrieved_at=end,
                resource_id=request.resource_id,
                observation_days=request.window_days,
            )
        return response

    return body


def _pct(value: float | None) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return min(Decimal(100), max(Decimal(0), parsed))


def _bytes_to_gb(value: float | None) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return max(Decimal(0), parsed / Decimal(1024**3))


def _count(value: float | None) -> int | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return max(0, int(parsed))


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


def list_advisor_cost_recommendations(
    request: ListAdvisorRecommendationsRequest, clients: AzureClients
) -> ListAdvisorRecommendationsResponse:
    """Read Azure Advisor's own cost recommendations.

    Read-only, and corroboration only: CostSentinel's deterministic detectors decide
    what is wasteful. Advisor agreeing is evidence; Advisor is not a substitute for a
    rule whose precision and recall we can measure.
    """

    @_guard(ListAdvisorRecommendationsResponse, "azure_list_advisor_cost_recommendations")
    def body() -> ListAdvisorRecommendationsResponse:
        rows = list(clients.advisor_recommendations(request.subscription_id))
        records: list[AdvisorRecommendationRecord] = []
        for row in rows:
            properties = _nested(row, "properties")
            if _text(properties, "category").lower() != "cost":
                continue
            short = _nested(properties, "shortDescription")
            extended = _nested(properties, "extendedProperties")
            records.append(
                AdvisorRecommendationRecord(
                    recommendation_id=_text(row, "id", "name"),
                    category=_text(properties, "category"),
                    impact=_text(properties, "impact"),
                    problem=_text(short, "problem"),
                    solution=_text(short, "solution"),
                    resource_id=(
                        _text(_nested(properties, "resourceMetadata"), "resourceId") or None
                    ),
                    annual_saving_amount=_decimal(_text(extended, "annualSavingsAmount")),
                    annual_saving_currency=_text(extended, "savingsCurrency"),
                )
            )
        if not records:
            return ListAdvisorRecommendationsResponse(
                status=ToolStatus.UNKNOWN,
                reason=(
                    f"Advisor returned no cost recommendations for "
                    f"{request.subscription_id}. Advisor needs time to generate them "
                    f"on a new subscription, and may legitimately have none."
                ),
                api_version=ADVISOR_API_VERSION,
                retrieved_at=_now(),
            )
        return ListAdvisorRecommendationsResponse(
            status=ToolStatus.VERIFIED,
            api_version=ADVISOR_API_VERSION,
            retrieved_at=_now(),
            recommendations=tuple(records),
        )

    return body
