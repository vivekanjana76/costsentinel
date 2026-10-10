"""Mapping an Azure resource's reported SKU to a catalogue SKU.

Azure does not report the thing it bills you for. A managed disk reports
``Premium_LRS`` and a size; the *meter* is ``P15 LRS Disk``, and which tier that is
depends on the size. A public IP reports ``Standard`` and an allocation method; the
meter depends on both. A storage account reports a redundancy and an access tier
separately.

So the resource SKU and the catalogue SKU are different things, and something has to
translate. That translation is here, deterministic and tested, rather than scattered
through the provider -- and when it cannot translate, it says so, and the resource is
priced as unknown instead of being matched to whatever is closest.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from costsentinel.domain.estate import ResourceKind

#: Premium SSD tier boundaries: the largest size each tier covers, in GiB.
#: Azure bills a disk at the tier its *provisioned* size falls into, so a 200 GiB
#: disk is billed as P15 (256 GiB), not pro rata.
PREMIUM_SSD_TIERS: tuple[tuple[int, str], ...] = (
    (4, "P1"),
    (8, "P2"),
    (16, "P3"),
    (32, "P4"),
    (64, "P6"),
    (128, "P10"),
    (256, "P15"),
    (512, "P20"),
    (1024, "P30"),
    (2048, "P40"),
    (4096, "P50"),
    (8192, "P60"),
    (16384, "P70"),
    (32767, "P80"),
)

#: ARM resource types mapped to the kinds CostSentinel reasons about. Lower-cased,
#: because Resource Graph is inconsistent about case.
RESOURCE_KINDS: Mapping[str, ResourceKind] = {
    "microsoft.compute/virtualmachines": ResourceKind.VIRTUAL_MACHINE,
    "microsoft.compute/disks": ResourceKind.MANAGED_DISK,
    "microsoft.compute/snapshots": ResourceKind.SNAPSHOT,
    "microsoft.network/publicipaddresses": ResourceKind.PUBLIC_IP,
    "microsoft.network/networkinterfaces": ResourceKind.NETWORK_INTERFACE,
    "microsoft.storage/storageaccounts": ResourceKind.STORAGE_ACCOUNT,
    "microsoft.sql/servers/databases": ResourceKind.SQL_DATABASE,
    "microsoft.web/serverfarms": ResourceKind.APP_SERVICE_PLAN,
}


def resource_kind(resource_type: str) -> ResourceKind | None:
    """The kind CostSentinel reasons about, or ``None`` for a type it does not price.

    Returning ``None`` rather than a catch-all kind matters: an unrecognised resource
    type is skipped entirely rather than detected against rules written for something
    else.
    """
    return RESOURCE_KINDS.get(resource_type.lower())


def premium_disk_tier(size_gb: Decimal | None) -> str | None:
    """The Premium SSD tier a provisioned size is billed at.

    Returns ``None`` for an absent or over-large size, so the caller prices the disk
    as unknown rather than guessing a tier.
    """
    if size_gb is None or size_gb <= 0:
        return None
    for boundary, tier in PREMIUM_SSD_TIERS:
        if size_gb <= boundary:
            return tier
    return None


def catalogue_sku(
    *,
    kind: ResourceKind,
    sku_name: str | None,
    properties: Mapping[str, str],
    size_gb: Decimal | None,
) -> str | None:
    """Translate a resource's reported SKU into a catalogue SKU.

    Returns ``None`` when the translation is not possible, which the caller must
    treat as an unknown cost. Guessing here would produce a plausible figure for the
    wrong meter, which is worse than admitting ignorance.
    """
    match kind:
        case ResourceKind.VIRTUAL_MACHINE | ResourceKind.SQL_DATABASE:
            # Both report the SKU the catalogue is keyed on directly.
            return sku_name or None

        case ResourceKind.APP_SERVICE_PLAN:
            # Reported as "P1v3" or sometimes "P1 v3"; the catalogue uses no space.
            return sku_name.replace(" ", "") if sku_name else None

        case ResourceKind.MANAGED_DISK:
            if not sku_name or not sku_name.startswith("Premium_LRS"):
                # Standard HDD and SSD, and the zone-redundant variants, are priced
                # from different meters this catalogue does not carry yet.
                return None
            tier = premium_disk_tier(size_gb)
            return f"Premium_LRS_{tier}" if tier else None

        case ResourceKind.SNAPSHOT:
            # Snapshots are billed per GB from one meter per redundancy, so the
            # size is the quantity rather than part of the SKU.
            return "Premium_LRS_Snapshot" if sku_name == "Premium_LRS" else None

        case ResourceKind.PUBLIC_IP:
            allocation = properties.get("publicIPAllocationMethod", "")
            if sku_name == "Standard" and allocation.lower() == "static":
                return "Standard_Static"
            # A Basic or dynamic address bills from a different meter.
            return None

        case ResourceKind.STORAGE_ACCOUNT:
            tier = properties.get("accessTier", "")
            if not sku_name or not tier:
                return None
            candidate = f"{sku_name}_{tier}"
            # Only the combinations the catalogue actually prices.
            return candidate if candidate in {"Standard_LRS_Hot", "Standard_GRS_Cool"} else None

        case _:
            return None


def reservation_sku(reserved_sku: str) -> str | None:
    """The catalogue SKU a reservation's commitment is priced against."""
    return reserved_sku or None
