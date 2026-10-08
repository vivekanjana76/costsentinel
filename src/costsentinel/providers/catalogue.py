"""The committed price catalogue.

Every monetary figure CostSentinel reports traces back to this snapshot, which is a
file in the repository rather than a network call. That is what lets the whole
pipeline -- including the savings arithmetic and the eval harness -- run offline and
produce the same numbers on every machine.

``scripts/refresh_prices.py`` replaces the snapshot from the public Azure Retail
Prices API. It is run by hand, never by CI and never by a test, so a price change is
a reviewable commit rather than a surprise in a test run.

The committed snapshot carries ``is_real_api_pull``. It is ``false`` for the curated
values shipped with Phase 2, which are plausible but were **not** fetched from Azure;
the refresh script sets it to ``true``. :func:`catalogue_provenance` reflects that
distinction, so a report built on curated figures says so rather than implying a
verified price.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
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
from costsentinel.domain.estate import ResourceKind, SkuPrice

#: The committed snapshot, resolved relative to this module so it travels with the
#: installed package rather than depending on the working directory.
CATALOGUE_PATH = Path(__file__).parent / "data" / "price_catalogue.json"

_CENTS = Decimal("0.01")


class CatalogueError(RuntimeError):
    """The price catalogue is missing, unreadable or malformed."""


class RegionPricing(Frozen):
    """How one region's prices relate to the base region."""

    region: str
    multiplier: Decimal
    basis: str


class PriceCatalogue(Frozen):
    """A loaded, validated price snapshot."""

    schema_version: int
    is_real_api_pull: bool
    retrieved_at: str
    source: str
    currency: Currency
    base_region: str
    regions: tuple[RegionPricing, ...]
    entries: tuple[SkuPrice, ...]

    def region_multiplier(self, region: str) -> Decimal:
        """Price multiplier for a region, defaulting to the base region's.

        An unlisted region falls back to 1.0 rather than raising: a resource in a
        region the snapshot has not been refreshed for still gets a defensible
        figure, and the ``basis`` string on each region records which are modelled
        rather than fetched.
        """
        match = next((r for r in self.regions if r.region == region), None)
        return match.multiplier if match else Decimal("1.00")

    def for_region(self, region: str) -> tuple[SkuPrice, ...]:
        """Every SKU priced for one region."""
        multiplier = self.region_multiplier(region)
        provenance = catalogue_provenance(self, reference_suffix=f"region={region}")
        return tuple(
            entry.model_copy(
                update={
                    "region": region,
                    "monthly_cost": _scaled(entry.monthly_cost, multiplier, provenance),
                }
            )
            for entry in self.entries
        )

    def price_for(self, sku: str, kind: ResourceKind, region: str) -> SkuPrice | None:
        """One priced SKU, matched on both the SKU name and the resource kind.

        Matching on the kind as well as the name is what stops a SQL tier being
        offered as a candidate for a virtual machine.
        """
        return next(
            (p for p in self.for_region(region) if p.sku == sku and p.applies_to is kind),
            None,
        )


def _scaled(base: MoneyAmount, multiplier: Decimal, provenance: Provenance) -> MoneyAmount:
    if base.amount is None:  # pragma: no cover -- catalogue entries are always priced
        return base
    return MoneyAmount.of(
        (base.amount * multiplier).quantize(_CENTS, rounding=ROUND_HALF_UP),
        currency=base.currency,
        provenance=provenance,
    )


def catalogue_provenance(catalogue: PriceCatalogue, *, reference_suffix: str = "") -> Provenance:
    """Provenance for a figure taken from the catalogue.

    A real API pull is ``VERIFIED``. The curated snapshot is ``UNVERIFIED`` -- the
    figure is defensible and traceable, but nobody has confirmed it against Azure,
    and a cost tool should not claim otherwise.
    """
    suffix = f" ({reference_suffix})" if reference_suffix else ""
    return Provenance(
        source=ProvenanceSource.AZURE_RETAIL_PRICES,
        # The snapshot's own timestamp, not the moment this process loaded it: a
        # price was retrieved when it was fetched from Azure, and dating it to load
        # time would both misreport it and make the catalogue non-deterministic.
        retrieved_at=_parse_retrieved_at(catalogue.retrieved_at),
        reference=(
            f"price_catalogue.json schema {catalogue.schema_version}, "
            f"retrieved_at {catalogue.retrieved_at}, "
            f"{'live API pull' if catalogue.is_real_api_pull else 'curated snapshot'}"
            f"{suffix}"
        ),
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


def load_catalogue(path: Path | None = None) -> PriceCatalogue:
    """Load and validate the price snapshot.

    Raises:
        CatalogueError: If the file is absent, unparseable, or missing a required
            field. A malformed catalogue is a loud failure rather than a silent
            fallback to zero prices, because silently-zero prices would make every
            saving look like nothing.
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

    base_region = str(_require(payload, "base_region"))
    currency = Currency(_require(payload, "currency"))
    is_real = bool(_require(payload, "is_real_api_pull"))
    retrieved_at = str(_require(payload, "retrieved_at"))
    schema_version = int(_require(payload, "schema_version"))

    stub = PriceCatalogue(
        schema_version=schema_version,
        is_real_api_pull=is_real,
        retrieved_at=retrieved_at,
        source=str(_require(payload, "source")),
        currency=currency,
        base_region=base_region,
        regions=(),
        entries=(),
    )
    provenance = catalogue_provenance(stub)

    regions = tuple(
        RegionPricing(
            region=name,
            multiplier=Decimal(str(spec["multiplier"])),
            basis=str(spec.get("basis", "")),
        )
        for name, spec in dict(_require(payload, "regions")).items()
    )

    raw_entries: Sequence[dict[str, Any]] = _require(payload, "skus")
    if not raw_entries:
        msg = f"price catalogue at {target} contains no SKUs"
        raise CatalogueError(msg)

    entries: list[SkuPrice] = []
    for raw in raw_entries:
        try:
            entries.append(
                SkuPrice(
                    sku=str(raw["sku"]),
                    applies_to=ResourceKind(raw["applies_to"]),
                    family=str(raw["family"]),
                    region=base_region,
                    vcpu=int(raw["vcpu"]) if raw.get("vcpu") is not None else None,
                    memory_gb=(
                        Decimal(str(raw["memory_gb"])) if raw.get("memory_gb") is not None else None
                    ),
                    monthly_cost=MoneyAmount.of(
                        Decimal(str(raw["monthly_cost"])),
                        currency=currency,
                        provenance=provenance,
                    ),
                )
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            msg = f"malformed SKU entry in {target}: {raw!r} ({exc})"
            raise CatalogueError(msg) from exc

    return stub.model_copy(update={"regions": regions, "entries": tuple(entries)})


@lru_cache(maxsize=1)
def default_catalogue() -> PriceCatalogue:
    """The committed catalogue, loaded once per process."""
    return load_catalogue()
