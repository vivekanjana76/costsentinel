"""Savings arithmetic. No language model is imported here, by design.

ARCHITECTURE.md D17 makes this a separate module precisely so the property is
checkable in one glance at the imports: a savings figure in CostSentinel is either
arithmetic over provider data or explicitly unknown. There is no third option, and
no code path by which a model could supply one.

Phase 3 promotes this module to its own graph node (the Savings Estimator). The
function boundary is already the node boundary, so that is a move, not a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.common import MoneyAmount, Provenance
from costsentinel.domain.estate import Resource, ResourceMetrics, SkuPrice
from costsentinel.domain.recommendations import ActionType, SavingsEstimate

_CENTS = Decimal("0.01")
_PCT = Decimal("100")


def choose_rightsize_target(
    resource: Resource,
    metrics: ResourceMetrics | None,
    prices: Sequence[SkuPrice],
    *,
    headroom_factor: Decimal,
) -> tuple[SkuPrice, SkuPrice] | None:
    """Pick a smaller SKU that still fits the observed peak, with headroom.

    The candidate is chosen arithmetically from the provider's catalogue rather than
    suggested by a model:

    1. required vCPU = current vCPU x observed peak CPU% x ``headroom_factor``;
    2. candidates are SKUs in the *same family* with at least that many vCPU and a
       strictly lower price;
    3. the smallest such candidate wins.

    Staying in-family keeps the memory-to-vCPU ratio, so the recommendation does not
    silently change the machine's character. ``headroom_factor`` is an explicit,
    configurable policy parameter -- not a derived quantity.

    Returns:
        ``(current_price, target_price)``, or ``None`` when the current SKU is not
        in the catalogue, utilisation was not measured, or nothing smaller fits.
    """
    if resource.sku is None or metrics is None or metrics.cpu_max_pct is None:
        return None

    current = next((p for p in prices if p.sku == resource.sku), None)
    if current is None or current.monthly_cost.amount is None:
        return None

    required_vcpu = Decimal(current.vcpu) * (metrics.cpu_max_pct / _PCT) * headroom_factor

    candidates = [
        price
        for price in prices
        if price.family == current.family
        and price.sku != current.sku
        and Decimal(price.vcpu) >= required_vcpu
        and price.monthly_cost.amount is not None
        and price.monthly_cost.amount < current.monthly_cost.amount
    ]
    if not candidates:
        return None

    target = min(candidates, key=lambda p: (p.vcpu, p.sku))
    return current, target


def _full_cost_saving(resource: Resource, *, what: str) -> SavingsEstimate:
    """The resource's whole monthly charge stops. Observed, not estimated."""
    cost = resource.monthly_cost
    if cost.amount is None:
        return SavingsEstimate.undetermined(
            f"{resource.name}: {what}, but its monthly cost is unknown, so the saving "
            f"cannot be quantified."
        )
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(
            cost.amount,
            currency=cost.currency,
            provenance=Provenance.calculated(
                f"full monthly cost of {resource.name} "
                f"({cost.display()}) ceases when {what}; "
                f"source: {cost.provenance.reference or cost.provenance.source.value}"
            ),
        ),
        basis=(
            f"{what.capitalize()} removes the whole {cost.display()} monthly charge "
            f"for {resource.name}. Taken from the provider's reported cost for this "
            f"resource, not modelled."
        ),
        is_estimated=False,
    )


def _resize_saving(
    resource: Resource,
    metrics: ResourceMetrics | None,
    prices: Sequence[SkuPrice],
    *,
    headroom_factor: Decimal,
) -> SavingsEstimate:
    """The price difference between the current SKU and a catalogue candidate."""
    chosen = choose_rightsize_target(resource, metrics, prices, headroom_factor=headroom_factor)
    if chosen is None:
        return SavingsEstimate.undetermined(
            f"{resource.name}: no smaller same-family SKU in the provider catalogue "
            f"fits the observed peak with a {headroom_factor}x headroom, so no "
            f"resize saving can be quantified."
        )

    current, target = chosen
    current_amount = current.monthly_cost.amount
    target_amount = target.monthly_cost.amount
    if current_amount is None or target_amount is None:  # pragma: no cover -- guarded above
        return SavingsEstimate.undetermined(
            f"{resource.name}: the catalogue has no price for one of {current.sku} / {target.sku}."
        )

    delta = (current_amount - target_amount).quantize(_CENTS, rounding=ROUND_HALF_UP)
    peak = metrics.cpu_max_pct if metrics and metrics.cpu_max_pct is not None else None
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(
            delta,
            currency=current.monthly_cost.currency,
            provenance=Provenance.calculated(
                f"{current.sku} ({current.monthly_cost.display()}) - "
                f"{target.sku} ({target.monthly_cost.display()})"
            ),
        ),
        basis=(
            f"Resizing {resource.name} from {current.sku} ({current.vcpu} vCPU, "
            f"{current.monthly_cost.display()}/month) to {target.sku} "
            f"({target.vcpu} vCPU, {target.monthly_cost.display()}/month) saves the "
            f"difference. Both prices are provider catalogue prices for "
            f"{current.region}. The target is the smallest same-family SKU that "
            f"still covers the observed peak of "
            f"{peak if peak is not None else 'unknown'}% CPU with a "
            f"{headroom_factor}x headroom."
        ),
        is_estimated=True,
    )


def estimate_savings(
    action: ActionType,
    resource: Resource,
    metrics: ResourceMetrics | None,
    prices: Sequence[SkuPrice],
    *,
    headroom_factor: Decimal,
) -> SavingsEstimate:
    """The monthly and annual saving from one action on one resource.

    An action with no savings model yields an explicit unknown rather than zero. A
    zero would quietly claim "this is worth nothing", which is a different and
    unfounded assertion.
    """
    match action:
        case ActionType.DELETE_ORPHANED_MANAGED_DISK:
            return _full_cost_saving(resource, what="deleting the orphaned disk")
        case ActionType.DELETE_UNATTACHED_PUBLIC_IP:
            return _full_cost_saving(resource, what="releasing the unattached address")
        case ActionType.DEALLOCATE_VIRTUAL_MACHINE:
            return _full_cost_saving(
                resource,
                what=(
                    "deallocating the virtual machine, whose compute charge then "
                    "ceases (attached disks bill separately and are unaffected)"
                ),
            )
        case ActionType.RESIZE_VIRTUAL_MACHINE:
            return _resize_saving(resource, metrics, prices, headroom_factor=headroom_factor)
        case _:
            return SavingsEstimate.undetermined(
                f"No savings model for action {action.value}; the figure is unknown "
                f"rather than assumed to be zero."
            )


def total_savings(estimates: Sequence[SavingsEstimate]) -> tuple[MoneyAmount, MoneyAmount, int]:
    """Sum the known savings, and report how many were unknown.

    Unknowns are counted and surfaced rather than treated as zero, so a report can
    say "plus N findings we could not price" instead of silently understating.
    """
    known = [e for e in estimates if e.monthly.amount is not None]
    unknown_count = len(estimates) - len(known)

    if not known:
        reason = (
            "no finding could be priced from provider data"
            if estimates
            else "no findings were produced"
        )
        return (
            MoneyAmount.undetermined(reason),
            MoneyAmount.undetermined(reason),
            unknown_count,
        )

    currency = known[0].monthly.currency
    monthly_total = sum(
        (e.monthly.amount for e in known if e.monthly.amount is not None),
        start=Decimal(0),
    )
    annual_total = sum(
        (e.annual.amount for e in known if e.annual.amount is not None),
        start=Decimal(0),
    )
    basis = f"sum of {len(known)} priced finding(s)"
    return (
        MoneyAmount.of(
            monthly_total.quantize(_CENTS, rounding=ROUND_HALF_UP),
            currency=currency,
            provenance=Provenance.calculated(f"{basis}, monthly"),
        ),
        MoneyAmount.of(
            annual_total.quantize(_CENTS, rounding=ROUND_HALF_UP),
            currency=currency,
            provenance=Provenance.calculated(f"{basis}, annual"),
        ),
        unknown_count,
    )
