"""Mock provider tests.

Determinism is the mock's contract, so it is asserted directly rather than assumed:
the same seed must yield identical output, in this process and in any other. The
estate's known-wasteful resources are also pinned here, because the detector and
graph tests depend on them being present.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from costsentinel.domain.common import ProvenanceSource, Verification
from costsentinel.domain.estate import Environment, ResourceKind, ResourceState
from costsentinel.providers.base import AzureProvider
from costsentinel.providers.mock import (
    CLIENT_MINISTRY,
    CLIENT_NORTHWIND,
    DEFAULT_WINDOW_DAYS,
    MOCK_AS_OF,
    MockAzureProvider,
)

EXPECTED_SUBSCRIPTIONS = 3
EXPECTED_RESOURCES = 17
EXPECTED_ORPHANED_DISKS = 2


def test_mock_provider_satisfies_the_protocol(provider: MockAzureProvider) -> None:
    """Structural conformance, so the real adapter has a checked shape to match."""
    assert isinstance(provider, AzureProvider)
    assert provider.name == "mock-azure"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_yields_identical_output() -> None:
    """The mock's contract: identical output for an identical seed."""
    left = MockAzureProvider(seed=1337)
    right = MockAzureProvider(seed=1337)

    assert [s.model_dump_json() for s in left.list_subscriptions()] == [
        s.model_dump_json() for s in right.list_subscriptions()
    ]
    for subscription in left.list_subscriptions():
        sub_id = subscription.subscription_id
        assert [r.model_dump_json() for r in left.list_resources(sub_id)] == [
            r.model_dump_json() for r in right.list_resources(sub_id)
        ]
        assert (
            left.get_cost_series(sub_id).model_dump_json()
            == right.get_cost_series(sub_id).model_dump_json()
        )


def test_repeated_calls_on_one_instance_are_identical(provider: MockAzureProvider) -> None:
    sub_id = provider.list_subscriptions()[0].subscription_id
    assert (
        provider.get_cost_series(sub_id).model_dump_json()
        == provider.get_cost_series(sub_id).model_dump_json()
    )


def test_different_seeds_change_the_cost_series(provider: MockAzureProvider) -> None:
    """The seed is real: it must actually vary the day-to-day series."""
    other = MockAzureProvider(seed=99)
    sub_id = provider.list_subscriptions()[0].subscription_id
    assert (
        provider.get_cost_series(sub_id).model_dump_json()
        != other.get_cost_series(sub_id).model_dump_json()
    )


def test_timestamps_derive_from_as_of_not_the_wall_clock(provider: MockAzureProvider) -> None:
    assert provider.as_of == MOCK_AS_OF
    assert provider.seed == 1337
    for subscription in provider.list_subscriptions():
        assert subscription.provenance.retrieved_at == MOCK_AS_OF


def test_as_of_is_overridable() -> None:
    from datetime import UTC, datetime

    anchor = datetime(2025, 1, 15, tzinfo=UTC)
    custom = MockAzureProvider(seed=1337, as_of=anchor)
    assert custom.as_of == anchor
    assert custom.list_subscriptions()[0].provenance.retrieved_at == anchor


# ---------------------------------------------------------------------------
# Shape of the estate
# ---------------------------------------------------------------------------


def test_estate_spans_multiple_clients_and_subscriptions(provider: MockAzureProvider) -> None:
    subscriptions = provider.list_subscriptions()
    assert len(subscriptions) == EXPECTED_SUBSCRIPTIONS
    assert {s.client for s in subscriptions} == {CLIENT_NORTHWIND, CLIENT_MINISTRY}
    assert {s.environment for s in subscriptions} == {
        Environment.PRODUCTION,
        Environment.DEVELOPMENT,
    }


def test_subscriptions_can_be_narrowed_by_client(provider: MockAzureProvider) -> None:
    """Client is the tenancy boundary, so narrowing must be exact."""
    northwind = provider.list_subscriptions(client=CLIENT_NORTHWIND)
    ministry = provider.list_subscriptions(client=CLIENT_MINISTRY)
    assert len(northwind) == 2
    assert len(ministry) == 1
    assert all(s.client == CLIENT_NORTHWIND for s in northwind)
    assert provider.list_subscriptions(client="no-such-client") == ()


def test_all_resources_are_reachable_from_their_subscription(
    provider: MockAzureProvider,
) -> None:
    total = sum(
        len(provider.list_resources(s.subscription_id)) for s in provider.list_subscriptions()
    )
    assert total == EXPECTED_RESOURCES


def _all_resources(provider: MockAzureProvider) -> list[object]:
    return [
        resource
        for subscription in provider.list_subscriptions()
        for resource in provider.list_resources(subscription.subscription_id)
    ]


def test_estate_contains_the_known_wasteful_resources(provider: MockAzureProvider) -> None:
    """The three the brief names, plus the oversized-but-active VM."""
    by_name = {
        r.name: r
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
    }

    orphan = by_name["disk-analytics-01-data"]
    assert orphan.kind is ResourceKind.MANAGED_DISK
    assert orphan.state is ResourceState.UNATTACHED
    assert orphan.attached_to is None

    idle = by_name["vm-analytics-01"]
    assert idle.kind is ResourceKind.VIRTUAL_MACHINE
    assert idle.state is ResourceState.RUNNING
    idle_metrics = provider.get_resource_metrics(idle.resource_id)
    assert idle_metrics is not None
    assert idle_metrics.cpu_max_pct == Decimal("3.8")

    free_ip = by_name["pip-legacy-api"]
    assert free_ip.kind is ResourceKind.PUBLIC_IP
    assert free_ip.state is ResourceState.UNATTACHED
    assert free_ip.attached_to is None

    oversized = by_name["vm-batch-02"]
    oversized_metrics = provider.get_resource_metrics(oversized.resource_id)
    assert oversized_metrics is not None
    assert oversized_metrics.cpu_max_pct == Decimal("18.5")


def test_estate_contains_healthy_resources_too(provider: MockAzureProvider) -> None:
    """Precision needs negatives: most of the estate must be behaving correctly."""
    resources = [
        r for s in provider.list_subscriptions() for r in provider.list_resources(s.subscription_id)
    ]
    unattached = [r for r in resources if r.state is ResourceState.UNATTACHED]
    assert len(unattached) == 3  # two disks and one public IP
    assert len(resources) - len(unattached) > len(unattached)


def test_orphaned_disks_appear_in_two_different_clients(provider: MockAzureProvider) -> None:
    """Ranking across clients only matters if waste exists in more than one."""
    orphans = [
        (s.client, r.name)
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
        if r.kind is ResourceKind.MANAGED_DISK and r.state is ResourceState.UNATTACHED
    ]
    assert len(orphans) == EXPECTED_ORPHANED_DISKS
    assert len({client for client, _ in orphans}) == 2


# ---------------------------------------------------------------------------
# Provenance and pricing
# ---------------------------------------------------------------------------


def test_every_resource_fact_carries_verified_provenance(provider: MockAzureProvider) -> None:
    for subscription in provider.list_subscriptions():
        for resource in provider.list_resources(subscription.subscription_id):
            assert resource.provenance.source is ProvenanceSource.MOCK_PROVIDER
            assert resource.provenance.verification is Verification.VERIFIED
            assert resource.provenance.reference


def test_no_resource_cost_has_model_provenance(provider: MockAzureProvider) -> None:
    """Structurally guaranteed by MoneyAmount; asserted here at the provider edge."""
    for subscription in provider.list_subscriptions():
        for resource in provider.list_resources(subscription.subscription_id):
            assert not resource.monthly_cost.provenance.is_model_derived


def test_virtual_machine_cost_matches_the_sku_catalogue(provider: MockAzureProvider) -> None:
    """A VM's cost and its catalogue price must not be able to drift apart."""
    for subscription in provider.list_subscriptions():
        prices = {p.sku: p for p in provider.list_sku_prices(subscription.tags["region"])}
        for resource in provider.list_resources(subscription.subscription_id):
            if resource.kind is ResourceKind.VIRTUAL_MACHINE and resource.sku:
                assert resource.monthly_cost.amount == prices[resource.sku].monthly_cost.amount


def test_sku_catalogue_is_priced_and_regionally_differentiated(
    provider: MockAzureProvider,
) -> None:
    eastus = {p.sku: p for p in provider.list_sku_prices("eastus")}
    qatar = {p.sku: p for p in provider.list_sku_prices("qatarcentral")}
    assert eastus
    assert set(eastus) == set(qatar)
    for sku, price in eastus.items():
        base = price.monthly_cost.amount
        regional = qatar[sku].monthly_cost.amount
        assert base is not None
        assert regional is not None
        assert price.vcpu >= 1
        assert regional > base


def test_unknown_region_falls_back_to_base_prices(provider: MockAzureProvider) -> None:
    base = {p.sku: p.monthly_cost.amount for p in provider.list_sku_prices("eastus")}
    other = {p.sku: p.monthly_cost.amount for p in provider.list_sku_prices("mars-central")}
    assert base == other


def test_cost_series_covers_the_requested_window(provider: MockAzureProvider) -> None:
    sub_id = provider.list_subscriptions()[0].subscription_id
    series = provider.get_cost_series(sub_id, window_days=DEFAULT_WINDOW_DAYS)
    assert series.window_days == DEFAULT_WINDOW_DAYS
    assert series.subscription_id == sub_id
    assert series.observed_spend().is_known
    assert all(point.amount > 0 for point in series.points)
    assert len({point.day for point in series.points}) == DEFAULT_WINDOW_DAYS


def test_cost_series_window_is_configurable(provider: MockAzureProvider) -> None:
    sub_id = provider.list_subscriptions()[0].subscription_id
    assert provider.get_cost_series(sub_id, window_days=7).window_days == 7


def test_metrics_exist_only_for_measured_resources(provider: MockAzureProvider) -> None:
    """A resource with no metrics returns None -- "not measured", not "idle"."""
    by_name = {
        r.name: r
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
    }
    assert provider.get_resource_metrics(by_name["vm-web-01"].resource_id) is not None
    assert provider.get_resource_metrics(by_name["disk-web-01-os"].resource_id) is None
    assert provider.get_resource_metrics("/no/such/resource") is None


def test_attached_resources_point_at_a_real_resource(provider: MockAzureProvider) -> None:
    ids = {
        r.resource_id
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
    }
    for subscription in provider.list_subscriptions():
        for resource in provider.list_resources(subscription.subscription_id):
            if resource.attached_to is not None:
                assert resource.attached_to in ids


@pytest.mark.parametrize("seed", [1, 42, 1337, 99999])
def test_any_seed_produces_a_valid_estate(seed: int) -> None:
    built = MockAzureProvider(seed=seed)
    assert len(built.list_subscriptions()) == EXPECTED_SUBSCRIPTIONS
    for subscription in built.list_subscriptions():
        assert built.get_cost_series(subscription.subscription_id).observed_spend().is_known
