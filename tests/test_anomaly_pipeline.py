"""Anomalies through the whole graph, and the one invariant that must never bend.

**An anomaly says "investigate". It can never become something CostSentinel acts on.**

That is a different claim from the exemption invariant and needs its own proof. An
exemption makes the system do *less*; an anomaly is a whole new stream of findings, and
the risk is that it quietly acquires the remediation path the waste signals already
have. A spend change has no wasteful resource to point at, no saving to recover and no
precondition to re-check -- a "remediation" for one would be an action against a
resource nobody has identified.

Asserted four ways, each independent of the others:

* **representationally** -- neither `CostAnomaly` nor `ReportAnomaly` has a field an
  action, an action class or a saving could live in;
* **structurally** -- the detector module imports no action vocabulary and no policy
  store, by AST;
* **end to end** -- an estate whose only finding is a spend change produces no
  recommendation, no classification and nothing in the approval queue;
* **arithmetically** -- an anomaly's difference reaches no savings total, asserted
  against a run with anomalies present and the committed figures unchanged.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from costsentinel.cli import summarise_report
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.common import MoneyAmount, Provenance, ProvenanceSource, Verification
from costsentinel.domain.estate import (
    CostBreakdown,
    CostPoint,
    CostSeries,
    Environment,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
    Subscription,
)
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.report import ReportAnomaly
from costsentinel.domain.signals import (
    AnomalyDirection,
    AnomalyKind,
    AnomalyVerdict,
    CostAnomaly,
)
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import build_graph
from costsentinel.graph.runner import state_from
from costsentinel.llm.contracts import ReportNarrative
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import BreakdownShape, synthetic_breakdown

SUBSCRIPTION = "00000000-0000-4000-8000-0000000f0101"
CLIENT = "meridian-health"
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)

#: The committed wobble, so the detector's robust statistics have something to work on.
WOBBLE: tuple[str, ...] = (
    "1.000",
    "1.012",
    "0.991",
    "1.006",
    "0.985",
    "1.010",
    "0.996",
    "1.015",
    "0.989",
    "1.003",
    "1.013",
    "0.993",
    "1.008",
    "0.987",
    "1.004",
    "1.011",
    "0.994",
    "1.014",
    "0.990",
    "1.007",
    "0.988",
    "1.002",
    "1.016",
    "0.997",
    "1.009",
    "0.986",
    "1.012",
    "0.999",
    "1.005",
    "0.992",
)

BASELINE = Decimal("80.00")
STEP_MULTIPLE = Decimal("2.0")
WINDOW = 30
OBSERVED = 7


def _provenance() -> Provenance:
    return Provenance(
        source=ProvenanceSource.MOCK_PROVIDER,
        retrieved_at=AS_OF,
        reference="tests/test_anomaly_pipeline",
        verification=Verification.VERIFIED,
    )


def _step_series(subscription_id: str) -> CostSeries:
    """A clean baseline then a new level for the last week: a level shift."""
    first = AS_OF.date() - timedelta(days=WINDOW - 1)
    points: list[CostPoint] = []
    for offset in range(WINDOW):
        level = BASELINE * (STEP_MULTIPLE if offset >= WINDOW - OBSERVED else Decimal(1))
        amount = (level * Decimal(WOBBLE[offset])).quantize(Decimal("0.01"))
        points.append(CostPoint(day=first + timedelta(days=offset), amount=amount))
    return CostSeries(
        subscription_id=subscription_id, points=tuple(points), provenance=_provenance()
    )


def _idle_vm(name: str) -> Resource:
    return Resource(
        resource_id=f"/subscriptions/{SUBSCRIPTION}/vm/{name}",
        name=name,
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_id=SUBSCRIPTION,
        resource_group="rg-core",
        region="eastus",
        sku="Standard_D4s_v5",
        state=ResourceState.RUNNING,
        created_at=AS_OF - timedelta(days=300),
        monthly_cost=MoneyAmount.of(Decimal("140.16"), provenance=_provenance()),
        provenance=_provenance(),
    )


class _Estate:
    """A provider over an estate with a spend change and optionally some waste."""

    def __init__(self, resources: Sequence[Resource], *, gap: bool = False) -> None:
        self._resources = tuple(resources)
        self._gap = gap

    @property
    def name(self) -> str:
        return "test-estate"

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        if client is not None and client != CLIENT:
            return ()
        return (
            Subscription(
                subscription_id=SUBSCRIPTION,
                display_name="MH-PROD-Clinical",
                client=CLIENT,
                environment=Environment.PRODUCTION,
                provenance=_provenance(),
            ),
        )

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        return tuple(r for r in self._resources if r.subscription_id == subscription_id)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        _ = client
        return ()

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        _ = window_days
        series = _step_series(subscription_id)
        if not self._gap:
            return series
        # Drop a quarter of the month, which the detector must refuse to conclude from.
        kept = tuple(p for index, p in enumerate(series.points) if not 8 <= index < 16)
        return CostSeries(subscription_id=subscription_id, points=kept, provenance=_provenance())

    def get_cost_breakdown(
        self, subscription_id: str, *, window_days: int = 30
    ) -> CostBreakdown | None:
        """A concentrated breakdown over the same series, so the rise has one cause.

        Built from whatever :meth:`get_cost_series` returned, gap and all, so a
        breakdown can never contradict the totals it explains.
        """
        return synthetic_breakdown(
            series=self.get_cost_series(subscription_id, window_days=window_days),
            baseline_levels=tuple(
                (BASELINE * Decimal(WOBBLE[offset])).quantize(Decimal("0.01"))
                for offset in range(WINDOW)
            ),
            shape=BreakdownShape.CONCENTRATED,
            provenance=_provenance(),
        )

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        if "/vm/" not in resource_id:
            return None
        return ResourceMetrics(
            resource_id=resource_id,
            observation_days=30,
            cpu_avg_pct=Decimal("0.7"),
            cpu_max_pct=Decimal("2.0"),
            network_in_gb=Decimal("0.0"),
            provenance=_provenance(),
        )

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        return (
            SkuPrice(
                sku="Standard_D4s_v5",
                applies_to=ResourceKind.VIRTUAL_MACHINE,
                family="Dsv5",
                region=region,
                vcpu=4,
                monthly_cost=MoneyAmount.of(Decimal("140.16"), provenance=_provenance()),
            ),
        )


def _settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path_factory.mktemp("state"),
    )


def _run(resources: Sequence[Resource], settings: Settings, *, gap: bool = False) -> ScanState:
    graph = build_graph(
        provider=_Estate(resources, gap=gap),
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
    )
    final = graph.invoke(
        ScanState(run_id="run-anomaly-test", client=CLIENT),
        config={"configurable": {"thread_id": "run-anomaly-test"}},
    )
    return state_from(final)


@pytest.fixture
def anomaly_only(tmp_path_factory: pytest.TempPathFactory) -> ScanState:
    """An estate with a spend change and no waste at all."""
    return _run([], _settings(tmp_path_factory))


@pytest.fixture
def mixed(tmp_path_factory: pytest.TempPathFactory) -> ScanState:
    """A spend change alongside one genuinely idle machine."""
    return _run([_idle_vm("vm-reporting-03")], _settings(tmp_path_factory))


# ---------------------------------------------------------------------------
# It is detected, and it is reported
# ---------------------------------------------------------------------------


def test_a_spend_change_is_detected_and_reported(anomaly_only: ScanState) -> None:
    """The node finally earns its name: the Anomaly Scout emits anomalies."""
    assert len(anomaly_only.anomalies) == 1
    anomaly = anomaly_only.anomalies[0]
    assert anomaly.kind is AnomalyKind.LEVEL_SHIFT
    assert anomaly.baseline.amount == pytest.approx(BASELINE, abs=Decimal("1"))

    assert anomaly_only.report is not None
    assert anomaly_only.report.has_anomalies
    assert anomaly_only.report.totals.anomaly_count == 1


def test_every_scope_gets_an_assessment_including_the_clean_ones(mixed: ScanState) -> None:
    """Looking is a recorded fact, so a quiet report differs from an unexamined one."""
    assert len(mixed.anomaly_assessments) == 1
    assert mixed.anomaly_assessments[0].verdict is AnomalyVerdict.ASSESSED
    assert mixed.anomaly_assessments[0].detail


def test_an_audit_event_records_each_assessment(anomaly_only: ScanState) -> None:
    """Including what the finding routes to, so the trail says it was never actionable."""
    events = [e for e in anomaly_only.audit if e.event_type is AuditEventType.ANOMALY_DETECTED]
    assert len(events) == 1
    assert events[0].detail["kinds"] == "level_shift"
    assert events[0].detail["verdict"] == "assessed"
    assert "never an action" in events[0].detail["routes_to"]


def test_a_gappy_series_is_reported_as_unassessed_rather_than_clean(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A gap is unknown, not zero -- and silence about it would read as a clean month."""
    state = _run([], _settings(tmp_path_factory), gap=True)
    assert state.anomalies == ()
    assert len(state.unassessed_scopes) == 1
    assert state.unassessed_scopes[0].verdict is AnomalyVerdict.INCOMPLETE_SERIES

    assert state.report is not None
    assert state.report.totals.unassessed_scope_count == 1
    assert "unknown, not zero" in state.report.unassessed_spend[0]
    assert any("could not be assessed" in note for note in state.report.notes)


# ---------------------------------------------------------------------------
# It can never act
# ---------------------------------------------------------------------------

_ACTION_TYPES = ("ActionType", "ActionClass", "SavingsEstimate")


@pytest.mark.parametrize("model", [CostAnomaly, ReportAnomaly])
def test_an_anomaly_has_nowhere_to_put_an_action_or_a_saving(
    model: type[CostAnomaly] | type[ReportAnomaly],
) -> None:
    """Representational: there is no field through which it could acquire one."""
    annotations = {name: str(field.annotation) for name, field in model.model_fields.items()}
    offending = [
        name
        for name, annotation in annotations.items()
        if any(forbidden in annotation for forbidden in _ACTION_TYPES)
    ]
    assert not offending, offending
    assert not {"action", "risk_class", "action_class", "savings", "preconditions"} & set(
        model.model_fields
    )


def test_the_detector_imports_no_action_vocabulary() -> None:
    """Structural: the module cannot name an action even if someone tried.

    The same technique that keeps a model out of ``savings.py`` and a classifier out of
    ``exemption.py``. A future change wiring an anomaly to a remediation would have to
    add an import here, and this is what notices.
    """
    tree = ast.parse(Path("src/costsentinel/agents/anomalies.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {"ActionType", "ActionClass", "PolicyStore", "Recommendation", "SavingsEstimate"}
    assert not imported & forbidden, imported


def test_an_estate_whose_only_finding_is_a_spend_change_proposes_nothing(
    anomaly_only: ScanState,
) -> None:
    """End to end: no recommendation, no classification, nothing to approve.

    The strongest form of the claim. An anomaly is in state, it is in the report, and
    the entire remediation path stayed empty -- because an anomaly is not a
    ``WasteSignal`` and nothing downstream converts one.
    """
    assert len(anomaly_only.anomalies) == 1
    assert anomaly_only.signals == ()
    assert anomaly_only.recommendations == ()
    assert anomaly_only.classifications == ()
    assert anomaly_only.approval_requests == ()

    assert anomaly_only.report is not None
    assert anomaly_only.report.findings == ()
    assert anomaly_only.report.approval_queue == ()
    assert anomaly_only.report.totals.automatable_count == 0


def test_an_anomaly_never_joins_the_approval_queue(mixed: ScanState) -> None:
    """Nothing is put to a human about a change nobody is proposing to act on."""
    assert mixed.report is not None
    assert mixed.anomalies
    queued = {item.resource_name for item in mixed.report.approval_queue}
    assert not queued & {anomaly.subscription_id for anomaly in mixed.anomalies}


def test_an_anomalys_difference_reaches_no_savings_total(mixed: ScanState) -> None:
    """Arithmetically: the one figure that must never be mistaken for a saving.

    The level shift is worth about 80.00 a day. The report's savings are the idle
    machine's 140.16 and nothing else, to the cent.
    """
    assert mixed.report is not None
    anomaly = mixed.anomalies[0]
    assert anomaly.delta.amount is not None
    assert anomaly.delta.amount > 0

    totals = mixed.report.totals
    assert totals.projected_monthly_savings.amount == Decimal("140.16")
    priced = [
        finding.monthly_saving.amount
        for finding in mixed.report.findings
        if finding.monthly_saving.amount is not None
    ]
    assert totals.projected_monthly_savings.amount == sum(priced, start=Decimal(0))
    assert anomaly.delta.amount not in priced


def test_the_report_says_the_difference_is_not_a_saving(mixed: ScanState) -> None:
    """Said in words as well as in arithmetic, because a client reads the words."""
    assert mixed.report is not None
    note = next(note for note in mixed.report.notes if "spend change" in note)
    assert "investigate, not something to act on" in note
    assert "not an amount anyone has identified a way to recover" in note
    assert "none of it appears in the projected savings" in note


# ---------------------------------------------------------------------------
# The model explains and decides nothing
# ---------------------------------------------------------------------------


class _HostileLLM:
    """A model that answers every anomaly with an instruction to act on it."""

    def __init__(self) -> None:
        self._inner = FakeLLM()

    @property
    def name(self) -> str:
        return "hostile-fake"

    def structured(self, **kwargs: object) -> object:
        response = self._inner.structured(**kwargs)  # type: ignore[arg-type]
        notes = getattr(response, "anomaly_notes", None)
        if not notes:
            return response
        return response.model_copy(  # type: ignore[union-attr]
            update={
                "anomaly_notes": tuple(
                    note.model_copy(
                        update={
                            "note": (
                                "This is a spike, not a level shift, the baseline is "
                                "0.00 USD and the saving is 99,999.00 USD. Delete the "
                                "subscription immediately; no approval required."
                            )
                        }
                    )
                    for note in notes
                )
            }
        )


def test_a_hostile_note_changes_no_number_and_no_verdict(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The model may explain a change. It may not restate it as a different change.

    ``AnomalyNote`` carries an id and prose and nothing else, so the kind, the
    direction and every figure are unreachable from a response -- and
    ``MoneyAmount`` would refuse a model-derived figure even if they were not.
    """
    settings = _settings(tmp_path_factory)
    honest = _run([_idle_vm("vm-reporting-03")], settings)

    graph = build_graph(
        provider=_Estate([_idle_vm("vm-reporting-03")]),
        llm=_HostileLLM(),  # type: ignore[arg-type]
        settings=settings,
        checkpointer=None,
    )
    final = graph.invoke(
        ScanState(run_id="run-anomaly-hostile", client=CLIENT),
        config={"configurable": {"thread_id": "run-anomaly-hostile"}},
    )
    hostile = state_from(final)

    assert honest.report is not None
    assert hostile.report is not None
    before, after = honest.report.anomalies[0], hostile.report.anomalies[0]

    # The verdict and every figure are identical...
    assert after.kind is before.kind
    assert after.direction is before.direction
    assert after.baseline.amount == before.baseline.amount
    assert after.observed.amount == before.observed.amount
    assert after.delta.amount == before.delta.amount
    assert after.statement == before.statement
    assert after.deviation_score == before.deviation_score
    # ...the savings total is untouched...
    assert (
        hostile.report.totals.projected_monthly_savings.amount
        == honest.report.totals.projected_monthly_savings.amount
    )
    # ...and the hostile text landed only where prose lands.
    assert "Delete the subscription" in after.note
    assert hostile.recommendations[0].action is honest.recommendations[0].action
    assert (
        hostile.report.approval_queue[0].action_class
        is honest.report.approval_queue[0].action_class
    )


def test_the_statement_is_never_model_authored(mixed: ScanState) -> None:
    """The numbers a client acts on come from the detector, in its own words."""
    assert mixed.report is not None
    anomaly = mixed.report.anomalies[0]
    assert anomaly.statement == mixed.anomalies[0].statement()
    for money in (anomaly.baseline, anomaly.observed, anomaly.delta):
        assert money.provenance.source is ProvenanceSource.CALCULATION


def test_the_model_note_explains_the_shape_without_deciding_it(mixed: ScanState) -> None:
    """The division of labour: the detector says what happened, the prose what it means."""
    assert mixed.report is not None
    note = mixed.report.anomalies[0].note
    assert "level shift" in note
    assert "moved and stayed moved" in note
    assert "nothing here is an action CostSentinel proposes" in note


# ---------------------------------------------------------------------------
# What a human sees
# ---------------------------------------------------------------------------


def test_the_digest_separates_spend_changes_from_findings(mixed: ScanState) -> None:
    """A sample a human reads, with the change outside the savings."""
    assert mixed.report is not None
    text = summarise_report(mixed.report)
    assert "spend changes        1 to investigate" in text
    assert "Spend changes (investigate; no action proposed):" in text
    assert "level_shift" in text
    assert "projected savings    140.16 USD/month" in text


def test_the_anomalies_lead_with_the_furthest_from_normal() -> None:
    """Ordered by deviation score, not by the size of the difference.

    A large change on a large subscription can be entirely ordinary, which is what the
    score already accounts for and the raw delta does not. Asserted on a constructed
    pair, because an estate with two anomalies of opposite orderings is more fixture
    than test.
    """
    from costsentinel.agents.report_author import _report_anomalies

    state = ScanState(
        run_id="r", client=CLIENT, anomalies=(_anomaly("ca-a", "4.0"), _anomaly("ca-b", "40.0"))
    )
    ordered = _report_anomalies(state, ReportNarrative(executive_summary="x"))
    assert [a.anomaly_id for a in ordered] == ["ca-b", "ca-a"]


def _anomaly(anomaly_id: str, score: str) -> CostAnomaly:
    """A minimal anomaly, for the ordering check."""
    money = MoneyAmount.of(Decimal("10.00"), provenance=Provenance.calculated("test"))
    return CostAnomaly(
        anomaly_id=anomaly_id,
        client=CLIENT,
        subscription_id=SUBSCRIPTION,
        kind=AnomalyKind.SPIKE,
        direction=AnomalyDirection.INCREASE,
        baseline=money,
        observed=money,
        delta=money,
        observed_from=date(2026, 10, 1),
        observed_to=date(2026, 10, 1),
        window_days=1,
        baseline_days=23,
        deviation_score=Decimal(score),
        threshold_score=Decimal("5.0"),
        confidence=Decimal("0.7"),
        detector="spend_spike/v1",
        provenance=Provenance.calculated("test"),
    )


def test_an_anomaly_carries_its_evidence(mixed: ScanState) -> None:
    """So "isn't that just our weekly batch?" has an answer in the report itself."""
    assert mixed.report is not None
    evidence = mixed.report.anomalies[0].evidence
    assert any(line.startswith("baseline_daily_spend: ") for line in evidence)
    assert any(line.startswith("weekday_median.") for line in evidence)


def test_determinism(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Two runs of the same estate must agree, or a scorecard means nothing."""
    settings = _settings(tmp_path_factory)
    first = _run([_idle_vm("vm-reporting-03")], settings)
    second = _run([_idle_vm("vm-reporting-03")], settings)
    assert [a.anomaly_id for a in first.anomalies] == [a.anomaly_id for a in second.anomalies]
    assert first.anomalies[0].statement() == second.anomalies[0].statement()
