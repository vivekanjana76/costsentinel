"""Savings Estimator: price every remediation the policy store permits.

Its own node, and deliberately the dullest one in the graph. It imports
:mod:`costsentinel.agents.savings`, which imports no language model, so the path
from a reported saving back to provider data contains nothing that could have
invented it. A test asserts the absent import rather than trusting the convention.

It runs *before* the Optimization Planner and prices every candidate action for
every signal, not just a chosen one. Two reasons:

* the planner then chooses a remediation knowing what each option is worth, which
  is how a human would decide;
* every monetary figure in the system is produced inside this one node, so the
  no-invented-numbers guarantee has a single place to hold rather than several.

The node also resolves the resource-versus-reservation distinction once, into a
:class:`~costsentinel.agents.savings.SavingsTarget`, so no savings function has to
care which it is looking at.
"""

from __future__ import annotations

from collections.abc import Sequence

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.savings import SavingsTarget, estimate_savings
from costsentinel.config import Settings
from costsentinel.domain.estate import Estate, SkuPrice
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.recommendations import PricedOption, SavingsEstimate
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.observability.logging import get_logger
from costsentinel.providers.base import AzureProvider

ACTOR = "savings_estimator"

_log = get_logger("agents.savings_estimator")


def target_for(estate: Estate, resource_id: str) -> SavingsTarget | None:
    """Resolve what an action would apply to: a resource or a reservation.

    Returns ``None`` when the signal points at something no longer in the estate
    snapshot, which the caller reports rather than pricing at zero.
    """
    resource = estate.resource(resource_id)
    if resource is not None:
        return SavingsTarget.from_resource(resource)
    reservation = estate.reservation(resource_id)
    if reservation is not None:
        return SavingsTarget.from_reservation(reservation)
    return None


def make_savings_estimator(
    *,
    provider: AzureProvider,
    settings: Settings,
    policy: PolicyStore | None = None,
) -> Node:
    """Build the Savings Estimator node."""
    store = policy or PolicyStore()

    def savings_estimator(state: ScanState) -> NodeUpdate:
        """Price every permitted remediation for every signal."""
        if state.estate is None or not state.signals:
            return {
                "audit": with_audit(
                    state,
                    [
                        audit(
                            state,
                            actor=ACTOR,
                            event_type=AuditEventType.RECOMMENDATION_PROPOSED,
                            subject=state.client,
                            detail={"priced_options": "0", "reason": "nothing to price"},
                        )
                    ],
                )
            }

        estate = state.estate
        errors: list[str] = list(state.errors)
        price_cache: dict[str, Sequence[SkuPrice]] = {}
        options: list[PricedOption] = []

        for signal in state.signals:
            target = target_for(estate, signal.resource_id)
            candidates = store.candidate_actions(signal.kind)

            if target is None:
                errors.append(
                    f"savings_estimator: signal {signal.signal_id} targets "
                    f"{signal.resource_id}, which is not in the estate snapshot; its "
                    f"options are reported as unpriced."
                )
                options.extend(
                    PricedOption(
                        signal_id=signal.signal_id,
                        action=action,
                        savings=SavingsEstimate.undetermined(
                            f"{signal.resource_name} is not in the estate snapshot, so "
                            f"no saving can be computed."
                        ),
                    )
                    for action in candidates
                )
                continue

            if target.region not in price_cache:
                price_cache[target.region] = provider.list_sku_prices(target.region)

            metrics = estate.metrics_for(target.identifier)
            for action in candidates:
                options.append(
                    PricedOption(
                        signal_id=signal.signal_id,
                        action=action,
                        savings=estimate_savings(
                            action,
                            target,
                            metrics,
                            price_cache[target.region],
                            headroom_factor=settings.rightsize_headroom_factor,
                        ),
                    )
                )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.RECOMMENDATION_PROPOSED,
                subject=option.signal_id,
                detail={
                    "priced_option": option.action.value,
                    "monthly_saving": option.savings.monthly.display(),
                    "annual_saving": option.savings.annual.display(),
                    "is_estimated": str(option.savings.is_estimated).lower(),
                    "provenance": option.savings.monthly.provenance.source.value,
                },
                offset=offset,
            )
            for offset, option in enumerate(options)
        ]

        priced = sum(1 for option in options if option.is_priced)
        _log.info(
            "options priced",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "signals": len(state.signals),
                "options": len(options),
                "priced": priced,
                "unpriced": len(options) - priced,
            },
        )

        return {
            "priced_options": tuple(options),
            "errors": tuple(errors),
            "audit": with_audit(state, events),
        }

    return savings_estimator
