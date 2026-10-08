"""Full graph tests.

These are the ones that justify the whole slice: a real run, over the real nodes,
against the mock estate, producing a report that flags the known wasteful resources
with figures that match hand-computed arithmetic.

They also pin the two properties the architecture claims:

* no number on a client report has model provenance;
* every destructive action is classified ``review`` or ``block``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from costsentinel.config import Settings
from costsentinel.domain.common import ProvenanceSource
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.report import ClientReport, ReportSeverity
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import PHASE_1_NODES, build_graph
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import (
    ScanFailedError,
    checkpointed_nodes,
    clients_in_scope,
    load_checkpointed_state,
    new_run_id,
    run_client_scan,
    run_scan,
)
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, CLIENT_NORTHWIND, MockAzureProvider

# Hand-computed from the mock catalogue, so a drift in the arithmetic is a failure
# rather than a silently updated expectation.
EXPECTED_NORTHWIND = {
    "vm-analytics-01": Decimal("560.64"),  # D16s_v5 compute charge ceases
    "vm-batch-02": Decimal("367.92"),  # E16s_v5 (735.84) -> E8s_v5 (367.92)
    "disk-analytics-01-data": Decimal("38.42"),  # full disk cost
    "pip-legacy-api": Decimal("3.65"),  # full address cost
}
EXPECTED_NORTHWIND_MONTHLY = Decimal("970.63")
EXPECTED_MINISTRY_MONTHLY = Decimal("86.06")  # 76.84 x 1.12 qatarcentral multiplier


@pytest.fixture
def northwind_report(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> ClientReport:
    """A completed scan of the client with four findings."""
    return run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-test-northwind",
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_graph_has_the_phase_1_nodes(
    provider: MockAzureProvider, llm: FakeLLM, settings: Settings
) -> None:
    graph = build_graph(provider=provider, llm=llm, settings=settings)
    assert set(PHASE_1_NODES) <= set(graph.get_graph().nodes)


def test_graph_compiles_without_a_checkpointer(
    provider: MockAzureProvider, llm: FakeLLM, settings: Settings
) -> None:
    """Useful for a unit test of the node sequence; not how a real sweep runs."""
    graph = build_graph(provider=provider, llm=llm, settings=settings, checkpointer=None)
    final = graph.invoke(ScanState(run_id="run-nocp", client=CLIENT_MINISTRY))
    assert ScanState.model_validate(final).report is not None


# ---------------------------------------------------------------------------
# The findings
# ---------------------------------------------------------------------------


def test_report_flags_every_known_wasteful_resource(northwind_report: ClientReport) -> None:
    """Recall: the orphaned disk, the idle oversized VM and the unattached IP."""
    flagged = {f.resource_name for f in northwind_report.findings}
    assert flagged == set(EXPECTED_NORTHWIND)


def test_report_identifies_each_waste_kind_correctly(northwind_report: ClientReport) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    assert by_name["disk-analytics-01-data"].waste_kind is WasteKind.ORPHANED_MANAGED_DISK
    assert by_name["pip-legacy-api"].waste_kind is WasteKind.UNATTACHED_PUBLIC_IP
    assert by_name["vm-analytics-01"].waste_kind is WasteKind.IDLE_VIRTUAL_MACHINE
    assert by_name["vm-batch-02"].waste_kind is WasteKind.OVERSIZED_VIRTUAL_MACHINE


def test_report_proposes_the_right_action_for_each_finding(
    northwind_report: ClientReport,
) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    assert (
        by_name["disk-analytics-01-data"].proposed_action is ActionType.DELETE_ORPHANED_MANAGED_DISK
    )
    assert by_name["pip-legacy-api"].proposed_action is ActionType.DELETE_UNATTACHED_PUBLIC_IP
    assert by_name["vm-analytics-01"].proposed_action is ActionType.DEALLOCATE_VIRTUAL_MACHINE
    assert by_name["vm-batch-02"].proposed_action is ActionType.RESIZE_VIRTUAL_MACHINE


def test_report_does_not_flag_healthy_resources(northwind_report: ClientReport) -> None:
    """Precision: the twelve healthy resources must stay out of the report."""
    flagged = {f.resource_name for f in northwind_report.findings}
    for healthy in (
        "vm-web-01",
        "disk-web-01-os",
        "pip-web-01",
        "st-core-archive",
        "disk-batch-02-os",
        "sql-core-reporting",
        "disk-analytics-01-os",
        "st-dev-scratch",
    ):
        assert healthy not in flagged
    assert northwind_report.resources_examined > len(flagged)


def test_savings_match_hand_computed_arithmetic(northwind_report: ClientReport) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    for name, expected in EXPECTED_NORTHWIND.items():
        assert by_name[name].monthly_saving.amount == expected
        assert by_name[name].annual_saving.amount == expected * 12


def test_totals_sum_the_findings(northwind_report: ClientReport) -> None:
    totals = northwind_report.totals
    assert totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert totals.projected_annual_savings.amount == EXPECTED_NORTHWIND_MONTHLY * 12
    assert totals.findings_count == len(EXPECTED_NORTHWIND)
    assert totals.observed_monthly_spend.is_known


def test_findings_are_ranked_by_savings(northwind_report: ClientReport) -> None:
    amounts = [
        f.monthly_saving.amount
        for f in northwind_report.findings
        if f.monthly_saving.amount is not None
    ]
    assert amounts == sorted(amounts, reverse=True)
    assert [f.rank for f in northwind_report.findings] == [1, 2, 3, 4]
    assert northwind_report.findings[0].resource_name == "vm-analytics-01"


def test_each_finding_carries_its_evidence_and_preconditions(
    northwind_report: ClientReport,
) -> None:
    for finding in northwind_report.findings:
        assert finding.evidence
        assert finding.preconditions
        assert finding.rationale
        assert finding.savings_basis
        assert finding.current_monthly_cost.is_known


def test_resize_savings_are_marked_estimated_and_deletions_are_not(
    northwind_report: ClientReport,
) -> None:
    """A modelled figure must be distinguishable from an observed one."""
    by_name = {f.resource_name: f for f in northwind_report.findings}
    assert by_name["vm-batch-02"].savings_is_estimated
    assert not by_name["disk-analytics-01-data"].savings_is_estimated


def test_severity_reflects_savings_against_spend(northwind_report: ClientReport) -> None:
    assert northwind_report.severity is ReportSeverity.HIGH


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_every_destructive_action_is_gated(northwind_report: ClientReport) -> None:
    """Safety recall: nothing destructive is classified ``allow``."""
    for finding in northwind_report.findings:
        assert finding.action_class is not ActionClass.ALLOW
        assert finding.requires_approval


def test_gated_findings_appear_in_the_approval_queue(northwind_report: ClientReport) -> None:
    queued = {item.recommendation_id for item in northwind_report.approval_queue}
    gated = {f.recommendation_id for f in northwind_report.findings if f.requires_approval}
    assert queued == gated
    assert northwind_report.totals.awaiting_approval_count == len(gated)


def test_production_deletion_is_blocked_not_merely_reviewed(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """The government production orphan has no execution path at all."""
    report = run_client_scan(
        client=CLIENT_MINISTRY,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-test-ministry",
    )
    finding = report.findings[0]
    assert finding.resource_name == "disk-portal-legacy-snapshot"
    assert finding.action_class is ActionClass.BLOCK
    assert not finding.action_class.is_automatable
    assert report.totals.automatable_count == 0
    assert report.totals.projected_monthly_savings.amount == EXPECTED_MINISTRY_MONTHLY
    assert "will not execute it" in report.approval_queue[0].reason


def test_report_states_that_nothing_was_changed(northwind_report: ClientReport) -> None:
    assert any("read-only" in note for note in northwind_report.notes)


# ---------------------------------------------------------------------------
# No invented numbers
# ---------------------------------------------------------------------------


def test_no_number_on_the_report_has_model_provenance(
    northwind_report: ClientReport,
) -> None:
    """The Phase 5 cost-figure-accuracy metric, asserted end to end now."""
    amounts = [
        northwind_report.totals.observed_monthly_spend,
        northwind_report.totals.projected_monthly_savings,
        northwind_report.totals.projected_annual_savings,
    ]
    for finding in northwind_report.findings:
        amounts += [finding.current_monthly_cost, finding.monthly_saving, finding.annual_saving]

    for amount in amounts:
        assert amount.provenance.source is not ProvenanceSource.LLM_INFERENCE
        assert not amount.provenance.is_model_derived


def test_the_prose_is_model_derived_and_labelled_as_such(
    northwind_report: ClientReport,
) -> None:
    """Prose may come from a model -- provided the report says so."""
    assert northwind_report.summary_provenance.source is ProvenanceSource.LLM_INFERENCE
    assert northwind_report.executive_summary


def test_the_summary_quotes_the_computed_totals(northwind_report: ClientReport) -> None:
    """Groundedness: the prose cites the arithmetic rather than restating it."""
    summary = northwind_report.executive_summary
    assert northwind_report.totals.projected_monthly_savings.display() in summary
    assert northwind_report.totals.projected_annual_savings.display() in summary
    assert northwind_report.totals.observed_monthly_spend.display() in summary


# ---------------------------------------------------------------------------
# Multi-tenancy
# ---------------------------------------------------------------------------


def test_a_report_never_spans_clients(northwind_report: ClientReport) -> None:
    assert northwind_report.client == CLIENT_NORTHWIND
    assert len(northwind_report.subscriptions_in_scope) == 2
    for finding in northwind_report.findings:
        assert finding.subscription_id in northwind_report.subscriptions_in_scope


def test_a_sweep_runs_each_client_as_its_own_run(settings: Settings) -> None:
    result = run_scan(settings=settings)
    assert result.clients == (CLIENT_MINISTRY, CLIENT_NORTHWIND)
    assert len(set(result.run_ids)) == len(result.reports)
    assert result.total_findings == 5
    assert result.total_awaiting_approval == 5
    assert result.for_client(CLIENT_NORTHWIND) is not None
    assert result.for_client("nobody") is None


def test_a_sweep_can_be_narrowed_to_one_client(settings: Settings) -> None:
    result = run_scan(client=CLIENT_MINISTRY, settings=settings)
    assert result.clients == (CLIENT_MINISTRY,)


def test_an_unknown_client_produces_an_empty_report(settings: Settings) -> None:
    """Zero findings is a valid outcome, not an error."""
    result = run_scan(client="no-such-client", settings=settings)
    report = result.reports[0]
    assert report.findings == ()
    assert report.severity is ReportSeverity.NONE
    assert not report.totals.observed_monthly_spend.is_known
    assert "no actionable waste" in report.executive_summary


def test_clients_in_scope_is_sorted_and_deduplicated(provider: MockAzureProvider) -> None:
    assert clients_in_scope(provider) == (CLIENT_MINISTRY, CLIENT_NORTHWIND)
    assert clients_in_scope(provider, CLIENT_NORTHWIND) == (CLIENT_NORTHWIND,)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_every_node_contributes_to_the_audit_trail(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-audit",
    )
    state = load_checkpointed_state(
        run_id="run-audit",
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
    )
    assert state is not None

    actors = {event.actor for event in state.audit}
    assert actors == set(PHASE_1_NODES)

    kinds = {event.event_type for event in state.audit}
    assert AuditEventType.ESTATE_READ in kinds
    assert AuditEventType.SIGNAL_DETECTED in kinds
    assert AuditEventType.RECOMMENDATION_PROPOSED in kinds
    assert AuditEventType.ACTION_CLASSIFIED in kinds
    assert AuditEventType.APPROVAL_REQUESTED in kinds
    assert AuditEventType.REPORT_GENERATED in kinds
    assert AuditEventType.SCAN_COMPLETED in kinds

    assert all(event.run_id == "run-audit" for event in state.audit)
    assert len({event.event_id for event in state.audit}) == len(state.audit)


def test_a_clean_run_records_no_errors(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-clean",
    )
    state = load_checkpointed_state(
        run_id="run-clean",
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
    )
    assert state is not None
    assert state.errors == ()


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_state_is_persisted_and_readable_from_a_fresh_connection(
    provider: MockAzureProvider, llm: FakeLLM, settings: Settings
) -> None:
    """Resumability, demonstrated rather than merely configured."""
    run_id = "run-durable"
    with sqlite_checkpointer(settings.checkpoint_path) as writer:
        run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=provider,
            llm=llm,
            settings=settings,
            checkpointer=writer,
            run_id=run_id,
        )

    assert settings.checkpoint_path.exists()

    # A new process would open a new connection; so does this.
    with sqlite_checkpointer(settings.checkpoint_path) as reader:
        state = load_checkpointed_state(
            run_id=run_id,
            provider=provider,
            llm=llm,
            settings=settings,
            checkpointer=reader,
        )
    assert state is not None
    assert state.report is not None
    assert state.report.client == CLIENT_NORTHWIND
    assert len(state.report.findings) == len(EXPECTED_NORTHWIND)


def test_every_node_writes_a_checkpoint(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """What a mid-sweep crash depends on: a checkpoint after each node."""
    run_id = "run-checkpoints"
    run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id=run_id,
    )
    written = checkpointed_nodes(
        run_id=run_id,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
    )
    assert set(PHASE_1_NODES) <= set(written)


def test_unknown_run_has_no_checkpointed_state(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    assert (
        load_checkpointed_state(
            run_id="run-never-happened",
            provider=provider,
            llm=llm,
            settings=settings,
            checkpointer=checkpointer,
        )
        is None
    )


def test_run_ids_are_unique() -> None:
    assert new_run_id() != new_run_id()
    assert new_run_id().startswith("run-")


# ---------------------------------------------------------------------------
# Determinism and resilience
# ---------------------------------------------------------------------------


def test_two_runs_of_the_same_estate_agree(settings: Settings) -> None:
    """Same provider seed, same fake model, same findings and figures."""

    def figures() -> list[tuple[str, Decimal | None, str]]:
        result = run_scan(client=CLIENT_NORTHWIND, settings=settings)
        return [
            (f.resource_name, f.monthly_saving.amount, f.proposed_action.value)
            for f in result.reports[0].findings
        ]

    assert figures() == figures()


def test_a_model_proposing_an_unoffered_action_is_rejected(
    provider: MockAzureProvider, settings: Settings, checkpointer: BaseCheckpointSaver[str]
) -> None:
    """Defence in depth: a valid-but-not-offered action must not slip through."""
    from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT
    from costsentinel.llm.contracts import PlannedRemediation, RemediationPlan

    class HostileLLM(FakeLLM):
        """Proposes an action that is in the enum but not among the candidates."""

        def structured(
            self,
            *,
            task: LLMTask,
            prompt: Prompt,
            schema: type[StructuredResponseT],
        ) -> StructuredResponseT:
            response = super().structured(task=task, prompt=prompt, schema=schema)
            if isinstance(response, RemediationPlan):
                hijacked = RemediationPlan(
                    items=tuple(
                        PlannedRemediation(
                            signal_id=item.signal_id,
                            action=ActionType.DELETE_UNCONFIRMED_RESOURCE,
                            rationale="trust me",
                            priority=item.priority,
                        )
                        for item in response.items
                    )
                )
                return schema.model_validate(hijacked.model_dump())
            return response

    report = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=HostileLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-hostile",
    )

    # Every action fell back to the policy's preferred one; none is the hijacked one.
    for finding in report.findings:
        assert finding.proposed_action is not ActionType.DELETE_UNCONFIRMED_RESOURCE
    assert any("not among the permitted candidates" in note for note in report.notes) or any(
        "processing note" in note for note in report.notes
    )


def test_a_model_omitting_a_signal_falls_back_to_policy(
    provider: MockAzureProvider, settings: Settings, checkpointer: BaseCheckpointSaver[str]
) -> None:
    """One bad response costs a rationale, not a whole sweep."""
    from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT
    from costsentinel.llm.contracts import RemediationPlan

    class SilentLLM(FakeLLM):
        """Returns an empty plan."""

        def structured(
            self,
            *,
            task: LLMTask,
            prompt: Prompt,
            schema: type[StructuredResponseT],
        ) -> StructuredResponseT:
            if schema is RemediationPlan:
                return schema.model_validate(RemediationPlan(items=()).model_dump())
            return super().structured(task=task, prompt=prompt, schema=schema)

    report = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=SilentLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-silent",
    )
    # Findings and figures survive; only the rationales are the policy's own.
    assert len(report.findings) == len(EXPECTED_NORTHWIND)
    assert report.totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert all("policy store's preferred remediation" in f.rationale for f in report.findings)


def test_a_graph_that_sets_no_report_leaves_state_report_none(settings: Settings) -> None:
    """Groundwork for the check below: "no report" is a real, reachable state."""
    from langgraph.graph import END, START, StateGraph

    _ = settings

    def noop(state: ScanState) -> dict[str, tuple[str, ...]]:
        return {"errors": (*state.errors, "did nothing")}

    graph: StateGraph[ScanState, None, ScanState, ScanState] = StateGraph(ScanState)
    graph.add_node("noop", noop)
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)

    final = graph.compile().invoke(ScanState(run_id="run-empty", client=CLIENT_NORTHWIND))
    state = ScanState.model_validate(final)
    assert state.report is None
    assert state.errors == ("did nothing",)


def test_runner_raises_when_a_run_produces_no_report(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing report is a failure, distinct from a report with zero findings."""
    from langgraph.graph import END, START, StateGraph

    # LangGraph may call a node by keyword, so the parameter must be named "state".
    def no_report(state: ScanState) -> dict[str, tuple[str, ...]]:
        return {"errors": (*state.errors, "node produced no report")}

    def build_reportless(**_kwargs: object) -> object:
        graph: StateGraph[ScanState, None, ScanState, ScanState] = StateGraph(ScanState)
        graph.add_node("noop", no_report)
        graph.add_edge(START, "noop")
        graph.add_edge("noop", END)
        return graph.compile()

    monkeypatch.setattr("costsentinel.graph.runner.build_graph", build_reportless)

    with pytest.raises(ScanFailedError, match="completed without a"):
        run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=provider,
            llm=llm,
            settings=settings,
            checkpointer=checkpointer,
            run_id="run-reportless",
        )


def test_a_backend_failure_propagates_rather_than_producing_a_partial_report(
    provider: MockAzureProvider,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """A model outage must not yield a report missing half its findings."""
    from costsentinel.llm.base import LLMError, LLMTask, Prompt, StructuredResponseT

    class BrokenLLM(FakeLLM):
        def structured(
            self,
            *,
            task: LLMTask,
            prompt: Prompt,
            schema: type[StructuredResponseT],
        ) -> StructuredResponseT:
            _ = (task, prompt, schema)
            msg = "backend exploded"
            raise LLMError(msg)

    with pytest.raises(LLMError, match="backend exploded"):
        run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=provider,
            llm=BrokenLLM(),
            settings=settings,
            checkpointer=checkpointer,
            run_id="run-broken",
        )


def test_a_custom_policy_store_is_honoured(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """Policy is injectable, which is how Phase 3 adds client overlays."""

    class StrictPolicy(PolicyStore):
        """Tightens everything to block."""

        @property
        def name(self) -> str:
            return "strict-test-policy"

        def classify(
            self, action: ActionType, *, environment: object = None
        ) -> tuple[ActionClass, str]:  # type: ignore[override]
            _ = (action, environment)
            return ActionClass.BLOCK, "test policy blocks everything"

    report = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        policy=StrictPolicy(),
        run_id="run-strict",
    )
    assert all(f.action_class is ActionClass.BLOCK for f in report.findings)
    assert report.totals.automatable_count == 0
