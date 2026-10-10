"""Deterministic waste detectors.

Detection is arithmetic over provider facts, never model reasoning
(ARCHITECTURE.md D1). Three consequences follow, and all three are the point:

* a finding cannot be hallucinated, because no model is involved;
* precision and recall are measurable, because the rules are fixed;
* an unknown metric yields *no* signal rather than a guess -- ``None`` means
  "cannot conclude", never "idle".

Each detector is a pure function of one resource and its metrics, so each is
independently testable without a graph, a provider or a model.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.estate import (
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
)
from costsentinel.domain.signals import Observation, WasteKind, WasteSignal

# --- thresholds ------------------------------------------------------------
# Explicit, documented policy constants. They are not derived quantities, and they
# are the knobs the Phase 5 evals calibrate against the labelled datasets.

#: At or below this peak CPU, a running VM is considered idle.
IDLE_CPU_MAX_PCT = Decimal("5.0")

#: Above this average CPU, a VM is considered appropriately sized.
OVERSIZED_CPU_AVG_PCT = Decimal("20.0")

#: Above this peak CPU, a VM is considered appropriately sized.
OVERSIZED_CPU_MAX_PCT = Decimal("40.0")

#: Minimum observation window before a utilisation-based conclusion is drawn.
MIN_OBSERVATION_DAYS = 7

#: Beyond this age, a snapshot is past any plausible restore window and is treated
#: as retained by neglect rather than by policy.
STALE_SNAPSHOT_AGE_DAYS = 180

#: A database with no connections at all over the window is idle. Zero is used
#: rather than a small threshold because a single connection is evidence of a
#: consumer, and the question this detector asks is whether anything uses it.
IDLE_SQL_MAX_CONNECTIONS = 0

#: At or below this utilisation, an App Service plan carries capacity it never uses.
OVERSIZED_PLAN_UTILISATION_PCT = Decimal("25.0")

#: Below this utilisation, a reservation pays for commitment nobody consumes.
UNUSED_RESERVATION_UTILISATION_PCT = Decimal("60.0")

# --- confidences -----------------------------------------------------------
# An unattached disk is a near-certain orphan: the provider states the attachment
# directly. A rightsizing conclusion depends on the observation window being
# representative, so it is scored lower.
_CONFIDENCE_ORPHANED_DISK = Decimal("0.95")
_CONFIDENCE_UNATTACHED_IP = Decimal("0.95")
_CONFIDENCE_IDLE_VM = Decimal("0.90")
_CONFIDENCE_OVERSIZED_VM = Decimal("0.75")
#: A snapshot age is a provider fact, so detection is certain; what is less certain
#: is whether anyone still wants it, which is what the preconditions cover.
_CONFIDENCE_STALE_SNAPSHOT = Decimal("0.85")
#: Zero connections is unambiguous, but a quarterly job could still need the
#: database, so this is not scored as highly as a structural orphan.
_CONFIDENCE_IDLE_SQL = Decimal("0.80")
_CONFIDENCE_OVERSIZED_PLAN = Decimal("0.70")
#: Reservation utilisation comes straight from the billing system.
_CONFIDENCE_UNUSED_RESERVATION = Decimal("0.92")

_KIND_SLUG: dict[WasteKind, str] = {
    WasteKind.ORPHANED_MANAGED_DISK: "orphdisk",
    WasteKind.UNATTACHED_PUBLIC_IP: "freepip",
    WasteKind.IDLE_VIRTUAL_MACHINE: "idlevm",
    WasteKind.OVERSIZED_VIRTUAL_MACHINE: "bigvm",
    WasteKind.STALE_SNAPSHOT: "oldsnap",
    WasteKind.IDLE_SQL_DATABASE: "idlesql",
    WasteKind.OVERSIZED_APP_SERVICE_PLAN: "bigplan",
    WasteKind.UNUSED_RESERVATION: "deadrsv",
}


def signal_id_for(kind: WasteKind, resource_id: str) -> str:
    """A stable, deterministic signal id.

    Derived with SHA-256 rather than :func:`hash`, whose string hashing is salted
    per process -- an id that changed between runs would break both checkpoint
    resume and episodic memory lookup.
    """
    digest = hashlib.sha256(f"{kind.value}:{resource_id}".encode()).hexdigest()[:10]
    return f"ws-{_KIND_SLUG.get(kind, 'waste')}-{digest}"


def _detector_provenance(detector: str, resource: Resource) -> Provenance:
    return Provenance(
        source=ProvenanceSource.CALCULATION,
        retrieved_at=resource.provenance.retrieved_at,
        reference=f"detector:{detector} over {resource.provenance.source.value}",
        verification=Verification.VERIFIED,
    )


def _observation(name: str, value: str, source: Provenance) -> Observation:
    return Observation(name=name, value=value, provenance=source)


def _age_days(resource: Resource, as_of: datetime) -> int | None:
    if resource.created_at is None:
        return None
    return (as_of - resource.created_at).days


def _signal(
    *,
    kind: WasteKind,
    resource: Resource,
    client: str,
    detector: str,
    confidence: Decimal,
    evidence: Sequence[Observation],
) -> WasteSignal:
    return WasteSignal(
        signal_id=signal_id_for(kind, resource.resource_id),
        kind=kind,
        client=client,
        subscription_id=resource.subscription_id,
        resource_id=resource.resource_id,
        resource_name=resource.name,
        resource_kind=resource.kind.value,
        evidence=tuple(evidence),
        monthly_cost=resource.monthly_cost,
        confidence=confidence,
        detector=detector,
        detected_at=resource.provenance.retrieved_at,
        provenance=_detector_provenance(detector, resource),
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def detect_orphaned_managed_disk(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A managed disk the provider reports as attached to nothing.

    High confidence: the attachment is a provider fact, not an inference. The
    remediation is still gated, because "unattached today" does not prove "safe to
    delete" -- that is what the preconditions are for.
    """
    _ = metrics
    if resource.kind is not ResourceKind.MANAGED_DISK:
        return None
    if resource.attached_to is not None or resource.state is not ResourceState.UNATTACHED:
        return None

    source = resource.provenance
    evidence = [
        _observation("state", resource.state.value, source),
        _observation("attached_to", "none", source),
        _observation("sku", resource.sku or "unknown", source),
        _observation("monthly_cost", resource.monthly_cost.display(), source),
    ]
    age = _age_days(resource, as_of)
    if age is not None:
        evidence.append(_observation("age_days", str(age), source))

    return _signal(
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        resource=resource,
        client=client,
        detector="orphaned_managed_disk/v1",
        confidence=_CONFIDENCE_ORPHANED_DISK,
        evidence=evidence,
    )


def detect_unattached_public_ip(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A reserved public IP associated with nothing.

    A static address bills whether or not anything answers on it, so an unattached
    one is pure waste -- small per address, and routinely numerous.
    """
    _ = metrics
    if resource.kind is not ResourceKind.PUBLIC_IP:
        return None
    if resource.attached_to is not None or resource.state is not ResourceState.UNATTACHED:
        return None

    source = resource.provenance
    evidence = [
        _observation("state", resource.state.value, source),
        _observation("associated_with", "none", source),
        _observation("sku", resource.sku or "unknown", source),
        _observation("monthly_cost", resource.monthly_cost.display(), source),
    ]
    age = _age_days(resource, as_of)
    if age is not None:
        evidence.append(_observation("age_days", str(age), source))

    return _signal(
        kind=WasteKind.UNATTACHED_PUBLIC_IP,
        resource=resource,
        client=client,
        detector="unattached_public_ip/v1",
        confidence=_CONFIDENCE_UNATTACHED_IP,
        evidence=evidence,
    )


def detect_idle_virtual_machine(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A running VM whose *peak* CPU never left the floor.

    Peak rather than average on purpose: a machine averaging 3% because it is busy
    one hour a day is not idle, and deallocating it would break something.
    """
    _ = as_of
    if resource.kind is not ResourceKind.VIRTUAL_MACHINE:
        return None
    if resource.state is not ResourceState.RUNNING:
        return None
    if metrics is None or metrics.cpu_max_pct is None:
        return None
    if metrics.observation_days < MIN_OBSERVATION_DAYS:
        return None
    if metrics.cpu_max_pct > IDLE_CPU_MAX_PCT:
        return None

    source = metrics.provenance
    evidence = [
        _observation("cpu_max_pct", str(metrics.cpu_max_pct), source),
        _observation("idle_threshold_pct", str(IDLE_CPU_MAX_PCT), source),
        _observation("observation_days", str(metrics.observation_days), source),
        _observation("state", resource.state.value, resource.provenance),
        _observation("sku", resource.sku or "unknown", resource.provenance),
        _observation("monthly_cost", resource.monthly_cost.display(), resource.provenance),
    ]
    if metrics.cpu_avg_pct is not None:
        evidence.append(_observation("cpu_avg_pct", str(metrics.cpu_avg_pct), source))
    if metrics.network_in_gb is not None:
        evidence.append(_observation("network_in_gb", str(metrics.network_in_gb), source))

    return _signal(
        kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        resource=resource,
        client=client,
        detector="idle_virtual_machine/v1",
        confidence=_CONFIDENCE_IDLE_VM,
        evidence=evidence,
    )


def detect_oversized_virtual_machine(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A running VM in use, but on far more capacity than it ever consumes.

    Requires *both* average and peak to be low, so a spiky workload is not
    mistakenly shrunk. Yields nothing for a machine that is already idle: the idle
    detector's remediation -- deallocate, saving the whole charge -- strictly
    dominates a resize, and two signals for one resource would double-count the
    saving.
    """
    _ = as_of
    if resource.kind is not ResourceKind.VIRTUAL_MACHINE:
        return None
    if resource.state is not ResourceState.RUNNING:
        return None
    if metrics is None or metrics.cpu_avg_pct is None or metrics.cpu_max_pct is None:
        return None
    if metrics.observation_days < MIN_OBSERVATION_DAYS:
        return None

    already_idle = metrics.cpu_max_pct <= IDLE_CPU_MAX_PCT
    busy_on_average = metrics.cpu_avg_pct >= OVERSIZED_CPU_AVG_PCT
    peaks_high = metrics.cpu_max_pct >= OVERSIZED_CPU_MAX_PCT
    if already_idle or busy_on_average or peaks_high:
        return None

    source = metrics.provenance
    evidence = [
        _observation("cpu_avg_pct", str(metrics.cpu_avg_pct), source),
        _observation("cpu_max_pct", str(metrics.cpu_max_pct), source),
        _observation("avg_threshold_pct", str(OVERSIZED_CPU_AVG_PCT), source),
        _observation("peak_threshold_pct", str(OVERSIZED_CPU_MAX_PCT), source),
        _observation("observation_days", str(metrics.observation_days), source),
        _observation("sku", resource.sku or "unknown", resource.provenance),
        _observation("monthly_cost", resource.monthly_cost.display(), resource.provenance),
    ]

    return _signal(
        kind=WasteKind.OVERSIZED_VIRTUAL_MACHINE,
        resource=resource,
        client=client,
        detector="oversized_virtual_machine/v1",
        confidence=_CONFIDENCE_OVERSIZED_VM,
        evidence=evidence,
    )


def detect_stale_snapshot(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A snapshot retained far past any plausible restore window.

    Age is a provider fact, so the detection itself is certain. What is uncertain is
    whether anyone still wants the snapshot, which is why the remediation carries
    preconditions about images, restore points and backup policies rather than
    treating age alone as permission to delete.

    A snapshot with no creation time yields nothing: with no age there is no
    conclusion to draw.
    """
    _ = metrics
    if resource.kind is not ResourceKind.SNAPSHOT:
        return None
    age = _age_days(resource, as_of)
    if age is None or age <= STALE_SNAPSHOT_AGE_DAYS:
        return None

    source = resource.provenance
    evidence = [
        _observation("age_days", str(age), source),
        _observation("stale_threshold_days", str(STALE_SNAPSHOT_AGE_DAYS), source),
        _observation("sku", resource.sku or "unknown", source),
        _observation("monthly_cost", resource.monthly_cost.display(), source),
    ]
    return _signal(
        kind=WasteKind.STALE_SNAPSHOT,
        resource=resource,
        client=client,
        detector="stale_snapshot/v1",
        confidence=_CONFIDENCE_STALE_SNAPSHOT,
        evidence=evidence,
    )


def detect_idle_sql_database(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A database nothing connected to over the whole observation window.

    Keyed on connection count rather than CPU, because a database can burn CPU on
    internal maintenance while serving nobody, and can serve a trickle of vital
    queries at near-zero CPU. Whether anything connected is the question that
    matters.

    A ``connection_count`` of ``None`` means connections were not measured, which
    yields nothing -- not idle.
    """
    _ = as_of
    if resource.kind is not ResourceKind.SQL_DATABASE:
        return None
    if metrics is None or metrics.connection_count is None:
        return None
    if metrics.observation_days < MIN_OBSERVATION_DAYS:
        return None
    if metrics.connection_count > IDLE_SQL_MAX_CONNECTIONS:
        return None

    source = metrics.provenance
    evidence = [
        _observation("connection_count", str(metrics.connection_count), source),
        _observation("connection_threshold", str(IDLE_SQL_MAX_CONNECTIONS), source),
        _observation("observation_days", str(metrics.observation_days), source),
        _observation("sku", resource.sku or "unknown", resource.provenance),
        _observation("monthly_cost", resource.monthly_cost.display(), resource.provenance),
    ]
    if metrics.cpu_avg_pct is not None:
        evidence.append(_observation("cpu_avg_pct", str(metrics.cpu_avg_pct), source))
    return _signal(
        kind=WasteKind.IDLE_SQL_DATABASE,
        resource=resource,
        client=client,
        detector="idle_sql_database/v1",
        confidence=_CONFIDENCE_IDLE_SQL,
        evidence=evidence,
    )


def detect_oversized_app_service_plan(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """An App Service plan carrying capacity its apps never use."""
    _ = as_of
    if resource.kind is not ResourceKind.APP_SERVICE_PLAN:
        return None
    if metrics is None or metrics.utilisation_pct is None:
        return None
    if metrics.observation_days < MIN_OBSERVATION_DAYS:
        return None
    if metrics.utilisation_pct > OVERSIZED_PLAN_UTILISATION_PCT:
        return None

    source = metrics.provenance
    evidence = [
        _observation("utilisation_pct", str(metrics.utilisation_pct), source),
        _observation("utilisation_threshold_pct", str(OVERSIZED_PLAN_UTILISATION_PCT), source),
        _observation("observation_days", str(metrics.observation_days), source),
        _observation("sku", resource.sku or "unknown", resource.provenance),
        _observation("monthly_cost", resource.monthly_cost.display(), resource.provenance),
    ]
    if metrics.cpu_max_pct is not None:
        evidence.append(_observation("cpu_max_pct", str(metrics.cpu_max_pct), source))
    return _signal(
        kind=WasteKind.OVERSIZED_APP_SERVICE_PLAN,
        resource=resource,
        client=client,
        detector="oversized_app_service_plan/v1",
        confidence=_CONFIDENCE_OVERSIZED_PLAN,
        evidence=evidence,
    )


def detect_unused_reservation(
    reservation: Reservation,
    *,
    client: str,
    as_of: datetime,
) -> WasteSignal | None:
    """A capacity commitment being paid for and not consumed.

    This waste is unlike the rest: the money is already committed, so the figure is
    not a saving waiting to be taken but a loss already being incurred. Recovering
    any of it means exchanging or re-scoping the reservation, never deleting
    something -- which is why its only forceful action is an exchange.

    Takes a :class:`Reservation` rather than a :class:`Resource`, so it has its own
    signature and sits outside the per-resource detector sweep.
    """
    _ = as_of
    if reservation.utilisation_pct is None:
        return None
    if reservation.utilisation_pct >= UNUSED_RESERVATION_UTILISATION_PCT:
        return None

    source = reservation.provenance
    evidence = [
        _observation("utilisation_pct", str(reservation.utilisation_pct), source),
        _observation("utilisation_threshold_pct", str(UNUSED_RESERVATION_UTILISATION_PCT), source),
        _observation("reserved_sku", reservation.reserved_sku, source),
        _observation("quantity", str(reservation.quantity), source),
        _observation("term_months", str(reservation.term_months), source),
        _observation(
            "monthly_amortised_cost", reservation.monthly_amortised_cost.display(), source
        ),
    ]
    if reservation.expires_on is not None:
        evidence.append(_observation("expires_on", reservation.expires_on.isoformat(), source))

    detector = "unused_reservation/v1"
    return WasteSignal(
        signal_id=signal_id_for(WasteKind.UNUSED_RESERVATION, reservation.reservation_id),
        kind=WasteKind.UNUSED_RESERVATION,
        client=client,
        subscription_id=reservation.scope_subscription_id or "shared-scope",
        resource_id=reservation.reservation_id,
        resource_name=reservation.name,
        resource_kind="reservation",
        evidence=tuple(evidence),
        monthly_cost=reservation.monthly_amortised_cost,
        confidence=_CONFIDENCE_UNUSED_RESERVATION,
        detector=detector,
        detected_at=reservation.provenance.retrieved_at,
        provenance=Provenance(
            source=ProvenanceSource.CALCULATION,
            retrieved_at=reservation.provenance.retrieved_at,
            reference=f"detector:{detector} over {reservation.provenance.source.value}",
            verification=Verification.VERIFIED,
        ),
    )


#: Signature every detector satisfies.
Detector = Callable[..., "WasteSignal | None"]

#: The per-resource detector set. The Phase 5 evals score it.
#:
#: Reservation detection is deliberately absent: a reservation is not a resource, so
#: :func:`detect_unused_reservation` has its own signature and the Anomaly Scout runs
#: it separately.
DETECTORS: tuple[Detector, ...] = (
    detect_orphaned_managed_disk,
    detect_unattached_public_ip,
    detect_idle_virtual_machine,
    detect_oversized_virtual_machine,
    detect_stale_snapshot,
    detect_idle_sql_database,
    detect_oversized_app_service_plan,
)


def run_detectors(
    resource: Resource,
    metrics: ResourceMetrics | None,
    *,
    client: str,
    as_of: datetime,
) -> tuple[WasteSignal, ...]:
    """Run every per-resource detector and collect what fired."""
    found = (detector(resource, metrics, client=client, as_of=as_of) for detector in DETECTORS)
    return tuple(signal for signal in found if signal is not None)


def run_reservation_detectors(
    reservations: Sequence[Reservation],
    *,
    client: str,
    as_of: datetime,
) -> tuple[WasteSignal, ...]:
    """Run the reservation detectors over a client's commitments."""
    found = (
        detect_unused_reservation(reservation, client=client, as_of=as_of)
        for reservation in reservations
    )
    return tuple(signal for signal in found if signal is not None)
