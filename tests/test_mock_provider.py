"""Mock provider tests.

Determinism is the mock's contract, so it is asserted directly rather than assumed:
the same seed must yield identical output, in this process and in any other. The
estate's known-wasteful resources are also pinned here, because the detector and
graph tests depend on them being present.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from costsentinel.agents.anomalies import OBSERVATION_DAYS
from costsentinel.domain.common import ProvenanceSource, Verification
from costsentinel.domain.estate import Environment, ResourceKind, ResourceState
from costsentinel.providers.base import AzureProvider
from costsentinel.providers.mock import (
    _STEP_DAYS_FROM_END,
    CLIENT_MINISTRY,
    CLIENT_NORTHWIND,
    DEFAULT_WINDOW_DAYS,
    MOCK_AS_OF,
    MockAzureProvider,
    SeriesShape,
)

EXPECTED_SUBSCRIPTIONS = 4
EXPECTED_RESOURCES = 21
EXPECTED_ORPHANED_DISKS = 2
EXPECTED_RESERVATIONS = 3
EXPECTED_SNAPSHOTS = 3


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
    assert [r.model_dump_json() for r in left.list_reservations()] == [
        r.model_dump_json() for r in right.list_reservations()
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


def test_the_cost_series_is_committed_and_does_not_depend_on_the_seed() -> None:
    """A deliberate change in Phase 7, and a stronger property than seed-variance.

    The series used to be the subscription's resource total spread over the window
    with seeded noise, which meant two things worth being rid of. Refreshing Azure's
    prices silently changed every series and therefore every anomaly verdict, and the
    seed changed them too -- so an anomaly scorecard would have depended on which seed
    happened to be configured.

    It is now a committed daily baseline times a committed shape times a committed
    wobble: the same bytes for any seed, which is what lets the eval harness score the
    detector at all.
    """
    sub_id = MockAzureProvider(seed=1337).list_subscriptions()[0].subscription_id
    series = [MockAzureProvider(seed=seed).get_cost_series(sub_id) for seed in (1, 99, 1337)]
    rendered = {one.model_dump_json() for one in series}
    assert len(rendered) == 1


def test_every_series_shape_is_present_in_the_estate() -> None:
    """A detector with nothing to detect over is untested.

    The seasonal shape is the one that matters most: it is where a detector without a
    weekday profile fails, and it fails every week.
    """
    provider = MockAzureProvider(seed=1337)
    shapes = {provider.series_shape(sub.subscription_id) for sub in provider.list_subscriptions()}
    assert shapes == set(SeriesShape)


def test_a_step_change_sits_entirely_inside_the_observation_window() -> None:
    """A coupling between the detector's policy and the fixture's data, asserted.

    The mock provider deliberately does not import the detector -- a provider must not
    depend on an agent -- so the two constants are independent and would drift
    silently. A step straddling the boundary would put the new level into the baseline
    and partly hide itself, which is the failure this pins.
    """
    assert _STEP_DAYS_FROM_END == OBSERVATION_DAYS


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
    assert len(ministry) == 2
    assert all(s.client == CLIENT_NORTHWIND for s in northwind)
    assert provider.list_subscriptions(client="no-such-client") == ()


def test_all_resources_are_reachable_from_their_subscription(
    provider: MockAzureProvider,
) -> None:
    total = sum(
        len(provider.list_resources(s.subscription_id)) for s in provider.list_subscriptions()
    )
    assert total == EXPECTED_RESOURCES


def test_snapshots_are_present_in_the_estate(provider: MockAzureProvider) -> None:
    snapshots = [
        r
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
        if r.kind is ResourceKind.SNAPSHOT
    ]
    assert len(snapshots) == EXPECTED_SNAPSHOTS


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


def test_estate_contains_the_phase_2_waste_kinds(provider: MockAzureProvider) -> None:
    """Each new detector needs a positive case in the estate to detect."""
    by_name = {
        r.name: r
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
    }

    stale = by_name["snap-web-01-pre-upgrade"]
    assert stale.kind is ResourceKind.SNAPSHOT
    assert stale.created_at is not None
    assert (provider.as_of - stale.created_at).days > 180

    idle_sql = by_name["sql-dev-sandbox"]
    assert idle_sql.kind is ResourceKind.SQL_DATABASE
    sql_metrics = provider.get_resource_metrics(idle_sql.resource_id)
    assert sql_metrics is not None
    assert sql_metrics.connection_count == 0

    plan_metrics = provider.get_resource_metrics(by_name["app-portal-plan"].resource_id)
    assert plan_metrics is not None
    assert plan_metrics.utilisation_pct == Decimal("11.4")


def test_estate_has_healthy_counter_examples_for_each_new_detector(
    provider: MockAzureProvider,
) -> None:
    """Without these, the new detectors could fire on everything and still pass."""
    by_name = {
        r.name: r
        for s in provider.list_subscriptions()
        for r in provider.list_resources(s.subscription_id)
    }

    recent = by_name["snap-analytics-nightly"]
    assert recent.created_at is not None
    assert (provider.as_of - recent.created_at).days < 180

    busy_sql = provider.get_resource_metrics(by_name["sql-core-reporting"].resource_id)
    assert busy_sql is not None
    assert busy_sql.connection_count is not None
    assert busy_sql.connection_count > 0

    well_used = [
        r for r in provider.list_reservations() if r.utilisation_pct and r.utilisation_pct > 60
    ]
    assert len(well_used) == 2


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def test_reservations_are_listed_and_client_scoped(provider: MockAzureProvider) -> None:
    assert len(provider.list_reservations()) == EXPECTED_RESERVATIONS
    northwind = provider.list_reservations(client=CLIENT_NORTHWIND)
    ministry = provider.list_reservations(client=CLIENT_MINISTRY)
    assert len(northwind) == 2
    assert len(ministry) == 1
    assert all(r.client == CLIENT_NORTHWIND for r in northwind)
    assert provider.list_reservations(client="no-such-client") == ()


def test_reservations_carry_commitment_structure(provider: MockAzureProvider) -> None:
    """Term, quantity and expiry are what distinguish a commitment from a resource."""
    under_used = next(r for r in provider.list_reservations() if r.name == "nw-compute-dsv5-3y")
    assert under_used.term_months == 36
    assert under_used.quantity == 4
    assert under_used.utilisation_pct == Decimal("34.5")
    assert under_used.expires_on is not None
    assert under_used.is_measured
    assert not under_used.monthly_amortised_cost.provenance.is_model_derived


def test_a_reservation_is_priced_from_the_real_reserved_meter(
    provider: MockAzureProvider,
) -> None:
    """The quantity is synthetic; the rate is not.

    What a given customer paid for a reservation is contract data with no public API,
    so the amortised cost is the published reserved rate times a quantity this
    provider supplies -- which keeps the last invented price out of the repository.
    """
    under_used = next(r for r in provider.list_reservations() if r.name == "nw-compute-dsv5-3y")
    reserved = provider.catalogue.reserved_price_for("Standard_D4s_v5", 36, "eastus")
    assert reserved is not None
    assert reserved.monthly_cost.amount is not None
    assert under_used.monthly_amortised_cost.amount == reserved.monthly_cost.amount * 4

    reference = under_used.monthly_amortised_cost.provenance.reference or ""
    assert "per instance per month" in reference
    assert "4 instance(s)" in reference
    assert reserved.meter is not None
    assert reserved.meter.meter_id in reference


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


def test_every_resource_cost_matches_the_catalogue(provider: MockAzureProvider) -> None:
    """A resource cost and its catalogue price must not be able to drift apart.

    Every kind is priced from the catalogue, not just compute, so there is one source
    for every figure the system reports. For a per-GB SKU the rate comes from the
    catalogue and the capacity from the provider, so the check is the product.
    """
    for subscription in provider.list_subscriptions():
        region = subscription.tags["region"]
        for resource in provider.list_resources(subscription.subscription_id):
            if resource.sku is None:
                continue
            priced = provider.catalogue.price_for(resource.sku, resource.kind, region)
            assert priced is not None, f"{resource.name} sku {resource.sku} is unpriced"
            expected = priced.monthly_for(resource.capacity_gb)
            assert resource.monthly_cost.amount == expected.amount


def test_a_per_gb_resource_carries_the_capacity_it_is_priced_on(
    provider: MockAzureProvider,
) -> None:
    """The rate is Azure's; the quantity is this provider's."""
    from costsentinel.domain.estate import PriceUnit

    seen = 0
    for subscription in provider.list_subscriptions():
        region = subscription.tags["region"]
        for resource in provider.list_resources(subscription.subscription_id):
            if resource.sku is None:
                continue
            priced = provider.catalogue.price_for(resource.sku, resource.kind, region)
            assert priced is not None
            if priced.unit is not PriceUnit.PER_GB_MONTH:
                continue
            seen += 1
            assert resource.capacity_gb is not None, f"{resource.name} has no capacity"
            assert priced.meter is not None
            assert resource.monthly_cost.amount == (
                priced.meter.unit_price * resource.capacity_gb
            ).quantize(Decimal("0.01"))
    assert seen >= 3  # snapshots and storage accounts


def test_every_resource_cost_is_either_real_or_explicitly_unknown(
    provider: MockAzureProvider,
) -> None:
    """Nothing in the estate claims a synthetic price any more."""
    from costsentinel.domain.common import ProvenanceSource, Verification

    for subscription in provider.list_subscriptions():
        for resource in provider.list_resources(subscription.subscription_id):
            provenance = resource.monthly_cost.provenance
            if resource.monthly_cost.is_known:
                assert provenance.source is ProvenanceSource.AZURE_RETAIL_PRICES
                assert provenance.verification is Verification.VERIFIED
            else:
                assert provenance.verification is Verification.UNKNOWN


def test_sku_catalogue_is_priced_per_region(provider: MockAzureProvider) -> None:
    """Each region is fetched separately, with its own meter per SKU.

    Not every SKU differs between regions: a few, like the standard static public
    address, are billed from a single global meter at the same rate everywhere. So
    the property worth asserting is that *most* SKUs are regionally priced and that
    a price difference always comes with a different meter -- not that every SKU
    differs, which would be a claim about Azure rather than about this code.
    """
    eastus = {(p.sku, p.applies_to): p for p in provider.list_sku_prices("eastus")}
    qatar = {(p.sku, p.applies_to): p for p in provider.list_sku_prices("qatarcentral")}
    assert eastus
    assert qatar

    # Most SKUs are sold in both, but not all -- which is a real fact, not a gap.
    shared = set(eastus) & set(qatar)
    assert len(shared) >= 20
    assert set(eastus) - set(qatar), "at least one SKU is region-limited"

    regionally_priced = 0
    for key in shared:
        base, other = eastus[key], qatar[key]
        assert base.meter is not None
        assert other.meter is not None
        if base.meter.unit_price != other.meter.unit_price:
            regionally_priced += 1
            assert base.meter.meter_id != other.meter.meter_id
        else:
            # A single global meter, legitimately priced the same in both.
            assert base.meter.meter_id == other.meter.meter_id

    # The overwhelming majority are regionally priced; a handful are global.
    assert regionally_priced >= len(shared) - 2


def test_an_unlisted_region_yields_no_prices(provider: MockAzureProvider) -> None:
    """No fallback: a region nobody priced is empty rather than borrowing prices."""
    assert provider.list_sku_prices("mars-central") == ()


def test_a_resource_in_an_unpriced_region_has_an_unknown_cost(
    provider: MockAzureProvider,
) -> None:
    """Borrowing a price from another region would quietly invent a figure."""
    from costsentinel.domain.estate import ResourceKind, ResourceState
    from costsentinel.providers.mock import _ResSpec

    spec = _ResSpec(
        name="vm-elsewhere",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
    )
    priced = provider._resource_monthly(spec, "mars-central")
    assert not priced.is_known
    assert "mars-central" in (priced.provenance.reference or "")


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
