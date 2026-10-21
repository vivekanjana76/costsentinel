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
from costsentinel.domain.report import (
    ClientReport,
    ReportFinding,
    ReportPeriod,
    ReportSeverity,
    ReportTotals,
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


def _report_with_gated_finding(rec_id: str = "rec-1") -> ClientReport:
    """A minimal report whose approval queue has one gated item.

    The approval gate reads its queue from the *report* rather than recomputing it, so
    a state that is "classified and gated" only halts once there is a document showing
    the client the same figures the approver will see.
    """
    finding = ReportFinding(
        rank=1,
        recommendation_id=rec_id,
        signal_id="ws-1",
        waste_kind=WasteKind.ORPHANED_MANAGED_DISK,
        subscription_id="sub-1",
        resource_id="/r/1",
        resource_name="disk-1",
        current_monthly_cost=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
        proposed_action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        action_class=ActionClass.REVIEW,
        requires_approval=True,
        confidence=Decimal("0.95"),
        rationale="because",
        monthly_saving=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
        annual_saving=MoneyAmount.of(Decimal("461.04"), provenance=PROVENANCE),
        savings_basis="the whole charge ceases",
        savings_is_estimated=False,
    )
    return ClientReport(
        report_id="rpt-run-1",
        run_id="run-1",
        client="acme",
        period=ReportPeriod(start=AS_OF.date(), end=AS_OF.date(), days=1),
        subscriptions_in_scope=("sub-1",),
        resources_examined=1,
        severity=ReportSeverity.LOW,
        executive_summary="one finding awaits a decision",
        summary_provenance=Provenance(source=ProvenanceSource.LLM_INFERENCE),
        totals=ReportTotals(
            observed_monthly_spend=MoneyAmount.undetermined("n/a"),
            projected_monthly_savings=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
            projected_annual_savings=MoneyAmount.of(Decimal("461.04"), provenance=PROVENANCE),
            findings_count=1,
            awaiting_approval_count=1,
            automatable_count=0,
        ),
        findings=(finding,),
        approval_queue=(),
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


def test_a_gated_action_is_reported_first_and_then_gated(settings: Settings) -> None:
    """Two steps from Phase 10, and the order is the point.

    The report is written *before* the approval gate (D119). A client waiting on their
    cost review must not be waiting on their own approval of the things in it, and the
    document is correct and complete either way -- so the gated state routes onward to
    the report author, and only the state that already has a report halts.

    Before Phase 10 this routed straight to the report author with `REQUIRE_APPROVAL`
    and the honest reason that nothing had an execution path anyway. The route existed
    and halted nothing.
    """
    gated = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
        memory_consulted=True,
    )
    first = _decide(settings, gated).supervisor_decisions[-1]
    assert first.action is RouteAction.PROCEED
    assert first.to_node == "report_author"
    assert "awaiting a decision" in first.reason


def test_a_gated_action_halts_once_the_report_exists(settings: Settings) -> None:
    """The halt itself: a durable pause, not a note in a document."""
    with_report = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
        memory_consulted=True,
        report=_report_with_gated_finding(),
    )
    after = _decide(settings, with_report)
    decision = after.supervisor_decisions[-1]
    assert decision.action is RouteAction.REQUIRE_APPROVAL
    assert decision.to_node == "approval_gate"
    assert route_from_supervisor(after) == "approval_gate"
    assert "recorded human decision" in decision.reason
    assert "durable checkpoint" in decision.reason
    # What approval means, stated in the routing reason an operator reads in the log.
    assert "no execution path" in decision.reason


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
        memory_consulted=True,
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


def test_a_recorded_decision_lets_a_paused_sweep_finish(settings: Settings) -> None:
    """The resume condition, read off the state the gate writes.

    A sweep halts while anything gated is undecided and completes once every gated
    item has a *resolving* record. A deferral is not one -- see
    `test_a_deferral_leaves_the_sweep_waiting`.
    """
    from costsentinel.domain.governance import ApprovalRecord, ApprovalState, FrozenFigures

    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
        memory_consulted=True,
        report=_report_with_gated_finding(),
        approval_records=(
            ApprovalRecord(
                record_id="apr-1",
                run_id="run-1",
                recommendation_id="rec-1",
                client="acme",
                subscription_id="sub-1",
                target_resource_id="/r/1",
                target_resource_name="disk-1",
                action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
                action_class=ActionClass.REVIEW,
                decision=Decision.APPROVED,
                state=ApprovalState.HANDED_OFF,
                approver="ops@example.com",
                reason="agreed in the Tuesday review",
                figures=FrozenFigures(
                    monthly_saving_display="10.00 USD",
                    annual_saving_display="120.00 USD",
                    current_monthly_cost_display="10.00 USD",
                ),
                provenance=PROVENANCE,
            ),
        ),
    )
    after = _decide(settings, state)
    assert after.supervisor_decisions[-1].action is RouteAction.COMPLETE
    assert route_from_supervisor(after) == "__end__"


def test_a_deferral_leaves_the_sweep_waiting(settings: Settings) -> None:
    """A deferral is a recorded answer that resolves nothing.

    Which is the whole difference between a deferral and a rejection, and the reason
    the supervisor reads the approval records rather than the decisions tuple.
    """
    from costsentinel.domain.governance import ApprovalRecord, ApprovalState, FrozenFigures

    state = ScanState(
        run_id="run-1",
        client="acme",
        estate=_estate(),
        signals=(_signal(),),
        root_causes=(_root_cause(),),
        priced_options=(_option(),),
        recommendations=(_recommendation(),),
        classifications=(_classification(),),
        memory_consulted=True,
        report=_report_with_gated_finding(),
        approval_records=(
            ApprovalRecord(
                record_id="apr-1",
                run_id="run-1",
                recommendation_id="rec-1",
                client="acme",
                subscription_id="sub-1",
                target_resource_id="/r/1",
                target_resource_name="disk-1",
                action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
                action_class=ActionClass.REVIEW,
                decision=Decision.DEFERRED,
                state=ApprovalState.DEFERRED,
                approver="ops@example.com",
                reason="revisit after the quarter close",
                figures=FrozenFigures(
                    monthly_saving_display="10.00 USD",
                    annual_saving_display="120.00 USD",
                    current_monthly_cost_display="10.00 USD",
                ),
                provenance=PROVENANCE,
            ),
        ),
    )
    after = _decide(settings, state)
    assert after.supervisor_decisions[-1].action is RouteAction.REQUIRE_APPROVAL
    assert route_from_supervisor(after) == "approval_gate"


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
        memory_consulted=True,
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
        memory_consulted=True,
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
