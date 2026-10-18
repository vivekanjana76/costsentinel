"""Anomaly Scout: read the estate, then run the deterministic detectors.

This is the only node that talks to a provider for discovery. Everything downstream
reasons over the single :class:`~costsentinel.domain.estate.Estate` snapshot it puts
in state, so the whole sweep sees one consistent view of the world rather than
re-querying and racing against changes.

Reservations are fetched alongside resources and run through their own detector,
because a commitment is not a resource and its waste is money already spent rather
than a charge that can simply be stopped.

Detection and *exemption* both happen here, in that order. The detectors are not
told about tags, so they keep answering the question they are good at -- is this
resource's utilisation consistent with waste -- and a separate deterministic pass
asks whether the client has said the resource is like that on purpose. Keeping the
two apart is what lets a disaster-recovery standby be reported as idle *and* left
alone, rather than quietly not reported at all.

The node finally earns its name in Phase 7: it runs the statistical spend-anomaly
detector over the cost series it already retrieves. Anomalies are a **separate
stream** from the waste signals, and nothing downstream merges them -- an anomaly says
"spend changed, investigate" and a waste signal says "this resource is wasteful, act",
and only the second has a remediation path (ARCHITECTURE.md D106).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from costsentinel.agents.anomalies import assess_estate
from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.detectors import run_detectors, run_reservation_detectors
from costsentinel.config import Settings
from costsentinel.domain.common import utc_now
from costsentinel.domain.estate import (
    CostSeries,
    Estate,
    Reservation,
    Resource,
    ResourceMetrics,
    Subscription,
)
from costsentinel.domain.exemption import ExemptionVerdict
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.signals import CostAnomaly, WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.exemption import ExemptionPolicy, get_exemption_policy
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


def _exempt(
    signals: Sequence[WasteSignal],
    estate: Estate,
    policy: ExemptionPolicy,
    *,
    client: str,
) -> tuple[ExemptionVerdict, ...]:
    """Verdicts for the signalled resources the client's tags protect.

    Only signalled resources are examined. Exemption exists to withhold a
    recommendation, so asking the question about a resource nothing was detected on
    would be work with no answer to give -- and an estate's worth of "we looked and
    found nothing" carried in every checkpoint for no reason.

    A reservation never appears here: a commitment carries no tags, so there is
    nothing to read and nothing to exempt from. That is a statement about the Azure
    API surface rather than a policy choice.
    """
    verdicts: list[ExemptionVerdict] = []
    seen: set[str] = set()
    for signal in signals:
        if signal.resource_id in seen:
            continue
        seen.add(signal.resource_id)
        resource = estate.resource(signal.resource_id)
        if resource is None:
            continue
        verdict = policy.for_resource(resource, client=client)
        if verdict.is_exempt:
            verdicts.append(verdict)
    return tuple(verdicts)


def make_anomaly_scout(
    *,
    provider: AzureProvider,
    settings: Settings,
    exemptions: ExemptionPolicy | None = None,
) -> Node:
    """Build the Anomaly Scout node."""
    exemption_policy = exemptions or get_exemption_policy(settings)

    def anomaly_scout(state: ScanState) -> NodeUpdate:
        """Read one client's estate and emit the waste signals found in it."""
        subscriptions = tuple(provider.list_subscriptions(client=state.client))

        reservations: tuple[Reservation, ...] = tuple(
            provider.list_reservations(client=state.client)
        )

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
            reservations=reservations,
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

        signals.extend(
            run_reservation_detectors(
                estate.reservations, client=state.client, as_of=estate.retrieved_at
            )
        )

        # Deterministic ordering, so a checkpoint resume and a re-run agree.
        signals.sort(key=lambda s: (s.subscription_id, s.resource_id, s.kind.value))

        exempt = _exempt(signals, estate, exemption_policy, client=state.client)
        signalled_resources = len({signal.resource_id for signal in signals})

        assessments = assess_estate(estate.cost_series, client=state.client)
        anomalies: tuple[CostAnomaly, ...] = tuple(
            anomaly for assessment in assessments for anomaly in assessment.anomalies
        )
        unassessed = tuple(a for a in assessments if not a.verdict.is_conclusive)

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
                    "reservations": str(len(reservations)),
                    "window_days": str(WINDOW_DAYS),
                    "exemption_ruleset": exemption_policy.name,
                    "exemptions_checked": str(signalled_resources),
                    "exemptions_applied": str(len(exempt)),
                    "spend_scopes_assessed": str(len(assessments)),
                    "spend_scopes_unassessed": str(len(unassessed)),
                    "anomalies": str(len(anomalies)),
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
        # One event per exemption, so "why was this not recommended?" is answerable
        # from the trail alone rather than by re-running the sweep.
        events += [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.EXEMPTION_APPLIED,
                subject=verdict.resource_id,
                detail={
                    "resource": verdict.resource_name,
                    "reason": verdict.reason.value,
                    "rule": verdict.rule_reference,
                    "matched_tag": verdict.matched_tag,
                    "effect": (
                        "surfaced as a finding; no remediation proposed and excluded "
                        "from the actionable savings total"
                    ),
                },
                offset=len(signals) + 1 + offset,
            )
            for offset, verdict in enumerate(exempt)
        ]
        # One event per spend assessment, conclusive or not. An inconclusive scope is
        # the interesting case: it means a client's series had gaps, and a trail that
        # only recorded findings would make that indistinguishable from a clean month.
        events += [
            audit(
                state,
                actor=ACTOR,
                event_type=(
                    AuditEventType.ANOMALY_DETECTED
                    if assessment.anomalies
                    else AuditEventType.SPEND_ASSESSED
                ),
                subject=assessment.scope,
                detail={
                    "verdict": assessment.verdict.value,
                    "days_observed": str(assessment.days_observed),
                    "days_missing": str(assessment.days_missing),
                    "anomalies": str(len(assessment.anomalies)),
                    "kinds": ",".join(a.kind.value for a in assessment.anomalies) or "none",
                    "detail": assessment.detail,
                    "routes_to": "investigation and the report; never an action",
                },
                offset=len(signals) + len(exempt) + 1 + offset,
            )
            for offset, assessment in enumerate(assessments)
        ]

        _log.info(
            "estate scanned",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "provider": provider.name,
                "subscriptions": len(subscriptions),
                "resources": len(resources),
                "reservations": len(reservations),
                "signals": len(signals),
                "exemptions": len(exempt),
                "exemption_ruleset": exemption_policy.name,
                "anomalies": len(anomalies),
                "spend_scopes_unassessed": len(unassessed),
            },
        )

        return {
            "estate": estate,
            "signals": tuple(signals),
            "exemptions": exempt,
            "anomalies": anomalies,
            "anomaly_assessments": assessments,
            "audit": with_audit(state, events),
        }

    return anomaly_scout
