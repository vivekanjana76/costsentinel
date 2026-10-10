"""Azure tool surface tests, driven entirely by synthetic responses.

No test here reaches the network. Every handler is called through an injected
:class:`FakeAzureClients` holding payloads shaped like the real API responses, which
is what lets the whole surface be covered with CI offline.

The assertions that matter are about the honesty model: a failed call carries no
data and says why, an unknown call is distinguishable from an empty one, and an
unparseable number is dropped rather than coerced to zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from costsentinel.tools.azure import (
    MAX_INVENTORY_ROWS,
    METRIC_NAMES,
    get_cost_series,
    get_resource_metrics,
    list_advisor_cost_recommendations,
    list_reservations,
    list_subscriptions,
    query_resources,
)
from costsentinel.tools.invoker import LocalToolInvoker, describe_surface, tool_specs
from costsentinel.tools.schemas import (
    GetCostSeriesRequest,
    GetResourceMetricsRequest,
    ListAdvisorRecommendationsRequest,
    ListReservationsRequest,
    ListSubscriptionsRequest,
    ListSubscriptionsResponse,
    QueryResourcesRequest,
    QueryResourcesResponse,
    ToolStatus,
)

SUBSCRIPTION = "00000000-0000-4000-8000-00000000a001"


class FakeAzureClients:
    """Synthetic Azure read clients, shaped like the real payloads.

    Each method can be told to raise, which is how the failure paths are covered
    without an unreachable API.
    """

    def __init__(
        self,
        *,
        graph_rows: Sequence[Mapping[str, Any]] = (),
        cost_rows: Sequence[Mapping[str, Any]] = (),
        metrics: Mapping[str, Mapping[str, float | None]] | None = None,
        advisor_rows: Sequence[Mapping[str, Any]] = (),
        reservation_rows: Sequence[Mapping[str, Any]] = (),
        raises: str | None = None,
    ) -> None:
        """Hold the synthetic payloads each read should return."""
        self._graph_rows = graph_rows
        self._cost_rows = cost_rows
        self._metrics = metrics or {}
        self._advisor_rows = advisor_rows
        self._reservation_rows = reservation_rows
        self._raises = raises
        self.queries: list[str] = []
        self.scopes: list[Sequence[str]] = []

    def _maybe_raise(self, what: str) -> None:
        if self._raises == what:
            msg = f"synthetic {what} outage"
            raise RuntimeError(msg)

    def resource_graph_query(
        self, query: str, subscriptions: Sequence[str]
    ) -> Iterable[Mapping[str, Any]]:
        self._maybe_raise("graph")
        self.queries.append(query)
        self.scopes.append(tuple(subscriptions))
        return self._graph_rows

    def cost_query(
        self, subscription_id: str, start: date, end: date
    ) -> Iterable[Mapping[str, Any]]:
        self._maybe_raise("cost")
        _ = (subscription_id, start, end)
        return self._cost_rows

    def metric_values(
        self,
        resource_id: str,
        metric_names: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> Mapping[str, Mapping[str, float | None]]:
        self._maybe_raise("metrics")
        _ = (resource_id, metric_names, start, end)
        return self._metrics

    def advisor_recommendations(self, subscription_id: str) -> Iterable[Mapping[str, Any]]:
        self._maybe_raise("advisor")
        _ = subscription_id
        return self._advisor_rows

    def reservation_orders(self) -> Iterable[Mapping[str, Any]]:
        self._maybe_raise("reservations")
        return self._reservation_rows


# ---------------------------------------------------------------------------
# The honesty model
# ---------------------------------------------------------------------------


def test_a_non_verified_response_must_say_why() -> None:
    """An unexplained non-verified result is indistinguishable from a bug."""
    with pytest.raises(ValueError, match="must say why"):
        ListSubscriptionsResponse(status=ToolStatus.FAILED)
    with pytest.raises(ValueError, match="must say why"):
        ListSubscriptionsResponse(status=ToolStatus.UNKNOWN, reason="   ")


def test_a_verified_response_needs_no_reason() -> None:
    response = ListSubscriptionsResponse(status=ToolStatus.VERIFIED)
    assert response.reason == ""
    assert response.status.is_usable


def test_a_failed_response_must_not_carry_data() -> None:
    """Otherwise a partial failure could be read as a partial success."""
    from costsentinel.tools.schemas import SubscriptionRecord

    with pytest.raises(ValueError, match="must not carry data"):
        ListSubscriptionsResponse(
            status=ToolStatus.FAILED,
            reason="boom",
            subscriptions=(SubscriptionRecord(subscription_id="s", display_name="s"),),
        )


def test_only_verified_is_usable() -> None:
    assert ToolStatus.VERIFIED.is_usable
    assert not ToolStatus.UNKNOWN.is_usable
    assert not ToolStatus.FAILED.is_usable


def test_a_tool_response_rejects_an_unexpected_field() -> None:
    """Azure controls these payloads, so an extra field is an error not a pass-through."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ListSubscriptionsResponse.model_validate({"status": "verified", "surprise": "value"})


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def test_subscriptions_are_read_and_projected() -> None:
    clients = FakeAzureClients(
        graph_rows=[
            {
                "subscriptionId": SUBSCRIPTION,
                "name": "NW-PROD-Core",
                "properties": {"state": "Enabled"},
                "tenantId": "tenant-1",
                "tags": {"env": "prod", "nested": {"dropped": True}},
            }
        ]
    )
    response = list_subscriptions(ListSubscriptionsRequest(), clients)
    assert response.status is ToolStatus.VERIFIED
    assert len(response.subscriptions) == 1
    record = response.subscriptions[0]
    assert record.subscription_id == SUBSCRIPTION
    assert record.display_name == "NW-PROD-Core"
    assert record.state == "Enabled"
    # Nested tag objects are dropped; the boundary stays a flat string map.
    assert record.tags == {"env": "prod"}


def test_no_subscriptions_is_unknown_not_an_empty_success() -> None:
    """An empty result and a missing Reader assignment look identical otherwise."""
    response = list_subscriptions(ListSubscriptionsRequest(), FakeAzureClients())
    assert response.status is ToolStatus.UNKNOWN
    assert "Reader" in response.reason
    assert response.subscriptions == ()


def test_subscriptions_can_be_filtered_by_tenant() -> None:
    clients = FakeAzureClients(
        graph_rows=[
            {"subscriptionId": "a", "name": "A", "tenantId": "t1"},
            {"subscriptionId": "b", "name": "B", "tenantId": "t2"},
        ]
    )
    response = list_subscriptions(ListSubscriptionsRequest(tenant_id="t2"), clients)
    assert [r.subscription_id for r in response.subscriptions] == ["b"]


def test_a_graph_outage_is_a_failure_with_a_reason() -> None:
    response = list_subscriptions(ListSubscriptionsRequest(), FakeAzureClients(raises="graph"))
    assert response.status is ToolStatus.FAILED
    assert "synthetic graph outage" in response.reason
    assert response.subscriptions == ()


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _disk_row(*, managed_by: str | None = None) -> dict[str, Any]:
    return {
        "id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.Compute/disks/d1"
        ),
        "name": "d1",
        "type": "microsoft.compute/disks",
        "location": "eastus",
        "resourceGroup": "rg",
        "subscriptionId": SUBSCRIPTION,
        "sku": {"name": "Premium_LRS", "tier": "Premium"},
        "tags": {"env": "dev"},
        "properties": {
            "diskSizeGB": 256,
            "timeCreated": "2025-02-01T00:00:00Z",
            "diskState": "Unattached",
            "nested": {"dropped": True},
        },
        "managedBy": managed_by,
    }


def test_inventory_is_read_and_projected() -> None:
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    response = query_resources(QueryResourcesRequest(subscription_id=SUBSCRIPTION), clients)

    assert response.status is ToolStatus.VERIFIED
    record = response.resources[0]
    assert record.name == "d1"
    assert record.sku_name == "Premium_LRS"
    assert record.sku_tier == "Premium"
    assert record.disk_size_gb == Decimal(256)
    assert record.created_time == datetime(2025, 2, 1, tzinfo=UTC)
    assert record.properties["diskState"] == "Unattached"
    assert "nested" not in record.properties
    assert not response.truncated


def test_an_unattached_resource_reports_no_owner() -> None:
    """``managedBy`` is the orphan test, so it has to survive the boundary."""
    attached = query_resources(
        QueryResourcesRequest(subscription_id=SUBSCRIPTION),
        FakeAzureClients(graph_rows=[_disk_row(managed_by="/subscriptions/x/vm")]),
    )
    orphan = query_resources(
        QueryResourcesRequest(subscription_id=SUBSCRIPTION),
        FakeAzureClients(graph_rows=[_disk_row(managed_by=None)]),
    )
    assert attached.resources[0].managed_by is not None
    assert orphan.resources[0].managed_by is None


def test_inventory_is_scoped_to_the_subscription_asked_for() -> None:
    """A real run reads what the operator named, not everything visible."""
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    query_resources(QueryResourcesRequest(subscription_id=SUBSCRIPTION), clients)
    assert clients.scopes == [(SUBSCRIPTION,)]


def test_inventory_can_be_narrowed_by_resource_type() -> None:
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    query_resources(
        QueryResourcesRequest(
            subscription_id=SUBSCRIPTION, resource_types=("Microsoft.Compute/disks",)
        ),
        clients,
    )
    assert "microsoft.compute/disks" in clients.queries[0]


def test_a_partial_inventory_is_labelled_partial() -> None:
    """A truncated inventory read as complete would under-report an estate."""
    rows = [{**_disk_row(), "name": f"d{index}"} for index in range(MAX_INVENTORY_ROWS + 5)]
    response = query_resources(
        QueryResourcesRequest(subscription_id=SUBSCRIPTION), FakeAzureClients(graph_rows=rows)
    )
    assert response.truncated
    assert len(response.resources) == MAX_INVENTORY_ROWS


def test_an_empty_inventory_is_unknown_not_verified() -> None:
    response = query_resources(
        QueryResourcesRequest(subscription_id=SUBSCRIPTION), FakeAzureClients()
    )
    assert response.status is ToolStatus.UNKNOWN
    assert SUBSCRIPTION in response.reason


def test_a_row_with_no_id_is_skipped() -> None:
    clients = FakeAzureClients(graph_rows=[{"name": "nameless"}])
    response = query_resources(QueryResourcesRequest(subscription_id=SUBSCRIPTION), clients)
    assert response.status is ToolStatus.UNKNOWN


def test_an_unparseable_creation_time_becomes_none_not_an_error() -> None:
    row = {**_disk_row(), "properties": {"timeCreated": "not-a-date"}}
    response = query_resources(
        QueryResourcesRequest(subscription_id=SUBSCRIPTION), FakeAzureClients(graph_rows=[row])
    )
    assert response.resources[0].created_time is None


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_cost_rows_are_read_and_sorted() -> None:
    clients = FakeAzureClients(
        cost_rows=[
            {"date": "2026-09-02", "cost": "12.50", "currency": "USD"},
            {"date": "2026-09-01", "cost": "10.00", "currency": "USD"},
        ]
    )
    response = get_cost_series(GetCostSeriesRequest(subscription_id=SUBSCRIPTION), clients)
    assert response.status is ToolStatus.VERIFIED
    assert [point.day.isoformat() for point in response.points] == ["2026-09-01", "2026-09-02"]
    assert response.points[1].amount == Decimal("12.50")
    assert response.currency == "USD"


def test_cost_rows_tolerate_the_alternative_field_spellings() -> None:
    """Azure spells these differently across API versions."""
    clients = FakeAzureClients(
        cost_rows=[{"UsageDate": "2026-09-01", "PreTaxCost": "7.25", "Currency": "EUR"}]
    )
    response = get_cost_series(GetCostSeriesRequest(subscription_id=SUBSCRIPTION), clients)
    assert response.points[0].amount == Decimal("7.25")
    assert response.currency == "EUR"


def test_an_unparseable_cost_row_is_dropped_not_zeroed() -> None:
    """A zero would understate a client's spend."""
    clients = FakeAzureClients(
        cost_rows=[
            {"date": "2026-09-01", "cost": "not-a-number"},
            {"date": "2026-09-02", "cost": "5.00"},
        ]
    )
    response = get_cost_series(GetCostSeriesRequest(subscription_id=SUBSCRIPTION), clients)
    assert len(response.points) == 1
    assert response.points[0].amount == Decimal("5.00")


def test_a_negative_cost_is_floored_at_zero() -> None:
    """A daily spend of less than nothing is not a thing the domain models.

    Credits and refunds legitimately appear as negative rows.
    """
    clients = FakeAzureClients(cost_rows=[{"date": "2026-09-01", "cost": "-3.00"}])
    response = get_cost_series(GetCostSeriesRequest(subscription_id=SUBSCRIPTION), clients)
    assert response.points[0].amount == Decimal(0)


def test_no_cost_rows_is_unknown_and_explains_the_lag() -> None:
    response = get_cost_series(
        GetCostSeriesRequest(subscription_id=SUBSCRIPTION), FakeAzureClients()
    )
    assert response.status is ToolStatus.UNKNOWN
    assert "48 hours" in response.reason
    assert "Cost Management Reader" in response.reason


def test_a_cost_outage_is_a_failure_with_a_reason() -> None:
    response = get_cost_series(
        GetCostSeriesRequest(subscription_id=SUBSCRIPTION), FakeAzureClients(raises="cost")
    )
    assert response.status is ToolStatus.FAILED
    assert response.points == ()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_are_read_and_normalised() -> None:
    clients = FakeAzureClients(
        metrics={
            "Percentage CPU": {"average": 6.1, "maximum": 18.5, "total": None},
            "NetworkIn": {"average": None, "maximum": None, "total": 1024**3 * 2},
        }
    )
    response = get_resource_metrics(GetResourceMetricsRequest(resource_id="/r/1"), clients)
    assert response.status is ToolStatus.VERIFIED
    assert response.cpu_avg_pct == Decimal("6.1")
    assert response.cpu_max_pct == Decimal("18.5")
    assert response.network_in_gb == Decimal(2)
    assert response.observation_days == 30


def test_a_sql_database_reports_connections_under_its_own_metric_name() -> None:
    clients = FakeAzureClients(
        metrics={
            "cpu_percent": {"average": 0.4, "maximum": 2.1, "total": None},
            "connection_successful": {"average": None, "maximum": None, "total": 0},
        }
    )
    response = get_resource_metrics(GetResourceMetricsRequest(resource_id="/r/sql"), clients)
    assert response.cpu_avg_pct == Decimal("0.4")
    assert response.connection_count == 0


def test_an_app_service_plan_reports_its_own_utilisation_metric() -> None:
    clients = FakeAzureClients(
        metrics={"CpuPercentage": {"average": 11.4, "maximum": 19.6, "total": None}}
    )
    response = get_resource_metrics(GetResourceMetricsRequest(resource_id="/r/plan"), clients)
    assert response.utilisation_pct == Decimal("11.4")


def test_no_metrics_is_unknown_rather_than_zeroes() -> None:
    """An unmeasured resource is not an idle one, and the detectors rely on that."""
    response = get_resource_metrics(
        GetResourceMetricsRequest(resource_id="/r/never-started"), FakeAzureClients()
    )
    assert response.status is ToolStatus.UNKNOWN
    assert not response.has_any_metric
    assert response.cpu_avg_pct is None
    assert response.cpu_max_pct is None
    assert "never have been" in response.reason


def test_a_percentage_above_one_hundred_is_clamped() -> None:
    """Azure occasionally reports slightly over 100; the domain bounds it."""
    clients = FakeAzureClients(
        metrics={"Percentage CPU": {"average": 101.4, "maximum": 180.0, "total": None}}
    )
    response = get_resource_metrics(GetResourceMetricsRequest(resource_id="/r/1"), clients)
    assert response.cpu_avg_pct == Decimal(100)
    assert response.cpu_max_pct == Decimal(100)


def test_the_metric_names_cover_every_detector_input() -> None:
    """A detector whose metric is not requested can never fire."""
    assert "Percentage CPU" in METRIC_NAMES
    assert "cpu_percent" in METRIC_NAMES
    assert "connection_successful" in METRIC_NAMES
    assert "CpuPercentage" in METRIC_NAMES


def test_a_metrics_outage_is_a_failure() -> None:
    response = get_resource_metrics(
        GetResourceMetricsRequest(resource_id="/r/1"), FakeAzureClients(raises="metrics")
    )
    assert response.status is ToolStatus.FAILED


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def _reservation_row(*, term: str = "P3Y", utilisation: float | None = 34.5) -> dict[str, Any]:
    aggregates = [{"grain": "30", "value": utilisation}] if utilisation is not None else []
    return {
        "id": "/providers/Microsoft.Capacity/reservationOrders/o1/reservations/r1",
        "name": "r1",
        "sku": {"name": "Standard_D4s_v5"},
        "properties": {
            "displayName": "nw-compute-dsv5-3y",
            "reservedResourceType": "VirtualMachines",
            "location": "eastus",
            "appliedScopes": [f"/subscriptions/{SUBSCRIPTION}"],
            "term": term,
            "quantity": 4,
            "expiryDate": "2028-02-01",
            "utilization": {"aggregates": aggregates},
        },
    }


def test_reservations_are_read_and_projected() -> None:
    clients = FakeAzureClients(reservation_rows=[_reservation_row()])
    response = list_reservations(ListReservationsRequest(), clients)

    assert response.status is ToolStatus.VERIFIED
    record = response.reservations[0]
    assert record.name == "nw-compute-dsv5-3y"
    assert record.reserved_sku == "Standard_D4s_v5"
    assert record.term_months == 36
    assert record.quantity == 4
    assert record.utilisation_pct == Decimal("34.5")
    assert record.scope_subscription_id == SUBSCRIPTION
    assert record.expires_on == date(2028, 2, 1)


def test_a_one_year_term_is_twelve_months() -> None:
    clients = FakeAzureClients(reservation_rows=[_reservation_row(term="P1Y")])
    response = list_reservations(ListReservationsRequest(), clients)
    assert response.reservations[0].term_months == 12


def test_an_unrecognised_term_defaults_to_a_year_rather_than_failing() -> None:
    clients = FakeAzureClients(reservation_rows=[_reservation_row(term="P7Y")])
    response = list_reservations(ListReservationsRequest(), clients)
    assert response.reservations[0].term_months == 12


def test_a_reservation_with_no_utilisation_is_unmeasured_not_zero() -> None:
    """Zero utilisation and unmeasured utilisation mean very different things."""
    clients = FakeAzureClients(reservation_rows=[_reservation_row(utilisation=None)])
    response = list_reservations(ListReservationsRequest(), clients)
    assert response.reservations[0].utilisation_pct is None


def test_reservations_can_be_narrowed_to_one_subscription() -> None:
    other = _reservation_row()
    other["properties"]["appliedScopes"] = ["/subscriptions/other-sub"]
    clients = FakeAzureClients(reservation_rows=[_reservation_row(), other])
    response = list_reservations(ListReservationsRequest(subscription_id=SUBSCRIPTION), clients)
    assert len(response.reservations) == 1


def test_no_reservations_explains_that_the_permission_is_separate() -> None:
    response = list_reservations(ListReservationsRequest(), FakeAzureClients())
    assert response.status is ToolStatus.UNKNOWN
    assert "separately" in response.reason


def test_a_reservations_outage_is_a_failure() -> None:
    response = list_reservations(ListReservationsRequest(), FakeAzureClients(raises="reservations"))
    assert response.status is ToolStatus.FAILED


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


def _advisor_row(category: str = "Cost") -> dict[str, Any]:
    return {
        "id": "/subscriptions/x/providers/Microsoft.Advisor/recommendations/rec1",
        "properties": {
            "category": category,
            "impact": "High",
            "shortDescription": {
                "problem": "Right-size or shutdown underutilized virtual machines",
                "solution": "Resize the virtual machine",
            },
            "resourceMetadata": {"resourceId": "/subscriptions/x/vm"},
            "extendedProperties": {
                "annualSavingsAmount": "1200.50",
                "savingsCurrency": "USD",
            },
        },
    }


def test_advisor_cost_recommendations_are_read() -> None:
    clients = FakeAzureClients(advisor_rows=[_advisor_row()])
    response = list_advisor_cost_recommendations(
        ListAdvisorRecommendationsRequest(subscription_id=SUBSCRIPTION), clients
    )
    assert response.status is ToolStatus.VERIFIED
    record = response.recommendations[0]
    assert record.impact == "High"
    assert record.annual_saving_amount == Decimal("1200.50")
    assert record.resource_id == "/subscriptions/x/vm"


def test_non_cost_advisor_categories_are_ignored() -> None:
    """CostSentinel is a FinOps tool; security advice is not its business."""
    clients = FakeAzureClients(advisor_rows=[_advisor_row(category="Security")])
    response = list_advisor_cost_recommendations(
        ListAdvisorRecommendationsRequest(subscription_id=SUBSCRIPTION), clients
    )
    assert response.status is ToolStatus.UNKNOWN
    assert response.recommendations == ()


def test_no_advisor_recommendations_is_unknown_and_says_it_may_be_legitimate() -> None:
    response = list_advisor_cost_recommendations(
        ListAdvisorRecommendationsRequest(subscription_id=SUBSCRIPTION), FakeAzureClients()
    )
    assert response.status is ToolStatus.UNKNOWN
    assert "legitimately have none" in response.reason


def test_an_advisor_outage_is_a_failure() -> None:
    response = list_advisor_cost_recommendations(
        ListAdvisorRecommendationsRequest(subscription_id=SUBSCRIPTION),
        FakeAzureClients(raises="advisor"),
    )
    assert response.status is ToolStatus.FAILED


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_the_invoker_validates_dispatches_and_types_the_result() -> None:
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    invoker = LocalToolInvoker(clients)
    response = invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    assert isinstance(response, QueryResourcesResponse)
    assert response.status is ToolStatus.VERIFIED
    assert invoker.call_count == 1


def test_an_invalid_request_is_a_failure_rather_than_a_raise() -> None:
    """A schema mismatch should degrade one signal, not abort a sweep."""
    invoker = LocalToolInvoker(FakeAzureClients())
    response = invoker.invoke("azure_query_resources", {"subscription_id": ""})
    assert response.status is ToolStatus.FAILED
    assert "invalid request" in response.reason


def test_an_unregistered_tool_raises() -> None:
    invoker = LocalToolInvoker(FakeAzureClients())
    with pytest.raises(KeyError, match="no tool named"):
        invoker.invoke("azure_delete_disk", {})


def test_responses_are_cached_within_one_sweep() -> None:
    """The Scout asks for the same inventory per subscription; Azure is rate limited."""
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    invoker = LocalToolInvoker(clients)
    first = invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    second = invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    assert first is second
    assert invoker.call_count == 1


def test_caching_can_be_turned_off() -> None:
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    invoker = LocalToolInvoker(clients, cache=False)
    invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    assert invoker.call_count == 2


def test_reset_clears_the_cache_so_one_invoker_serves_another_sweep() -> None:
    clients = FakeAzureClients(graph_rows=[_disk_row()])
    invoker = LocalToolInvoker(clients)
    invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    invoker.reset()
    invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
    assert invoker.call_count == 1


def test_a_handler_returning_the_wrong_type_is_a_failure() -> None:
    """Defensive: the invoker validates the response, not just the request."""
    from costsentinel.tools import registry

    spec = registry.BY_NAME["azure_query_resources"]
    original = spec.handler
    hijacked = spec.model_copy(update={"handler": lambda _request, _clients: "not a response"})
    registry.BY_NAME["azure_query_resources"] = hijacked
    try:
        invoker = LocalToolInvoker(FakeAzureClients())
        response = invoker.invoke("azure_query_resources", {"subscription_id": SUBSCRIPTION})
        assert response.status is ToolStatus.FAILED
        assert "not QueryResourcesResponse" in response.reason
    finally:
        registry.BY_NAME["azure_query_resources"] = spec.model_copy(update={"handler": original})


def test_every_tool_can_be_dispatched_by_name() -> None:
    """A registered tool that cannot be called is worse than one that is absent."""
    clients = FakeAzureClients()
    invoker = LocalToolInvoker(clients, cache=False)
    payloads: dict[str, dict[str, Any]] = {
        "azure_list_subscriptions": {},
        "azure_query_resources": {"subscription_id": SUBSCRIPTION},
        "azure_list_reservations": {},
        "azure_get_cost_series": {"subscription_id": SUBSCRIPTION},
        "azure_get_resource_metrics": {"resource_id": "/r/1"},
        "azure_list_advisor_cost_recommendations": {"subscription_id": SUBSCRIPTION},
    }
    for spec in tool_specs():
        response = invoker.invoke(spec.name, payloads[spec.name])
        assert isinstance(response, spec.response_model)
        # Every one has an empty fake behind it, so each reports unknown with a reason.
        assert response.status is ToolStatus.UNKNOWN
        assert response.reason


def test_the_surface_description_matches_the_registry() -> None:
    lines = describe_surface()
    assert len(lines) == len(tool_specs())
    for spec, line in zip(tool_specs(), lines, strict=True):
        assert spec.name in line
        assert spec.azure_service in line


# ---------------------------------------------------------------------------
# The MCP server binding
# ---------------------------------------------------------------------------


def test_the_server_refuses_to_build_without_fastmcp() -> None:
    """Fastmcp is an optional extra and is not installed in CI."""
    from costsentinel.tools.server import McpDependencyError, build_server

    with pytest.raises(McpDependencyError, match="uv sync --extra mcp"):
        build_server(FakeAzureClients())


def test_the_server_instructions_state_the_read_only_guarantee() -> None:
    """An operator attaching a client should be told what it can and cannot do."""
    from costsentinel.tools.server import SERVER_INSTRUCTIONS

    lowered = SERVER_INSTRUCTIONS.lower()
    assert "read-only" in lowered
    assert "no create, update, delete" in lowered
    assert "verified, unknown or failed" in lowered
    assert "{permissions}" in SERVER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# The live client bundle
# ---------------------------------------------------------------------------


def test_the_live_clients_refuse_to_build_without_the_azure_extra() -> None:
    from costsentinel.tools.clients import AzureDependencyError, build_credential

    with pytest.raises(AzureDependencyError, match="uv sync --extra azure"):
        build_credential()


def test_the_live_clients_accept_an_injected_credential() -> None:
    """So the bundle is constructible in a test without the SDKs or a login."""
    from costsentinel.tools.clients import LiveAzureClients

    sentinel = object()
    clients = LiveAzureClients(credential=sentinel)
    assert clients.credential is sentinel


def test_every_live_client_method_needs_the_extra() -> None:
    """Each is lazily built, so each independently reports the missing dependency."""
    from costsentinel.tools.clients import AzureDependencyError, LiveAzureClients

    clients = LiveAzureClients(credential=object())
    calls = (
        lambda: clients.resource_graph_query("Resources", []),
        lambda: clients.cost_query(SUBSCRIPTION, date(2026, 9, 1), date(2026, 9, 30)),
        lambda: clients.metric_values(
            "/r/1", ("Percentage CPU",), datetime.now(UTC), datetime.now(UTC)
        ),
        lambda: clients.advisor_recommendations(SUBSCRIPTION),
        clients.reservation_orders,
    )
    for call in calls:
        with pytest.raises(AzureDependencyError, match="not installed"):
            call()
