"""Price catalogue and refresh-script tests.

The catalogue is the single source of every monetary figure CostSentinel reports, so
the important properties are that it loads, that it is honest about whether it was
really fetched, and that a malformed one fails loudly instead of silently pricing
everything at zero -- which would make all savings look like nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from costsentinel.domain.common import ProvenanceSource, Verification
from costsentinel.domain.estate import ResourceKind
from costsentinel.providers.catalogue import (
    CATALOGUE_PATH,
    CatalogueError,
    catalogue_provenance,
    default_catalogue,
    load_catalogue,
)

REFRESH_SCRIPT = Path("scripts/refresh_prices.py")


# ---------------------------------------------------------------------------
# Loading the committed snapshot
# ---------------------------------------------------------------------------


def test_the_committed_snapshot_loads() -> None:
    catalogue = load_catalogue()
    assert catalogue.schema_version == 1
    assert catalogue.entries
    assert catalogue.base_region == "eastus"
    assert catalogue.regions


def test_the_snapshot_is_loaded_once_per_process() -> None:
    assert default_catalogue() is default_catalogue()


def test_every_entry_is_priced_and_typed() -> None:
    for entry in load_catalogue().entries:
        assert entry.monthly_cost.is_known
        assert entry.monthly_cost.amount is not None
        assert entry.monthly_cost.amount > 0
        assert isinstance(entry.applies_to, ResourceKind)
        assert entry.family


def test_compute_and_tier_skus_carry_capacity_and_others_do_not() -> None:
    """A disk has no vCPU, so it cannot take part in capacity-based rightsizing."""
    by_kind: dict[ResourceKind, list[bool]] = {}
    for entry in load_catalogue().entries:
        by_kind.setdefault(entry.applies_to, []).append(entry.has_capacity)

    assert all(by_kind[ResourceKind.VIRTUAL_MACHINE])
    assert all(by_kind[ResourceKind.SQL_DATABASE])
    assert all(by_kind[ResourceKind.APP_SERVICE_PLAN])
    assert not any(by_kind[ResourceKind.MANAGED_DISK])
    assert not any(by_kind[ResourceKind.PUBLIC_IP])
    assert not any(by_kind[ResourceKind.SNAPSHOT])


def test_each_resource_kind_has_more_than_one_sku_where_rightsizing_applies() -> None:
    """A single-SKU family has nothing to scale down to."""
    catalogue = load_catalogue()
    for kind in (
        ResourceKind.VIRTUAL_MACHINE,
        ResourceKind.SQL_DATABASE,
        ResourceKind.APP_SERVICE_PLAN,
    ):
        skus = [e for e in catalogue.entries if e.applies_to is kind]
        assert len(skus) >= 2, f"{kind.value} has nothing to rightsize to"


# ---------------------------------------------------------------------------
# Honesty about provenance
# ---------------------------------------------------------------------------


def test_the_shipped_snapshot_admits_it_was_not_fetched() -> None:
    """Curated figures must not be presented as verified Azure prices."""
    catalogue = load_catalogue()
    assert catalogue.is_real_api_pull is False
    assert "NOT a live pull" in catalogue.source

    provenance = catalogue_provenance(catalogue)
    assert provenance.source is ProvenanceSource.AZURE_RETAIL_PRICES
    assert provenance.verification is Verification.UNVERIFIED
    assert "curated snapshot" in (provenance.reference or "")


def test_a_real_pull_would_be_marked_verified(tmp_path: Path) -> None:
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["is_real_api_pull"] = True
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    provenance = catalogue_provenance(load_catalogue(target))
    assert provenance.verification is Verification.VERIFIED
    assert "live API pull" in (provenance.reference or "")


def test_provenance_is_dated_from_the_snapshot_not_from_load_time() -> None:
    """A price was retrieved when it was fetched, not when a process started."""
    catalogue = load_catalogue()
    provenance = catalogue_provenance(catalogue)
    assert provenance.retrieved_at.isoformat().startswith("2026-10-08")
    # Which is also what makes the catalogue deterministic across processes.
    assert catalogue_provenance(load_catalogue()).retrieved_at == provenance.retrieved_at


def test_an_unparseable_timestamp_falls_back_visibly(tmp_path: Path) -> None:
    payload = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    payload["retrieved_at"] = "not-a-date"
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    provenance = catalogue_provenance(load_catalogue(target))
    assert provenance.retrieved_at.year == 1970


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def test_the_base_region_multiplier_is_one() -> None:
    catalogue = load_catalogue()
    assert catalogue.region_multiplier(catalogue.base_region) == Decimal("1.00")


def test_a_modelled_region_says_so_in_its_basis() -> None:
    """A modelled uplift must not be mistaken for a fetched price."""
    catalogue = load_catalogue()
    qatar = next(r for r in catalogue.regions if r.region == "qatarcentral")
    assert qatar.multiplier > Decimal(1)
    assert "not a fetched price" in qatar.basis


def test_an_unlisted_region_falls_back_to_the_base_prices() -> None:
    catalogue = load_catalogue()
    assert catalogue.region_multiplier("mars-central") == Decimal("1.00")


def test_pricing_matches_on_both_sku_and_kind() -> None:
    """Without the kind, a SQL tier could be priced as a virtual machine."""
    catalogue = load_catalogue()
    assert catalogue.price_for("GP_Gen5_4", ResourceKind.SQL_DATABASE, "eastus") is not None
    assert catalogue.price_for("GP_Gen5_4", ResourceKind.VIRTUAL_MACHINE, "eastus") is None
    assert catalogue.price_for("no-such-sku", ResourceKind.VIRTUAL_MACHINE, "eastus") is None


def test_regional_prices_are_scaled_and_rounded_to_cents() -> None:
    catalogue = load_catalogue()
    base = catalogue.price_for("P1v3", ResourceKind.APP_SERVICE_PLAN, "eastus")
    regional = catalogue.price_for("P1v3", ResourceKind.APP_SERVICE_PLAN, "qatarcentral")
    assert base is not None
    assert regional is not None
    scaled = regional.monthly_cost.amount
    assert base.monthly_cost.amount == Decimal("219.00")
    assert scaled is not None
    assert scaled == Decimal("245.28")  # 219.00 x 1.12
    assert scaled.as_tuple().exponent == -2


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


def test_a_missing_required_key_raises(tmp_path: Path) -> None:
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
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
    payload["skus"] = [{"sku": "broken", "applies_to": "not_a_kind", "family": "x"}]
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


def test_the_script_lives_outside_the_installed_package() -> None:
    """It must not be importable as part of the library at runtime."""
    assert REFRESH_SCRIPT.exists()
    assert not (Path("src/costsentinel") / "refresh_prices.py").exists()
