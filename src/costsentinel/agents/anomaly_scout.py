"""Anomaly Scout: read the estate, then run the deterministic detectors.

This is the only node that talks to a provider for discovery. Everything downstream
reasons over the single :class:`~costsentinel.domain.estate.Estate` snapshot it puts
in state, so the whole sweep sees one consistent view of the world rather than
re-querying and racing against changes.

Phase 3 adds statistical cost-anomaly detection over the cost series this node
already retrieves.
"""

from __future__ import annotations

from datetime import datetime

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.detectors import run_detectors
from costsentinel.config import Settings
from costsentinel.domain.common import utc_now
from costsentinel.domain.estate import (
    CostSeries,
    Estate,
    Resource,
    ResourceMetrics,
    Subscription,
)
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.signals import WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.observability.logging import get_logger
from costsentinel.providers.base import AzureProvider

ACTOR = "anomaly_scout"

#: Trailing window for the cost series.
WINDOW_DAYS = 30

_log = get_logger("agents.anomaly_scout")


def _as_of(subscriptions: tuple[Subscription, ...]) -> datetime:
    """The observation anchor, taken from the provider's own timestamps.

    Using the provider's ``retrieved_at`` rather than the wall clock keeps a mock
    run reproducible and keeps a real run's age calculations consistent with the
    data they were computed from.
    """
    if not subscriptions:
        return utc_now()
    return max(sub.provenance.retrieved_at for sub in subscriptions)


def make_anomaly_scout(*, provider: AzureProvider, settings: Settings) -> Node:
    """Build the Anomaly Scout node."""
    _ = settings

    def anomaly_scout(state: ScanState) -> NodeUpdate:
        """Read one client's estate and emit the waste signals found in it."""
        subscriptions = tuple(provider.list_subscriptions(client=state.client))

        resources: list[Resource] = []
        cost_series: list[CostSeries] = []
        metrics: list[ResourceMetrics] = []

        for subscription in subscriptions:
            subscription_resources = tuple(provider.list_resources(subscription.subscription_id))
            resources.extend(subscription_resources)
            cost_series.append(
                provider.get_cost_series(subscription.subscription_id, window_days=WINDOW_DAYS)
            )
            for resource in subscription_resources:
                measured = provider.get_resource_metrics(resource.resource_id)
                if measured is not None:
                    metrics.append(measured)

        estate = Estate(
            client=state.client,
            retrieved_at=_as_of(subscriptions),
            subscriptions=subscriptions,
            resources=tuple(resources),
            cost_series=tuple(cost_series),
            metrics=tuple(metrics),
        )

        signals: list[WasteSignal] = []
        for resource in estate.resources:
            signals.extend(
                run_detectors(
                    resource,
                    estate.metrics_for(resource.resource_id),
                    client=state.client,
                    as_of=estate.retrieved_at,
                )
            )

        # Deterministic ordering, so a checkpoint resume and a re-run agree.
        signals.sort(key=lambda s: (s.subscription_id, s.resource_id, s.kind.value))

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.ESTATE_READ,
                subject=state.client,
                detail={
                    "provider": provider.name,
                    "subscriptions": str(len(subscriptions)),
                    "resources": str(len(resources)),
                    "cost_series": str(len(cost_series)),
                    "metrics": str(len(metrics)),
                    "window_days": str(WINDOW_DAYS),
                },
            )
        ]
        events += [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.SIGNAL_DETECTED,
                subject=signal.signal_id,
                detail={
                    "kind": signal.kind.value,
                    "resource": signal.resource_name,
                    "resource_id": signal.resource_id,
                    "subscription_id": signal.subscription_id,
                    "detector": signal.detector,
                    "monthly_cost": signal.monthly_cost.display(),
                    "confidence": str(signal.confidence),
                },
                offset=offset,
            )
            for offset, signal in enumerate(signals, start=1)
        ]

        _log.info(
            "estate scanned",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "provider": provider.name,
                "subscriptions": len(subscriptions),
                "resources": len(resources),
                "signals": len(signals),
            },
        )

        return {
            "estate": estate,
            "signals": tuple(signals),
            "audit": with_audit(state, events),
        }

    return anomaly_scout
