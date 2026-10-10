"""Price catalogue and refresh-script tests.

The catalogue is the single source of every monetary figure CostSentinel reports, so
the properties that matter are: it loads; every price names the Azure meter it came
from, so a reviewer can check it by hand; a region a SKU is not sold in is absent
rather than estimated; and a malformed snapshot fails loudly instead of silently
pricing everything at zero, which would make all savings look like nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from costsentinel.domain.common import ProvenanceSource, Verification
from costsentinel.domain.estate import PriceUnit, ResourceKind
from costsentinel.providers.catalogue import (
    CATALOGUE_PATH,
    SUPPORTED_SCHEMA_VERSION,
    CatalogueError,
    catalogue_provenance,
    default_catalogue,
    load_catalogue,
)

REFRESH_SCRIPT = Path("scripts/refresh_prices.py")

#: Meter name fragments that must never price a running resource. Spot and Low
#: Priority are evictable capacity; a crawl that took the cheapest meter would commit
#: these as if they were what a VM costs.
FORBIDDEN_METER_FRAGMENTS = ("Spot", "Low Priority")

#: Product name fragments that are a different product at a different rate.
FORBIDDEN_PRODUCT_FRAGMENTS = ("Windows",)


# ---------------------------------------------------------------------------
# Loading the committed snapshot
# ---------------------------------------------------------------------------


def test_the_committed_snapshot_loads() -> None:
    catalogue = load_catalogue()
    assert catalogue.schema_version == SUPPORTED_SCHEMA_VERSION
    assert catalogue.entries
    assert catalogue.hours_per_month == 730
    assert set(catalogue.regions) == {"eastus", "qatarcentral"}


def test_the_snapshot_is_loaded_once_per_process() -> None:
    assert default_catalogue() is default_catalogue()


def test_every_entry_names_the_meter_it_came_from() -> None:
    """Without this, "the catalogue says so" is the end of the audit trail."""
    for entry in load_catalogue().entries:
        assert entry.meter is not None, f"{entry.sku} has no meter"
        assert entry.meter.meter_id
        assert entry.meter.meter_name
        assert entry.meter.product_name
        assert entry.meter.unit_of_measure
        assert entry.meter.unit_price > 0
        assert entry.meter.meter_id in entry.meter.describe()


def test_a_flat_or_hourly_entry_has_a_known_monthly_cost() -> None:
    for entry in load_catalogue().entries:
        if entry.unit.needs_capacity:
            continue
        assert entry.monthly_cost.is_known, f"{entry.sku} in {entry.region} is unpriced"
        assert entry.monthly_cost.amount is not None
        assert entry.monthly_cost.amount > 0


def test_a_per_gb_entry_has_no_monthly_cost_until_a_capacity_is_supplied() -> None:
    """The quantity belongs to the resource, not to the price."""
    per_gb = [e for e in load_catalogue().entries if e.unit is PriceUnit.PER_GB_MONTH]
    assert per_gb, "the estate has per-GB resources, so the catalogue must price some"
    for entry in per_gb:
        assert not entry.monthly_cost.is_known
        assert "needs a capacity" in (entry.monthly_cost.provenance.reference or "")

        priced = entry.monthly_for(Decimal(100))
        assert priced.is_known
        assert entry.meter is not None
        assert priced.amount == (entry.meter.unit_price * 100).quantize(Decimal("0.01"))
        assert "100 GB" in (priced.provenance.reference or "")


def test_a_per_gb_entry_without_a_capacity_is_unknown_not_zero() -> None:
    entry = next(e for e in load_catalogue().entries if e.unit is PriceUnit.PER_GB_MONTH)
    unpriced = entry.monthly_for(None)
    assert not unpriced.is_known
    assert "no capacity was reported" in (unpriced.provenance.reference or "")


def test_a_reservation_entry_carries_its_term() -> None:
    """A reservation is quoted as a total for the term, so the term is the divisor."""
    reserved = [e for e in load_catalogue().entries if e.unit is PriceUnit.PER_TERM]
    assert reserved
    for entry in reserved:
        assert entry.term_months in {12, 36}
        assert entry.monthly_cost.is_known
        assert entry.meter is not None
        expected = (entry.meter.unit_price / entry.term_months).quantize(Decimal("0.01"))
        assert entry.monthly_cost.amount == expected


def test_compute_and_tier_skus_carry_capacity_and_others_do_not() -> None:
    """A disk has no vCPU, so it cannot take part in capacity-based rightsizing."""
    by_kind: dict[ResourceKind, list[bool]] = {}
    for entry in load_catalogue().entries:
        if entry.unit is PriceUnit.PER_TERM:
            continue  # a reserved entry is a price, not a sizing candidate
        by_kind.setdefault(entry.applies_to, []).append(entry.has_capacity)

    assert all(by_kind[ResourceKind.VIRTUAL_MACHINE])
    assert all(by_kind[ResourceKind.SQL_DATABASE])
    assert all(by_kind[ResourceKind.APP_SERVICE_PLAN])
    assert not any(by_kind[ResourceKind.MANAGED_DISK])
    assert not any(by_kind[ResourceKind.PUBLIC_IP])
    assert not any(by_kind[ResourceKind.SNAPSHOT])


def test_each_rightsizable_kind_has_more_than_one_sku_per_region() -> None:
    """A single-SKU family has nothing to scale down to."""
    catalogue = load_catalogue()
    for region in catalogue.regions:
        for kind in (
            ResourceKind.VIRTUAL_MACHINE,
            ResourceKind.SQL_DATABASE,
            ResourceKind.APP_SERVICE_PLAN,
        ):
            skus = [
                e
                for e in catalogue.for_region(region)
                if e.applies_to is kind and e.unit is not PriceUnit.PER_TERM
            ]
            assert len(skus) >= 2, f"{kind.value} in {region} has nothing to rightsize to"


# ---------------------------------------------------------------------------
# Honesty about provenance
# ---------------------------------------------------------------------------


def test_the_shipped_snapshot_is_a_real_api_pull() -> None:
    """Phase 3 replaced the curated figures with fetched ones."""
    catalogue = load_catalogue()
    assert catalogue.is_real_api_pull is True
    assert catalogue.source == "Azure Retail Prices API"

    provenance = catalogue_provenance(catalogue)
    assert provenance.source is ProvenanceSource.AZURE_RETAIL_PRICES
    assert provenance.verification is Verification.VERIFIED
    assert "live API pull" in (provenance.reference or "")


def test_a_curated_snapshot_would_be_marked_unverified(tmp_path: Path) -> None:
    """The distinction must survive, so a future curated snapshot cannot pass as real."""
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["is_real_api_pull"] = False
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    provenance = catalogue_provenance(load_catalogue(target))
    assert provenance.verification is Verification.UNVERIFIED
    assert "curated snapshot" in (provenance.reference or "")


def test_every_price_carries_verified_provenance() -> None:
    for entry in load_catalogue().entries:
        assert entry.rate_provenance is not None
        assert entry.rate_provenance.verification is Verification.VERIFIED
        assert entry.rate_provenance.source is ProvenanceSource.AZURE_RETAIL_PRICES
        assert not entry.rate_provenance.is_model_derived


def test_provenance_names_the_meter_and_the_region() -> None:
    catalogue = load_catalogue()
    entry = catalogue.price_for("Standard_D4s_v5", ResourceKind.VIRTUAL_MACHINE, "eastus")
    assert entry is not None
    assert entry.rate_provenance is not None
    reference = entry.rate_provenance.reference or ""
    assert "region=eastus" in reference
    assert "Virtual Machines Dsv5 Series" in reference
    assert entry.meter is not None
    assert entry.meter.meter_id in reference


def test_provenance_is_dated_from_the_snapshot_not_from_load_time() -> None:
    """A price was retrieved when it was fetched, not when a process started."""
    catalogue = load_catalogue()
    first = catalogue_provenance(catalogue).retrieved_at
    assert first.isoformat().startswith(catalogue.retrieved_at[:10])
    # Which is also what makes the catalogue deterministic across processes.
    assert catalogue_provenance(load_catalogue()).retrieved_at == first


def test_an_unparseable_timestamp_falls_back_visibly(tmp_path: Path) -> None:
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["retrieved_at"] = "not-a-date"
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    provenance = catalogue_provenance(load_catalogue(target))
    assert provenance.retrieved_at.year == 1970


# ---------------------------------------------------------------------------
# Only pay-as-you-go consumption rates
# ---------------------------------------------------------------------------


def test_no_price_came_from_a_spot_or_low_priority_meter() -> None:
    """Evictable capacity is not what a running resource costs."""
    for entry in load_catalogue().entries:
        assert entry.meter is not None
        for fragment in FORBIDDEN_METER_FRAGMENTS:
            assert fragment not in entry.meter.meter_name, f"{entry.sku}: {fragment} meter"


def test_no_price_came_from_a_windows_meter() -> None:
    """A Windows image carries a licence premium and is a different product."""
    for entry in load_catalogue().entries:
        assert entry.meter is not None
        for fragment in FORBIDDEN_PRODUCT_FRAGMENTS:
            assert fragment not in entry.meter.product_name, f"{entry.sku}: {fragment} product"


def test_only_reserved_entries_came_from_a_reservation_meter() -> None:
    """A reservation rate must not be committed as a pay-as-you-go price."""
    for entry in load_catalogue().entries:
        reserved_by_name = "_RI" in entry.sku
        assert (entry.unit is PriceUnit.PER_TERM) == reserved_by_name


def test_an_hourly_rate_annualises_over_a_730_hour_month() -> None:
    catalogue = load_catalogue()
    entry = catalogue.price_for("Standard_D4s_v5", ResourceKind.VIRTUAL_MACHINE, "eastus")
    assert entry is not None
    assert entry.unit is PriceUnit.PER_HOUR
    assert entry.meter is not None
    assert entry.monthly_cost.amount == (
        entry.meter.unit_price * catalogue.hours_per_month
    ).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def test_prices_are_fetched_per_region_not_derived() -> None:
    """Two regions, two separately fetched meters, two different meter ids."""
    catalogue = load_catalogue()
    base = catalogue.price_for("Standard_D4s_v5", ResourceKind.VIRTUAL_MACHINE, "eastus")
    other = catalogue.price_for("Standard_D4s_v5", ResourceKind.VIRTUAL_MACHINE, "qatarcentral")
    assert base is not None
    assert other is not None
    assert base.meter is not None
    assert other.meter is not None
    assert base.meter.meter_id != other.meter.meter_id
    assert base.monthly_cost.amount != other.monthly_cost.amount


def test_a_sku_not_sold_in_a_region_is_absent_rather_than_estimated() -> None:
    """Cool GRS block blob is genuinely not sold in qatarcentral."""
    catalogue = load_catalogue()
    assert (
        catalogue.price_for("Standard_GRS_Cool", ResourceKind.STORAGE_ACCOUNT, "eastus") is not None
    )
    assert (
        catalogue.price_for("Standard_GRS_Cool", ResourceKind.STORAGE_ACCOUNT, "qatarcentral")
        is None
    )


def test_an_unlisted_region_has_no_prices_at_all() -> None:
    """No fallback: a region nobody fetched prices for yields nothing, not base prices."""
    assert load_catalogue().for_region("mars-central") == ()


def test_each_region_has_its_own_entry_set() -> None:
    catalogue = load_catalogue()
    for region in catalogue.regions:
        entries = catalogue.for_region(region)
        assert entries
        assert all(e.region == region for e in entries)


def test_pricing_matches_on_sku_kind_and_region() -> None:
    """Without the kind, a SQL tier could be priced as a virtual machine."""
    catalogue = load_catalogue()
    assert catalogue.price_for("GP_Gen5_4", ResourceKind.SQL_DATABASE, "eastus") is not None
    assert catalogue.price_for("GP_Gen5_4", ResourceKind.VIRTUAL_MACHINE, "eastus") is None
    assert catalogue.price_for("no-such-sku", ResourceKind.VIRTUAL_MACHINE, "eastus") is None


def test_reserved_price_lookup_is_by_sku_and_term() -> None:
    catalogue = load_catalogue()
    three_year = catalogue.reserved_price_for("Standard_D4s_v5", 36, "eastus")
    one_year = catalogue.reserved_price_for("Standard_D4s_v5", 12, "eastus")
    assert three_year is not None
    assert one_year is not None
    assert three_year.monthly_cost.amount is not None
    assert one_year.monthly_cost.amount is not None
    # A longer commitment buys a lower monthly rate; that is the point of one.
    assert three_year.monthly_cost.amount < one_year.monthly_cost.amount
    assert catalogue.reserved_price_for("Standard_D4s_v5", 60, "eastus") is None


def test_a_reservation_is_cheaper_per_month_than_pay_as_you_go() -> None:
    """A sanity check on the real figures: otherwise nobody would buy one."""
    catalogue = load_catalogue()
    on_demand = catalogue.price_for("Standard_D4s_v5", ResourceKind.VIRTUAL_MACHINE, "eastus")
    reserved = catalogue.reserved_price_for("Standard_D4s_v5", 36, "eastus")
    assert on_demand is not None
    assert reserved is not None
    assert on_demand.monthly_cost.amount is not None
    assert reserved.monthly_cost.amount is not None
    assert reserved.monthly_cost.amount < on_demand.monthly_cost.amount


# ---------------------------------------------------------------------------
# A malformed catalogue fails loudly
# ---------------------------------------------------------------------------


def test_a_missing_catalogue_raises(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError, match="not found"):
        load_catalogue(tmp_path / "absent.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    target = tmp_path / "catalogue.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(CatalogueError, match="not valid JSON"):
        load_catalogue(target)


def test_an_older_schema_is_rejected_rather_than_partially_read(tmp_path: Path) -> None:
    """A half-understood price table is worse than no price table."""
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogueError, match="schema version 1"):
        load_catalogue(target)


def test_a_missing_required_key_raises(tmp_path: Path) -> None:
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps({"schema_version": SUPPORTED_SCHEMA_VERSION}), encoding="utf-8")
    with pytest.raises(CatalogueError, match="missing required key"):
        load_catalogue(target)


def test_an_empty_catalogue_raises_rather_than_pricing_everything_at_zero(
    tmp_path: Path,
) -> None:
    """Silently-zero prices would make every saving look like nothing."""
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["skus"] = []
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogueError, match="contains no SKUs"):
        load_catalogue(target)


def test_a_malformed_sku_entry_raises(tmp_path: Path) -> None:
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["skus"] = [
        {
            "sku": "broken",
            "applies_to": "not_a_kind",
            "family": "x",
            "unit": "per_hour",
            "prices": {},
        }
    ]
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogueError, match="malformed SKU entry"):
        load_catalogue(target)


def test_an_entry_with_no_meter_raises(tmp_path: Path) -> None:
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["skus"] = [
        {
            "sku": "broken",
            "applies_to": "virtual_machine",
            "family": "x",
            "unit": "per_hour",
            "prices": {"eastus": {"monthly_cost": "1.00"}},
        }
    ]
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogueError, match="malformed SKU entry"):
        load_catalogue(target)


# ---------------------------------------------------------------------------
# The refresh script is manual only
# ---------------------------------------------------------------------------


def test_no_test_imports_the_refresh_script() -> None:
    """CI and the suite must never reach the network to price anything.

    This file is skipped: it names the forbidden imports in order to look for them,
    so scanning itself would always fail.
    """
    forbidden = ("import refresh_prices", "from refresh_prices", "scripts.refresh_prices")
    for path in Path("tests").glob("test_*.py"):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in source, f"{path.name} reaches for the refresh script"


def test_ci_does_not_invoke_the_refresh_script() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "refresh_prices" not in workflow


def test_the_script_warns_that_it_changes_committed_figures() -> None:
    """A silent price change would silently change what a client is told."""
    source = REFRESH_SCRIPT.read_text(encoding="utf-8")
    assert "Run this by hand" in source
    assert "re-baselining" in source
    assert "Never from CI" in source


def test_the_script_excludes_the_meters_that_would_misprice_a_resource() -> None:
    """The exclusions are the difference between a real price and a plausible one."""
    source = REFRESH_SCRIPT.read_text(encoding="utf-8")
    for fragment in FORBIDDEN_METER_FRAGMENTS:
        assert fragment in source
    assert "Windows" in source
    assert "Reservation" in source


def test_the_script_lives_outside_the_installed_package() -> None:
    """It must not be importable as part of the library at runtime."""
    assert REFRESH_SCRIPT.exists()
    assert not (Path("src/costsentinel") / "refresh_prices.py").exists()
