"""Detector tests: recall, precision, and the refusal to guess.

The third group matters most. A detector given an unmeasured resource must stay
silent rather than conclude "idle", because an unknown metric is not a zero one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from costsentinel.agents.detectors import (
    IDLE_CPU_MAX_PCT,
    MIN_OBSERVATION_DAYS,
    detect_idle_virtual_machine,
    detect_orphaned_managed_disk,
    detect_oversized_virtual_machine,
    detect_unattached_public_ip,
    run_detectors,
    signal_id_for,
)
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
)
from costsentinel.domain.signals import WasteKind

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER,
    retrieved_at=AS_OF,
    reference="test",
    verification=Verification.VERIFIED,
)


def _resource(
    *,
    name: str = "r1",
    kind: ResourceKind = ResourceKind.MANAGED_DISK,
    state: ResourceState = ResourceState.UNATTACHED,
    attached_to: str | None = None,
    sku: str | None = "Premium_LRS_P10",
    cost: str | None = "19.71",
    age_days: int = 100,
) -> Resource:
    return Resource(
        resource_id=f"/subscriptions/sub-1/resources/{name}",
        name=name,
        kind=kind,
        subscription_id="sub-1",
        resource_group="rg-1",
        region="eastus",
        sku=sku,
        state=state,
        attached_to=attached_to,
        created_at=AS_OF.replace(year=AS_OF.year) - (AS_OF - AS_OF) if age_days == 0 else None,
        monthly_cost=(
            MoneyAmount.of(Decimal(cost), provenance=PROVENANCE)
            if cost is not None
            else MoneyAmount.undetermined("no price")
        ),
        provenance=PROVENANCE,
    )


def _metrics(
    resource: Resource,
    *,
    cpu_avg: str | None = None,
    cpu_max: str | None = None,
    days: int = 30,
    network_in: str | None = None,
) -> ResourceMetrics:
    return ResourceMetrics(
        resource_id=resource.resource_id,
        observation_days=days,
        cpu_avg_pct=Decimal(cpu_avg) if cpu_avg is not None else None,
        cpu_max_pct=Decimal(cpu_max) if cpu_max is not None else None,
        network_in_gb=Decimal(network_in) if network_in is not None else None,
        provenance=PROVENANCE,
    )


# ---------------------------------------------------------------------------
# Signal ids
# ---------------------------------------------------------------------------


def test_signal_ids_are_stable_across_processes() -> None:
    """Derived with SHA-256, not hash(), whose string hashing is salted per process."""
    first = signal_id_for(WasteKind.ORPHANED_MANAGED_DISK, "/r/1")
    second = signal_id_for(WasteKind.ORPHANED_MANAGED_DISK, "/r/1")
    assert first == second
    assert first.startswith("ws-orphdisk-")


def test_signal_ids_differ_by_kind_and_resource() -> None:
    assert signal_id_for(WasteKind.ORPHANED_MANAGED_DISK, "/r/1") != signal_id_for(
        WasteKind.UNATTACHED_PUBLIC_IP, "/r/1"
    )
    assert signal_id_for(WasteKind.ORPHANED_MANAGED_DISK, "/r/1") != signal_id_for(
        WasteKind.ORPHANED_MANAGED_DISK, "/r/2"
    )


# ---------------------------------------------------------------------------
# Recall: the detectors fire on real waste
# ---------------------------------------------------------------------------


def test_orphaned_disk_is_detected() -> None:
    disk = _resource(name="disk-orphan")
    signal = detect_orphaned_managed_disk(disk, None, client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.kind is WasteKind.ORPHANED_MANAGED_DISK
    assert signal.resource_name == "disk-orphan"
    assert signal.confidence >= Decimal("0.9")
    assert signal.evidence_value("attached_to") == "none"
    assert signal.monthly_cost.amount == Decimal("19.71")
    assert signal.provenance.source is ProvenanceSource.CALCULATION


def test_unattached_public_ip_is_detected() -> None:
    ip = _resource(name="pip-free", kind=ResourceKind.PUBLIC_IP, cost="3.65")
    signal = detect_unattached_public_ip(ip, None, client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.kind is WasteKind.UNATTACHED_PUBLIC_IP
    assert signal.evidence_value("associated_with") == "none"


def test_idle_virtual_machine_is_detected() -> None:
    vm = _resource(
        name="vm-idle",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        attached_to=None,
        sku="Standard_D16s_v5",
        cost="560.64",
    )
    signal = detect_idle_virtual_machine(
        vm, _metrics(vm, cpu_avg="1.2", cpu_max="3.8", network_in="4.1"), client="acme", as_of=AS_OF
    )
    assert signal is not None
    assert signal.kind is WasteKind.IDLE_VIRTUAL_MACHINE
    assert signal.evidence_value("cpu_max_pct") == "3.8"
    assert signal.evidence_value("idle_threshold_pct") == str(IDLE_CPU_MAX_PCT)
    assert signal.evidence_value("network_in_gb") == "4.1"


def test_oversized_virtual_machine_is_detected() -> None:
    vm = _resource(
        name="vm-big",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_E16s_v5",
        cost="735.84",
    )
    signal = detect_oversized_virtual_machine(
        vm, _metrics(vm, cpu_avg="6.1", cpu_max="18.5"), client="acme", as_of=AS_OF
    )
    assert signal is not None
    assert signal.kind is WasteKind.OVERSIZED_VIRTUAL_MACHINE
    # Lower confidence than an orphan: it depends on the window being representative.
    assert signal.confidence < Decimal("0.9")


# ---------------------------------------------------------------------------
# Precision: the detectors stay quiet on healthy resources
# ---------------------------------------------------------------------------


def test_attached_disk_is_not_flagged() -> None:
    disk = _resource(
        name="disk-os", state=ResourceState.ATTACHED, attached_to="/subscriptions/sub-1/vm"
    )
    assert detect_orphaned_managed_disk(disk, None, client="acme", as_of=AS_OF) is None


def test_attached_public_ip_is_not_flagged() -> None:
    ip = _resource(
        name="pip-live",
        kind=ResourceKind.PUBLIC_IP,
        state=ResourceState.ATTACHED,
        attached_to="/subscriptions/sub-1/vm",
    )
    assert detect_unattached_public_ip(ip, None, client="acme", as_of=AS_OF) is None


def test_wrong_resource_kind_is_not_flagged() -> None:
    storage = _resource(
        name="st-1", kind=ResourceKind.STORAGE_ACCOUNT, state=ResourceState.AVAILABLE
    )
    assert detect_orphaned_managed_disk(storage, None, client="acme", as_of=AS_OF) is None
    assert detect_unattached_public_ip(storage, None, client="acme", as_of=AS_OF) is None
    assert detect_idle_virtual_machine(storage, None, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(storage, None, client="acme", as_of=AS_OF) is None


def test_busy_virtual_machine_is_not_flagged() -> None:
    vm = _resource(
        name="vm-busy",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        cost="140.16",
    )
    metrics = _metrics(vm, cpu_avg="41.8", cpu_max="78.3")
    assert detect_idle_virtual_machine(vm, metrics, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(vm, metrics, client="acme", as_of=AS_OF) is None


def test_spiky_workload_is_not_shrunk() -> None:
    """Low average but a high peak is a spiky workload, not an oversized one."""
    vm = _resource(
        name="vm-spiky",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D8s_v5",
        cost="280.32",
    )
    metrics = _metrics(vm, cpu_avg="4.0", cpu_max="95.0")
    assert detect_oversized_virtual_machine(vm, metrics, client="acme", as_of=AS_OF) is None


def test_stopped_virtual_machine_is_not_flagged_as_idle() -> None:
    """A deallocated machine is already not billing compute."""
    vm = _resource(
        name="vm-stopped",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.DEALLOCATED,
        sku="Standard_D4s_v5",
        cost="140.16",
    )
    metrics = _metrics(vm, cpu_avg="0", cpu_max="0")
    assert detect_idle_virtual_machine(vm, metrics, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(vm, metrics, client="acme", as_of=AS_OF) is None


def test_an_idle_machine_yields_only_the_idle_signal() -> None:
    """Two signals for one resource would double-count the saving."""
    vm = _resource(
        name="vm-idle",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D16s_v5",
        cost="560.64",
    )
    metrics = _metrics(vm, cpu_avg="1.2", cpu_max="3.8")
    signals = run_detectors(vm, metrics, client="acme", as_of=AS_OF)
    assert [s.kind for s in signals] == [WasteKind.IDLE_VIRTUAL_MACHINE]


# ---------------------------------------------------------------------------
# The refusal to guess
# ---------------------------------------------------------------------------


def test_unmeasured_machine_yields_no_utilisation_signal() -> None:
    """``None`` metrics mean "cannot conclude", never "idle"."""
    vm = _resource(
        name="vm-unmeasured",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        cost="140.16",
    )
    assert detect_idle_virtual_machine(vm, None, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(vm, None, client="acme", as_of=AS_OF) is None


def test_missing_cpu_metric_yields_no_signal() -> None:
    vm = _resource(
        name="vm-partial",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        cost="140.16",
    )
    only_network = _metrics(vm, network_in="100")
    assert detect_idle_virtual_machine(vm, only_network, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(vm, only_network, client="acme", as_of=AS_OF) is None

    # Oversizing needs both average and peak.
    peak_only = _metrics(vm, cpu_max="10")
    assert detect_oversized_virtual_machine(vm, peak_only, client="acme", as_of=AS_OF) is None


def test_short_observation_window_yields_no_signal() -> None:
    """A conclusion from two days of data is not a conclusion."""
    vm = _resource(
        name="vm-new",
        kind=ResourceKind.VIRTUAL_MACHINE,
        state=ResourceState.RUNNING,
        sku="Standard_D4s_v5",
        cost="140.16",
    )
    short = _metrics(vm, cpu_avg="1.0", cpu_max="2.0", days=MIN_OBSERVATION_DAYS - 1)
    assert detect_idle_virtual_machine(vm, short, client="acme", as_of=AS_OF) is None
    assert detect_oversized_virtual_machine(vm, short, client="acme", as_of=AS_OF) is None


def test_signal_cost_is_unknown_when_the_resource_cost_is_unknown() -> None:
    """An unpriced orphan is still reported, with an honest unknown cost."""
    disk = _resource(name="disk-unpriced", cost=None)
    signal = detect_orphaned_managed_disk(disk, None, client="acme", as_of=AS_OF)
    assert signal is not None
    assert not signal.monthly_cost.is_known
    assert signal.evidence_value("monthly_cost") == "unknown USD"


def test_run_detectors_returns_nothing_for_a_healthy_resource() -> None:
    healthy = _resource(
        name="st-1", kind=ResourceKind.STORAGE_ACCOUNT, state=ResourceState.AVAILABLE
    )
    assert run_detectors(healthy, None, client="acme", as_of=AS_OF) == ()


def test_age_evidence_is_included_when_creation_time_is_known() -> None:
    from datetime import timedelta

    disk = _resource(name="disk-aged").model_copy(
        update={"created_at": AS_OF - timedelta(days=241)}
    )
    signal = detect_orphaned_managed_disk(disk, None, client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.evidence_value("age_days") == "241"


def test_age_evidence_is_omitted_when_creation_time_is_unknown() -> None:
    disk = _resource(name="disk-no-age")
    assert disk.created_at is None
    signal = detect_orphaned_managed_disk(disk, None, client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.evidence_value("age_days") is None
