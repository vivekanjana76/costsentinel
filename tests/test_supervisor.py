"""Supervisor routing tests.

Routing is a pure function of state, so these build a state and assert the decision
rather than running a graph. That is the payoff of keeping every conditional in one
node: retry, escalation and the approval seam are all testable without a sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from costsentinel.agents.supervisor import (
    MAX_ATTEMPTS,
    make_supervisor,
    route_from_supervisor,
)
from costsentinel.config import Settings
from costsentinel.domain.analysis import RootCause
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import Estate, Reservation, ResourceKind
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalDecision,
    Decision,
    RouteAction,
)
from costsentinel.domain.recommendations import (
    ActionClass,
    ActionType,
    PricedOption,
    Recommendation,
    SavingsEstimate,
)
from costsentinel.domain.signals import Observation, WasteKind, WasteSignal
from costsentinel.domain.state import ScanState

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER,
    retrieved_at=AS_OF,
    reference="test",
    verification=Verification.VERIFIED,
)


def _estate(*, resources: int = 0, reservations: int = 0) -> Estate:
    return Estate(
        client="acme",
        retrieved_at=AS_OF,
        subscriptions=(),
        resources=(),
        reservations=tuple(
            Reservation(
                reservation_id=f"rsv-{index}",
                name=f"rsv-{index}",
                client="acme",
                reserved_sku="Standard_D4s_v5",
                reserved_kind=ResourceKind.VIRTUAL_MACHINE,
                region="eastus",
                term_months=12,
                quantity=1,
                monthly_amortised_cost=MoneyAmount.of(Decimal("10"), provenance=PROVENANCE),
                provenance=PROVENANCE,
            )
            for index in range(reservations)
        ),
    ).model_copy(update={"resources": ()} if not resources else {})


def _signal(signal_id: str = "ws-1") -> WasteSignal:
    return WasteSignal(
        signal_id=signal_id,
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        client="acme",
        subscription_id="sub-1",
        resource_id="/r/1",
        resource_name="disk-1",
        resource_kind="managed_disk",
        evidence=(Observation(name="state", value="unattached", provenance=PROVENANCE),),
        monthly_cost=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
        confidence=Decimal("0.95"),
        detector="test/v1",
        detected_at=AS_OF,
        provenance=PROVENANCE,
    )


def _root_cause(signal_id: str = "ws-1") -> RootCause:
    return RootCause(
        signal_id=signal_id,
        narrative="because",
        narrative_provenance=Provenance.calculated("test"),
        confidence=Decimal("0.9"),
        analysed_at=AS_OF,
    )


def _option(signal_id: str = "ws-1") -> PricedOption:
    return PricedOption(
        signal_id=signal_id,
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        savings=SavingsEstimate.undetermined("test"),
    )


def _recommendation(rec_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        signal_id="ws-1",
        client="acme",
        subscription_id="sub-1",
        target_resource_id="/r/1",
        target_resource_name="disk-1",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        risk_class=ActionClass.REVIEW,
        rationale="because",
        rationale_provenance=Provenance(source=ProvenanceSource.LLM_INFERENCE),
        preconditions=("x.",),
        savings=SavingsEstimate.undetermined("test"),
        confidence=Decimal("0.95"),
        rank=1,
    )


def _classification(
    rec_id: str = "rec-1", *, action_class: ActionClass = ActionClass.REVIEW
) -> ActionClassification:
    return ActionClassification(
        recommendation_id=rec_id,
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        action_class=action_class,
        floor_class=ActionClass.REVIEW,
        requires_approval=action_class.requires_approval,
        is_automatable=action_class.is_automatable,
        policy_reference="test",
        reason="test",
        classified_at=AS_OF,
        provenance=PROVENANCE,
    )


def _decide(settings: Settings, state: ScanState) -> ScanState:
    """Run the supervisor once and fold its update back into the state."""
    update = make_supervisor(settings=settings)(state)
    return state.model_copy(update=update)


# ---------------------------------------------------------------------------
# The straight-through path
# ---------------------------------------------------------------------------


def test_a_fresh_run_starts_with_discovery(settings: Settings) -> None:
    after = _decide(settings, ScanState(run_id="run-1", client="acme"))
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.PROCEED
    assert decision.to_node == "anomaly_scout"
    assert route_from_supervisor(after) == "anomaly_scout"


def test_signals_route_to_diagnosis(settings: Settings) -> None:
    state = ScanState(run_id="run-1", client="acme", estate=_estate(), signals=(_signal(),))
    after = _decide(settings, state)
    assert after.supervisor_decisions[-1].to_node == "root_cause_analyst"


def test_a_diagnosis_routes_to_pricing(settings: Settings) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
    )
    assert _decide(settings, state).supervisor_decisions[-1].to_node == "savings_estimator"


def test_priced_options_route_to_planning(settings: Settings) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
    )
    assert _decide(settings, state).supervisor_decisions[-1].to_node == "optimization_planner"


def test_recommendations_must_be_classified_before_anything_else(
    settings: Settings,
) -> None:
    """The Policy Guard is not optional; it is the mandatory next step."""
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
    )
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert decision.to_node == "policy_guard"
    assert "classified before anything is reported" in decision.reason


# ---------------------------------------------------------------------------
# Short circuit
# ---------------------------------------------------------------------------


def test_an_estate_with_no_waste_short_circuits_to_the_report(settings: Settings) -> None:
    """Running four nodes over an empty list produces the same empty report."""
    state = ScanState(run_id="run-1", client="acme", estate=_estate())
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert decision.action is RouteAction.SHORT_CIRCUIT
    assert decision.to_node == "report_author"
    assert "no waste detected" in decision.reason


def test_the_short_circuit_reason_counts_what_was_examined(settings: Settings) -> None:
    """A clean report must be able to say how much was looked at."""
    state = ScanState(run_id="run-1", client="acme", estate=_estate(reservations=2))
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert "2 reservation(s)" in decision.reason


# ---------------------------------------------------------------------------
# Retry and escalation
# ---------------------------------------------------------------------------


def test_a_missing_diagnosis_is_retried(settings: Settings) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        retries={"root_cause_analyst": 1},
    )
    after = _decide(settings, state)
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.RETRY
    assert decision.to_node == "root_cause_analyst"
    assert decision.attempt == 2
    assert after.retries["root_cause_analyst"] == 2


def test_retries_are_bounded_and_then_escalate(settings: Settings) -> None:
    """A second failure is a real problem for a human, not something to grind against."""
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        retries={"root_cause_analyst": MAX_ATTEMPTS},
    )
    after = _decide(settings, state)
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.ESCALATE
    # The run proceeds with what it has rather than dying.
    assert decision.to_node == "savings_estimator"
    assert "no diagnosis after" in decision.reason


def test_an_escalation_is_recorded_so_the_report_can_say_it_is_incomplete(
    settings: Settings,
) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        retries={"root_cause_analyst": MAX_ATTEMPTS},
    )
    after = _decide(settings, state)
    assert after.escalations
    assert "root_cause_analyst" in after.escalations[0]


def test_a_planner_that_produces_nothing_escalates_to_the_report(
    settings: Settings,
) -> None:
    """Findings without remediations are still worth reporting."""
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        retries={"optimization_planner": MAX_ATTEMPTS},
    )
    after = _decide(settings, state)
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.ESCALATE
    assert decision.to_node == "report_author"
    assert "without remediations" in decision.reason


# ---------------------------------------------------------------------------
# The approval seam
# ---------------------------------------------------------------------------


def test_a_gated_action_routes_through_the_approval_seam(settings: Settings) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
    )
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert decision.action is RouteAction.REQUIRE_APPROVAL
    assert "recorded human decision" in decision.reason
    assert "execution path in this phase" in decision.reason


def test_a_recorded_decision_clears_the_approval_seam(settings: Settings) -> None:
    """What Phase 7 will resume on: the gate is satisfied by a persisted decision."""
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
        decisions=(
            ApprovalDecision(
                recommendation_id="rec-1",
                decision=Decision.APPROVED,
                approver="ops@example.com",
                decided_at=AS_OF,
            ),
        ),
    )
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert decision.action is RouteAction.PROCEED
    assert decision.to_node == "report_author"


def test_an_allow_only_run_needs_no_approval(settings: Settings) -> None:
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(action_class=ActionClass.ALLOW),),
    )
    decision = _decide(settings, state).supervisor_decisions[-1]
    assert decision.action is RouteAction.PROCEED
    assert decision.to_node == "report_author"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def test_a_finished_run_completes(settings: Settings) -> None:
    from costsentinel.domain.report import (
        ClientReport,
        ReportPeriod,
        ReportSeverity,
        ReportTotals,
    )

    report = ClientReport(
        report_id="rpt-1",
        run_id="run-1",
        client="acme",
        period=ReportPeriod(start=AS_OF.date(), end=AS_OF.date(), days=1),
        subscriptions_in_scope=(),
        resources_examined=0,
        severity=ReportSeverity.NONE,
        executive_summary="done",
        summary_provenance=Provenance.calculated("test"),
        totals=ReportTotals(
            observed_monthly_spend=MoneyAmount.undetermined("n/a"),
            projected_monthly_savings=MoneyAmount.undetermined("n/a"),
            projected_annual_savings=MoneyAmount.undetermined("n/a"),
            findings_count=0,
            awaiting_approval_count=0,
            automatable_count=0,
        ),
        findings=(),
        approval_queue=(),
    )
    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(action_class=ActionClass.ALLOW),),
        report=report,
    )
    after = _decide(settings, state)
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.COMPLETE
    assert route_from_supervisor(after) == "__end__"


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------


def test_every_decision_states_a_reason(settings: Settings) -> None:
    """A retry or escalation that does not say why is indistinguishable from a bug."""
    states = [
        ScanState(run_id="run-1", client="acme"),
        ScanState(run_id="run-1", client="acme", estate=_estate()),
        ScanState(run_id="run-1", client="acme", estate=_estate(), signals=(_signal(),)),
    ]
    for state in states:
        decision = _decide(settings, state).supervisor_decisions[-1]
        assert decision.reason
        assert decision.from_node == "supervisor"


def test_decisions_accumulate_rather_than_replace(settings: Settings) -> None:
    state = ScanState(run_id="run-1", client="acme")
    after_first = _decide(settings, state)
    after_second = _decide(settings, after_first)
    assert len(after_second.supervisor_decisions) == 2


def test_routing_without_a_recorded_decision_falls_back_to_discovery() -> None:
    """Defensive: an unrouted state starts the sweep rather than dead-ending."""
    assert route_from_supervisor(ScanState(run_id="run-1", client="acme")) == "anomaly_scout"
