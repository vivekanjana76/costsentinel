"""Defensive paths in the Phase 2 nodes and detectors.

These are the branches that only fire when something is missing or inconsistent.
They are tested because that is exactly when they matter: a detector that concluded
"idle" from an unmeasured resource, or an estimator that priced a vanished resource
at zero, would be wrong in the direction a client notices.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from costsentinel.agents.detectors import (
    MIN_OBSERVATION_DAYS,
    detect_idle_sql_database,
    detect_oversized_app_service_plan,
    detect_stale_snapshot,
    detect_unused_reservation,
    run_reservation_detectors,
)
from costsentinel.agents.report_author import _resource_count, _waste_breakdown
from costsentinel.agents.savings_estimator import make_savings_estimator, target_for
from costsentinel.config import Settings
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    Estate,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
)
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.report import ReportFinding
from costsentinel.domain.signals import Observation, WasteKind, WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.providers.mock import MockAzureProvider

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER,
    retrieved_at=AS_OF,
    reference="test",
    verification=Verification.VERIFIED,
)


def _resource(
    *,
    name: str,
    kind: ResourceKind,
    sku: str | None = None,
    cost: str = "10.00",
    age_days: int | None = 400,
) -> Resource:
    return Resource(
        resource_id=f"/subscriptions/sub-1/resources/{name}",
        name=name,
        kind=kind,
        subscription_id="sub-1",
        resource_group="rg-1",
        region="eastus",
        sku=sku,
        state=ResourceState.AVAILABLE,
        created_at=AS_OF - timedelta(days=age_days) if age_days is not None else None,
        monthly_cost=MoneyAmount.of(Decimal(cost), provenance=PROVENANCE),
        provenance=PROVENANCE,
    )


def _metrics(
    resource: Resource,
    *,
    days: int = 30,
    cpu_avg: str | None = None,
    cpu_max: str | None = None,
    connections: int | None = None,
    utilisation: str | None = None,
) -> ResourceMetrics:
    return ResourceMetrics(
        resource_id=resource.resource_id,
        observation_days=days,
        cpu_avg_pct=Decimal(cpu_avg) if cpu_avg is not None else None,
        cpu_max_pct=Decimal(cpu_max) if cpu_max is not None else None,
        connection_count=connections,
        utilisation_pct=Decimal(utilisation) if utilisation is not None else None,
        provenance=PROVENANCE,
    )


def _reservation(
    *,
    utilisation: str | None = "34.5",
    expires: bool = True,
    cost: str = "420.48",
) -> Reservation:
    return Reservation(
        reservation_id="rsv-1",
        name="nw-compute",
        client="acme",
        scope_subscription_id="sub-1",
        reserved_sku="Standard_D4s_v5",
        reserved_kind=ResourceKind.VIRTUAL_MACHINE,
        region="eastus",
        term_months=36,
        quantity=4,
        monthly_amortised_cost=MoneyAmount.of(Decimal(cost), provenance=PROVENANCE),
        utilisation_pct=Decimal(utilisation) if utilisation is not None else None,
        expires_on=(AS_OF + timedelta(days=100)).date() if expires else None,
        provenance=PROVENANCE,
    )


# ---------------------------------------------------------------------------
# Stale snapshot
# ---------------------------------------------------------------------------


def test_a_snapshot_with_no_creation_time_yields_nothing() -> None:
    """With no age there is no conclusion to draw."""
    snapshot = _resource(name="snap-x", kind=ResourceKind.SNAPSHOT, age_days=None)
    assert detect_stale_snapshot(snapshot, None, client="acme", as_of=AS_OF) is None


def test_a_recent_snapshot_yields_nothing() -> None:
    snapshot = _resource(name="snap-new", kind=ResourceKind.SNAPSHOT, age_days=5)
    assert detect_stale_snapshot(snapshot, None, client="acme", as_of=AS_OF) is None


def test_a_non_snapshot_yields_nothing() -> None:
    disk = _resource(name="disk-x", kind=ResourceKind.MANAGED_DISK)
    assert detect_stale_snapshot(disk, None, client="acme", as_of=AS_OF) is None


# ---------------------------------------------------------------------------
# Idle SQL database
# ---------------------------------------------------------------------------


def test_an_unmeasured_database_is_not_idle() -> None:
    """``None`` connections means not measured, which is not the same as zero."""
    database = _resource(name="sql-x", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4")
    assert detect_idle_sql_database(database, None, client="acme", as_of=AS_OF) is None
    assert (
        detect_idle_sql_database(
            database, _metrics(database, cpu_avg="0"), client="acme", as_of=AS_OF
        )
        is None
    )


def test_a_connected_database_is_not_idle() -> None:
    database = _resource(name="sql-busy", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4")
    metrics = _metrics(database, connections=1)
    assert detect_idle_sql_database(database, metrics, client="acme", as_of=AS_OF) is None


def test_a_short_window_does_not_conclude_a_database_is_idle() -> None:
    database = _resource(name="sql-new", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4")
    metrics = _metrics(database, connections=0, days=MIN_OBSERVATION_DAYS - 1)
    assert detect_idle_sql_database(database, metrics, client="acme", as_of=AS_OF) is None


def test_an_idle_database_includes_cpu_evidence_when_measured() -> None:
    database = _resource(name="sql-idle", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4")
    signal = detect_idle_sql_database(
        database, _metrics(database, connections=0, cpu_avg="0.4"), client="acme", as_of=AS_OF
    )
    assert signal is not None
    assert signal.evidence_value("cpu_avg_pct") == "0.4"


def test_an_idle_database_without_cpu_evidence_still_fires() -> None:
    database = _resource(name="sql-idle", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4")
    signal = detect_idle_sql_database(
        database, _metrics(database, connections=0), client="acme", as_of=AS_OF
    )
    assert signal is not None
    assert signal.evidence_value("cpu_avg_pct") is None


def test_a_non_database_yields_no_idle_sql_signal() -> None:
    disk = _resource(name="disk-x", kind=ResourceKind.MANAGED_DISK)
    assert detect_idle_sql_database(disk, None, client="acme", as_of=AS_OF) is None


# ---------------------------------------------------------------------------
# Oversized App Service plan
# ---------------------------------------------------------------------------


def test_an_unmeasured_plan_is_not_oversized() -> None:
    plan = _resource(name="app-x", kind=ResourceKind.APP_SERVICE_PLAN, sku="P1v3")
    assert detect_oversized_app_service_plan(plan, None, client="acme", as_of=AS_OF) is None
    assert (
        detect_oversized_app_service_plan(
            plan, _metrics(plan, cpu_max="5"), client="acme", as_of=AS_OF
        )
        is None
    )


def test_a_busy_plan_is_not_oversized() -> None:
    plan = _resource(name="app-busy", kind=ResourceKind.APP_SERVICE_PLAN, sku="P1v3")
    metrics = _metrics(plan, utilisation="80")
    assert detect_oversized_app_service_plan(plan, metrics, client="acme", as_of=AS_OF) is None


def test_a_short_window_does_not_conclude_a_plan_is_oversized() -> None:
    plan = _resource(name="app-new", kind=ResourceKind.APP_SERVICE_PLAN, sku="P1v3")
    metrics = _metrics(plan, utilisation="5", days=MIN_OBSERVATION_DAYS - 1)
    assert detect_oversized_app_service_plan(plan, metrics, client="acme", as_of=AS_OF) is None


def test_an_oversized_plan_without_cpu_evidence_still_fires() -> None:
    plan = _resource(name="app-quiet", kind=ResourceKind.APP_SERVICE_PLAN, sku="P1v3")
    signal = detect_oversized_app_service_plan(
        plan, _metrics(plan, utilisation="5"), client="acme", as_of=AS_OF
    )
    assert signal is not None
    assert signal.evidence_value("cpu_max_pct") is None


def test_a_non_plan_yields_no_oversized_plan_signal() -> None:
    disk = _resource(name="disk-x", kind=ResourceKind.MANAGED_DISK)
    assert detect_oversized_app_service_plan(disk, None, client="acme", as_of=AS_OF) is None


# ---------------------------------------------------------------------------
# Unused reservation
# ---------------------------------------------------------------------------


def test_an_unmeasured_reservation_yields_nothing() -> None:
    """Utilisation is the whole basis of the conclusion."""
    assert (
        detect_unused_reservation(_reservation(utilisation=None), client="acme", as_of=AS_OF)
        is None
    )


def test_a_well_used_reservation_yields_nothing() -> None:
    assert (
        detect_unused_reservation(_reservation(utilisation="96.2"), client="acme", as_of=AS_OF)
        is None
    )


def test_a_reservation_without_an_expiry_still_fires() -> None:
    signal = detect_unused_reservation(_reservation(expires=False), client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.evidence_value("expires_on") is None


def test_a_reservation_signal_reports_its_scope_and_kind() -> None:
    signal = detect_unused_reservation(_reservation(), client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.resource_kind == "reservation"
    assert signal.subscription_id == "sub-1"
    assert signal.kind is WasteKind.UNUSED_RESERVATION
    assert signal.provenance.source is ProvenanceSource.CALCULATION


def test_a_shared_scope_reservation_reports_a_shared_scope() -> None:
    """A reservation spanning the enrolment has no single subscription."""
    shared = _reservation().model_copy(update={"scope_subscription_id": None})
    signal = detect_unused_reservation(shared, client="acme", as_of=AS_OF)
    assert signal is not None
    assert signal.subscription_id == "shared-scope"


def test_running_the_reservation_detectors_over_nothing_yields_nothing() -> None:
    assert run_reservation_detectors((), client="acme", as_of=AS_OF) == ()


# ---------------------------------------------------------------------------
# Savings Estimator: targets that have vanished
# ---------------------------------------------------------------------------


def test_target_resolution_prefers_a_resource_then_a_reservation() -> None:
    resource = _resource(name="disk-1", kind=ResourceKind.MANAGED_DISK)
    reservation = _reservation()
    estate = Estate(
        client="acme",
        retrieved_at=AS_OF,
        subscriptions=(),
        resources=(resource,),
        reservations=(reservation,),
    )
    assert target_for(estate, resource.resource_id) is not None
    reservation_target = target_for(estate, reservation.reservation_id)
    assert reservation_target is not None
    assert reservation_target.kind_label == "reservation"
    assert target_for(estate, "/nothing/here") is None


def test_a_signal_targeting_something_absent_is_unpriced_not_zeroed(
    settings: Settings,
) -> None:
    """A vanished resource must not be reported as a zero-value finding."""
    orphan_signal = WasteSignal(
        signal_id="ws-gone",
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        client="acme",
        subscription_id="sub-1",
        resource_id="/subscriptions/sub-1/resources/already-deleted",
        resource_name="already-deleted",
        resource_kind="managed_disk",
        evidence=(Observation(name="state", value="unattached", provenance=PROVENANCE),),
        monthly_cost=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
        confidence=Decimal("0.95"),
        detector="test/v1",
        detected_at=AS_OF,
        provenance=PROVENANCE,
    )
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=Estate(client="acme", retrieved_at=AS_OF, subscriptions=(), resources=()),
        signals=(orphan_signal,),
    )
    update = make_savings_estimator(provider=MockAzureProvider(), settings=settings)(state)

    options = update["priced_options"]
    assert options
    assert all(not option.is_priced for option in options)
    assert any("not in the estate snapshot" in error for error in update["errors"])


def test_the_estimator_prices_nothing_when_there_are_no_signals(
    settings: Settings,
) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=Estate(client="acme", retrieved_at=AS_OF, subscriptions=(), resources=()),
    )
    update = make_savings_estimator(provider=MockAzureProvider(), settings=settings)(state)
    assert "priced_options" not in update
    assert update["audit"]


def test_the_estimator_prices_every_candidate_not_just_one(settings: Settings) -> None:
    """The planner chooses knowing what each option is worth."""
    provider = MockAzureProvider()
    subscription = provider.list_subscriptions()[1]
    disk = next(
        r
        for r in provider.list_resources(subscription.subscription_id)
        if r.name == "disk-analytics-01-data"
    )
    signal = WasteSignal(
        signal_id="ws-1",
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        client="acme",
        subscription_id=subscription.subscription_id,
        resource_id=disk.resource_id,
        resource_name=disk.name,
        resource_kind="managed_disk",
        evidence=(),
        monthly_cost=disk.monthly_cost,
        confidence=Decimal("0.95"),
        detector="test/v1",
        detected_at=AS_OF,
        provenance=PROVENANCE,
    )
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=Estate(
            client="acme", retrieved_at=AS_OF, subscriptions=(subscription,), resources=(disk,)
        ),
        signals=(signal,),
    )
    options = make_savings_estimator(provider=provider, settings=settings)(state)["priced_options"]
    actions = {option.action for option in options}
    # Three candidates for an orphaned disk, all priced before anything is chosen.
    assert ActionType.DELETE_ORPHANED_MANAGED_DISK in actions
    assert ActionType.NOTIFY_OWNER in actions
    assert len(actions) >= 2
    priced = {o.action for o in options if o.is_priced}
    assert ActionType.DELETE_ORPHANED_MANAGED_DISK in priced
    # "Tell someone" has no savings model, so it is unknown rather than zero.
    assert ActionType.NOTIFY_OWNER not in priced


# ---------------------------------------------------------------------------
# Report Author helpers
# ---------------------------------------------------------------------------


def _finding(kind: WasteKind, monthly: str | None) -> ReportFinding:
    saving = (
        MoneyAmount.of(Decimal(monthly), provenance=Provenance.calculated("test"))
        if monthly is not None
        else MoneyAmount.undetermined("not priced")
    )
    return ReportFinding(
        rank=1,
        recommendation_id="rec-1",
        signal_id="ws-1",
        waste_kind=kind,
        subscription_id="sub-1",
        resource_id="/r/1",
        resource_name="thing",
        current_monthly_cost=saving,
        proposed_action=ActionType.NOTIFY_OWNER,
        action_class=ActionClass.ALLOW,
        requires_approval=False,
        confidence=Decimal("0.9"),
        rationale="because",
        monthly_saving=saving,
        annual_saving=saving,
        savings_basis="test",
        savings_is_estimated=False,
    )


def test_a_waste_kind_whose_findings_are_all_unpriced_is_still_reported() -> None:
    """Dropping the group would hide waste that was found but could not be costed."""
    breakdown = _waste_breakdown((_finding(WasteKind.UNEXPECTED_EGRESS, None),))
    assert len(breakdown) == 1
    assert breakdown[0].findings_count == 1
    assert not breakdown[0].monthly_saving.is_known
    assert "could be priced" in (breakdown[0].monthly_saving.provenance.reference or "")


def test_unpriced_groups_sort_below_priced_ones() -> None:
    breakdown = _waste_breakdown(
        (
            _finding(WasteKind.UNEXPECTED_EGRESS, None),
            _finding(WasteKind.ORPHANED_MANAGED_DISK, "10.00"),
        )
    )
    assert breakdown[0].waste_kind is WasteKind.ORPHANED_MANAGED_DISK


def test_no_findings_means_no_breakdown() -> None:
    assert _waste_breakdown(()) == ()


def test_resource_count_includes_reservations() -> None:
    """A reservation is something the sweep examined, so it counts."""
    estate = Estate(
        client="acme",
        retrieved_at=AS_OF,
        subscriptions=(),
        resources=(_resource(name="disk-1", kind=ResourceKind.MANAGED_DISK),),
        reservations=(_reservation(),),
    )
    state = ScanState(run_id="run-1", client="acme", estate=estate)
    assert _resource_count(state) == 2
    assert _resource_count(ScanState(run_id="run-1", client="acme")) == 0
