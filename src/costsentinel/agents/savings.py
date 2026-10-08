"""Savings arithmetic. No language model is imported here, by design.

ARCHITECTURE.md D17 makes this a separate module precisely so the property is
checkable in one glance at the imports: a savings figure in CostSentinel is either
arithmetic over provider data or explicitly unknown. There is no third option, and no
code path by which a model could supply one. A test asserts the absence of the import
rather than trusting the convention.

:mod:`costsentinel.agents.savings_estimator` wraps these functions as the Savings
Estimator graph node.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from costsentinel.domain.common import Frozen, MoneyAmount, Percentage, Provenance
from costsentinel.domain.estate import (
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    SkuPrice,
)
from costsentinel.domain.recommendations import ActionType, SavingsEstimate

_CENTS = Decimal("0.01")
_PCT = Decimal("100")


class SavingsTarget(Frozen):
    """What an action would be applied to, reduced to the facts savings need.

    An indirection over :class:`Resource` and :class:`Reservation`, because the two
    are genuinely different things that both cost money. Without it, the savings
    functions would have to special-case reservations, or reservations would have to
    be forced into the shape of a resource -- and a reservation has a term, a
    quantity and a utilisation against a commitment, which a resource does not.
    """

    identifier: str
    name: str
    kind: ResourceKind | None = None
    kind_label: str
    region: str
    sku: str | None = None
    monthly_cost: MoneyAmount
    utilisation_pct: Percentage | None = None

    @classmethod
    def from_resource(cls, resource: Resource) -> SavingsTarget:
        """Build a target from a billable resource."""
        return cls(
            identifier=resource.resource_id,
            name=resource.name,
            kind=resource.kind,
            kind_label=resource.kind.value,
            region=resource.region,
            sku=resource.sku,
            monthly_cost=resource.monthly_cost,
        )

    @classmethod
    def from_reservation(cls, reservation: Reservation) -> SavingsTarget:
        """Build a target from a capacity commitment."""
        return cls(
            identifier=reservation.reservation_id,
            name=reservation.name,
            kind=None,
            kind_label="reservation",
            region=reservation.region,
            sku=reservation.reserved_sku,
            monthly_cost=reservation.monthly_amortised_cost,
            utilisation_pct=reservation.utilisation_pct,
        )


def peak_signal_for(kind: ResourceKind | None, metrics: ResourceMetrics | None) -> Decimal | None:
    """The utilisation percentage that governs sizing for this kind of resource.

    Different resources are sized against different signals: a virtual machine and a
    SQL database against peak CPU, an App Service plan against instance utilisation.
    Returning ``None`` means the governing signal was not measured, so no sizing
    conclusion can be drawn.
    """
    if metrics is None:
        return None
    if kind is ResourceKind.APP_SERVICE_PLAN:
        return metrics.utilisation_pct
    return metrics.cpu_max_pct


def choose_rightsize_target(
    *,
    sku: str | None,
    kind: ResourceKind | None,
    prices: Sequence[SkuPrice],
    observed_peak_pct: Decimal | None,
    headroom_factor: Decimal,
) -> tuple[SkuPrice, SkuPrice] | None:
    """Pick a smaller SKU that still fits the observed peak, with headroom.

    The candidate is chosen arithmetically from the provider catalogue rather than
    suggested by a model:

    1. required capacity = current vCPU x observed peak % x ``headroom_factor``;
    2. candidates are SKUs of the *same kind and family* with at least that capacity
       and a strictly lower price;
    3. the smallest such candidate wins.

    Matching on kind as well as family is what stops a SQL tier being offered as a
    candidate for a virtual machine, since both are priced per vCPU. Staying
    in-family keeps the memory-to-vCPU ratio, so the recommendation does not quietly
    change the machine's character. ``headroom_factor`` is an explicit, configurable
    policy parameter, not a derived quantity.

    Returns:
        ``(current_price, target_price)``, or ``None`` when the current SKU is not in
        the catalogue, the governing utilisation was not measured, the current SKU
        has no capacity to reason about, or nothing smaller fits.
    """
    if sku is None or observed_peak_pct is None:
        return None

    current = next(
        (p for p in prices if p.sku == sku and (kind is None or p.applies_to is kind)),
        None,
    )
    if current is None or current.monthly_cost.amount is None or current.vcpu is None:
        return None

    required = Decimal(current.vcpu) * (observed_peak_pct / _PCT) * headroom_factor

    candidates = [
        price
        for price in prices
        if price.applies_to is current.applies_to
        and price.family == current.family
        and price.sku != current.sku
        and price.vcpu is not None
        and Decimal(price.vcpu) >= required
        and price.monthly_cost.amount is not None
        and price.monthly_cost.amount < current.monthly_cost.amount
    ]
    if not candidates:
        return None

    target = min(candidates, key=lambda p: (p.vcpu or 0, p.sku))
    return current, target


def _full_cost_saving(target: SavingsTarget, *, what: str) -> SavingsEstimate:
    """The target's whole monthly charge stops. Observed, not estimated."""
    cost = target.monthly_cost
    if cost.amount is None:
        return SavingsEstimate.undetermined(
            f"{target.name}: {what}, but its monthly cost is unknown, so the saving "
            f"cannot be quantified."
        )
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(
            cost.amount,
            currency=cost.currency,
            provenance=Provenance.calculated(
                f"full monthly cost of {target.name} ({cost.display()}) ceases when "
                f"{what}; source: "
                f"{cost.provenance.reference or cost.provenance.source.value}"
            ),
        ),
        basis=(
            f"{what.capitalize()} removes the whole {cost.display()} monthly charge "
            f"for {target.name}. Taken from the provider's reported cost for this "
            f"{target.kind_label}, not modelled."
        ),
        is_estimated=False,
    )


def _rightsize_saving(
    target: SavingsTarget,
    metrics: ResourceMetrics | None,
    prices: Sequence[SkuPrice],
    *,
    headroom_factor: Decimal,
    verb: str,
) -> SavingsEstimate:
    """The price difference between the current SKU and a catalogue candidate."""
    peak = peak_signal_for(target.kind, metrics)
    chosen = choose_rightsize_target(
        sku=target.sku,
        kind=target.kind,
        prices=prices,
        observed_peak_pct=peak,
        headroom_factor=headroom_factor,
    )
    if chosen is None:
        return SavingsEstimate.undetermined(
            f"{target.name}: no smaller same-family SKU in the provider catalogue "
            f"fits the observed peak with a {headroom_factor}x headroom, so no "
            f"{verb} saving can be quantified."
        )

    current, candidate = chosen
    current_amount = current.monthly_cost.amount
    target_amount = candidate.monthly_cost.amount
    if current_amount is None or target_amount is None:  # pragma: no cover -- guarded above
        return SavingsEstimate.undetermined(
            f"{target.name}: the catalogue has no price for one of {current.sku} / {candidate.sku}."
        )

    delta = (current_amount - target_amount).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(
            delta,
            currency=current.monthly_cost.currency,
            provenance=Provenance.calculated(
                f"{current.sku} ({current.monthly_cost.display()}) - "
                f"{candidate.sku} ({candidate.monthly_cost.display()})"
            ),
        ),
        basis=(
            f"{verb.capitalize()} {target.name} from {current.sku} "
            f"({current.vcpu} vCPU, {current.monthly_cost.display()}/month) to "
            f"{candidate.sku} ({candidate.vcpu} vCPU, "
            f"{candidate.monthly_cost.display()}/month) saves the difference. Both "
            f"prices are provider catalogue prices for {current.region}. The target "
            f"is the smallest same-family SKU that still covers the observed peak of "
            f"{peak if peak is not None else 'unknown'}% with a "
            f"{headroom_factor}x headroom."
        ),
        is_estimated=True,
    )


def _reservation_waste(target: SavingsTarget) -> SavingsEstimate:
    """The unconsumed share of a commitment that is already being paid for.

    Deliberately framed as waste already being incurred rather than a saving waiting
    to be collected: the money is committed, and recovering any of it needs an
    exchange whose terms depend on the agreement. ``is_estimated`` is true for
    exactly that reason.
    """
    cost = target.monthly_cost
    utilisation = target.utilisation_pct
    if cost.amount is None or utilisation is None:
        return SavingsEstimate.undetermined(
            f"{target.name}: a reservation saving needs both an amortised cost and a "
            f"measured utilisation; one of them is unknown."
        )

    unused_fraction = (_PCT - utilisation) / _PCT
    wasted = (cost.amount * unused_fraction).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(
            wasted,
            currency=cost.currency,
            provenance=Provenance.calculated(
                f"{cost.display()} amortised monthly x ({_PCT} - {utilisation})% unused commitment"
            ),
        ),
        basis=(
            f"{target.name} is {utilisation}% utilised, so {unused_fraction * _PCT:.1f}% "
            f"of its {cost.display()} monthly amortised cost buys capacity nobody "
            f"consumes. That money is already committed: this figure is waste being "
            f"incurred now, and recovering it depends on the exchange terms in the "
            f"agreement, which is why it is marked estimated."
        ),
        is_estimated=True,
    )


def estimate_savings(
    action: ActionType,
    target: SavingsTarget,
    metrics: ResourceMetrics | None,
    prices: Sequence[SkuPrice],
    *,
    headroom_factor: Decimal,
) -> SavingsEstimate:
    """The monthly and annual saving from one action on one target.

    An action with no savings model yields an explicit unknown rather than zero. A
    zero would quietly claim "this is worth nothing", which is a different and
    unfounded assertion.
    """
    match action:
        case ActionType.DELETE_ORPHANED_MANAGED_DISK:
            return _full_cost_saving(target, what="deleting the orphaned disk")
        case ActionType.DELETE_UNATTACHED_PUBLIC_IP:
            return _full_cost_saving(target, what="releasing the unattached address")
        case ActionType.DELETE_STALE_SNAPSHOT:
            return _full_cost_saving(target, what="deleting the stale snapshot")
        case ActionType.DEALLOCATE_VIRTUAL_MACHINE:
            return _full_cost_saving(
                target,
                what=(
                    "deallocating the virtual machine, whose compute charge then "
                    "ceases (attached disks bill separately and are unaffected)"
                ),
            )
        case ActionType.RESIZE_VIRTUAL_MACHINE:
            return _rightsize_saving(
                target, metrics, prices, headroom_factor=headroom_factor, verb="resizing"
            )
        case ActionType.SCALE_DOWN_SQL_DATABASE:
            return _rightsize_saving(
                target, metrics, prices, headroom_factor=headroom_factor, verb="scaling down"
            )
        case ActionType.SCALE_DOWN_APP_SERVICE_PLAN:
            return _rightsize_saving(
                target, metrics, prices, headroom_factor=headroom_factor, verb="scaling down"
            )
        case ActionType.EXCHANGE_UNUSED_RESERVATION:
            return _reservation_waste(target)
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
