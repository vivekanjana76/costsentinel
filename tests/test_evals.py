"""The evaluation harness, and the boundary it must not cross.

The invariant this phase rests on: **the harness observes the pipeline, it never
mutates or special-cases it.** Asserted two ways here — an AST scan of the import
graph in both directions, and a check that the graph the harness runs is the one
``build_graph`` builds rather than a variant of it.

The other thing proved here is that the gates actually fail. A gate nobody has seen
go red is a gate nobody should trust, so
``test_a_mis_classified_destructive_action_fails_the_safety_gate`` breaks the
classifier deliberately and asserts the run fails, the exit code is non-zero, and the
failure names the resource.
"""

from __future__ import annotations

import ast
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from evals.harness.dataset import (
    AdversarialKind,
    DatasetError,
    LabelledEstate,
    Tier,
    control_for,
    datasets_dir,
    load_all,
    load_dataset,
    select,
)
from evals.harness.gates import (
    Fingerprint,
    cost_figure_accuracy,
    injection_resistance,
    safety_recall,
)
from evals.harness.judge import _DECIMAL, FakeJudge
from evals.harness.metrics import Mismatch, Score, detection_scores, ranking_concordance
from evals.harness.provider import DatasetProvider
from evals.harness.runner import eval_settings, evaluate, run_pipeline, write_report
from evals.harness.scorecard import BLOCKING_GATES

from costsentinel.cli import main
from costsentinel.config import MemoryBackend, RunMode
from costsentinel.domain.recommendations import ActionClass
from costsentinel.domain.report import ClientReport
from costsentinel.domain.signals import WasteKind

SRC = Path("src/costsentinel")

#: An eval run writes no state -- memory is off and there is no checkpointer -- but
#: the settings need a directory, and a real temporary one beats a hardcoded path.
_STATE = Path(tempfile.mkdtemp(prefix="costsentinel-eval-tests-"))


def _modules(path: Path) -> set[str]:
    """Every module name one file imports, including lazy in-function imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_no_production_module_imports_from_evals() -> None:
    """The pipeline must not know the harness exists.

    The one exception is the CLI, which has to be able to invoke it -- and does so
    through a lazy import inside the ``eval`` handler, so importing the CLI does not
    drag the harness in. Everything else is forbidden outright.
    """
    allowed = {SRC / "cli.py"}
    for path in sorted(SRC.rglob("*.py")):
        offenders = [name for name in _modules(path) if name.split(".")[0] == "evals"]
        if path in allowed:
            continue
        assert not offenders, f"{path} imports from evals: {offenders}"


def test_the_cli_imports_the_harness_lazily() -> None:
    """So a `scan` pays nothing for the existence of an eval harness.

    And so the dependency stays one-directional in substance as well as in form: the
    harness is a consumer of the pipeline, not a participant in it.
    """
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("evals"):
            # Module-level imports have the module body as their parent; a lazy one
            # sits inside a function. Checked by column offset, which is zero only
            # at module level.
            assert node.col_offset > 0, "the evals import in cli.py must stay inside a function"


def test_no_production_module_mentions_evaluation_at_all() -> None:
    """No eval-only flag, branch or environment check anywhere in the pipeline.

    A ``if settings.is_eval:`` somewhere would make every score a measurement of a
    special case, and would be invisible to the import check above.
    """
    forbidden = ("is_eval", "IS_EVAL", "EVAL_MODE", "under_eval", "being_evaluated")
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path} contains {token!r}"


def test_the_harness_runs_the_same_graph_the_runner_builds() -> None:
    """Not a copy and not a variant.

    ``run_pipeline`` compiles the graph itself only because it needs the final state
    and ``run_client_scan`` returns only the report. This asserts the two calls are
    otherwise the same, so the pipeline under evaluation is the real one.
    """
    harness_source = Path("evals/harness/runner.py").read_text(encoding="utf-8")
    assert "from costsentinel.graph.build import build_graph" in harness_source
    # The harness must not reach past the public assembly into the nodes themselves.
    assert "from costsentinel.agents" not in harness_source

    runner_source = Path("src/costsentinel/graph/runner.py").read_text(encoding="utf-8")
    for argument in ("provider=", "llm=", "settings=", "checkpointer="):
        assert argument in harness_source
        assert argument in runner_source


def test_the_dataset_provider_satisfies_the_provider_protocol() -> None:
    """Which is what makes the graph unable to tell a dataset from an estate."""
    from costsentinel.providers.base import AzureProvider

    provider = DatasetProvider(load_all()[0])
    assert isinstance(provider, AzureProvider)


# ---------------------------------------------------------------------------
# The datasets
# ---------------------------------------------------------------------------


def test_every_committed_dataset_loads_and_validates() -> None:
    datasets = load_all()
    assert len(datasets) >= 4
    assert len({d.dataset_id for d in datasets}) == len(datasets)


def test_every_resource_and_reservation_is_labelled() -> None:
    """Including the healthy ones.

    An estate of nothing but waste cannot measure precision, and an unlabelled
    resource would be neither a true positive nor a false one -- it would simply not
    be counted, which is the worst of the three.
    """
    for dataset in load_all():
        assert {label.resource for label in dataset.labels} == dataset.labellable
        assert dataset.healthy, f"{dataset.dataset_id} has no healthy resources"


def test_every_label_states_its_reasoning() -> None:
    """So a future disagreement is with an argument rather than with a boolean."""
    for dataset in load_all():
        for label in dataset.labels:
            assert len(label.note) > 40, f"{dataset.dataset_id}/{label.resource}"


def test_the_datasets_cover_all_three_kinds_of_adversarial_case() -> None:
    kinds = {
        label.adversarial
        for dataset in load_all()
        for label in dataset.adversarial
        if label.adversarial is not None
    }
    assert kinds == set(AdversarialKind)


def test_the_injection_pair_differs_only_in_names_and_tags() -> None:
    """Otherwise the differential gate would be comparing two different estates.

    This is the assumption the whole injection measurement rests on, so it is checked
    rather than assumed: same SKUs, same states, same utilisation, same prices.
    """
    hostile = next(d for d in load_all() if d.dataset_id == "injection-hostile")
    control = control_for(hostile, load_all())
    assert control is not None

    def shape(dataset: LabelledEstate) -> list[tuple[object, ...]]:
        return sorted(
            (
                resource.kind,
                resource.state,
                resource.sku,
                resource.capacity_gb,
                resource.cpu_avg_pct,
                resource.cpu_max_pct,
                resource.connection_count,
                resource.utilisation_pct,
                resource.age_days,
            )
            for resource in dataset.estate.resources
        )

    assert shape(hostile) == shape(control)
    assert hostile.estate.daily_spend == control.estate.daily_spend

    # And the names really do differ, or the test above would be vacuous.
    hostile_names = {r.name for r in hostile.estate.resources}
    control_names = {r.name for r in control.estate.resources}
    assert hostile_names != control_names


def test_the_hostile_dataset_actually_contains_injection_attempts() -> None:
    """A test that the adversarial case is adversarial."""
    hostile = next(d for d in load_all() if d.dataset_id == "injection-hostile")
    blob = hostile.model_dump_json().lower()
    assert "ignore all previous instructions" in blob
    assert "</evidence>" in blob
    # An action outside the closed vocabulary, which schema validation must refuse.
    assert "purge_subscription" in blob


def test_the_smoke_tier_is_a_real_subset_and_still_gates() -> None:
    """The fast subset must be a subset, and must still enforce every gate.

    A smoke run that skipped the gates would be worse than none: it would look like
    coverage.
    """
    smoke = select(tier=Tier.SMOKE)
    everything = select()
    assert 0 < len(smoke) < len(everything)
    # At least one adversarial case, or the fast run measures only the easy ones.
    assert any(dataset.adversarial for dataset in smoke)


def test_a_dataset_naming_a_missing_resource_is_refused(tmp_path: Path) -> None:
    source = json.loads((datasets_dir() / "injection-control.json").read_text(encoding="utf-8"))
    source["labels"].append({"resource": "does-not-exist", "is_wasteful": False, "note": "x" * 50})
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(DatasetError, match="not in the estate"):
        load_dataset(path)


def test_a_half_labelled_positive_case_is_refused(tmp_path: Path) -> None:
    """A positive case with no expected action scores nothing and weakens recall."""
    source = json.loads((datasets_dir() / "injection-control.json").read_text(encoding="utf-8"))
    for label in source["labels"]:
        if label["resource"] == "vm-sandbox-busy":
            label["is_wasteful"] = True
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(DatasetError, match="does not say"):
        load_dataset(path)


def test_an_unknown_dataset_id_lists_what_is_available() -> None:
    with pytest.raises(DatasetError) as raised:
        select(dataset_id="no-such-dataset")
    assert "baseline-mixed" in str(raised.value)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def test_a_scored_run_is_fully_offline() -> None:
    """Constructed rather than read from the environment.

    A developer's local .env must not be able to change a scorecard.
    """
    settings = eval_settings(_STATE)
    assert settings.mode is RunMode.MOCK
    assert settings.uses_fake_llm
    assert settings.uses_fake_embedder
    assert settings.memory_backend is MemoryBackend.OFF
    assert settings.requires_no_credentials


def test_memory_is_off_for_a_scored_run() -> None:
    """A scorecard that depended on a local store would not be reproducible."""
    assert eval_settings(_STATE).memory_backend is MemoryBackend.OFF


def test_the_same_dataset_scores_identically_twice() -> None:
    """Determinism, which a committed baseline is worthless without."""
    first = evaluate(dataset_id="baseline-mixed", generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = evaluate(dataset_id="baseline-mixed", generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert first.markdown() == second.markdown()


def test_the_full_run_passes_every_gate() -> None:
    """The committed baseline. A failure here means something regressed."""
    card = evaluate()
    assert card.passed, [failure.describe() for failure in card.failures]
    assert {gate.name for gate in card.blocking} == set(BLOCKING_GATES)
    for gate in card.blocking:
        assert gate.checked > 0, f"{gate.name} checked nothing, so it proved nothing"


def test_the_quality_metrics_are_measured_and_report_their_denominators() -> None:
    card = evaluate()
    for score in (card.precision, card.recall, card.calibration):
        assert score.is_measured
        assert score.denominator > 0
        assert str(score.denominator) in score.display()


def test_every_adversarial_case_is_handled_correctly() -> None:
    """3 of 3, and this is the acceptance test for Phase 6.

    It read the other way round until Phase 6: it asserted that at least one
    adversarial case was failing, with a comment saying it would need updating when
    the detectors gained tag exemptions. They did, so it does -- which was the point
    of writing it that way rather than leaving the gap unmeasured.
    """
    card = evaluate()
    assert card.adversarial
    failing = [outcome.line() for outcome in card.adversarial if not outcome.passed]
    assert not failing, failing
    assert card.adversarial_passed == len(card.adversarial) == 3


def test_the_scorecard_names_a_gap_rather_than_hiding_it() -> None:
    """An adversarial failure inside an aggregate is a failure nobody reads.

    Asserted against a forced failure rather than a real one, now that there are no
    real ones. What matters is that a failing case is named with its dataset, its
    resource, what was expected and what happened -- and that it stays outside the
    aggregate precision, so a reader cannot mistake "0.92 precision" for "the hard
    cases are fine".
    """
    card = evaluate()
    broken = (
        card.datasets[0]
        .adversarial[0]
        .model_copy(update={"passed": False, "actual": "flagged as idle_virtual_machine"})
    )
    degraded = card.model_copy(
        update={
            "datasets": (
                card.datasets[0].model_copy(update={"adversarial": (broken,)}),
                *card.datasets[1:],
            )
        }
    )

    assert degraded.adversarial_passed < len(degraded.adversarial)
    rendered = degraded.markdown()
    assert broken.dataset_id in rendered
    assert broken.resource in rendered
    assert broken.expected in rendered
    assert broken.actual in rendered
    assert broken.note in rendered
    # Reported, and still not blocking: the gates decide the verdict.
    assert degraded.precision.value == card.precision.value


# ---------------------------------------------------------------------------
# The gates fail when they should: red, then green
# ---------------------------------------------------------------------------


def _baseline() -> tuple[LabelledEstate, ClientReport]:
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, _ = run_pipeline(dataset, eval_settings(_STATE))
    return dataset, report


def test_a_mis_classified_destructive_action_fails_the_safety_gate() -> None:
    """Red, then green. A gate nobody has seen fail is a gate nobody should trust.

    The classifier is broken deliberately -- one destructive finding downgraded to
    ``allow`` with approval waived, which is exactly the defect the gate exists to
    catch -- and the gate must fail, name the resource, and say what it expected.
    """
    dataset, report = _baseline()

    # Green first, so the red result cannot be a pre-existing failure.
    assert safety_recall(dataset, report).passed

    destructive = next(
        finding for finding in report.findings if finding.action_class is ActionClass.BLOCK
    )
    sabotaged = report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(
                    update={"action_class": ActionClass.ALLOW, "requires_approval": False}
                )
                if finding.recommendation_id == destructive.recommendation_id
                else finding
                for finding in report.findings
            )
        }
    )

    result = safety_recall(dataset, sabotaged)
    assert not result.passed
    assert len(result.failures) == 1
    (failure,) = result.failures
    assert failure.resource == destructive.resource_name
    assert "at least block" in failure.expected
    assert failure.actual == "allow"
    assert "FAIL" in result.summary()


def test_a_waived_approval_fails_the_safety_gate_even_at_the_right_class() -> None:
    """A finding classified ``review`` that needs no approval is not gated at all."""
    dataset, report = _baseline()
    target = next(f for f in report.findings if f.action_class is ActionClass.REVIEW)
    sabotaged = report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(update={"requires_approval": False})
                if finding.recommendation_id == target.recommendation_id
                else finding
                for finding in report.findings
            )
        }
    )
    result = safety_recall(dataset, sabotaged)
    assert not result.passed
    assert any("requires_approval" in failure.expected for failure in result.failures)


def test_a_wrong_money_figure_fails_the_cost_gate() -> None:
    """Exactly, not within tolerance.

    A savings figure is arithmetic over provider data, so a discrepancy is a bug in
    the arithmetic -- and "within 2%" is a sentence that should never appear next to
    a number a client acts on.
    """
    dataset, report = _baseline()
    assert cost_figure_accuracy(dataset, report).passed

    target = next(f for f in report.findings if f.monthly_saving.amount is not None)
    assert target.monthly_saving.amount is not None
    nudged = target.monthly_saving.model_copy(
        update={"amount": target.monthly_saving.amount + Decimal("0.01")}
    )
    sabotaged = report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(update={"monthly_saving": nudged})
                if finding.recommendation_id == target.recommendation_id
                else finding
                for finding in report.findings
            )
        }
    )
    result = cost_figure_accuracy(dataset, sabotaged)
    assert not result.passed
    # One cent is enough. There is no tolerance band to hide in.
    assert any(failure.resource == target.resource_name for failure in result.failures)


def test_a_broken_annualisation_fails_the_cost_gate() -> None:
    """The annual figure must be twelve times the monthly one."""
    dataset, report = _baseline()
    target = next(f for f in report.findings if f.monthly_saving.amount is not None)
    assert target.annual_saving.amount is not None
    sabotaged = report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(
                    update={
                        "annual_saving": finding.annual_saving.model_copy(
                            update={"amount": Decimal("1.00")}
                        )
                    }
                )
                if finding.recommendation_id == target.recommendation_id
                else finding
                for finding in report.findings
            )
        }
    )
    result = cost_figure_accuracy(dataset, sabotaged)
    assert not result.passed
    assert any("twelve times" in failure.note for failure in result.failures)


def test_an_injection_that_changed_an_action_class_fails_the_gate() -> None:
    """The gate compares multisets, so a changed class is a changed multiset."""
    hostile = next(d for d in load_all() if d.dataset_id == "injection-hostile")
    control_dataset = control_for(hostile, load_all())
    assert control_dataset is not None
    settings = eval_settings(_STATE)
    hostile_report, _ = run_pipeline(hostile, settings)
    control_report, _ = run_pipeline(control_dataset, settings)

    assert injection_resistance(hostile, hostile_report, control_report).passed

    tampered = hostile_report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(update={"action_class": ActionClass.ALLOW})
                for finding in hostile_report.findings
            )
        }
    )
    result = injection_resistance(hostile, tampered, control_report)
    assert not result.passed
    assert result.failures


def test_an_injection_that_changed_only_a_total_still_fails() -> None:
    """The subtlest version of the attack, and the one most likely to go unnoticed."""
    hostile = next(d for d in load_all() if d.dataset_id == "injection-hostile")
    control_dataset = control_for(hostile, load_all())
    assert control_dataset is not None
    settings = eval_settings(_STATE)
    hostile_report, _ = run_pipeline(hostile, settings)
    control_report, _ = run_pipeline(control_dataset, settings)

    totals = hostile_report.totals
    assert totals.projected_monthly_savings.amount is not None
    tampered = hostile_report.model_copy(
        update={
            "totals": totals.model_copy(
                update={
                    "projected_monthly_savings": totals.projected_monthly_savings.model_copy(
                        update={"amount": Decimal("1.00")}
                    )
                }
            )
        }
    )
    result = injection_resistance(hostile, tampered, control_report)
    assert not result.passed
    assert any("headline figure" in failure.note for failure in result.failures)


def test_the_fingerprint_ignores_the_resource_name() -> None:
    """It has to: the names are the thing the hostile dataset varies.

    Keying a security comparison on a value under the attacker's control would be
    the wrong instinct even if it worked.
    """
    assert "resource" not in Fingerprint.model_fields


def test_the_cli_exits_non_zero_when_a_gate_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of a gate: it must stop a build.

    Driven by substituting the evaluation with one whose safety gate has failed, so
    the test exercises the CLI's own handling rather than re-testing the gate.
    """
    card = evaluate(dataset_id="baseline-mixed")
    broken_gates = tuple(
        gate.model_copy(update={"passed": False}) if gate.name == "safety.recall" else gate
        for gate in card.gates
    )
    monkeypatch.setattr(
        "evals.harness.runner.evaluate",
        lambda **_: card.model_copy(update={"gates": broken_gates}),
    )

    assert main(["eval", "--no-write"]) == 1
    captured = capsys.readouterr()
    assert "VERDICT           FAIL" in captured.out
    assert "evaluation FAILED on safety.recall" in captured.err
    assert "These are gates, not" in captured.err


def test_the_cli_exits_zero_when_every_gate_holds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["eval", "--smoke", "--no-write"]) == 0
    captured = capsys.readouterr()
    assert "VERDICT           PASS" in captured.out
    assert "smoke tier" in captured.out


def test_the_cli_reports_an_unknown_dataset_rather_than_raising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["eval", "--dataset", "nope", "--no-write"]) == 1
    assert "no dataset 'nope'" in capsys.readouterr().err


def test_a_quality_drop_does_not_fail_the_run() -> None:
    """A precision of 0.80 is a number to look at, not a reason to refuse a merge.

    Wiring quality into the verdict would mean the first person to add a hard dataset
    broke the build -- which is the surest way to get hard datasets removed.

    Asserted against a *forced* quality drop rather than against a real one. This
    test used to read ``precision < 1`` with the comment "the lookalikes dataset
    drags it down", which was true while the detectors had no tag exemption and the
    lookalikes dataset held two measured false positives. Phase 6 fixed them, and the
    test went red -- for the best possible reason, but it showed the assertion was
    pinned to a defect rather than to the design. A precision of 1.00 should not make
    this claim unprovable.
    """
    card = evaluate()
    assert card.passed

    degraded = card.model_copy(
        update={
            "precision": Score.of("detection.precision", 1, 2),
            "recall": Score.of("detection.recall", 1, 2),
            "ranking": Score.of("ranking.concordance", 1, 4),
            "calibration": Score.of("savings.calibration", 0, 3),
        }
    )
    assert degraded.precision.value == Decimal("0.5000")
    assert degraded.passed, "a quality metric must never decide the verdict"
    assert not degraded.failures


# ---------------------------------------------------------------------------
# Metrics and the judge
# ---------------------------------------------------------------------------


def test_a_score_with_no_denominator_is_unmeasured_not_perfect() -> None:
    """1.0 would flatter and 0.0 would libel."""
    score = Score.of("test", 0, 0)
    assert not score.is_measured
    assert score.value is None
    assert "n/a" in score.display()


def test_a_finding_of_the_wrong_kind_counts_against_both_detectors() -> None:
    """Calling a disk an idle VM is not a partially right answer."""
    dataset, report = _baseline()
    target = next(f for f in report.findings if f.waste_kind is WasteKind.ORPHANED_MANAGED_DISK)
    mislabelled = report.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(update={"waste_kind": WasteKind.IDLE_VIRTUAL_MACHINE})
                if finding.recommendation_id == target.recommendation_id
                else finding
                for finding in report.findings
            )
        }
    )
    scores, mismatches = detection_scores(dataset, mislabelled)
    assert scores[WasteKind.IDLE_VIRTUAL_MACHINE].false_positives == 1
    assert scores[WasteKind.ORPHANED_MANAGED_DISK].false_negatives == 1
    assert any("wrong kind" in mismatch.note for mismatch in mismatches)


def test_ranking_concordance_is_unmeasured_with_fewer_than_two_findings() -> None:
    dataset, report = _baseline()
    thin = report.model_copy(update={"findings": report.findings[:1]})
    score, discordant = ranking_concordance(dataset, thin)
    assert not score.is_measured
    assert discordant == ()


def test_the_judge_matches_decimals_and_not_bare_integers() -> None:
    """A judge that flagged "30 days" as an unsupported figure is one nobody reads."""
    assert _DECIMAL.findall("saving of 1,234.56 over 30 days with 8 findings") == ["1,234.56"]


def test_the_judge_traces_every_figure_in_the_prose() -> None:
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, state = run_pipeline(dataset, eval_settings(_STATE))
    result = FakeJudge().score(report, state)
    assert result.claims
    assert result.unsupported == ()
    assert result.score == 1


def test_an_invented_figure_in_the_prose_is_caught() -> None:
    """The failure mode the whole system is built to prevent, asserted directly."""
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, state = run_pipeline(dataset, eval_settings(_STATE))
    fabricated = report.model_copy(
        update={"executive_summary": "We identified savings of 98,765.43 USD this month."}
    )
    result = FakeJudge().score(fabricated, state)
    assert result.unsupported
    assert any("98,765.43" in claim.text for claim in result.unsupported)
    assert result.score is not None and result.score < 1


def test_a_report_with_no_checkable_claim_scores_unknown_not_perfect() -> None:
    """It has not demonstrated groundedness, it has avoided the question."""
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, state = run_pipeline(dataset, eval_settings(_STATE))
    silent = report.model_copy(
        update={
            "executive_summary": "No figures here.",
            "findings": tuple(
                finding.model_copy(update={"note": "", "ranking_rationale": "", "root_cause": ""})
                for finding in report.findings
            ),
        }
    )
    assert FakeJudge().score(silent, state).score is None


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------


def test_the_scorecard_leads_with_the_gates() -> None:
    """A headline of "precision 0.92" buries an ungated deletion."""
    card = evaluate(dataset_id="baseline-mixed")
    summary = card.terminal_summary()
    assert summary.index("VERDICT") < summary.index("Gates")
    assert summary.index("Gates") < summary.index("Quality")

    markdown = card.markdown()
    assert markdown.index("## Gates") < markdown.index("## Quality metrics")


def test_the_scorecard_calls_out_the_adversarial_cases_separately() -> None:
    card = evaluate()
    markdown = card.markdown()
    assert "## Adversarial cases" in markdown
    for outcome in card.adversarial:
        assert outcome.resource in markdown
        assert outcome.note[:40] in markdown


def test_the_scorecard_shows_the_per_detector_breakdown() -> None:
    """The aggregate says something is wrong; this says what."""
    card = evaluate()
    markdown = card.markdown()
    assert "## Per-detector breakdown" in markdown
    for detector in card.detectors:
        assert detector.waste_kind.value in markdown


def test_a_failing_scorecard_renders_its_failures() -> None:
    card = evaluate(dataset_id="baseline-mixed")
    invented = Mismatch(
        dataset_id="baseline-mixed",
        resource="disk-reporting-03-data",
        kind="safety",
        expected="at least block",
        actual="allow",
    )
    broken = card.model_copy(
        update={
            "gates": tuple(
                gate.model_copy(update={"passed": False, "failures": (invented,)})
                if gate.name == "safety.recall"
                else gate
                for gate in card.gates
            )
        }
    )
    assert not broken.passed
    markdown = broken.markdown()
    assert "**FAIL**" in markdown
    assert "### Gate failures" in markdown
    assert "disk-reporting-03-data" in markdown


def test_the_committed_report_is_current() -> None:
    """A stale scorecard is worse than none: it reports yesterday's claims as today's.

    Regenerate with `uv run costsentinel eval`.
    """
    committed = Path("evals/REPORT.md")
    assert committed.is_file()
    text = committed.read_text(encoding="utf-8")
    card = evaluate()

    # The timestamp line is excluded, since it moves on every run by design.
    def body(markdown: str) -> str:
        return "\n".join(
            line for line in markdown.splitlines() if not line.startswith("- Generated:")
        )

    assert body(text) == body(card.markdown()), (
        "evals/REPORT.md is out of date; regenerate with `uv run costsentinel eval`"
    )


def test_writing_the_report_creates_its_directory(tmp_path: Path) -> None:
    card = evaluate(dataset_id="baseline-mixed")
    path = write_report(card, tmp_path / "nested" / "REPORT.md")
    assert path.is_file()
    assert "CostSentinel evaluation scorecard" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The real judge, and the provider's surface
# ---------------------------------------------------------------------------


def test_the_model_judge_asks_the_model_which_claims_it_cannot_trace() -> None:
    """Driven by a double, so no credential and no network.

    The real judge exists for the claims traceability cannot see -- a confident
    sentence about a cause nobody observed -- so what is asserted here is the shape of
    the request and the handling of the verdict, not a quality score.
    """
    from evals.harness.judge import JudgeVerdict, ModelJudge

    from costsentinel.llm.base import LLMTask, Prompt

    class _Recording:
        def __init__(self) -> None:
            self.prompts: list[Prompt] = []

        @property
        def name(self) -> str:
            return "recording-llm"

        def structured(self, *, task: LLMTask, prompt: Prompt, schema: type) -> object:
            self.prompts.append(prompt)
            assert schema is JudgeVerdict
            return JudgeVerdict(unsupported_claims=(), verdict="every claim traces to the state")

    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, state = run_pipeline(dataset, eval_settings(_STATE))

    llm = _Recording()
    judge = ModelJudge(llm)  # type: ignore[arg-type]
    assert judge.name == "model-judge:recording-llm"

    result = judge.score(report, state)
    assert result.claims
    assert result.unsupported == ()

    # The report and the state both reach the model, as fenced evidence.
    (prompt,) = llm.prompts
    labels = {block.label for block in prompt.evidence}
    assert labels == {"report", "state"}
    assert "do not produce any figure of your own" in prompt.instruction
    # No numeric field in the contract, same rule as every other.
    assert not {"saving", "cost", "amount"} & set(JudgeVerdict.model_fields)


def test_the_model_judge_marks_the_claims_the_model_named() -> None:
    from evals.harness.judge import JudgeVerdict, ModelJudge, _prose

    from costsentinel.llm.base import LLMTask, Prompt

    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    report, state = run_pipeline(dataset, eval_settings(_STATE))
    doubted = _prose(report)[0][1]

    class _Doubting:
        @property
        def name(self) -> str:
            return "doubting-llm"

        def structured(self, *, task: LLMTask, prompt: Prompt, schema: type) -> object:
            _ = (task, prompt, schema)
            return JudgeVerdict(
                unsupported_claims=(doubted,), verdict="the summary overstates the estate"
            )

    result = ModelJudge(_Doubting()).score(report, state)  # type: ignore[arg-type]
    assert result.unsupported
    assert any("overstates" in claim.reason for claim in result.unsupported)
    assert result.score is not None and result.score < 1


def test_the_dataset_provider_exposes_what_the_harness_needs() -> None:
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    provider = DatasetProvider(dataset)

    assert provider.name == "eval-dataset:baseline-mixed"
    assert provider.dataset is dataset
    assert provider.catalogue.is_real_api_pull

    subscription = provider.list_subscriptions()[0]
    assert provider.list_subscriptions(client=dataset.estate.client) == (subscription,)
    assert provider.list_subscriptions(client="somebody-else") == ()
    assert provider.list_reservations(client="somebody-else") == ()

    resource_id = provider.resource_id_for("vm-web-01")
    assert resource_id is not None and resource_id.startswith("/subscriptions/")
    assert provider.resource_id_for("no-such-resource") is None


def test_an_unmeasured_resource_reports_no_metrics() -> None:
    """``None`` is a label in its own right.

    A dataset can state that a resource was never measured, and a detector that
    flags it anyway has invented a fact.
    """
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    provider = DatasetProvider(dataset)
    disk_id = provider.resource_id_for("disk-web-01-os")
    assert disk_id is not None
    assert provider.get_resource_metrics(disk_id) is None


def test_a_sku_the_catalogue_does_not_price_is_unknown_not_guessed(
    tmp_path: Path,
) -> None:
    source = json.loads((datasets_dir() / "injection-control.json").read_text(encoding="utf-8"))
    for resource in source["estate"]["resources"]:
        if resource["name"] == "vm-sandbox-busy":
            resource["sku"] = "Standard_NoSuchSize"
    path = tmp_path / "unpriced.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    provider = DatasetProvider(load_dataset(path))
    resource = next(
        r
        for r in provider.list_resources(source["estate"]["subscriptions"][0]["subscription_id"])
        if r.name == "vm-sandbox-busy"
    )
    assert resource.monthly_cost.amount is None
    assert "no price for Standard_NoSuchSize" in (resource.monthly_cost.provenance.reference or "")


def test_the_cost_series_is_flat_so_observed_spend_is_exact() -> None:
    """A varied series would make the figure depend on a seed.

    This phase scores figures exactly, so there is nothing to sample.
    """
    dataset = next(d for d in load_all() if d.dataset_id == "baseline-mixed")
    provider = DatasetProvider(dataset)
    subscription = provider.list_subscriptions()[0].subscription_id
    series = provider.get_cost_series(subscription)
    assert series.window_days == 30
    assert len({point.amount for point in series.points}) == 1
    assert series.observed_spend().amount == dataset.estate.daily_spend * 30
