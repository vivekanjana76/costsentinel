#!/usr/bin/env python
"""Refresh the committed price catalogue from the public Azure Retail Prices API.

**Run this by hand. Never from CI, and never from a test.**

CostSentinel's savings figures are baselined against the committed snapshot at
``src/costsentinel/providers/data/price_catalogue.json``. That file is the single
source of every price the system reports, which is what lets tests and CI run offline
and produce identical numbers on every machine.

Running this script changes committed figures. Expect the savings assertions in
``tests/test_graph.py`` to need re-baselining afterwards, and review the diff before
committing it -- a silent price change would silently change what CostSentinel tells
a client they can save.

Usage::

    uv run python scripts/refresh_prices.py --verify    # resolve meters, write nothing
    uv run python scripts/refresh_prices.py --dry-run   # fetch and report, write nothing
    uv run python scripts/refresh_prices.py             # write the snapshot

The API is public and needs no authentication, but it does need network access --
which is precisely why this is not part of any automated run.

Why a selection table rather than a crawl
-----------------------------------------

The Retail Prices API returns many meters per SKU: Spot, Low Priority, Windows,
DevTest, reservations and savings plans, plus volume tiers. A generic crawl that
picked, say, the cheapest meter would quietly commit Spot rates as if they were what a
resource costs -- a plausible-looking number that is simply wrong.

So every catalogue SKU below names the meter it wants, explicitly. Each resolved price
is committed together with the meter id, meter name, product name and unit it came
from, so a reviewer can check any figure against Azure by hand. A selector that
matches zero meters, or two with different prices, is an error that writes nothing
rather than a guess.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_PATH = REPO_ROOT / "src" / "costsentinel" / "providers" / "data" / "price_catalogue.json"

API_ENDPOINT = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
SCHEMA_VERSION = 2
HOURS_PER_MONTH = 730
CURRENCY = "USD"
USER_AGENT = "CostSentinel-price-refresh/0.3 (+https://github.com/vivekanjana76/costsentinel)"

#: Regions the mock estate spans. Prices are fetched for each, directly.
DEFAULT_REGIONS = ("eastus", "qatarcentral")

MAX_PAGES = 20
REQUEST_TIMEOUT_SECONDS = 30
_CENTS = Decimal("0.01")
_RATE_PLACES = Decimal("0.00000001")

#: How a unit price becomes a monthly figure.
UNIT_PER_HOUR = "per_hour"
UNIT_PER_MONTH = "per_month"
UNIT_PER_GB_MONTH = "per_gb_month"
#: A reservation is quoted as a total for its whole term, not a rate.
UNIT_PER_TERM = "per_term"

#: Meter name fragments that are never a pay-as-you-go rate for a running resource.
EXCLUDED_METER_FRAGMENTS = ("Spot", "Low Priority")

#: Price types that are not pay-as-you-go consumption.
REQUIRED_PRICE_TYPE = "Consumption"


class Selector(NamedTuple):
    """Which Azure meter a catalogue SKU takes its price from.

    ``service_name`` plus any of ``product_name``, ``sku_name`` and ``arm_sku_name``
    go into the server-side OData filter. The rest are applied to the result, because
    the API does not support negation or substring exclusion in a filter.
    """

    service_name: str
    unit_of_measure: str
    product_name: str | None = None
    sku_name: str | None = None
    arm_sku_name: str | None = None
    meter_name: str | None = None
    product_excludes: tuple[str, ...] = ()
    first_tier_only: bool = False
    reservation_term: str | None = None

    def odata_filter(self, region: str) -> str:
        """The server-side filter, narrowed as far as the API allows."""
        price_type = "Reservation" if self.reservation_term else REQUIRED_PRICE_TYPE
        clauses = [
            f"armRegionName eq '{region}'",
            f"serviceName eq '{self.service_name}'",
            f"type eq '{price_type}'",
        ]
        if self.product_name:
            clauses.append(f"productName eq '{self.product_name}'")
        if self.sku_name:
            clauses.append(f"skuName eq '{self.sku_name}'")
        if self.arm_sku_name:
            clauses.append(f"armSkuName eq '{self.arm_sku_name}'")
        return " and ".join(clauses)

    def matches(self, meter: dict[str, Any]) -> bool:
        """Whether one meter is the one this SKU wants."""
        expected_type = "Reservation" if self.reservation_term else REQUIRED_PRICE_TYPE
        if str(meter.get("type")) != expected_type:
            return False
        if str(meter.get("reservationTerm") or "") != (self.reservation_term or ""):
            return False
        if str(meter.get("unitOfMeasure")) != self.unit_of_measure:
            return False
        meter_name = str(meter.get("meterName", ""))
        if self.meter_name is not None and meter_name != self.meter_name:
            return False
        if any(fragment in meter_name for fragment in EXCLUDED_METER_FRAGMENTS):
            return False
        product = str(meter.get("productName", ""))
        if any(excluded in product for excluded in self.product_excludes):
            return False
        # A tiered meter quotes a lower rate above a volume threshold; the first
        # tier is the one a small account actually pays.
        return not (
            self.first_tier_only and Decimal(str(meter.get("tierMinimumUnits", 0) or 0)) != 0
        )


class CatalogueSku(NamedTuple):
    """One SKU in CostSentinel's catalogue, and where its price comes from."""

    sku: str
    applies_to: str
    family: str
    unit: str
    selector: Selector
    vcpu: int | None = None
    memory_gb: str | None = None
    term_months: int | None = None


def _vm(sku: str, family: str, vcpu: int, memory_gb: str) -> CatalogueSku:
    """A pay-as-you-go Linux virtual machine meter.

    Windows images carry a licence premium and are a different product; Spot and Low
    Priority are evictable capacity at a different rate. None of them is what a
    running Linux VM costs, so all three are excluded.
    """
    return CatalogueSku(
        sku=sku,
        applies_to="virtual_machine",
        family=family,
        unit=UNIT_PER_HOUR,
        vcpu=vcpu,
        memory_gb=memory_gb,
        selector=Selector(
            service_name="Virtual Machines",
            arm_sku_name=sku,
            unit_of_measure="1 Hour",
            product_excludes=("Windows",),
        ),
    )


def _reserved_vm(sku: str, family: str, term: str, term_months: int) -> CatalogueSku:
    """A reserved-instance meter for a virtual machine.

    A reservation is quoted as one total for the whole term, so the catalogue stores
    that total and the term; the amortised monthly figure is the division. Pricing a
    mock reservation from the real reserved rate is what keeps the last synthetic
    price out of the repository -- what a given customer actually paid is contract
    data with no public API, but a reservation bought at list price costs this.
    """
    return CatalogueSku(
        sku=f"{sku}_RI{term_months}M",
        applies_to="virtual_machine",
        family=family,
        unit=UNIT_PER_TERM,
        term_months=term_months,
        selector=Selector(
            service_name="Virtual Machines",
            arm_sku_name=sku,
            unit_of_measure="1 Hour",
            product_excludes=("Windows",),
            reservation_term=term,
        ),
    )


def _disk(tier: str) -> CatalogueSku:
    """A premium managed disk, billed flat per month.

    The ``Disk Mount`` meter is a separate small per-mount fee, not the disk itself,
    so the meter name is pinned exactly.
    """
    return CatalogueSku(
        sku=f"Premium_LRS_{tier}",
        applies_to="managed_disk",
        family="PremiumSSD",
        unit=UNIT_PER_MONTH,
        selector=Selector(
            service_name="Storage",
            product_name="Premium SSD Managed Disks",
            sku_name=f"{tier} LRS",
            meter_name=f"{tier} LRS Disk",
            unit_of_measure="1/Month",
        ),
    )


def _sql(vcore: int) -> CatalogueSku:
    """A General Purpose Gen5 vCore meter, provisioned rather than serverless.

    Serverless, zone-redundant, Gen4 and the DC, FSv2 and M series are all separate
    products at separate rates, so the product name is pinned exactly.
    """
    return CatalogueSku(
        sku=f"GP_Gen5_{vcore}",
        applies_to="sql_database",
        family="SqlGpGen5",
        unit=UNIT_PER_HOUR,
        vcpu=vcore,
        memory_gb=f"{Decimal('5.1') * vcore}",
        selector=Selector(
            service_name="SQL Database",
            product_name="SQL Database Single/Elastic Pool General Purpose - Compute Gen5",
            sku_name=f"{vcore} vCore",
            meter_name="vCore",
            unit_of_measure="1 Hour",
        ),
    )


def _app_service(
    sku: str, meter: str, product: str, vcpu: int, memory_gb: str, family: str
) -> CatalogueSku:
    """An App Service plan meter, Linux rather than Windows."""
    return CatalogueSku(
        sku=sku,
        applies_to="app_service_plan",
        family=family,
        unit=UNIT_PER_HOUR,
        vcpu=vcpu,
        memory_gb=memory_gb,
        selector=Selector(
            service_name="Azure App Service",
            product_name=product,
            meter_name=meter,
            unit_of_measure="1 Hour",
        ),
    )


#: The catalogue. Every SKU the mock estate uses, and the meter each takes its price
#: from. Adding a resource to the estate means adding its SKU here.
CATALOGUE: tuple[CatalogueSku, ...] = (
    # --- compute ---------------------------------------------------------
    _vm("Standard_D2s_v5", "Dsv5", 2, "8"),
    _vm("Standard_D4s_v5", "Dsv5", 4, "16"),
    _vm("Standard_D8s_v5", "Dsv5", 8, "32"),
    _vm("Standard_D16s_v5", "Dsv5", 16, "64"),
    _vm("Standard_D32s_v5", "Dsv5", 32, "128"),
    _vm("Standard_E2s_v5", "Esv5", 2, "16"),
    _vm("Standard_E4s_v5", "Esv5", 4, "32"),
    _vm("Standard_E8s_v5", "Esv5", 8, "64"),
    _vm("Standard_E16s_v5", "Esv5", 16, "128"),
    _vm("Standard_E32s_v5", "Esv5", 32, "256"),
    # --- reserved compute, for pricing a reservation's amortised cost ----
    _reserved_vm("Standard_D4s_v5", "Dsv5Reserved", "1 Year", 12),
    _reserved_vm("Standard_D4s_v5", "Dsv5Reserved", "3 Years", 36),
    # --- managed disks ---------------------------------------------------
    _disk("P10"),
    _disk("P15"),
    _disk("P20"),
    _disk("P30"),
    # --- snapshots, billed per GB stored ---------------------------------
    CatalogueSku(
        sku="Premium_LRS_Snapshot",
        applies_to="snapshot",
        family="PremiumSSDSnapshot",
        unit=UNIT_PER_GB_MONTH,
        selector=Selector(
            service_name="Storage",
            product_name="Premium SSD Managed Disks",
            sku_name="Snapshots LRS",
            meter_name="LRS Snapshots",
            unit_of_measure="1 GB/Month",
        ),
    ),
    # --- public addresses ------------------------------------------------
    CatalogueSku(
        sku="Standard_Static",
        applies_to="public_ip",
        family="PublicIPStandard",
        unit=UNIT_PER_HOUR,
        selector=Selector(
            service_name="Virtual Network",
            product_name="IP Addresses",
            sku_name="Standard",
            meter_name="Standard IPv4 Static Public IP",
            unit_of_measure="1 Hour",
        ),
    ),
    # --- blob storage, billed per GB stored ------------------------------
    CatalogueSku(
        sku="Standard_LRS_Hot",
        applies_to="storage_account",
        family="BlockBlobHotLRS",
        unit=UNIT_PER_GB_MONTH,
        selector=Selector(
            service_name="Storage",
            product_name="General Block Blob v2",
            sku_name="Hot LRS",
            meter_name="Hot LRS Data Stored",
            unit_of_measure="1 GB/Month",
            first_tier_only=True,
        ),
    ),
    CatalogueSku(
        sku="Standard_GRS_Cool",
        applies_to="storage_account",
        family="BlockBlobCoolGRS",
        unit=UNIT_PER_GB_MONTH,
        selector=Selector(
            service_name="Storage",
            product_name="General Block Blob v2",
            sku_name="Cool GRS",
            meter_name="Cool GRS Data Stored",
            unit_of_measure="1 GB/Month",
            first_tier_only=True,
        ),
    ),
    # --- SQL -------------------------------------------------------------
    _sql(2),
    _sql(4),
    _sql(8),
    # --- App Service -----------------------------------------------------
    _app_service(
        "P0v3",
        "P0v3 App",
        "Azure App Service Premium v3 Plan - Linux",
        1,
        "4",
        "AppServicePremiumV3",
    ),
    _app_service(
        "P1v3",
        "P1 v3 App",
        "Azure App Service Premium v3 Plan - Linux",
        2,
        "8",
        "AppServicePremiumV3",
    ),
    _app_service(
        "P2v3",
        "P2 v3 App",
        "Azure App Service Premium v3 Plan - Linux",
        4,
        "16",
        "AppServicePremiumV3",
    ),
    _app_service(
        "S1",
        "S1 App",
        "Azure App Service Standard Plan - Linux",
        1,
        "1.75",
        "AppServiceStandard",
    ),
)


class RefreshError(RuntimeError):
    """The refresh could not complete. Nothing is written."""


def _fetch_page(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        msg = f"could not reach the Retail Prices API: {exc}"
        raise RefreshError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Retail Prices API returned a non-JSON response: {exc}"
        raise RefreshError(msg) from exc
    if "Items" not in payload:
        msg = f"Retail Prices API response has no 'Items' key: {sorted(payload)}"
        raise RefreshError(msg)
    return payload


def fetch_meters(selector: Selector, region: str) -> list[dict[str, Any]]:
    """Every meter the selector's server-side filter returns for one region."""
    query = urllib.parse.urlencode(
        {
            "api-version": API_VERSION,
            "currencyCode": CURRENCY,
            "$filter": selector.odata_filter(region),
        }
    )
    url = f"{API_ENDPOINT}?{query}"
    items: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        payload = _fetch_page(url)
        items.extend(payload["Items"])
        next_url = payload.get("NextPageLink")
        if not next_url:
            return items
        url = str(next_url)
    msg = f"pagination for {selector.service_name} in {region} exceeded {MAX_PAGES} pages"
    raise RefreshError(msg)


def monthly_from(unit: str, unit_price: Decimal, term_months: int | None = None) -> Decimal | None:
    """Normalise a unit price to a monthly figure, where that is possible at all.

    A per-GB rate cannot become a per-resource monthly price without a quantity, and
    the quantity belongs to the resource rather than to the price. So those return
    ``None`` and the provider supplies the capacity.
    """
    if unit == UNIT_PER_HOUR:
        return (unit_price * HOURS_PER_MONTH).quantize(_CENTS, rounding=ROUND_HALF_UP)
    if unit == UNIT_PER_MONTH:
        return unit_price.quantize(_CENTS, rounding=ROUND_HALF_UP)
    if unit == UNIT_PER_TERM and term_months:
        return (unit_price / Decimal(term_months)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return None


def resolve(entry: CatalogueSku, region: str) -> dict[str, Any] | None:
    """Resolve one SKU's price in one region to exactly one meter.

    Returns ``None`` when the SKU is simply not sold in that region, which is a
    legitimate answer -- the resource is then priced as unknown rather than guessed.

    Raises:
        RefreshError: If the selector matches several meters at *different* prices,
            which means the selector is ambiguous and a human has to narrow it.
    """
    candidates = [m for m in fetch_meters(entry.selector, region) if entry.selector.matches(m)]
    if not candidates:
        return None

    prices = {Decimal(str(m["retailPrice"])) for m in candidates}
    if len(prices) > 1:
        detail = sorted(
            f"{m.get('meterName')!r}/{m.get('productName')!r}={m.get('retailPrice')}"
            for m in candidates
        )
        msg = (
            f"selector for {entry.sku} in {region} is ambiguous: "
            f"{len(candidates)} meters at {len(prices)} different prices -- {detail}"
        )
        raise RefreshError(msg)

    meter = candidates[0]
    unit_price = Decimal(str(meter["retailPrice"]))
    monthly = monthly_from(entry.unit, unit_price, entry.term_months)
    priced: dict[str, Any] = {
        "unit_price": f"{unit_price.quantize(_RATE_PLACES).normalize():f}",
        "meter_id": str(meter.get("meterId", "")),
        "meter_name": str(meter.get("meterName", "")),
        "product_name": str(meter.get("productName", "")),
        "sku_name": str(meter.get("skuName", "")),
        "unit_of_measure": str(meter.get("unitOfMeasure", "")),
        "effective_start_date": str(meter.get("effectiveStartDate", "")),
    }
    if monthly is not None:
        priced["monthly_cost"] = f"{monthly}"
    return priced


def _describe(priced: dict[str, Any]) -> str:
    """A one-line price for the progress output."""
    monthly = priced.get("monthly_cost")
    return str(monthly) if monthly else f"{priced['unit_price']}/unit"


def build_snapshot(regions: Sequence[str], *, verify_only: bool) -> dict[str, Any]:
    """Resolve every catalogue SKU in every region."""
    skus: list[dict[str, Any]] = []
    missing: list[str] = []

    for entry in CATALOGUE:
        prices: dict[str, Any] = {}
        for region in regions:
            resolved = resolve(entry, region)
            if resolved is None:
                missing.append(f"{entry.sku} in {region}")
                continue
            prices[region] = resolved
        status = ", ".join(
            f"{region}={_describe(prices[region])}" for region in regions if region in prices
        )
        print(
            f"  {entry.sku:28} {entry.unit:14} {status or 'NOT SOLD IN ANY REGION'}",
            file=sys.stderr,
        )
        if not prices:
            continue
        skus.append(
            {
                "sku": entry.sku,
                "applies_to": entry.applies_to,
                "family": entry.family,
                "unit": entry.unit,
                **({"vcpu": entry.vcpu} if entry.vcpu is not None else {}),
                **({"memory_gb": entry.memory_gb} if entry.memory_gb is not None else {}),
                **({"term_months": entry.term_months} if entry.term_months is not None else {}),
                "prices": prices,
            }
        )

    if missing:
        print(f"\nnot sold in some regions ({len(missing)}): {', '.join(missing)}", file=sys.stderr)
    if not skus:
        msg = "no SKU resolved in any region; refusing to write an empty catalogue"
        raise RefreshError(msg)
    if verify_only:
        return {}

    return {
        "schema_version": SCHEMA_VERSION,
        "is_real_api_pull": True,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source": "Azure Retail Prices API",
        "provenance_note": (
            "Fetched from the public Azure Retail Prices API by "
            "scripts/refresh_prices.py. Every price names the meter it came from, so "
            "any figure can be checked against Azure by hand. Pay-as-you-go Linux "
            "consumption rates only: Spot, Low Priority, Windows, DevTest and "
            "reservation meters are excluded. Hourly rates are multiplied by a "
            "730-hour month. Per-GB rates are stored as rates, because turning one "
            "into a per-resource monthly price needs a capacity, and the capacity "
            "belongs to the resource rather than to the price."
        ),
        "api_endpoint": API_ENDPOINT,
        "api_version": API_VERSION,
        "currency": CURRENCY,
        "hours_per_month": HOURS_PER_MONTH,
        "regions": list(regions),
        "skus": skus,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Returns:
        ``0`` on success, ``2`` if the refresh failed. Nothing is written on failure.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help=f"Region to fetch. Repeat for more. Default: {', '.join(DEFAULT_REGIONS)}.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Resolve every selector and report, but build nothing.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and build, but do not write.")
    parser.add_argument(
        "--out", type=Path, default=CATALOGUE_PATH, help=f"Default: {CATALOGUE_PATH}"
    )
    args = parser.parse_args(argv)

    regions: Sequence[str] = tuple(args.regions or DEFAULT_REGIONS)
    print(
        f"Resolving {len(CATALOGUE)} catalogue SKU(s) across {', '.join(regions)} "
        f"from the public Azure Retail Prices API.\n",
        file=sys.stderr,
    )
    try:
        snapshot = build_snapshot(regions, verify_only=args.verify)
    except RefreshError as exc:
        print(f"\nrefresh_prices: {exc}", file=sys.stderr)
        print("refresh_prices: nothing was written.", file=sys.stderr)
        return 2

    if args.verify:
        print("\nVerify only: every selector resolved. Nothing built.", file=sys.stderr)
        return 0

    rendered = json.dumps(snapshot, indent=2) + "\n"
    if args.dry_run:
        print(
            f"\nDry run: {len(snapshot['skus'])} SKUs, {len(rendered)} bytes. Not written.",
            file=sys.stderr,
        )
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(
        f"\nWrote {len(snapshot['skus'])} SKUs to {args.out}.\n"
        f"Review the diff before committing: savings assertions in the tests are "
        f"baselined against these figures and will need re-baselining.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
