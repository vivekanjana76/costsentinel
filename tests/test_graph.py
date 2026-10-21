"""Full graph tests.

These are the ones that justify the whole slice: a real run, over the real nodes,
against the mock estate, producing a report that flags the known wasteful resources
with figures that match hand-computed arithmetic.

They also pin the properties the architecture claims:

* no number on a client report has model provenance;
* per-client totals equal the sum of their findings;
* every destructive action is classified ``review`` or ``block``;
* the ranking is deterministic and a hostile model cannot change it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from costsentinel.config import Settings
from costsentinel.domain.common import ProvenanceSource
from costsentinel.domain.governance import AuditEventType, RouteAction
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.report import ClientReport, ReportSeverity
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import (
    ALL_NODES,
    NODE_APPROVAL_GATE,
    SWEEP_NODES,
    build_graph,
)
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import (
    ScanFailedError,
    checkpointed_nodes,
    clients_in_scope,
    load_checkpointed_state,
    new_run_id,
    run_client_scan,
    run_scan,
    state_from,
)
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, CLIENT_NORTHWIND, MockAzureProvider

# Hand-computed from the committed price catalogue, which since Phase 3 holds real
# Azure Retail Prices figures. A drift in the arithmetic is a failure rather than a
# silently updated expectation; a drift in Azure's prices is a deliberate re-baseline
# after running scripts/refresh_prices.py.
#
# eastus rates: D16s_v5 560.64, E16s_v5 735.84, E8s_v5 367.92, GP_Gen5_4 444.47,
# GP_Gen5_2 222.24, P15 disk 38.01, Standard_Static 3.65, snapshot 0.132/GB,
# D4s_v5 3-year reserved 55.36/instance/month.
EXPECTED_NORTHWIND: dict[str, Decimal] = {
    "vm-analytics-01": Decimal("560.64"),  # D16s_v5 compute charge ceases
    "vm-batch-02": Decimal("367.92"),  # E16s_v5 (735.84) -> E8s_v5 (367.92)
    "sql-dev-sandbox": Decimal("222.23"),  # GP_Gen5_4 (444.47) -> GP_Gen5_2 (222.24)
    "nw-compute-dsv5-3y": Decimal("145.04"),  # 55.36 x 4 = 221.44, x (100-34.5)% unused
    "snap-web-01-pre-upgrade": Decimal("67.58"),  # 0.132/GB x 512 GB
    "disk-analytics-01-data": Decimal("38.01"),  # full P15 disk cost
    "snap-analytics-baseline": Decimal("16.90"),  # 0.132/GB x 128 GB
    "pip-legacy-api": Decimal("3.65"),  # full address cost
}
EXPECTED_NORTHWIND_MONTHLY = Decimal("1421.97")

#: Flagged, and worth nothing to the total. Added to the estate in Phase 9 because it
#: had no resource that exercised either of these paths, so two shipped features
#: appeared in no rendered report (#76).
#:
#: Both are named here rather than folded into the map above, because neither carries a
#: saving and the map's whole job is to pin savings. `vm-core-api-02` is oversized in
#: the band where no same-family SKU fits its peak with 2.0x headroom, so its saving is
#: honestly unknown. `vm-core-dr-standby` is idle and tagged as a standby, so it is
#: surfaced and no action is proposed.
EXPECTED_NORTHWIND_UNPRICED = ("vm-core-api-02",)
EXPECTED_NORTHWIND_EXEMPT = ("vm-core-dr-standby",)

#: Every finding, priced or not. The exempt resource is deliberately absent: it is not
#: a finding, it is a signal that was withheld.
EXPECTED_NORTHWIND_FINDINGS = tuple(EXPECTED_NORTHWIND) + EXPECTED_NORTHWIND_UNPRICED

# qatarcentral rates: P1v3 139.43, P0v3 69.72, P20 disk 79.87.
EXPECTED_MINISTRY: dict[str, Decimal] = {
    "disk-portal-legacy-snapshot": Decimal("79.87"),
    "app-portal-plan": Decimal("69.71"),  # P1v3 (139.43) -> P0v3 (69.72)
}
EXPECTED_MINISTRY_MONTHLY = Decimal("149.58")

#: Four model calls per client: root cause, planning, ranking rationale, report prose.
EXPECTED_LLM_CALLS_PER_CLIENT = 4


@pytest.fixture
def northwind_report(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> ClientReport:
    """A completed scan of the client with eight findings."""
    report, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-test-northwind",
    )
    return report


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_graph_has_the_six_specialists_a_supervisor_and_the_consult_step(
    provider: MockAzureProvider, llm: FakeLLM, settings: Settings
) -> None:
    """Memory Consult and the approval gate join the six, and are not among them.

    CLAUDE.md section 5 names six specialists. The consult step reorders and annotates
    what they produced; the approval gate (Phase 10) halts the sweep until a person
    decides. Both are support nodes, so the count asserted here is six plus two rather
    than eight specialists -- and it is asserted as a number so adding a node is a
    deliberate change rather than a silent one.
    """
    graph = build_graph(provider=provider, llm=llm, settings=settings)
    nodes = set(graph.get_graph().nodes)
    assert set(ALL_NODES) <= nodes
    assert len(SWEEP_NODES) == 8
    assert NODE_APPROVAL_GATE in SWEEP_NODES
    assert "memory_consult" in SWEEP_NODES
    # Six specialists plus one support node, which is why the tuple is no longer
    # called SPECIALIST_NODES.
    # Six specialists plus two support nodes: the memory consult step and the
    # Phase 10 approval gate.
    assert len(SWEEP_NODES) - 2 == 6
    assert "supervisor" in nodes


def test_graph_compiles_without_a_checkpointer(
    provider: MockAzureProvider, llm: FakeLLM, settings: Settings
) -> None:
    """Useful for a unit test of the node sequence; not how a real sweep runs."""
    graph = build_graph(provider=provider, llm=llm, settings=settings, checkpointer=None)
    final = graph.invoke(ScanState(run_id="run-nocp", client=CLIENT_MINISTRY))
    # `state_from` rather than `model_validate`, because a sweep with gated items
    # halts at the approval gate and returns LangGraph's `__interrupt__` key
    # alongside the state. A graph compiled without a checkpointer cannot persist
    # that pause, which is exactly why this configuration is documented as useful
    # for asserting the node sequence and not for running a sweep.
    assert state_from(final).report is not None


# ---------------------------------------------------------------------------
# The findings
# ---------------------------------------------------------------------------


def test_report_flags_every_known_wasteful_resource(northwind_report: ClientReport) -> None:
    """Recall, across all the waste kinds the estate contains."""
    flagged = {f.resource_name for f in northwind_report.findings}
    assert flagged == set(EXPECTED_NORTHWIND_FINDINGS)


def test_report_identifies_each_waste_kind_correctly(northwind_report: ClientReport) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    assert by_name["disk-analytics-01-data"].waste_kind is WasteKind.ORPHANED_MANAGED_DISK
    assert by_name["pip-legacy-api"].waste_kind is WasteKind.UNATTACHED_PUBLIC_IP
    assert by_name["vm-analytics-01"].waste_kind is WasteKind.IDLE_VIRTUAL_MACHINE
    assert by_name["vm-batch-02"].waste_kind is WasteKind.OVERSIZED_VIRTUAL_MACHINE
    assert by_name["snap-web-01-pre-upgrade"].waste_kind is WasteKind.STALE_SNAPSHOT
    assert by_name["snap-analytics-baseline"].waste_kind is WasteKind.STALE_SNAPSHOT
    assert by_name["sql-dev-sandbox"].waste_kind is WasteKind.IDLE_SQL_DATABASE
    assert by_name["nw-compute-dsv5-3y"].waste_kind is WasteKind.UNUSED_RESERVATION


def test_report_proposes_the_right_action_for_each_finding(
    northwind_report: ClientReport,
) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    expected = {
        "disk-analytics-01-data": ActionType.DELETE_ORPHANED_MANAGED_DISK,
        "pip-legacy-api": ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        "vm-analytics-01": ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        "vm-batch-02": ActionType.RESIZE_VIRTUAL_MACHINE,
        "snap-web-01-pre-upgrade": ActionType.DELETE_STALE_SNAPSHOT,
        "snap-analytics-baseline": ActionType.DELETE_STALE_SNAPSHOT,
        "sql-dev-sandbox": ActionType.SCALE_DOWN_SQL_DATABASE,
        "nw-compute-dsv5-3y": ActionType.EXCHANGE_UNUSED_RESERVATION,
    }
    for name, action in expected.items():
        assert by_name[name].proposed_action is action


def test_report_does_not_flag_healthy_resources(northwind_report: ClientReport) -> None:
    """Precision: the healthy resources must stay out of the report."""
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
        "snap-analytics-nightly",
        "nw-sql-gen5-1y",
    ):
        assert healthy not in flagged
    assert northwind_report.resources_examined > len(flagged)


def test_savings_match_hand_computed_arithmetic(northwind_report: ClientReport) -> None:
    by_name = {f.resource_name: f for f in northwind_report.findings}
    for name, expected in EXPECTED_NORTHWIND.items():
        assert by_name[name].monthly_saving.amount == expected
        assert by_name[name].annual_saving.amount == expected * 12


def test_totals_equal_the_sum_of_the_findings(northwind_report: ClientReport) -> None:
    """Per-client totals are summed, never model output."""
    totals = northwind_report.totals
    summed = sum(
        (f.monthly_saving.amount for f in northwind_report.findings if f.monthly_saving.amount),
        start=Decimal(0),
    )
    assert totals.projected_monthly_savings.amount == summed
    assert totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert totals.projected_annual_savings.amount == EXPECTED_NORTHWIND_MONTHLY * 12
    assert totals.findings_count == len(EXPECTED_NORTHWIND_FINDINGS)
    assert totals.unpriced_findings_count == len(EXPECTED_NORTHWIND_UNPRICED)
    # The point of both new sections: neither adds a penny to the headline.
    assert totals.exempt_findings_count == len(EXPECTED_NORTHWIND_EXEMPT)
    assert totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert totals.observed_monthly_spend.is_known


def test_waste_breakdown_groups_and_sums_by_kind(northwind_report: ClientReport) -> None:
    breakdown = {b.waste_kind: b for b in northwind_report.waste_breakdown}
    assert len(breakdown) == 7  # eight findings, two of which are stale snapshots

    snapshots = breakdown[WasteKind.STALE_SNAPSHOT]
    assert snapshots.findings_count == 2
    assert snapshots.monthly_saving.amount == Decimal("67.58") + Decimal("16.90")

    # Ordered with the biggest category first.
    amounts = [
        b.monthly_saving.amount
        for b in northwind_report.waste_breakdown
        if b.monthly_saving.amount is not None
    ]
    assert amounts == sorted(amounts, reverse=True)


def test_breakdown_total_matches_the_report_total(northwind_report: ClientReport) -> None:
    summed = sum(
        (
            b.monthly_saving.amount
            for b in northwind_report.waste_breakdown
            if b.monthly_saving.amount
        ),
        start=Decimal(0),
    )
    assert summed == northwind_report.totals.projected_monthly_savings.amount


def test_each_finding_carries_everything_needed_to_dispute_it(
    northwind_report: ClientReport,
) -> None:
    for finding in northwind_report.findings:
        assert finding.evidence
        assert finding.preconditions
        assert finding.rationale
        assert finding.savings_basis
        assert finding.root_cause, f"{finding.resource_name} has no root cause"
        assert finding.root_cause_factors
        assert finding.ranking_breakdown
        assert finding.ranking_composite is not None
        assert finding.current_monthly_cost.is_known
        assert Decimal(0) < finding.confidence <= Decimal(1)


def test_resize_savings_are_marked_estimated_and_deletions_are_not(
    northwind_report: ClientReport,
) -> None:
    """A modelled figure must be distinguishable from an observed one."""
    by_name = {f.resource_name: f for f in northwind_report.findings}
    assert by_name["vm-batch-02"].savings_is_estimated
    assert by_name["sql-dev-sandbox"].savings_is_estimated
    assert by_name["nw-compute-dsv5-3y"].savings_is_estimated
    assert not by_name["disk-analytics-01-data"].savings_is_estimated
    assert not by_name["snap-web-01-pre-upgrade"].savings_is_estimated


def test_severity_reflects_savings_against_spend(northwind_report: ClientReport) -> None:
    assert northwind_report.severity is ReportSeverity.HIGH


def test_report_is_complete_on_a_clean_run(northwind_report: ClientReport) -> None:
    assert northwind_report.is_complete
    assert northwind_report.incomplete_reasons == ()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_findings_are_ranked_by_the_composite_not_by_savings(
    northwind_report: ClientReport,
) -> None:
    """The ranking's whole point: savings alone is the wrong order.

    Descending *within a tier*, not globally. The tier comes first (D104), so a
    quantified finding outranks an unquantified one whatever their composites say --
    which is why the global claim this test used to make held only while the mock
    estate happened to contain nothing unquantified.
    """
    priced = [f for f in northwind_report.findings if f.monthly_saving.is_known]
    composites = [f.ranking_composite for f in priced if f.ranking_composite is not None]
    assert composites == sorted(composites, reverse=True)
    assert [f.rank for f in northwind_report.findings] == list(
        range(1, len(EXPECTED_NORTHWIND_FINDINGS) + 1)
    )

    by_name = {f.resource_name: f.rank for f in northwind_report.findings}
    # A smaller but far more certain saving outranks a larger, less certain one.
    assert EXPECTED_NORTHWIND["pip-legacy-api"] < EXPECTED_NORTHWIND["snap-analytics-baseline"]
    assert by_name["pip-legacy-api"] < by_name["snap-analytics-baseline"]


def test_an_unquantified_finding_ranks_below_every_priced_one(
    northwind_report: ClientReport,
) -> None:
    """D104 end to end, which nothing checked until the estate could produce one.

    The unquantified finding's composite is *higher* than the lowest priced finding's,
    so if the tier were a weight rather than a sort key it would outrank it. A FinOps
    tool must not assert that something it cannot cost is the bigger opportunity.
    """
    unpriced = [f for f in northwind_report.findings if not f.monthly_saving.is_known]
    priced = [f for f in northwind_report.findings if f.monthly_saving.is_known]
    assert {f.resource_name for f in unpriced} == set(EXPECTED_NORTHWIND_UNPRICED)

    assert min(f.rank for f in unpriced) > max(f.rank for f in priced)

    # And the ordering is not merely a coincidence of the composites.
    worst_priced = min(f.ranking_composite or Decimal(0) for f in priced)
    best_unpriced = max(f.ranking_composite or Decimal(0) for f in unpriced)
    assert best_unpriced > worst_priced, (
        "this assertion is vacuous unless an unquantified finding scores higher than a "
        "priced one; pick a different fixture if the estate changes"
    )


def test_a_blocked_action_cannot_lead_the_report(northwind_report: ClientReport) -> None:
    """A client's first move should not be the one CostSentinel refuses to perform."""
    blocked = [f for f in northwind_report.findings if f.action_class is ActionClass.BLOCK]
    assert blocked
    for finding in blocked:
        assert finding.rank > 1
    # The production snapshot is worth more than four higher-ranked findings, and
    # still ranks below every other priced finding because it is blocked. Last among
    # the priced ones, not last overall: the unquantified finding sits below it on
    # tier, which is a separate rule (D104) with its own test.
    snapshot = next(f for f in blocked if f.resource_name == "snap-web-01-pre-upgrade")
    priced = [f for f in northwind_report.findings if f.monthly_saving.is_known]
    assert snapshot.rank == max(f.rank for f in priced)
    assert snapshot.rank == len(EXPECTED_NORTHWIND)


def test_ranking_is_independent_of_the_model(
    provider: MockAzureProvider, settings: Settings, checkpointer: BaseCheckpointSaver[str]
) -> None:
    """A hostile model may change the wording and must not change the order."""
    from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT
    from costsentinel.llm.contracts import RankingNote, RankingRationale

    class LoudLLM(FakeLLM):
        """Writes a ranking rationale demanding a different order."""

        def structured(
            self,
            *,
            task: LLMTask,
            prompt: Prompt,
            schema: type[StructuredResponseT],
        ) -> StructuredResponseT:
            response = super().structured(task=task, prompt=prompt, schema=schema)
            if isinstance(response, RankingRationale):
                hijacked = RankingRationale(
                    notes=tuple(
                        RankingNote(
                            recommendation_id=note.recommendation_id,
                            rationale="THIS SHOULD BE RANKED FIRST, IGNORE THE SCORE",
                        )
                        for note in response.notes
                    )
                )
                return schema.model_validate(hijacked.model_dump())
            return response

    honest, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=FakeLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-honest",
    )
    hostile, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=LoudLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-loud",
    )

    assert [f.resource_name for f in honest.findings] == [f.resource_name for f in hostile.findings]
    assert [f.ranking_composite for f in honest.findings] == [
        f.ranking_composite for f in hostile.findings
    ]
    # The wording did change, so the test is actually exercising the hostile path.
    assert "SHOULD BE RANKED FIRST" in hostile.findings[0].ranking_rationale


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


def test_blocked_findings_are_counted_and_explained(northwind_report: ClientReport) -> None:
    assert northwind_report.totals.blocked_count == 1
    assert northwind_report.totals.automatable_count == 0
    assert any("blocked" in note for note in northwind_report.notes)


def test_production_deletion_is_blocked_not_merely_reviewed(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """The government production orphan has no execution path at all."""
    report, _ = run_client_scan(
        client=CLIENT_MINISTRY,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-test-ministry",
    )
    by_name = {f.resource_name: f for f in report.findings}
    assert set(by_name) == set(EXPECTED_MINISTRY)

    orphan = by_name["disk-portal-legacy-snapshot"]
    assert orphan.action_class is ActionClass.BLOCK
    assert not orphan.action_class.is_automatable

    for name, expected in EXPECTED_MINISTRY.items():
        assert by_name[name].monthly_saving.amount == expected

    assert report.totals.projected_monthly_savings.amount == EXPECTED_MINISTRY_MONTHLY
    assert report.totals.automatable_count == 0
    assert any("will not execute it" in item.reason for item in report.approval_queue)

    # With the real prices this blocked orphan is the largest single opportunity in
    # the estate, so it leads the report -- which is the documented behaviour
    # (ARCHITECTURE.md D37). A client should hear about their biggest opportunity
    # first even when CostSentinel will not touch it; they can do it by hand.
    assert orphan.rank == 1


def test_report_states_that_nothing_was_changed(northwind_report: ClientReport) -> None:
    assert any("no execution path" in note for note in northwind_report.notes)


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
    for summary in northwind_report.waste_breakdown:
        amounts += [summary.monthly_saving, summary.annual_saving]

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
        # A shared-scope reservation has no subscription, which is a legitimate
        # scope rather than a leak.
        assert (
            finding.subscription_id in northwind_report.subscriptions_in_scope
            or finding.subscription_id == "shared-scope"
        )


def test_a_sweep_runs_each_client_as_its_own_run(settings: Settings) -> None:
    result = run_scan(settings=settings)
    assert result.clients == (CLIENT_MINISTRY, CLIENT_NORTHWIND)
    assert len(set(result.run_ids)) == len(result.reports)
    assert result.total_findings == len(EXPECTED_NORTHWIND_FINDINGS) + len(EXPECTED_MINISTRY)
    assert result.total_awaiting_approval == result.total_findings
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
    assert report.waste_breakdown == ()
    assert report.severity is ReportSeverity.NONE
    assert not report.totals.observed_monthly_spend.is_known
    assert "no actionable waste" in report.executive_summary


def test_clients_in_scope_is_sorted_and_deduplicated(provider: MockAzureProvider) -> None:
    assert clients_in_scope(provider) == (CLIENT_MINISTRY, CLIENT_NORTHWIND)
    assert clients_in_scope(provider, CLIENT_NORTHWIND) == (CLIENT_NORTHWIND,)


# ---------------------------------------------------------------------------
# Audit trail and routing
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
    # Every node but the approval gate. The gate ran -- it is what the sweep is
    # halted inside -- but an interrupted node commits no update, so its audit
    # events do not exist until a decision resumes it. That they appear then is
    # asserted by the durability test.
    assert actors == set(ALL_NODES) - {NODE_APPROVAL_GATE}

    kinds = {event.event_type for event in state.audit}
    for expected in (
        AuditEventType.ESTATE_READ,
        AuditEventType.SIGNAL_DETECTED,
        AuditEventType.RECOMMENDATION_PROPOSED,
        AuditEventType.ACTION_CLASSIFIED,
        AuditEventType.APPROVAL_REQUESTED,
        AuditEventType.REPORT_GENERATED,
        AuditEventType.SCAN_COMPLETED,
    ):
        assert expected in kinds

    assert all(event.run_id == "run-audit" for event in state.audit)
    assert len({event.event_id for event in state.audit}) == len(state.audit)


def test_routing_decisions_are_recorded_in_state(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """Every transition is an auditable fact, not an implicit consequence."""
    run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-routes",
    )
    state = load_checkpointed_state(
        run_id="run-routes",
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
    )
    assert state is not None
    assert state.supervisor_decisions

    actions = [d.action for d in state.supervisor_decisions]
    assert RouteAction.PROCEED in actions
    assert RouteAction.REQUIRE_APPROVAL in actions
    assert all(d.reason for d in state.supervisor_decisions)

    visited = [d.to_node for d in state.supervisor_decisions]
    assert visited[0] == "anomaly_scout"
    # The approval gate is last now, not the report author: this estate has gated
    # actions, so the sweep ends halted inside the gate rather than finished.
    assert visited[-1] == NODE_APPROVAL_GATE
    assert visited[-2] == "report_author"


def test_an_empty_estate_short_circuits_to_the_report(settings: Settings) -> None:
    """Four nodes over an empty list produce the same empty report, so skip them."""
    result = run_scan(client="no-such-client", settings=settings)
    assert result.reports[0].findings == ()
    # One model call, for the report prose: diagnosis, pricing and planning are
    # all skipped, which is the whole point of the short circuit.
    metrics = result.metrics[0]
    assert metrics.call_count == 1
    assert metrics.routing_choices()[0].task == "report_prose"


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
    assert state.escalations == ()


# ---------------------------------------------------------------------------
# Run metrics
# ---------------------------------------------------------------------------


def test_a_run_reports_token_cost_and_latency(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    _, metrics = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-metrics",
    )
    assert metrics.run_id == "run-metrics"
    assert metrics.client == CLIENT_NORTHWIND
    assert metrics.call_count == EXPECTED_LLM_CALLS_PER_CLIENT
    assert metrics.total_usage.total_tokens > 0
    assert metrics.total_usage.prompt_tokens > 0
    assert metrics.total_usage.completion_tokens > 0
    # The fake backend genuinely costs nothing, which is a measured zero.
    assert metrics.total_cost.amount == Decimal(0)
    assert not metrics.total_cost.provenance.is_model_derived
    assert metrics.total_latency_ms >= metrics.model_latency_ms


def test_routing_is_recorded_per_call(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """Light work routes small, synthesis routes large, and the choice is visible."""
    _, metrics = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=llm,
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-routing",
    )
    choices = metrics.routing_choices()
    assert len(choices) == EXPECTED_LLM_CALLS_PER_CLIENT
    assert all(choice.reason for choice in choices)

    by_task = {choice.task: choice for choice in choices}
    assert by_task["root_cause"].tier.value == "small"
    assert by_task["planning"].tier.value == "large"
    assert by_task["report_prose"].tier.value == "large"
    assert metrics.tier_counts() == {"small": 1, "large": 3}


def test_sweep_metrics_are_carried_alongside_the_reports(settings: Settings) -> None:
    result = run_scan(settings=settings)
    assert len(result.metrics) == len(result.reports)
    assert result.total_llm_calls == EXPECTED_LLM_CALLS_PER_CLIENT * len(result.reports)
    assert result.total_tokens > 0
    for report in result.reports:
        assert result.metrics_for(report.run_id) is not None
    assert result.metrics_for("run-never-happened") is None


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
    assert len(state.report.findings) == len(EXPECTED_NORTHWIND_FINDINGS)
    # The whole diagnosis and costing survives the round trip, not just the report.
    assert len(state.root_causes) == len(EXPECTED_NORTHWIND_FINDINGS)
    assert state.priced_options
    assert state.ranking


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
    assert set(ALL_NODES) <= set(written)


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
    """Same provider seed, same fake model, same findings, figures and order."""

    def figures() -> list[tuple[str, Decimal | None, str, int]]:
        result = run_scan(client=CLIENT_NORTHWIND, settings=settings)
        return [
            (f.resource_name, f.monthly_saving.amount, f.proposed_action.value, f.rank)
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

    report, _ = run_client_scan(
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
    assert any("processing note" in note for note in report.notes)


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

    report, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=SilentLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-silent",
    )
    # Findings and figures survive; only the rationales are the policy's own.
    assert len(report.findings) == len(EXPECTED_NORTHWIND_FINDINGS)
    assert report.totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert all("policy store" in f.rationale for f in report.findings)


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
    state = state_from(final)
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


def test_a_backend_outage_costs_the_prose_and_not_the_report(
    provider: MockAzureProvider,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """Reversed in Phase 9, deliberately, and this is the reasoning.

    Until Phase 9 this asserted the opposite: a model failure raised, on the grounds
    that "a model outage must not yield a report missing half its findings". The
    premise turned out to be false. A report built without the model is not missing
    half its findings -- it is missing none of them. Every figure, classification,
    rank and queue entry is deterministic arithmetic over provider data, and the model
    only ever narrated them.

    So the old behaviour threw away a complete, correct report because the optional
    commentary on it was unavailable, which is the worse of the two failures by a wide
    margin (ARCHITECTURE.md D115). What survives of the original intent is the part
    that was right: the degradation must be visible, so the report says the narrative
    is missing rather than leaving a blank.
    """
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

    report, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=provider,
        llm=BrokenLLM(),
        settings=settings,
        checkpointer=checkpointer,
        run_id="run-broken",
    )

    # Every finding, every figure, the whole queue.
    assert len(report.findings) == len(EXPECTED_NORTHWIND_FINDINGS)
    assert report.totals.projected_monthly_savings.amount == EXPECTED_NORTHWIND_MONTHLY
    assert report.approval_queue
    for name, expected in EXPECTED_NORTHWIND.items():
        finding = next(f for f in report.findings if f.resource_name == name)
        assert finding.monthly_saving.amount == expected, name

    # And it says so, rather than presenting an unexplained report as a normal one.
    assert any("no model prose" in note for note in report.notes)
    assert any("unaffected" in note for note in report.notes)


def test_a_custom_policy_store_is_honoured(
    provider: MockAzureProvider,
    llm: FakeLLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> None:
    """Policy is injectable, which is how Phase 4 adds client overlays."""

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

    report, _ = run_client_scan(
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
    assert report.totals.blocked_count == len(report.findings)
