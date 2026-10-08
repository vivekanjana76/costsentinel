"""Savings arithmetic and the policy store.

Two guarantees are pinned here:

* a savings figure is arithmetic over provider data or explicitly unknown -- never
  a guess, and never a zero standing in for an unknown;
* the action-class floor can be tightened by policy and can never be loosened.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from costsentinel.agents import savings as savings_module
from costsentinel.agents.savings import (
    choose_rightsize_target,
    estimate_savings,
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


def _metrics(resource: Resource, *, cpu_max: str | None = "18.5") -> ResourceMetrics:
    return ResourceMetrics(
        resource_id=resource.resource_id,
        observation_days=30,
        cpu_avg_pct=Decimal("6.1"),
        cpu_max_pct=Decimal(cpu_max) if cpu_max is not None else None,
        provenance=PROVENANCE,
    )


def _prices() -> list[SkuPrice]:
    return list(MockAzureProvider(seed=1337).list_sku_prices("eastus"))


# ---------------------------------------------------------------------------
# The structural guarantee
# ---------------------------------------------------------------------------


def test_savings_module_imports_no_language_model() -> None:
    """ARCHITECTURE.md D17: verifiable in one glance at the imports."""
    source = inspect.getsource(savings_module)
    assert "costsentinel.llm" not in source
    assert "LLM" not in source


# ---------------------------------------------------------------------------
# Full-cost savings
# ---------------------------------------------------------------------------


def test_deleting_an_orphaned_disk_saves_its_whole_cost() -> None:
    disk = _resource(
        name="disk-orphan", kind=ResourceKind.MANAGED_DISK, sku="Premium_LRS_P15", cost="38.42"
    )
    estimate = estimate_savings(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, disk, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("38.42")
    assert estimate.annual.amount == Decimal("38.42") * 12
    assert not estimate.is_estimated  # observed, not modelled
    assert estimate.monthly.provenance.source is ProvenanceSource.CALCULATION
    assert "38.42" in estimate.basis


def test_releasing_a_public_ip_saves_its_whole_cost() -> None:
    ip = _resource(name="pip-free", kind=ResourceKind.PUBLIC_IP, sku="Standard_Static", cost="3.65")
    estimate = estimate_savings(
        ActionType.DELETE_UNATTACHED_PUBLIC_IP, ip, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("3.65")


def test_deallocating_a_vm_saves_the_compute_charge_and_says_so() -> None:
    """The basis must be explicit that attached disks keep billing."""
    vm = _resource(name="vm-idle", sku="Standard_D16s_v5", cost="560.64")
    estimate = estimate_savings(
        ActionType.DEALLOCATE_VIRTUAL_MACHINE, vm, None, [], headroom_factor=HEADROOM
    )
    assert estimate.monthly.amount == Decimal("560.64")
    assert "disks bill separately" in estimate.basis


def test_unpriced_resource_yields_an_unknown_saving_not_zero() -> None:
    disk = _resource(name="disk-unpriced", kind=ResourceKind.MANAGED_DISK, cost=None)
    estimate = estimate_savings(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, disk, None, [], headroom_factor=HEADROOM
    )
    assert not estimate.is_known
    assert estimate.monthly.amount is None
    assert "unknown" in estimate.basis


def test_an_action_with_no_savings_model_is_unknown_not_zero() -> None:
    vm = _resource()
    estimate = estimate_savings(ActionType.NOTIFY_OWNER, vm, None, [], headroom_factor=HEADROOM)
    assert not estimate.is_known
    assert "unknown rather than assumed to be zero" in estimate.basis


# ---------------------------------------------------------------------------
# Rightsizing
# ---------------------------------------------------------------------------


def test_rightsize_target_is_the_smallest_same_family_sku_that_fits() -> None:
    """16 vCPU at 18.5% peak, doubled for headroom, needs ~5.92 vCPU -> E8s_v5."""
    vm = _resource(sku="Standard_E16s_v5")
    chosen = choose_rightsize_target(vm, _metrics(vm), _prices(), headroom_factor=HEADROOM)
    assert chosen is not None
    current, target = chosen
    assert current.sku == "Standard_E16s_v5"
    assert target.sku == "Standard_E8s_v5"
    assert target.family == current.family  # stays in family, keeping the memory ratio


def test_resize_saving_is_the_catalogue_price_difference() -> None:
    vm = _resource(sku="Standard_E16s_v5", cost="735.84")
    estimate = estimate_savings(
        ActionType.RESIZE_VIRTUAL_MACHINE,
        vm,
        _metrics(vm),
        _prices(),
        headroom_factor=HEADROOM,
    )
    assert estimate.monthly.amount == Decimal("735.84") - Decimal("367.92")
    assert estimate.is_estimated  # depends on the headroom assumption
    assert "Standard_E8s_v5" in estimate.basis
    assert "provider catalogue prices" in estimate.basis
    assert "2.0x headroom" in estimate.basis


def test_higher_headroom_picks_a_larger_target_or_none() -> None:
    """The headroom factor is a real knob, not decoration."""
    vm = _resource(sku="Standard_E16s_v5")
    generous = choose_rightsize_target(vm, _metrics(vm), _prices(), headroom_factor=Decimal("6.0"))
    assert generous is None  # 16 x 0.185 x 6 = 17.76 vCPU: nothing smaller fits


def test_no_rightsize_target_when_nothing_smaller_fits() -> None:
    """A machine already on the smallest SKU in its family cannot be shrunk."""
    vm = _resource(sku="Standard_E2s_v5", cost="91.98")
    chosen = choose_rightsize_target(vm, _metrics(vm), _prices(), headroom_factor=HEADROOM)
    assert chosen is None

    estimate = estimate_savings(
        ActionType.RESIZE_VIRTUAL_MACHINE, vm, _metrics(vm), _prices(), headroom_factor=HEADROOM
    )
    assert not estimate.is_known
    assert "no smaller same-family SKU" in estimate.basis


def test_no_rightsize_target_without_metrics() -> None:
    vm = _resource()
    assert choose_rightsize_target(vm, None, _prices(), headroom_factor=HEADROOM) is None
    assert (
        choose_rightsize_target(vm, _metrics(vm, cpu_max=None), _prices(), headroom_factor=HEADROOM)
        is None
    )


def test_no_rightsize_target_for_an_uncatalogued_sku() -> None:
    vm = _resource(sku="Standard_Nonexistent_v9")
    assert choose_rightsize_target(vm, _metrics(vm), _prices(), headroom_factor=HEADROOM) is None


def test_no_rightsize_target_without_a_sku() -> None:
    vm = _resource(sku=None)
    assert choose_rightsize_target(vm, _metrics(vm), _prices(), headroom_factor=HEADROOM) is None


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
    floor = policy.floor_for(ActionType.DELETE_ORPHANED_MANAGED_DISK)
    assert floor is ActionClass.REVIEW

    development, _ = policy.classify(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, environment=Environment.DEVELOPMENT
    )
    production, reason = policy.classify(
        ActionType.DELETE_ORPHANED_MANAGED_DISK, environment=Environment.PRODUCTION
    )
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


def test_destructive_actions_carry_preconditions(policy: PolicyStore) -> None:
    for action in (
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        ActionType.RESIZE_VIRTUAL_MACHINE,
    ):
        preconditions = policy.preconditions(action)
        assert len(preconditions) >= 2
        assert all(p.endswith(".") for p in preconditions)


def test_orphan_deletion_requires_a_verified_snapshot(policy: PolicyStore) -> None:
    """The precondition a model response must not be able to drop."""
    preconditions = " ".join(policy.preconditions(ActionType.DELETE_ORPHANED_MANAGED_DISK))
    assert "still unattached" in preconditions
    assert "snapshot" in preconditions


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
    """The Phase 1 default must be safe: no decision means the action stays gated."""
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
