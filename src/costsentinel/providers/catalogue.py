"""The committed price catalogue.

Every monetary figure CostSentinel reports traces back to this snapshot, which is a
file in the repository rather than a network call. That is what lets the whole
pipeline -- including the savings arithmetic and the eval harness -- run offline and
produce the same numbers on every machine.

``scripts/refresh_prices.py`` writes the snapshot from the public Azure Retail Prices
API. It is run by hand, never by CI and never by a test, so a price change is a
reviewable commit rather than a surprise in a test run.

Prices are stored **per region, fetched directly**. There is no multiplier model: a
SKU not sold in a region is simply absent for that region, and a resource there is
priced as unknown rather than estimated from somewhere else.

Every price carries the Azure meter it came from, so any figure can be checked by
hand. :func:`catalogue_provenance` reports ``VERIFIED`` for a real API pull and
``UNVERIFIED`` otherwise -- a cost tool should not imply a figure was confirmed
against Azure when it was not.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from costsentinel.domain.common import (
    Currency,
    Frozen,
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import MeterReference, PriceUnit, ResourceKind, SkuPrice

#: The committed snapshot, resolved relative to this module so it travels with the
#: installed package rather than depending on the working directory.
CATALOGUE_PATH = Path(__file__).parent / "data" / "price_catalogue.json"

#: The only schema this loader understands. A snapshot from an older refresh is
#: rejected rather than partially read, because a half-understood price table is
#: worse than no price table.
SUPPORTED_SCHEMA_VERSION = 2


class CatalogueError(RuntimeError):
    """The price catalogue is missing, unreadable or malformed."""


class PriceCatalogue(Frozen):
    """A loaded, validated price snapshot."""

    schema_version: int
    is_real_api_pull: bool
    retrieved_at: str
    source: str
    currency: Currency
    regions: tuple[str, ...]
    hours_per_month: int
    entries: tuple[SkuPrice, ...]

    def for_region(self, region: str) -> tuple[SkuPrice, ...]:
        """Every SKU priced for one region.

        A SKU the catalogue has no price for in that region is absent, not
        zero-priced -- so a rightsizing search there simply has fewer candidates
        rather than a free one.
        """
        return tuple(entry for entry in self.entries if entry.region == region)

    def price_for(self, sku: str, kind: ResourceKind, region: str) -> SkuPrice | None:
        """One priced SKU, matched on name, resource kind and region.

        Matching on the kind as well as the name is what stops a SQL tier being
        offered as a candidate for a virtual machine, since both are priced per vCPU.
        """
        return next(
            (
                entry
                for entry in self.entries
                if entry.sku == sku and entry.applies_to is kind and entry.region == region
            ),
            None,
        )

    def reserved_price_for(self, sku: str, term_months: int, region: str) -> SkuPrice | None:
        """The reserved-instance price for one SKU, term and region.

        Reserved entries are named ``<sku>_RI<term>M`` by the refresh script, so a
        reservation's amortised cost comes from the real reserved meter rather than
        from a figure somebody made up.
        """
        return self.price_for(f"{sku}_RI{term_months}M", ResourceKind.VIRTUAL_MACHINE, region)


def catalogue_provenance(
    catalogue: PriceCatalogue,
    *,
    reference_suffix: str = "",
    meter: MeterReference | None = None,
) -> Provenance:
    """Provenance for a figure taken from the catalogue.

    A real API pull is ``VERIFIED``. Anything else is ``UNVERIFIED``: the figure is
    traceable, but nobody has confirmed it against Azure.
    """
    parts = [
        f"price_catalogue.json schema {catalogue.schema_version}",
        f"retrieved_at {catalogue.retrieved_at}",
        "live API pull" if catalogue.is_real_api_pull else "curated snapshot",
    ]
    if meter is not None:
        parts.append(meter.describe())
    if reference_suffix:
        parts.append(reference_suffix)
    return Provenance(
        source=ProvenanceSource.AZURE_RETAIL_PRICES,
        # The snapshot's own timestamp, not the moment this process loaded it: a
        # price was retrieved when it was fetched from Azure, and dating it to load
        # time would both misreport it and make the catalogue non-deterministic.
        retrieved_at=_parse_retrieved_at(catalogue.retrieved_at),
        reference="; ".join(parts),
        verification=(
            Verification.VERIFIED if catalogue.is_real_api_pull else Verification.UNVERIFIED
        ),
    )


def _parse_retrieved_at(raw: str) -> datetime:
    """Parse the snapshot timestamp, falling back to the epoch if it is unusable.

    A malformed timestamp must not break pricing: the figures are still usable and
    the fallback is visibly wrong rather than silently plausible.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        msg = f"{CATALOGUE_PATH.name} is missing required key {key!r}"
        raise CatalogueError(msg)
    return payload[key]


def _meter_from(raw: dict[str, Any]) -> MeterReference:
    return MeterReference(
        meter_id=str(raw["meter_id"]),
        meter_name=str(raw["meter_name"]),
        product_name=str(raw["product_name"]),
        sku_name=str(raw["sku_name"]),
        unit_of_measure=str(raw["unit_of_measure"]),
        unit_price=Decimal(str(raw["unit_price"])),
        effective_start_date=str(raw.get("effective_start_date", "")),
    )


def load_catalogue(path: Path | None = None) -> PriceCatalogue:
    """Load and validate the price snapshot.

    Raises:
        CatalogueError: If the file is absent, unparseable, of an unsupported schema
            version, or missing a required field. A malformed catalogue is a loud
            failure rather than a silent fallback to zero prices, because
            silently-zero prices would make every saving look like nothing.
    """
    target = path or CATALOGUE_PATH
    try:
        payload: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"price catalogue not found at {target}"
        raise CatalogueError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"price catalogue at {target} is not valid JSON: {exc}"
        raise CatalogueError(msg) from exc

    schema_version = int(_require(payload, "schema_version"))
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        msg = (
            f"price catalogue at {target} is schema version {schema_version}; this "
            f"build understands version {SUPPORTED_SCHEMA_VERSION} only. Re-run "
            f"scripts/refresh_prices.py."
        )
        raise CatalogueError(msg)

    currency = Currency(_require(payload, "currency"))
    stub = PriceCatalogue(
        schema_version=schema_version,
        is_real_api_pull=bool(_require(payload, "is_real_api_pull")),
        retrieved_at=str(_require(payload, "retrieved_at")),
        source=str(_require(payload, "source")),
        currency=currency,
        regions=tuple(str(region) for region in _require(payload, "regions")),
        hours_per_month=int(_require(payload, "hours_per_month")),
        entries=(),
    )

    raw_entries: Sequence[dict[str, Any]] = _require(payload, "skus")
    if not raw_entries:
        msg = f"price catalogue at {target} contains no SKUs"
        raise CatalogueError(msg)

    entries: list[SkuPrice] = []
    for raw in raw_entries:
        try:
            entries.extend(_entries_from(raw, stub, currency))
        except (KeyError, ValueError, ArithmeticError) as exc:
            msg = f"malformed SKU entry in {target}: {raw.get('sku', raw)!r} ({exc})"
            raise CatalogueError(msg) from exc

    return stub.model_copy(update={"entries": tuple(entries)})


def _entries_from(
    raw: dict[str, Any], catalogue: PriceCatalogue, currency: Currency
) -> list[SkuPrice]:
    """One :class:`SkuPrice` per region the SKU is priced in."""
    unit = PriceUnit(raw["unit"])
    kind = ResourceKind(raw["applies_to"])
    built: list[SkuPrice] = []

    for region, priced in dict(raw["prices"]).items():
        meter = _meter_from(priced)
        provenance = catalogue_provenance(
            catalogue, reference_suffix=f"region={region}", meter=meter
        )
        monthly_raw = priced.get("monthly_cost")
        monthly = (
            MoneyAmount.of(Decimal(str(monthly_raw)), currency=currency, provenance=provenance)
            if monthly_raw is not None
            else MoneyAmount.undetermined(
                f"{raw['sku']} is billed per {meter.unit_of_measure}; a monthly figure "
                f"needs a capacity from the provider",
                currency=currency,
            )
        )
        built.append(
            SkuPrice(
                sku=str(raw["sku"]),
                applies_to=kind,
                family=str(raw["family"]),
                region=str(region),
                unit=unit,
                meter=meter,
                rate_provenance=provenance,
                term_months=int(raw["term_months"]) if raw.get("term_months") else None,
                vcpu=int(raw["vcpu"]) if raw.get("vcpu") is not None else None,
                memory_gb=(
                    Decimal(str(raw["memory_gb"])) if raw.get("memory_gb") is not None else None
                ),
                monthly_cost=monthly,
            )
        )
    return built


@lru_cache(maxsize=1)
def default_catalogue() -> PriceCatalogue:
    """The committed catalogue, loaded once per process."""
    return load_catalogue()
