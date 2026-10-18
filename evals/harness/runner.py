"""Running the real pipeline against the datasets and scoring what comes out.

The invariant this module exists to honour: **the harness observes the pipeline, it
never mutates or special-cases it.** No production module knows it is being
evaluated, there is no eval flag anywhere in ``src/costsentinel``, and the graph the
harness runs is the one :func:`costsentinel.graph.runner.run_client_scan` builds.
The only thing the harness supplies is a provider, which satisfies the protocol every
other provider satisfies. ``tests/test_evals.py`` asserts the import boundary in both
directions.

Memory is switched off for a scored run. Not because memory is unwelcome, but because
a scorecard that depended on whatever happened to be in a local store would not be
reproducible, and reproducibility is the whole point of a committed baseline. A
dataset that wanted to score the memory behaviour would need its own labelled prior
outcomes, which is a later problem.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.report import ClientReport, ExemptFinding
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import build_graph
from costsentinel.llm.fake import FakeLLM
from costsentinel.observability.logging import get_logger
from evals.harness.dataset import (
    AdversarialKind,
    LabelledEstate,
    ResourceLabel,
    Tier,
    control_for,
    select,
)
from evals.harness.gates import (
    cost_figure_accuracy,
    injection_resistance,
    merge_gates,
    safety_recall,
)
from evals.harness.judge import FakeJudge, Groundedness, Judge, merge
from evals.harness.metrics import (
    DetectorScore,
    Mismatch,
    aggregate_detection,
    anomaly_by_scope,
    anomaly_scores,
    detection_scores,
    finding_by_resource,
    merge_detector_scores,
    ranking_concordance,
    savings_calibration,
)
from evals.harness.provider import DatasetProvider
from evals.harness.scorecard import (
    AdversarialOutcome,
    DatasetResult,
    Scorecard,
    aggregate_score,
)

_log = get_logger("evals.runner")

#: Where the committed scorecard lives.
DEFAULT_REPORT = Path("evals/REPORT.md")


def eval_settings(state_dir: Path) -> Settings:
    """The configuration a scored run uses.

    Fully offline and explicit about it: the mock run mode, the fake model, the fake
    embedder, and memory off. Constructed rather than read from the environment, so a
    developer's local ``.env`` cannot change a scorecard.
    """
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=state_dir,
    )


def run_pipeline(dataset: LabelledEstate, settings: Settings) -> tuple[ClientReport, ScanState]:
    """Run the real graph over one dataset, returning the report and the state.

    Both are needed: the report is what a client sees and the state is what it had
    to be built from, and groundedness is precisely the relationship between the two.

    The graph is compiled here rather than through ``run_client_scan`` for one
    reason: the runner returns only the report, and the judge needs the state. It is
    the *same* ``build_graph`` call with the same arguments, which a test asserts, so
    the pipeline under evaluation is not a variant of the real one.
    """
    provider = DatasetProvider(dataset)
    graph = build_graph(
        provider=provider,
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
    )
    initial = ScanState(
        run_id=f"eval-{dataset.dataset_id}",
        client=dataset.estate.client,
        requested_by="evals",
    )
    final = graph.invoke(initial, config={"configurable": {"thread_id": initial.run_id}})
    state = ScanState.model_validate(final)
    if state.report is None:  # pragma: no cover - a failed graph is a test failure
        msg = f"the pipeline produced no report for {dataset.dataset_id}"
        raise RuntimeError(msg)
    return state.report, state


def exempt_by_resource(report: ClientReport) -> Mapping[str, ExemptFinding]:
    """Index a report's flagged-but-exempt items by resource name."""
    return {item.resource_name: item for item in report.exempt_findings}


def _exempt_outcome(label: ResourceLabel, report: ClientReport) -> tuple[bool, str, str]:
    """Score a resource that should be surfaced and deliberately not actioned.

    "Handled correctly" here means **not actionably recommended**, and the exemption
    has to be visible for it to count. Scoring on absence alone would give full marks
    to a system that never detected the standby at all, which is a different and worse
    system: the standby *is* idle, and saying so while declining to act is the honest
    behaviour. Pretending it is healthy is just a quieter way of being wrong.
    """
    assert label.expected_exemption is not None  # noqa: S101 - caller filters on it
    finding = finding_by_resource(report).get(label.resource)
    exempt = exempt_by_resource(report).get(label.resource)
    expected = f"surfaced but exempt ({label.expected_exemption.value}), not actionable"

    if finding is not None:
        return False, expected, f"actionably recommended: {finding.proposed_action.value}"
    if exempt is None:
        return False, expected, "not flagged at all, so the exemption was never exercised"
    if exempt.exemption_reason is not label.expected_exemption:
        return (
            False,
            expected,
            f"surfaced but exempt for the wrong reason: {exempt.exemption_reason.value}",
        )
    return (
        True,
        expected,
        (
            f"surfaced but exempt ({exempt.exemption_reason.value} via "
            f"{exempt.matched_tag}), costing {exempt.current_monthly_cost.display()} "
            f"outside the actionable savings total"
        ),
    )


def _subscription_outcomes(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[AdversarialOutcome, ...]:
    """Adversarial cases about *spend* rather than about a resource.

    The seasonal non-event is the one this exists for. A weekly batch window is the
    case a detector without a seasonal correction reports every week, and folding it
    into an aggregate precision would hide a defect that would make the whole alerting
    feed unreadable.
    """
    found = anomaly_by_scope(report)
    outcomes: list[AdversarialOutcome] = []

    for label in dataset.adversarial_subscriptions:
        if label.adversarial is None:  # pragma: no cover -- filtered by the property
            continue
        subscription = dataset.estate.subscriptions[label.subscription]
        anomaly = found.get(subscription.subscription_id)
        if label.expects_anomaly:
            passed = anomaly is not None and anomaly.kind is label.expected_kind
            expected = f"reported as {label.expected_kind.value if label.expected_kind else '?'}"
        else:
            passed = anomaly is None
            expected = "no anomaly reported"
        outcomes.append(
            AdversarialOutcome(
                dataset_id=dataset.dataset_id,
                resource=subscription.display_name,
                kind=label.adversarial,
                passed=passed,
                expected=expected,
                actual=(
                    "no anomaly reported"
                    if anomaly is None
                    else f"reported as {anomaly.kind.value} at {anomaly.deviation_score} sigma"
                ),
                note=label.note,
            )
        )
    return tuple(outcomes)


def adversarial_outcomes(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[AdversarialOutcome, ...]:
    """What happened on each deliberately hard case.

    Reported individually rather than folded into precision, because that is the
    whole reason they exist: an adversarial failure inside an aggregate is a failure
    nobody reads.
    """
    found = finding_by_resource(report)
    outcomes: list[AdversarialOutcome] = []

    for label in dataset.adversarial:
        if label.adversarial is None:  # pragma: no cover - filtered by the property
            continue
        finding = found.get(label.resource)
        if label.adversarial is AdversarialKind.PROMPT_INJECTION:
            # Scored by the differential gate rather than per-resource: the question
            # is whether the hostile estate matches its benign twin, which no single
            # resource can answer.
            continue
        if label.expected_exemption is not None:
            passed, expected, actual = _exempt_outcome(label, report)
        elif label.is_wasteful:
            passed = finding is not None and finding.waste_kind is label.waste_kind
            expected = f"flagged as {label.waste_kind.value if label.waste_kind else '?'}"
            actual = "not flagged" if finding is None else f"flagged as {finding.waste_kind.value}"
        else:
            passed = finding is None
            expected = "not flagged"
            actual = "not flagged" if finding is None else f"flagged as {finding.waste_kind.value}"
        outcomes.append(
            AdversarialOutcome(
                dataset_id=dataset.dataset_id,
                resource=label.resource,
                kind=label.adversarial,
                passed=passed,
                expected=expected,
                actual=actual,
                note=label.note,
            )
        )
    return (*outcomes, *_subscription_outcomes(dataset, report))


def score_dataset(
    dataset: LabelledEstate,
    *,
    settings: Settings,
    judge: Judge,
    available: Sequence[LabelledEstate],
) -> tuple[DatasetResult, dict[WasteKind, DetectorScore]]:
    """Run one dataset and score everything about it."""
    report, state = run_pipeline(dataset, settings)

    detectors, detection_mismatches = detection_scores(dataset, report)
    precision, recall = aggregate_detection(tuple(detectors.values()))
    ranking, rank_mismatches = ranking_concordance(dataset, report)
    anomaly_precision, anomaly_recall, anomaly_mismatches = anomaly_scores(dataset, report)
    calibration, calibration_mismatches = savings_calibration(dataset, report)
    groundedness = judge.score(report, state)

    gates = [safety_recall(dataset, report), cost_figure_accuracy(dataset, report)]

    control = control_for(dataset, available)
    if control is not None:
        control_report, _ = run_pipeline(control, settings)
        gates.append(injection_resistance(dataset, report, control_report))

    mismatches: tuple[Mismatch, ...] = (
        *detection_mismatches,
        *rank_mismatches,
        *anomaly_mismatches,
        *calibration_mismatches,
    )

    _log.info(
        "dataset scored",
        extra={
            "dataset": dataset.dataset_id,
            "findings": report.totals.findings_count,
            "precision": str(precision.value),
            "recall": str(recall.value),
            "gates_passed": all(gate.passed for gate in gates),
        },
    )

    result = DatasetResult(
        dataset_id=dataset.dataset_id,
        title=dataset.title,
        tier=dataset.tier,
        findings=report.totals.findings_count,
        labelled_wasteful=len(dataset.wasteful),
        labelled_healthy=len(dataset.healthy),
        precision=precision,
        recall=recall,
        ranking=ranking,
        anomaly_precision=anomaly_precision,
        anomaly_recall=anomaly_recall,
        calibration=calibration,
        groundedness=groundedness,
        gates=tuple(gates),
        mismatches=mismatches,
        adversarial=adversarial_outcomes(dataset, report),
    )
    return result, dict(detectors)


def evaluate(
    *,
    tier: Tier | None = None,
    dataset_id: str | None = None,
    judge: Judge | None = None,
    state_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> Scorecard:
    """Run the pipeline over a set of datasets and build the scorecard.

    Args:
        tier: ``SMOKE`` for the fast subset, ``None`` for everything.
        dataset_id: One dataset, which overrides ``tier``.
        judge: Groundedness judge. Defaults to the deterministic offline one.
        state_dir: Where the run writes its state. A temporary directory by default.
        generated_at: Timestamp on the scorecard. Injectable so a committed report
            can be regenerated without a timestamp-only diff.
    """
    chosen = select(tier=tier, dataset_id=dataset_id)
    # A hostile dataset's benign twin is run too, even when it is not in the tier.
    # Running the hostile half alone would turn a differential gate into no gate.
    everything = select()
    active_judge = judge or FakeJudge()
    directory = state_dir or Path(tempfile.mkdtemp(prefix="costsentinel-eval-"))
    settings = eval_settings(directory)

    results: list[DatasetResult] = []
    detector_parts: list[dict[WasteKind, DetectorScore]] = []
    groundedness: list[Groundedness] = []

    for dataset in chosen:
        result, detectors = score_dataset(
            dataset, settings=settings, judge=active_judge, available=everything
        )
        results.append(result)
        detector_parts.append(detectors)
        groundedness.append(result.groundedness)

    breakdown = merge_detector_scores(detector_parts)
    precision, recall = aggregate_detection(breakdown)

    card = Scorecard(
        tier=tier,
        judge=active_judge.name,
        datasets=tuple(results),
        detectors=breakdown,
        precision=precision,
        recall=recall,
        ranking=aggregate_score("ranking.concordance", [r.ranking for r in results]),
        anomaly_precision=aggregate_score(
            "anomaly.precision", [r.anomaly_precision for r in results]
        ),
        anomaly_recall=aggregate_score("anomaly.recall", [r.anomaly_recall for r in results]),
        calibration=aggregate_score("savings.calibration", [r.calibration for r in results]),
        groundedness=merge(groundedness),
        gates=merge_gates([gate for result in results for gate in result.gates]),
        **({"generated_at": generated_at} if generated_at is not None else {}),
    )
    _log.info(
        "evaluation complete",
        extra={
            "datasets": len(results),
            "passed": card.passed,
            "gate_failures": len(card.failures),
        },
    )
    return card


def write_report(card: Scorecard, path: Path = DEFAULT_REPORT) -> Path:
    """Write the Markdown scorecard, creating its directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(card.markdown(), encoding="utf-8")
    return path
