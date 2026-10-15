"""The read-only real provider, driven entirely offline.

Every test here goes through a fake invoker returning recorded-shape responses, so
nothing touches Azure, no credential is needed and CI stays offline. What is being
tested is the translation layer, which is where the risk actually is: Azure reports
``Premium_LRS`` and a size where the meter is ``P15``, reports power state
inconsistently, and has no notion of CostSentinel's client boundary at all.

The failure paths get as much attention as the happy ones. A cost read that fails
must not produce a subscription that appears to cost nothing, and an unmeasured VM
must not appear idle -- those are the two ways a cost tool lies to a client without
anybody noticing.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from costsentinel.config import RunMode, Settings
from costsentinel.domain.common import ProvenanceSource, Verification
from costsentinel.domain.estate import Environment, ResourceKind, ResourceState
from costsentinel.providers.azure import (
    UNASSIGNED_CLIENT,
    AzureLiveProvider,
    _currency,
    _environment_from,
)
from costsentinel.providers.base import ProviderError, ProviderNotConfiguredError
from costsentinel.tools.schemas import (
    CostPointRecord,
    GetCostSeriesResponse,
    GetResourceMetricsResponse,
    ListReservationsResponse,
    ListSubscriptionsResponse,
    QueryResourcesResponse,
    ReservationRecord,
    ResourceRecord,
    SubscriptionRecord,
    ToolResponse,
    ToolStatus,
)

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


class FakeInvoker:
    """Returns canned responses per tool name, and records what was asked for."""

    def __init__(self, responses: dict[str, ToolResponse]) -> None:
        """Stub the given tool names; anything else asked for is a test bug."""
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        self.calls.append((name, payload))
        try:
            return self.responses[name]
        except KeyError:  # pragma: no cover - a test asked for an unstubbed tool
            raise AssertionError(f"unstubbed tool {name}") from None


def _settings(**kwargs: Any) -> Settings:
    return Settings(mode=RunMode.REAL, **kwargs)


def _subscriptions(
    *records: SubscriptionRecord, status: ToolStatus = ToolStatus.VERIFIED
) -> ListSubscriptionsResponse:
    return ListSubscriptionsResponse(
        status=status,
        reason="" if status is ToolStatus.VERIFIED else "stubbed",
        subscriptions=records,
    )


def _provider(
    responses: dict[str, ToolResponse],
    *,
    scope: tuple[str, ...] = (SUB_A,),
    settings: Settings | None = None,
) -> tuple[AzureLiveProvider, FakeInvoker]:
    invoker = FakeInvoker(responses)
    provider = AzureLiveProvider(
        invoker=invoker,
        settings=settings or _settings(),
        subscription_ids=scope,
    )
    return provider, invoker


def _resource(**kwargs: Any) -> ResourceRecord:
    defaults: dict[str, Any] = {
        "resource_id": f"/subscriptions/{SUB_A}/resourceGroups/rg/providers/x/y",
        "name": "thing",
        "resource_type": "Microsoft.Compute/virtualMachines",
        "location": "eastus",
        "resource_group": "rg",
        "subscription_id": SUB_A,
    }
    return ResourceRecord(**(defaults | kwargs))


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_construction_refuses_an_empty_scope() -> None:
    with pytest.raises(ProviderNotConfiguredError, match="explicit subscription scope"):
        _provider({}, scope=())


def test_duplicate_subscription_ids_are_collapsed_in_order() -> None:
    provider, _ = _provider({}, scope=(SUB_A, SUB_B, SUB_A))
    assert provider.subscription_ids == (SUB_A, SUB_B)


def test_a_subscription_outside_scope_is_filtered_from_the_listing() -> None:
    """Azure returns everything the credential sees; the provider returns the scope."""
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="in scope"),
                SubscriptionRecord(subscription_id=SUB_B, display_name="somebody else's"),
            )
        }
    )
    assert [s.subscription_id for s in provider.list_subscriptions()] == [SUB_A]


def test_reading_a_subscription_outside_scope_is_refused() -> None:
    """A sweep must not widen its own scope mid-run."""
    provider, _ = _provider({})
    with pytest.raises(ProviderError, match="not in this run's scope"):
        provider.list_resources(SUB_B)
    with pytest.raises(ProviderError, match="not in this run's scope"):
        provider.get_cost_series(SUB_B)


def test_a_requested_subscription_the_credential_cannot_see_is_logged_not_invented(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = _provider({"azure_list_subscriptions": _subscriptions()}, scope=(SUB_A, SUB_B))
    with caplog.at_level("WARNING"):
        assert provider.list_subscriptions() == ()
    assert "not visible to this credential" in caplog.text


# ---------------------------------------------------------------------------
# Client attribution
# ---------------------------------------------------------------------------


def test_the_client_comes_from_a_subscription_tag() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(
                    subscription_id=SUB_A,
                    display_name="Northwind prod",
                    tags={"client": "Northwind", "environment": "prod"},
                )
            )
        }
    )
    (subscription,) = provider.list_subscriptions()
    assert subscription.client == "Northwind"
    assert subscription.environment is Environment.PRODUCTION


def test_the_client_tag_name_is_configurable() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(
                    subscription_id=SUB_A,
                    display_name="tagged differently",
                    tags={"CostCentre": "Ministry"},
                )
            )
        },
        settings=_settings(azure_client_tag="CostCentre"),
    )
    assert provider.list_subscriptions()[0].client == "Ministry"


@pytest.mark.parametrize("tags", [{}, {"client": "   "}])
def test_an_untagged_subscription_is_named_unassigned_not_guessed(
    tags: dict[str, str],
) -> None:
    """A gap to fix, rather than a subscription quietly joining a client's report."""
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="orphan", tags=tags)
            )
        }
    )
    assert provider.list_subscriptions()[0].client == UNASSIGNED_CLIENT


def test_the_listing_can_be_narrowed_to_one_client() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(
                    subscription_id=SUB_A, display_name="a", tags={"client": "Northwind"}
                ),
                SubscriptionRecord(
                    subscription_id=SUB_B, display_name="b", tags={"client": "Ministry"}
                ),
            )
        },
        scope=(SUB_A, SUB_B),
    )
    assert [s.subscription_id for s in provider.list_subscriptions(client="Ministry")] == [SUB_B]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"environment": "production"}, Environment.PRODUCTION),
        ({"env": "UAT"}, Environment.STAGING),
        ({"env": "dev"}, Environment.DEVELOPMENT),
        ({"environment": "qa"}, Environment.TEST),
        ({"env": "sbx"}, Environment.SANDBOX),
        ({"environment": "whatever"}, Environment.UNKNOWN),
        ({}, Environment.UNKNOWN),
    ],
)
def test_environment_tags_map_or_stay_unknown(tags: dict[str, str], expected: Environment) -> None:
    """Unknown is the safe default: policy treats production as stricter.

    Guessing a subscription into development would loosen a class it should not.
    """
    assert _environment_from(tags) is expected


# ---------------------------------------------------------------------------
# Inventory and pricing
# ---------------------------------------------------------------------------


def test_a_premium_disk_is_priced_from_the_tier_its_size_falls_into() -> None:
    """Azure reports ``Premium_LRS`` and a size; the meter is a tier."""
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        name="orphan-disk",
                        resource_type="Microsoft.Compute/disks",
                        sku_name="Premium_LRS",
                        disk_size_gb=Decimal(200),
                        properties={"diskState": "Unattached"},
                    ),
                ),
            )
        }
    )
    (disk,) = provider.list_resources(SUB_A)
    assert disk.kind is ResourceKind.MANAGED_DISK
    assert disk.sku == "Premium_LRS_P15"
    assert disk.state is ResourceState.UNATTACHED
    # The real P15 rate in the committed catalogue, not a figure from this test.
    assert disk.monthly_cost.amount == Decimal("38.01")
    assert disk.monthly_cost.provenance.source is ProvenanceSource.AZURE_RETAIL_PRICES


def test_a_per_gb_resource_is_priced_from_the_rate_times_the_reported_size() -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        name="old-snapshot",
                        resource_type="Microsoft.Compute/snapshots",
                        sku_name="Premium_LRS",
                        disk_size_gb=Decimal(512),
                    ),
                ),
            )
        }
    )
    (snapshot,) = provider.list_resources(SUB_A)
    assert snapshot.capacity_gb == Decimal(512)
    amount = snapshot.monthly_cost.amount
    assert amount is not None and amount > 0


def test_a_per_gb_resource_with_no_reported_size_is_unknown_not_zero() -> None:
    """The rate is known; the quantity is not, so no monthly figure exists."""
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        name="sizeless-snapshot",
                        resource_type="Microsoft.Compute/snapshots",
                        sku_name="Premium_LRS",
                    ),
                ),
            )
        }
    )
    (snapshot,) = provider.list_resources(SUB_A)
    assert snapshot.monthly_cost.amount is None
    assert snapshot.monthly_cost.provenance.verification is Verification.UNKNOWN


def test_an_unmappable_sku_yields_an_unknown_cost_and_keeps_the_resource() -> None:
    """Dropping it would hide a resource; guessing a meter would be worse."""
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        name="standard-hdd",
                        resource_type="Microsoft.Compute/disks",
                        sku_name="Standard_LRS",
                        disk_size_gb=Decimal(64),
                    ),
                ),
            )
        }
    )
    (disk,) = provider.list_resources(SUB_A)
    assert disk.sku is None
    assert disk.monthly_cost.amount is None
    reference = disk.monthly_cost.provenance.reference or ""
    assert "no catalogue meter matches" in reference


def test_a_mapped_sku_the_catalogue_has_no_regional_price_for_is_unknown() -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        name="vm-in-an-unpriced-region",
                        sku_name="Standard_D4s_v5",
                        location="westeurope",
                    ),
                ),
            )
        }
    )
    (vm,) = provider.list_resources(SUB_A)
    assert vm.sku == "Standard_D4s_v5"
    assert vm.monthly_cost.amount is None
    assert "refresh_prices" in (vm.monthly_cost.provenance.reference or "")


def test_an_unrecognised_resource_type_is_skipped_entirely() -> None:
    """Rather than detected against rules written for something else."""
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(resource_type="Microsoft.Insights/components", name="appinsights"),
                ),
            )
        }
    )
    assert provider.list_resources(SUB_A) == ()


@pytest.mark.parametrize(
    ("properties", "managed_by", "expected"),
    [
        ({"diskState": "Attached"}, "/vm/1", ResourceState.ATTACHED),
        ({"diskState": "Unattached"}, None, ResourceState.UNATTACHED),
        ({"diskState": "Reserved"}, "/vm/1", ResourceState.UNATTACHED),
        ({}, None, ResourceState.UNATTACHED),
    ],
)
def test_disk_attachment_state_is_derived_from_azures_own_fields(
    properties: dict[str, str], managed_by: str | None, expected: ResourceState
) -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        resource_type="Microsoft.Compute/disks",
                        sku_name="Premium_LRS",
                        disk_size_gb=Decimal(128),
                        properties=properties,
                        managed_by=managed_by,
                    ),
                ),
            )
        }
    )
    assert provider.list_resources(SUB_A)[0].state is expected


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        ({"ipConfiguration": "/nic/1/ipConfigurations/x"}, ResourceState.ATTACHED),
        ({}, ResourceState.UNATTACHED),
    ],
)
def test_public_ip_attachment_state_is_derived_from_its_ip_configuration(
    properties: dict[str, str], expected: ResourceState
) -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(
                        resource_type="Microsoft.Network/publicIPAddresses",
                        sku_name="Standard",
                        properties={"publicIPAllocationMethod": "Static"} | properties,
                    ),
                ),
            )
        }
    )
    assert provider.list_resources(SUB_A)[0].state is expected


@pytest.mark.parametrize(
    ("power_state", "expected"),
    [
        ("PowerState/running", ResourceState.RUNNING),
        ("PowerState/deallocated", ResourceState.DEALLOCATED),
        ("PowerState/stopped", ResourceState.STOPPED),
        ("", ResourceState.RUNNING),
    ],
)
def test_vm_power_state_defaults_to_running_when_azure_does_not_project_it(
    power_state: str, expected: ResourceState
) -> None:
    """A VM that exists is billed unless explicitly deallocated.

    So running is the safe read of a missing power state: it keeps the resource in
    the cost picture rather than quietly excusing it.
    """
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(
                    _resource(sku_name="Standard_D4s_v5", properties={"powerState": power_state}),
                ),
            )
        }
    )
    assert provider.list_resources(SUB_A)[0].state is expected


def test_a_failed_inventory_read_raises_rather_than_looking_like_a_clean_estate() -> None:
    """The single most important failure path in the provider."""
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.FAILED, reason="Resource Graph throttled the query"
            )
        }
    )
    with pytest.raises(ProviderError, match="throttled"):
        provider.list_resources(SUB_A)


def test_a_failed_subscription_read_raises() -> None:
    provider, _ = _provider({"azure_list_subscriptions": _subscriptions(status=ToolStatus.FAILED)})
    with pytest.raises(ProviderError, match="could not read subscriptions"):
        provider.list_subscriptions()


def test_a_truncated_inventory_is_flagged_as_partial(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED, resources=(), truncated=True
            )
        }
    )
    with caplog.at_level("WARNING"):
        provider.list_resources(SUB_A)
    assert "truncated" in caplog.text


def test_an_unverified_read_does_not_become_verified_provenance() -> None:
    """The tool honesty model has to survive the trip into the domain.

    An ``unknown`` read is traceable but unconfirmed, and flattening it to verified
    at the boundary would launder a doubt the tool layer went to trouble to record.
    """
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.UNKNOWN,
                reason="Resource Graph returned a partial projection",
                resources=(_resource(sku_name="Standard_D4s_v5"),),
            )
        }
    )
    (resource,) = provider.list_resources(SUB_A)
    assert resource.provenance.verification is Verification.UNVERIFIED
    assert "unknown" in (resource.provenance.reference or "")


def test_a_verified_read_becomes_verified_provenance() -> None:
    provider, _ = _provider(
        {
            "azure_query_resources": QueryResourcesResponse(
                status=ToolStatus.VERIFIED,
                resources=(_resource(sku_name="Standard_D4s_v5"),),
            )
        }
    )
    (resource,) = provider.list_resources(SUB_A)
    assert resource.provenance.verification is Verification.VERIFIED
    assert resource.provenance.source is ProvenanceSource.AZURE_RESOURCE_GRAPH


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_a_cost_series_carries_the_days_azure_reported() -> None:
    provider, invoker = _provider(
        {
            "azure_get_cost_series": GetCostSeriesResponse(
                status=ToolStatus.VERIFIED,
                subscription_id=SUB_A,
                currency="QAR",
                points=(
                    CostPointRecord(day=date(2026, 10, 1), amount=Decimal("120.50")),
                    CostPointRecord(day=date(2026, 10, 2), amount=Decimal("118.00")),
                ),
            )
        }
    )
    series = provider.get_cost_series(SUB_A, window_days=7)
    assert series.window_days == 2
    assert series.currency.value == "QAR"
    assert series.observed_spend().amount == Decimal("238.50")
    assert invoker.calls[0][1]["window_days"] == 7


def test_a_failed_cost_read_is_an_empty_series_whose_provenance_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not a zero-cost series. A zero would tell a client their estate is free."""
    provider, _ = _provider(
        {
            "azure_get_cost_series": GetCostSeriesResponse(
                status=ToolStatus.FAILED,
                reason="Cost Management returned 403; Cost Management Reader is missing",
            )
        }
    )
    with caplog.at_level("WARNING"):
        series = provider.get_cost_series(SUB_A)
    assert series.points == ()
    assert series.provenance.verification is Verification.UNVERIFIED
    assert "403" in (series.provenance.reference or "")
    assert "reported as unknown" in caplog.text


def test_an_unrecognised_currency_code_falls_back_to_usd() -> None:
    """Rather than failing a whole sweep over a currency CostSentinel cannot name."""
    assert _currency("ZZZ").value == "USD"
    assert _currency("qar").value == "QAR"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_are_returned_when_azure_measured_them() -> None:
    provider, _ = _provider(
        {
            "azure_get_resource_metrics": GetResourceMetricsResponse(
                status=ToolStatus.VERIFIED,
                resource_id="/vm/1",
                observation_days=30,
                cpu_avg_pct=Decimal("3.4"),
                cpu_max_pct=Decimal("11.2"),
            )
        }
    )
    metrics = provider.get_resource_metrics("/vm/1")
    assert metrics is not None
    assert metrics.cpu_avg_pct == Decimal("3.4")
    assert metrics.provenance.source is ProvenanceSource.AZURE_MONITOR
    assert metrics.provenance.verification is Verification.VERIFIED


@pytest.mark.parametrize(
    "response",
    [
        GetResourceMetricsResponse(
            status=ToolStatus.UNKNOWN, reason="the VM has never been started"
        ),
        GetResourceMetricsResponse(
            status=ToolStatus.FAILED, reason="Monitor returned 404 for this resource"
        ),
        GetResourceMetricsResponse(
            status=ToolStatus.VERIFIED, resource_id="/vm/1", observation_days=30
        ),
    ],
)
def test_absent_metrics_are_none_so_nothing_is_reported_as_idle_unmeasured(
    response: GetResourceMetricsResponse,
) -> None:
    """``None`` means "cannot conclude". Zero would mean "nothing is using this"."""
    provider, _ = _provider({"azure_get_resource_metrics": response})
    assert provider.get_resource_metrics("/vm/1") is None


def test_an_observation_window_of_zero_days_is_floored_at_one() -> None:
    """``ResourceMetrics`` requires at least one day, and Azure can report none."""
    provider, _ = _provider(
        {
            "azure_get_resource_metrics": GetResourceMetricsResponse(
                status=ToolStatus.VERIFIED,
                resource_id="/vm/1",
                observation_days=0,
                cpu_avg_pct=Decimal("1.0"),
            )
        }
    )
    metrics = provider.get_resource_metrics("/vm/1")
    assert metrics is not None and metrics.observation_days == 1


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def _reservation(**kwargs: Any) -> ReservationRecord:
    defaults: dict[str, Any] = {
        "reservation_id": "/providers/Microsoft.Capacity/reservationOrders/o/reservations/r",
        "name": "northwind-d4s-ri",
        "reserved_sku": "Standard_D4s_v5",
        "region": "eastus",
        "scope_subscription_id": SUB_A,
        "term_months": 36,
        "quantity": 4,
        "utilisation_pct": Decimal("31.0"),
        "expires_on": date(2027, 6, 30),
    }
    return ReservationRecord(**(defaults | kwargs))


def test_a_reservation_is_priced_from_the_real_reserved_meter_times_its_quantity() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(
                    subscription_id=SUB_A, display_name="a", tags={"client": "Northwind"}
                )
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED, reservations=(_reservation(),)
            ),
        }
    )
    (reservation,) = provider.list_reservations()
    assert reservation.client == "Northwind"
    # 36-month reserved D4s_v5 in eastus, from the committed catalogue, times 4.
    assert reservation.monthly_amortised_cost.amount == Decimal("55.36") * 4
    assert reservation.monthly_amortised_cost.provenance.source is (
        ProvenanceSource.AZURE_RETAIL_PRICES
    )


def test_the_amortised_figure_says_it_is_a_list_price_not_what_was_paid() -> None:
    """Azure exposes no API for the negotiated price, so the figure must not claim to be it."""
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="a")
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED, reservations=(_reservation(),)
            ),
        }
    )
    provenance = provider.list_reservations()[0].monthly_amortised_cost.provenance
    reference = provenance.reference or ""
    assert "LIST reserved price" in reference
    assert "not the price this customer actually paid" in reference
    assert provenance.verification is Verification.UNVERIFIED


def test_a_reservation_with_no_catalogue_price_is_unknown_not_zero() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="a")
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED,
                reservations=(_reservation(reserved_sku="Standard_M128ms", term_months=12),),
            ),
        }
    )
    cost = provider.list_reservations()[0].monthly_amortised_cost
    assert cost.amount is None
    assert "no 12-month reserved price" in (cost.provenance.reference or "")


def test_a_reservation_scoped_outside_the_run_is_filtered_out() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="a")
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED,
                reservations=(_reservation(scope_subscription_id=SUB_B),),
            ),
        }
    )
    assert provider.list_reservations() == ()


def test_a_shared_scope_reservation_is_kept_but_attributed_to_nobody() -> None:
    """A shared-scope commitment spans the enrolment, so no client owns it."""
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="a")
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED,
                reservations=(_reservation(scope_subscription_id=None),),
            ),
        }
    )
    (reservation,) = provider.list_reservations()
    assert reservation.client == UNASSIGNED_CLIENT
    assert reservation.scope_subscription_id is None


def test_reservations_can_be_narrowed_to_one_client() -> None:
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(
                    subscription_id=SUB_A, display_name="a", tags={"client": "Northwind"}
                )
            ),
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.VERIFIED, reservations=(_reservation(),)
            ),
        }
    )
    assert provider.list_reservations(client="Ministry") == ()
    assert len(provider.list_reservations(client="Northwind")) == 1


def test_a_failed_reservation_read_degrades_the_sweep_rather_than_ending_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reservation data needs an enrolment-level role many operators will not have.

    A sweep that refused to run at all without it would be useless to most of them,
    so this one signal degrades while the rest of the report stands.
    """
    provider, _ = _provider(
        {
            "azure_list_reservations": ListReservationsResponse(
                status=ToolStatus.FAILED, reason="403 from the Reservations API"
            )
        }
    )
    with caplog.at_level("WARNING"):
        assert provider.list_reservations() == ()
    assert "without them" in caplog.text


# ---------------------------------------------------------------------------
# Prices and identity
# ---------------------------------------------------------------------------


def test_prices_come_from_the_committed_catalogue_not_from_azure_at_runtime() -> None:
    """So a real sweep's figures are reproducible and a price change is a commit."""
    provider, invoker = _provider({})
    prices = provider.list_sku_prices("eastus")
    assert prices
    assert all(price.region == "eastus" for price in prices)
    assert invoker.calls == []


def test_an_unpriced_region_yields_no_candidates_rather_than_free_ones() -> None:
    provider, _ = _provider({})
    assert provider.list_sku_prices("antarctica-south") == ()


def test_the_provider_satisfies_the_protocol_so_the_graph_runs_unchanged() -> None:
    from costsentinel.providers.base import AzureProvider

    provider, _ = _provider({})
    assert isinstance(provider, AzureProvider)
    assert provider.name == "azure-live"
    assert provider.catalogue.is_real_api_pull


def test_get_provider_returns_the_mock_by_default() -> None:
    """The default and the only provider CI and the demo use."""
    from costsentinel.providers import MockAzureProvider, get_provider

    assert isinstance(get_provider(Settings()), MockAzureProvider)


def test_get_provider_refuses_a_real_run_with_no_scope() -> None:
    """Checked before any Azure import, so this holds without the optional extra."""
    from costsentinel.providers import get_provider

    with pytest.raises(ProviderNotConfiguredError, match="explicit subscription scope"):
        get_provider(_settings())


def test_an_explicit_scope_overrides_the_environment() -> None:
    """Which is how ``--subscription`` reaches the provider."""
    from costsentinel.providers import get_provider

    settings = _settings(azure_subscription_ids=SUB_B)
    assert settings.subscription_scope == (SUB_B,)
    # Reaching Azure needs the optional extra, so only the refusal path is asserted
    # here; the scope resolution itself is what this test pins.
    with pytest.raises(ProviderNotConfiguredError):
        get_provider(_settings(), subscription_ids=[])


def test_provenance_timestamps_are_timezone_aware() -> None:
    """A naive timestamp in an audit trail is a timestamp nobody can correlate."""
    provider, _ = _provider(
        {
            "azure_list_subscriptions": _subscriptions(
                SubscriptionRecord(subscription_id=SUB_A, display_name="a")
            )
        }
    )
    retrieved = provider.list_subscriptions()[0].provenance.retrieved_at
    assert retrieved.tzinfo is not None
    assert retrieved <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# SKU translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_gb", "expected"),
    [
        (Decimal(1), "P1"),
        (Decimal(4), "P1"),
        (Decimal(5), "P2"),
        (Decimal(200), "P15"),
        (Decimal(256), "P15"),
        (Decimal(257), "P20"),
        (Decimal(32767), "P80"),
        # Azure bills the tier a provisioned size falls into, not pro rata, so the
        # boundaries are the whole point of this table.
        (Decimal(40000), None),
        (None, None),
        (Decimal(0), None),
    ],
)
def test_a_premium_disk_tier_is_the_band_its_provisioned_size_falls_into(
    size_gb: Decimal | None, expected: str | None
) -> None:
    from costsentinel.providers.sku_mapping import premium_disk_tier

    assert premium_disk_tier(size_gb) == expected


@pytest.mark.parametrize(
    ("kind", "sku_name", "properties", "size_gb", "expected"),
    [
        # Reported directly.
        (ResourceKind.VIRTUAL_MACHINE, "Standard_D4s_v5", {}, None, "Standard_D4s_v5"),
        (ResourceKind.SQL_DATABASE, "GP_Gen5_4", {}, None, "GP_Gen5_4"),
        (ResourceKind.VIRTUAL_MACHINE, None, {}, None, None),
        # An App Service plan is reported with or without a space.
        (ResourceKind.APP_SERVICE_PLAN, "P1 v3", {}, None, "P1v3"),
        (ResourceKind.APP_SERVICE_PLAN, "P1v3", {}, None, "P1v3"),
        (ResourceKind.APP_SERVICE_PLAN, None, {}, None, None),
        # A disk's meter depends on both redundancy and size.
        (ResourceKind.MANAGED_DISK, "Premium_LRS", {}, Decimal(100), "Premium_LRS_P10"),
        (ResourceKind.MANAGED_DISK, "Premium_ZRS", {}, Decimal(100), None),
        (ResourceKind.MANAGED_DISK, "Premium_LRS", {}, None, None),
        # A snapshot bills per GB, so the size is the quantity, not part of the SKU.
        (ResourceKind.SNAPSHOT, "Premium_LRS", {}, Decimal(512), "Premium_LRS_Snapshot"),
        (ResourceKind.SNAPSHOT, "Standard_LRS", {}, Decimal(512), None),
        # A public IP's meter depends on SKU and allocation method together.
        (
            ResourceKind.PUBLIC_IP,
            "Standard",
            {"publicIPAllocationMethod": "Static"},
            None,
            "Standard_Static",
        ),
        (
            ResourceKind.PUBLIC_IP,
            "Standard",
            {"publicIPAllocationMethod": "Dynamic"},
            None,
            None,
        ),
        (ResourceKind.PUBLIC_IP, "Basic", {"publicIPAllocationMethod": "Static"}, None, None),
        # A storage account reports redundancy and access tier separately.
        (
            ResourceKind.STORAGE_ACCOUNT,
            "Standard_LRS",
            {"accessTier": "Hot"},
            None,
            "Standard_LRS_Hot",
        ),
        (
            ResourceKind.STORAGE_ACCOUNT,
            "Standard_GRS",
            {"accessTier": "Cool"},
            None,
            "Standard_GRS_Cool",
        ),
        # A combination the catalogue does not price is unknown, not nearest-match.
        (
            ResourceKind.STORAGE_ACCOUNT,
            "Premium_ZRS",
            {"accessTier": "Hot"},
            None,
            None,
        ),
        (ResourceKind.STORAGE_ACCOUNT, "Standard_LRS", {}, None, None),
        (ResourceKind.STORAGE_ACCOUNT, None, {"accessTier": "Hot"}, None, None),
        # A kind the catalogue carries no meter for at all.
        (ResourceKind.NETWORK_INTERFACE, "whatever", {}, None, None),
    ],
)
def test_sku_translation_is_exact_or_absent_never_nearest_match(
    kind: ResourceKind,
    sku_name: str | None,
    properties: dict[str, str],
    size_gb: Decimal | None,
    expected: str | None,
) -> None:
    """``None`` means an unknown cost, which is the honest answer.

    Guessing here would produce a plausible figure for the wrong meter, and a
    plausible wrong number in a client report is worse than an admitted gap.
    """
    from costsentinel.providers.sku_mapping import catalogue_sku

    assert (
        catalogue_sku(kind=kind, sku_name=sku_name, properties=properties, size_gb=size_gb)
        == expected
    )


@pytest.mark.parametrize(
    ("resource_type", "expected"),
    [
        ("Microsoft.Compute/virtualMachines", ResourceKind.VIRTUAL_MACHINE),
        ("MICROSOFT.COMPUTE/DISKS", ResourceKind.MANAGED_DISK),
        ("microsoft.sql/servers/databases", ResourceKind.SQL_DATABASE),
        ("Microsoft.KeyVault/vaults", None),
    ],
)
def test_resource_types_map_case_insensitively_or_not_at_all(
    resource_type: str, expected: ResourceKind | None
) -> None:
    """Resource Graph is inconsistent about case, and an unknown type is skipped."""
    from costsentinel.providers.sku_mapping import resource_kind

    assert resource_kind(resource_type) is expected


def test_a_reservations_commitment_sku_is_its_reported_sku() -> None:
    from costsentinel.providers.sku_mapping import reservation_sku

    assert reservation_sku("Standard_D4s_v5") == "Standard_D4s_v5"
    assert reservation_sku("") is None


# ---------------------------------------------------------------------------
# A real run without the optional extra
# ---------------------------------------------------------------------------


def _azure_extra_installed() -> bool:
    """Whether the optional ``azure`` extra is present.

    ``find_spec`` raises rather than returning ``None`` when the parent package is
    missing entirely, which is precisely the case this has to detect.
    """
    try:
        return importlib.util.find_spec("azure.identity") is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(
    _azure_extra_installed(),
    reason="the optional azure extra is installed, so the dependency path cannot fire",
)
def test_a_real_run_without_the_azure_extra_says_what_to_install() -> None:
    """The condition CI itself is in, so this path is exercised rather than assumed.

    A missing optional dependency has to name the install command. "No module named
    azure.identity" leaves an operator guessing which extra they wanted.
    """
    from costsentinel.providers import get_provider

    with pytest.raises(ProviderNotConfiguredError) as raised:
        get_provider(_settings(), subscription_ids=[SUB_A])
    message = str(raised.value)
    assert "uv sync" in message
    assert "azure" in message
