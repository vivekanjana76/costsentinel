"""Savings arithmetic and the policy store.

Two guarantees are pinned here:

* a savings figure is arithmetic over provider data or explicitly unknown -- never a
  guess, and never a zero standing in for an unknown;
* the action-class floor can be tightened by policy and can never be loosened.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from costsentinel.agents import savings as savings_module
from costsentinel.agents.savings import (
    SavingsTarget,
    choose_rightsize_target,
    estimate_savings,
    peak_signal_for,
    total_savings,
)
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    Environment,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
)
from costsentinel.domain.recommendations import ActionClass, ActionType, SavingsEstimate
from costsentinel.domain.signals import WasteKind
from costsentinel.guardrails.approval import NoDecisionGate
from costsentinel.guardrails.policy import ACTION_CLASS_FLOOR, PolicyStore
from costsentinel.providers.mock import MockAzureProvider

PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER, reference="test", verification=Verification.VERIFIED
)
HEADROOM = Decimal("2.0")


def _resource(
    *,
    name: str = "vm-1",
    kind: ResourceKind = ResourceKind.VIRTUAL_MACHINE,
    sku: str | None = "Standard_E16s_v5",
    cost: str | None = "735.84",
) -> Resource:
    return Resource(
        resource_id=f"/subscriptions/sub-1/resources/{name}",
        name=name,
        kind=kind,
        subscription_id="sub-1",
        resource_group="rg-1",
        region="eastus",
        sku=sku,
        state=ResourceState.RUNNING,
        monthly_cost=(
            MoneyAmount.of(Decimal(cost), provenance=PROVENANCE)
            if cost is not None
            else MoneyAmount.undetermined("no price")
        ),
        provenance=PROVENANCE,
    )


def _target(**kwargs: object) -> SavingsTarget:
    """A savings target built from a synthetic resource."""
    return SavingsTarget.from_resource(_resource(**kwargs))  # type: ignore[arg-type]


def _metrics(
    resource_id: str = "/subscriptions/sub-1/resources/vm-1",
    *,
    cpu_max: str | None = "18.5",
    utilisation: str | None = None,
    connections: int | None = None,
) -> ResourceMetrics:
    return ResourceMetrics(
        resource_id=resource_id,
        observation_days=30,
        cpu_avg_pct=Decimal("6.1"),
        cpu_max_pct=Decimal(cpu_max) if cpu_max is not None else None,
        utilisation_pct=Decimal(utilisation) if utilisation is not None else None,
        connection_count=connections,
        provenance=PROVENANCE,
    )


def _prices(region: str = "eastus") -> list[SkuPrice]:
    return list(MockAzureProvider(seed=1337).list_sku_prices(region))


# ---------------------------------------------------------------------------
# The structural guarantee
# ---------------------------------------------------------------------------


def test_savings_module_imports_no_language_model() -> None:
    """ARCHITECTURE.md D17: verifiable in one glance at the imports."""
    source = inspect.getsource(savings_module)
    assert "costsentinel.llm" not in source
    assert "LLM" not in source


def test_savings_estimator_node_imports_no_language_model() -> None:
    """The node that wraps the arithmetic must stay model-free too."""
    from costsentinel.agents import savings_estimator

    source = inspect.getsource(savings_estimator)
    assert "costsentinel.llm" not in source


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def test_target_from_resource_carries_the_sizing_facts() -> None:
    target = _target()
    assert target.kind is ResourceKind.VIRTUAL_MACHINE
    assert target.kind_label == "virtual_machine"
    assert target.sku == "Standard_E16s_v5"
    assert target.utilisation_pct is None


def test_target_from_reservation_carries_utilisation() -> None:
    """A reservation has no resource kind, and its utilisation is the key fact."""
    reservation = Reservation(
        reservation_id="rsv-1",
        name="nw-compute",
        client="acme",
        reserved_sku="Standard_D4s_v5",
        reserved_kind=ResourceKind.VIRTUAL_MACHINE,
        region="eastus",
        term_months=36,
        quantity=4,
        monthly_amortised_cost=MoneyAmount.of(Decimal("420.48"), provenance=PROVENANCE),
        utilisation_pct=Decimal("34.5"),
        provenance=PROVENANCE,
    )
    target = SavingsTarget.from_reservation(reservation)
    assert target.kind is None
    assert target.kind_label == "reservation"
    assert target.utilisation_pct == Decimal("34.5")


def test_peak_signal_depends_on_the_resource_kind() -> None:
    """An App Service plan is sized on utilisation, everything else on peak CPU."""
    metrics = _metrics(cpu_max="18.5", utilisation="11.4")
    assert peak_signal_for(ResourceKind.VIRTUAL_MACHINE, metrics) == Decimal("18.5")
    assert peak_signal_for(ResourceKind.SQL_DATABASE, metrics) == Decimal("18.5")
    assert peak_signal_for(ResourceKind.APP_SERVICE_PLAN, metrics) == Decimal("11.4")
    assert peak_signal_for(ResourceKind.VIRTUAL_MACHINE, None) is None


# ---------------------------------------------------------------------------
# Full-cost savings
# ---------------------------------------------------------------------------


def test_deleting_an_orphaned_disk_saves_its_whole_cost() -> None:
    target = _target(
        name="disk-orphan", kind=ResourceKind.MANAGED_DISK, sku="Premium_LRS_P15", cost="38.42"
    )
    estimate = estimate_savings(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, target, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("38.42")
    assert estimate.annual.amount == Decimal("38.42") * 12
    assert not estimate.is_estimated  # observed, not modelled
    assert estimate.monthly.provenance.source is ProvenanceSource.CALCULATION
    assert "38.42" in estimate.basis


def test_releasing_a_public_ip_saves_its_whole_cost() -> None:
    target = _target(
        name="pip-free", kind=ResourceKind.PUBLIC_IP, sku="Standard_Static", cost="3.65"
    )
    estimate = estimate_savings(
        ActionType.DELETE_UNATTACHED_PUBLIC_IP, target, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("3.65")


def test_deleting_a_stale_snapshot_saves_its_whole_cost() -> None:
    target = _target(
        name="snap-old",
        kind=ResourceKind.SNAPSHOT,
        sku="Standard_LRS_Snapshot_512",
        cost="24.58",
    )
    estimate = estimate_savings(
        ActionType.DELETE_STALE_SNAPSHOT, target, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("24.58")
    assert not estimate.is_estimated


def test_deallocating_a_vm_saves_the_compute_charge_and_says_so() -> None:
    """The basis must be explicit that attached disks keep billing."""
    target = _target(name="vm-idle", sku="Standard_D16s_v5", cost="560.64")
    estimate = estimate_savings(
        ActionType.DEALLOCATE_VIRTUAL_MACHINE, target, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("560.64")
    assert "disks bill separately" in estimate.basis


def test_unpriced_resource_yields_an_unknown_saving_not_zero() -> None:
    target = _target(name="disk-unpriced", kind=ResourceKind.MANAGED_DISK, cost=None)
    estimate = estimate_savings(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, target, None, [], headroom_factor=HEADROOM
    )
    assert not estimate.is_known
    assert estimate.monthly.amount is None
    assert "unknown" in estimate.basis


def test_an_action_with_no_savings_model_is_unknown_not_zero() -> None:
    estimate = estimate_savings(
        ActionType.NOTIFY_OWNER, _target(), None, [], headroom_factor=HEADROOM
    )
    assert not estimate.is_known
    assert "unknown rather than assumed to be zero" in estimate.basis


# ---------------------------------------------------------------------------
# Rightsizing
# ---------------------------------------------------------------------------


def test_rightsize_target_is_the_smallest_same_family_sku_that_fits() -> None:
    """16 vCPU at 18.5% peak, doubled for headroom, needs ~5.92 vCPU -> E8s_v5."""
    chosen = choose_rightsize_target(
        sku="Standard_E16s_v5",
        kind=ResourceKind.VIRTUAL_MACHINE,
        prices=_prices(),
        observed_peak_pct=Decimal("18.5"),
        headroom_factor=HEADROOM,
    )
    assert chosen is not None
    current, target = chosen
    assert current.sku == "Standard_E16s_v5"
    assert target.sku == "Standard_E8s_v5"
    assert target.family == current.family  # stays in family, keeping the memory ratio


def test_rightsizing_never_crosses_resource_kinds() -> None:
    """A SQL tier must not be offered as a candidate for a virtual machine.

    Both are priced per vCPU, so without the kind filter they are interchangeable
    to the search -- and recommending GP_Gen5_2 for a VM would be nonsense.
    """
    chosen = choose_rightsize_target(
        sku="GP_Gen5_4",
        kind=ResourceKind.SQL_DATABASE,
        prices=_prices(),
        observed_peak_pct=Decimal("2.1"),
        headroom_factor=HEADROOM,
    )
    assert chosen is not None
    current, target = chosen
    assert current.applies_to is ResourceKind.SQL_DATABASE
    assert target.applies_to is ResourceKind.SQL_DATABASE
    assert target.sku == "GP_Gen5_2"


def test_resize_saving_is_the_catalogue_price_difference() -> None:
    target = _target(sku="Standard_E16s_v5", cost="735.84")
    estimate = estimate_savings(
        ActionType.RESIZE_VIRTUAL_MACHINE,
        target,
        _metrics(target.identifier),
        _prices(),
        headroom_factor=HEADROOM,
    )
    assert estimate.monthly.amount == Decimal("735.84") - Decimal("367.92")
    assert estimate.is_estimated  # depends on the headroom assumption
    assert "Standard_E8s_v5" in estimate.basis
    assert "provider catalogue prices" in estimate.basis
    assert "2.0x headroom" in estimate.basis


def test_sql_scale_down_saving_is_the_tier_difference() -> None:
    target = _target(
        name="sql-idle", kind=ResourceKind.SQL_DATABASE, sku="GP_Gen5_4", cost="147.30"
    )
    estimate = estimate_savings(
        ActionType.SCALE_DOWN_SQL_DATABASE,
        target,
        _metrics(target.identifier, cpu_max="2.1", connections=0),
        _prices(),
        headroom_factor=HEADROOM,
    )
    assert estimate.monthly.amount == Decimal("147.30") - Decimal("73.65")
    assert estimate.is_estimated


def test_app_service_scale_down_uses_utilisation_not_cpu() -> None:
    """The plan is sized on instance utilisation, so that is the governing signal."""
    target = _target(name="app-plan", kind=ResourceKind.APP_SERVICE_PLAN, sku="P1v3", cost="219.00")
    estimate = estimate_savings(
        ActionType.SCALE_DOWN_APP_SERVICE_PLAN,
        target,
        _metrics(target.identifier, cpu_max="19.6", utilisation="11.4"),
        _prices(),
        headroom_factor=HEADROOM,
    )
    assert estimate.monthly.amount == Decimal("219.00") - Decimal("109.50")
    assert "P0v3" in estimate.basis


def test_higher_headroom_picks_a_larger_target_or_none() -> None:
    """The headroom factor is a real knob, not decoration."""
    generous = choose_rightsize_target(
        sku="Standard_E16s_v5",
        kind=ResourceKind.VIRTUAL_MACHINE,
        prices=_prices(),
        observed_peak_pct=Decimal("18.5"),
        headroom_factor=Decimal("6.0"),
    )
    assert generous is None  # 16 x 0.185 x 6 = 17.76 vCPU: nothing smaller fits


def test_no_rightsize_target_when_nothing_smaller_fits() -> None:
    """A machine already on the smallest SKU in its family cannot be shrunk."""
    target = _target(sku="Standard_E2s_v5", cost="91.98")
    assert (
        choose_rightsize_target(
            sku="Standard_E2s_v5",
            kind=ResourceKind.VIRTUAL_MACHINE,
            prices=_prices(),
            observed_peak_pct=Decimal("18.5"),
            headroom_factor=HEADROOM,
        )
        is None
    )
    estimate = estimate_savings(
        ActionType.RESIZE_VIRTUAL_MACHINE,
        target,
        _metrics(target.identifier),
        _prices(),
        headroom_factor=HEADROOM,
    )
    assert not estimate.is_known
    assert "no smaller same-family SKU" in estimate.basis


def test_no_rightsize_target_without_the_governing_metric() -> None:
    assert (
        choose_rightsize_target(
            sku="Standard_E16s_v5",
            kind=ResourceKind.VIRTUAL_MACHINE,
            prices=_prices(),
            observed_peak_pct=None,
            headroom_factor=HEADROOM,
        )
        is None
    )


def test_no_rightsize_target_for_an_uncatalogued_or_absent_sku() -> None:
    for sku in ("Standard_Nonexistent_v9", None):
        assert (
            choose_rightsize_target(
                sku=sku,
                kind=ResourceKind.VIRTUAL_MACHINE,
                prices=_prices(),
                observed_peak_pct=Decimal("18.5"),
                headroom_factor=HEADROOM,
            )
            is None
        )


def test_no_rightsize_target_for_a_sku_without_capacity() -> None:
    """A disk has no vCPU, so there is nothing to size against."""
    assert (
        choose_rightsize_target(
            sku="Premium_LRS_P15",
            kind=ResourceKind.MANAGED_DISK,
            prices=_prices(),
            observed_peak_pct=Decimal("10"),
            headroom_factor=HEADROOM,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def _reservation_target(utilisation: str | None, cost: str | None = "420.48") -> SavingsTarget:
    return SavingsTarget(
        identifier="rsv-1",
        name="nw-compute-dsv5-3y",
        kind=None,
        kind_label="reservation",
        region="eastus",
        sku="Standard_D4s_v5",
        monthly_cost=(
            MoneyAmount.of(Decimal(cost), provenance=PROVENANCE)
            if cost is not None
            else MoneyAmount.undetermined("no amortised cost reported")
        ),
        utilisation_pct=Decimal(utilisation) if utilisation is not None else None,
    )


def test_reservation_waste_is_the_unconsumed_share() -> None:
    """420.48 at 34.5% utilised leaves 65.5% being paid for and not used."""
    estimate = estimate_savings(
        ActionType.EXCHANGE_UNUSED_RESERVATION,
        _reservation_target("34.5"),
        None,
        [],
        headroom_factor=HEADROOM,
    )
    assert estimate.monthly.amount == Decimal("275.41")
    assert estimate.annual.amount == Decimal("275.41") * 12
    # Estimated, because recovering it depends on the exchange terms.
    assert estimate.is_estimated
    assert "already committed" in estimate.basis


def test_reservation_waste_is_unknown_without_utilisation_or_cost() -> None:
    for utilisation, cost in (("34.5", None), (None, "420.48")):
        estimate = estimate_savings(
            ActionType.EXCHANGE_UNUSED_RESERVATION,
            _reservation_target(utilisation, cost),
            None,
            [],
            headroom_factor=HEADROOM,
        )
        assert not estimate.is_known


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def _known(amount: str) -> SavingsEstimate:
    return SavingsEstimate.from_monthly(
        MoneyAmount.of(Decimal(amount), provenance=Provenance.calculated("test")),
        basis="test",
        is_estimated=False,
    )


def test_totals_sum_known_savings() -> None:
    monthly, annual, unknown = total_savings([_known("10.00"), _known("2.50")])
    assert monthly.amount == Decimal("12.50")
    assert annual.amount == Decimal("150.00")
    assert unknown == 0


def test_totals_count_unknowns_rather_than_treating_them_as_zero() -> None:
    monthly, _, unknown = total_savings(
        [_known("10.00"), SavingsEstimate.undetermined("not priced")]
    )
    assert monthly.amount == Decimal("10.00")
    assert unknown == 1


def test_totals_of_only_unknowns_are_unknown() -> None:
    monthly, annual, unknown = total_savings([SavingsEstimate.undetermined("nope")])
    assert not monthly.is_known
    assert not annual.is_known
    assert unknown == 1
    assert "no finding could be priced" in (monthly.provenance.reference or "")


def test_totals_of_nothing_are_unknown_not_zero() -> None:
    monthly, _, unknown = total_savings([])
    assert not monthly.is_known
    assert unknown == 0
    assert "no findings were produced" in (monthly.provenance.reference or "")


# ---------------------------------------------------------------------------
# The policy store
# ---------------------------------------------------------------------------


def test_every_action_has_a_floor(policy: PolicyStore) -> None:
    for action in ActionType:
        assert action in ACTION_CLASS_FLOOR
        assert policy.floor_for(action) in set(ActionClass)


def test_destructive_actions_are_never_allow(policy: PolicyStore) -> None:
    """CLAUDE.md golden rule 3: nothing destructive runs without a human."""
    destructive = {
        ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        ActionType.RESIZE_VIRTUAL_MACHINE,
        ActionType.SCALE_DOWN_SQL_DATABASE,
        ActionType.SCALE_DOWN_APP_SERVICE_PLAN,
        ActionType.EXCHANGE_UNUSED_RESERVATION,
        ActionType.DELETE_STALE_SNAPSHOT,
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        ActionType.DELETE_UNCONFIRMED_RESOURCE,
    }
    for action in destructive:
        assert policy.floor_for(action) is not ActionClass.ALLOW
        assert policy.floor_for(action).requires_approval


def test_safe_reversible_actions_are_allow(policy: PolicyStore) -> None:
    for action in (ActionType.APPLY_TAG, ActionType.NOTIFY_OWNER, ActionType.NO_ACTION):
        effective, _ = policy.classify(action)
        assert effective is ActionClass.ALLOW
        assert not effective.requires_approval
        assert effective.is_automatable


def test_deleting_an_unconfirmed_resource_is_always_blocked(policy: PolicyStore) -> None:
    """No environment, and no approver, makes this automatable."""
    for environment in Environment:
        effective, _ = policy.classify(
            ActionType.DELETE_UNCONFIRMED_RESOURCE, environment=environment
        )
        assert effective is ActionClass.BLOCK
        assert not effective.is_automatable


def test_production_tightens_a_delete_to_block(policy: PolicyStore) -> None:
    """Tightening above the floor is permitted and happens (ARCHITECTURE.md D8)."""
    for action in (
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.DELETE_STALE_SNAPSHOT,
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
    ):
        assert policy.floor_for(action) is ActionClass.REVIEW
        development, _ = policy.classify(action, environment=Environment.DEVELOPMENT)
        production, reason = policy.classify(action, environment=Environment.PRODUCTION)
        assert development is ActionClass.REVIEW
        assert production is ActionClass.BLOCK
        assert "production" in reason


def test_staging_is_treated_as_production_like(policy: PolicyStore) -> None:
    effective, _ = policy.classify(
        ActionType.DELETE_UNATTACHED_PUBLIC_IP, environment=Environment.STAGING
    )
    assert effective is ActionClass.BLOCK


def test_classification_never_falls_below_the_floor(policy: PolicyStore) -> None:
    for action in ActionType:
        floor = policy.floor_for(action)
        for environment in Environment:
            effective, _ = policy.classify(action, environment=environment)
            assert effective.severity >= floor.severity


def test_production_deallocation_notes_the_change_window(policy: PolicyStore) -> None:
    effective, reason = policy.classify(
        ActionType.DEALLOCATE_VIRTUAL_MACHINE, environment=Environment.PRODUCTION
    )
    assert effective is ActionClass.REVIEW
    assert "change window" in reason


def test_every_waste_kind_has_candidate_actions(policy: PolicyStore) -> None:
    for kind in WasteKind:
        candidates = policy.candidate_actions(kind)
        assert candidates
        assert all(isinstance(action, ActionType) for action in candidates)


def test_each_new_waste_kind_leads_with_a_remediating_action(policy: PolicyStore) -> None:
    """A kind whose only option is "tell someone" cannot produce a saving."""
    expected = {
        WasteKind.STALE_SNAPSHOT: ActionType.DELETE_STALE_SNAPSHOT,
        WasteKind.IDLE_SQL_DATABASE: ActionType.SCALE_DOWN_SQL_DATABASE,
        WasteKind.OVERSIZED_APP_SERVICE_PLAN: ActionType.SCALE_DOWN_APP_SERVICE_PLAN,
        WasteKind.UNUSED_RESERVATION: ActionType.EXCHANGE_UNUSED_RESERVATION,
    }
    for kind, action in expected.items():
        assert policy.candidate_actions(kind)[0] is action


def test_destructive_actions_carry_preconditions(policy: PolicyStore) -> None:
    for action in (
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        ActionType.DELETE_STALE_SNAPSHOT,
        ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        ActionType.RESIZE_VIRTUAL_MACHINE,
        ActionType.SCALE_DOWN_SQL_DATABASE,
        ActionType.SCALE_DOWN_APP_SERVICE_PLAN,
        ActionType.EXCHANGE_UNUSED_RESERVATION,
    ):
        preconditions = policy.preconditions(action)
        assert len(preconditions) >= 2
        assert all(p.endswith(".") for p in preconditions)


def test_orphan_deletion_requires_a_verified_snapshot(policy: PolicyStore) -> None:
    """The precondition a model response must not be able to drop."""
    preconditions = " ".join(policy.preconditions(ActionType.DELETE_ORPHANED_MANAGED_DISK))
    assert "still unattached" in preconditions
    assert "snapshot" in preconditions


def test_reservation_exchange_requires_finance_sign_off(policy: PolicyStore) -> None:
    """A commercial action needs a commercial precondition, not just a technical one."""
    preconditions = " ".join(policy.preconditions(ActionType.EXCHANGE_UNUSED_RESERVATION))
    assert "Finance" in preconditions
    assert "exchange or refund window" in preconditions


def test_classification_record_is_audit_grade(policy: PolicyStore) -> None:
    classification = policy.classification_for(
        recommendation_id="rec-1",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        environment=Environment.PRODUCTION,
    )
    assert classification.action_class is ActionClass.BLOCK
    assert classification.floor_class is ActionClass.REVIEW
    assert classification.requires_approval
    assert not classification.is_automatable
    assert classification.policy_reference.startswith("builtin-policy-v1:")
    assert classification.provenance.source is ProvenanceSource.POLICY_STORE
    assert classification.reason


def test_policy_store_has_a_name(policy: PolicyStore) -> None:
    assert policy.name == "builtin-policy-v1"


# ---------------------------------------------------------------------------
# The approval-gate seam
# ---------------------------------------------------------------------------


def test_default_gate_records_but_never_decides() -> None:
    """The safe default: no decision means the action stays gated."""
    from costsentinel.domain.governance import ApprovalRequest

    gate = NoDecisionGate()
    request = ApprovalRequest(
        request_id="apr-1",
        run_id="run-1",
        recommendation_id="rec-1",
        client="acme",
        subscription_id="sub-1",
        target_resource_id="/r/1",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        action_class=ActionClass.REVIEW,
        summary="delete the orphan",
        monthly_saving_display="38.42 USD",
    )
    assert gate.request(request) is None
    assert gate.requests == [request]
    assert gate.name == "no-decision-gate"
