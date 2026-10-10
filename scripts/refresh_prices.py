#!/usr/bin/env python
"""Refresh the committed price catalogue from the public Azure Retail Prices API.

**Run this by hand. Never from CI, and never from a test.**

CostSentinel's savings figures are baselined against the committed snapshot at
``src/costsentinel/providers/data/price_catalogue.json``. That file is the single
source of every price the system reports, which is what lets tests and CI run
offline and produce identical numbers on every machine.

Running this script changes committed figures. Expect the savings assertions in
``tests/test_graph.py`` to need re-baselining afterwards, and review the diff before
committing it -- a silent price change would silently change what CostSentinel tells
a client they can save.

Usage::

    uv run python scripts/refresh_prices.py --dry-run
    uv run python scripts/refresh_prices.py
    uv run python scripts/refresh_prices.py --region eastus --region qatarcentral

The API is public and needs no authentication, but it does need network access --
which is precisely why this is not part of any automated run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_PATH = REPO_ROOT / "src" / "costsentinel" / "providers" / "data" / "price_catalogue.json"

API_ENDPOINT = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
HOURS_PER_MONTH = 730
USER_AGENT = "CostSentinel-price-refresh/0.2 (+https://github.com/vivekanjana76/costsentinel)"

#: Hard cap on pages, so a pagination bug cannot turn into an unbounded crawl.
MAX_PAGES = 40
REQUEST_TIMEOUT_SECONDS = 30
_CENTS = Decimal("0.01")

#: Which meters to ask for, per resource kind. Keeping the filters explicit here
#: rather than inferring them means a refresh fetches exactly the SKUs the catalogue
#: already prices, so the snapshot's shape does not drift between runs.
_FILTERS: dict[str, str] = {
    "virtual_machine": (
        "serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and type eq 'Consumption'"
    ),
    "managed_disk": "serviceName eq 'Storage' and productName eq 'Premium SSD Managed Disks'",
    "sql_database": "serviceName eq 'SQL Database' and priceType eq 'Consumption'",
    "app_service_plan": "serviceName eq 'Azure App Service' and priceType eq 'Consumption'",
    "public_ip": "serviceName eq 'Virtual Network' and priceType eq 'Consumption'",
    "snapshot": "serviceName eq 'Storage' and productName eq 'Standard Page Blob'",
    "storage_account": "serviceName eq 'Storage' and priceType eq 'Consumption'",
}


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


def fetch_meters(kind: str, region: str) -> list[dict[str, Any]]:
    """Fetch every price meter for one resource kind in one region."""
    query = urllib.parse.urlencode(
        {
            "api-version": API_VERSION,
            "currencyCode": "USD",
            "$filter": f"armRegionName eq '{region}' and {_FILTERS[kind]}",
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
    msg = f"pagination for {kind} in {region} exceeded {MAX_PAGES} pages; aborting"
    raise RefreshError(msg)


def monthly_from_meter(meter: dict[str, Any]) -> Decimal | None:
    """Convert a meter's unit price to a monthly figure, or ``None`` if not possible.

    Hourly meters are multiplied by a 730-hour month. Monthly meters pass through.
    Anything else -- per-GB, per-operation, per-10k -- cannot be turned into a
    per-resource monthly price without a usage assumption, so it is skipped rather
    than guessed.
    """
    try:
        unit_price = Decimal(str(meter["retailPrice"]))
    except (KeyError, ArithmeticError):
        return None
    unit = str(meter.get("unitOfMeasure", "")).lower()
    if "hour" in unit:
        return (unit_price * HOURS_PER_MONTH).quantize(_CENTS, rounding=ROUND_HALF_UP)
    if "month" in unit:
        return unit_price.quantize(_CENTS, rounding=ROUND_HALF_UP)
    return None


def build_entries(kinds: Iterable[str], region: str) -> list[dict[str, Any]]:
    """Fetch and normalise catalogue entries for the given kinds."""
    entries: list[dict[str, Any]] = []
    for kind in kinds:
        meters = fetch_meters(kind, region)
        print(f"  {kind:20s} {len(meters):4d} meters from {region}", file=sys.stderr)
        for meter in meters:
            monthly = monthly_from_meter(meter)
            sku = str(meter.get("armSkuName") or meter.get("skuName") or "").strip()
            if monthly is None or not sku or monthly <= 0:
                continue
            entries.append(
                {
                    "sku": sku,
                    "applies_to": kind,
                    "family": str(meter.get("productName", "")).strip() or kind,
                    "monthly_cost": f"{monthly}",
                    "meter_id": str(meter.get("meterId", "")),
                    "unit_of_measure": str(meter.get("unitOfMeasure", "")),
                }
            )
    # Deduplicate on (sku, kind), keeping the cheapest meter for each -- the API
    # returns several meters per SKU (spot, low-priority, OS variants) and the
    # cheapest consumption meter is the closest match to a pay-as-you-go Linux rate.
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (entry["sku"], entry["applies_to"])
        current = best.get(key)
        if current is None or Decimal(entry["monthly_cost"]) < Decimal(current["monthly_cost"]):
            best[key] = entry
    return sorted(best.values(), key=lambda e: (e["applies_to"], e["sku"]))


def build_snapshot(regions: Sequence[str], kinds: Sequence[str]) -> dict[str, Any]:
    """Build the full snapshot payload for the given regions."""
    base_region = regions[0]
    entries = build_entries(kinds, base_region)
    if not entries:
        msg = (
            f"no usable price entries were returned for {base_region}; refusing to "
            f"write an empty catalogue"
        )
        raise RefreshError(msg)

    region_block: dict[str, Any] = {
        base_region: {
            "multiplier": "1.00",
            "basis": "base region; SKU prices below are quoted for this region directly",
        }
    }
    for region in regions[1:]:
        extra = build_entries(kinds, region)
        ratio = _median_ratio(entries, extra)
        region_block[region] = {
            "multiplier": f"{ratio}",
            "basis": (
                f"median of {len(extra)} fetched {region} meters against the base "
                f"region, computed by scripts/refresh_prices.py"
            ),
        }

    return {
        "schema_version": 1,
        "is_real_api_pull": True,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source": f"Azure Retail Prices API, {len(entries)} SKUs for {base_region}",
        "provenance_note": (
            "Fetched from the public Azure Retail Prices API by "
            "scripts/refresh_prices.py. Hourly meters are multiplied by a 730-hour "
            "month; per-GB and per-operation meters are skipped because turning them "
            "into a per-resource monthly price would require a usage assumption."
        ),
        "api_endpoint": API_ENDPOINT,
        "api_version": API_VERSION,
        "currency": "USD",
        "hours_per_month": HOURS_PER_MONTH,
        "base_region": base_region,
        "regions": region_block,
        "skus": entries,
    }


def _median_ratio(base: list[dict[str, Any]], other: list[dict[str, Any]]) -> Decimal:
    """Median price ratio of a second region against the base, to 2 decimal places."""
    base_by_key = {(e["sku"], e["applies_to"]): Decimal(e["monthly_cost"]) for e in base}
    ratios = sorted(
        Decimal(e["monthly_cost"]) / base_by_key[(e["sku"], e["applies_to"])]
        for e in other
        if (e["sku"], e["applies_to"]) in base_by_key
        and base_by_key[(e["sku"], e["applies_to"])] > 0
    )
    if not ratios:
        msg = "no overlapping SKUs between regions; cannot derive a regional multiplier"
        raise RefreshError(msg)
    return ratios[len(ratios) // 2].quantize(_CENTS, rounding=ROUND_HALF_UP)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Returns:
        ``0`` on success, ``2`` if the refresh failed. Nothing is written on failure.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Region to fetch. Repeat for more; the first is the base region.",
    )
    parser.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        choices=sorted(_FILTERS),
        help="Resource kind to fetch. Repeat for more. Default: all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report, but do not write the snapshot.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=CATALOGUE_PATH,
        help=f"Snapshot path. Default: {CATALOGUE_PATH}",
    )
    args = parser.parse_args(argv)

    regions: Sequence[str] = args.regions or ["eastus", "qatarcentral"]
    kinds: Sequence[str] = args.kinds or sorted(_FILTERS)

    print(
        f"Fetching {len(kinds)} resource kind(s) for region(s) {', '.join(regions)} "
        f"from the public Azure Retail Prices API.",
        file=sys.stderr,
    )
    try:
        snapshot = build_snapshot(regions, kinds)
    except RefreshError as exc:
        print(f"refresh_prices: {exc}", file=sys.stderr)
        print("refresh_prices: nothing was written.", file=sys.stderr)
        return 2

    rendered = json.dumps(snapshot, indent=2, sort_keys=False) + "\n"
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
        f"Review the diff before committing: savings assertions in tests are "
        f"baselined against these figures and may need re-baselining.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
