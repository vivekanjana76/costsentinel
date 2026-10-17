"""Exemption changes the exempt resources' handling, and nothing else at all.

The phase's quietest requirement and the easiest one to get wrong. Adding a whole new
stage between detection and planning is exactly the kind of change that moves a
number somewhere nobody was looking, and a cost report whose figures shifted for an
unrelated reason is a cost report a client stops believing.

So this is a differential, run over a real dataset rather than a contrived estate:
the same sweep with the exemption rules in force and with an empty ruleset, compared
field by field. Switching them off is done through configuration -- an empty rules
file is a perfectly valid one -- rather than through a flag in production code, which
keeps the harness's observer boundary intact: no module under ``src/costsentinel``
learns that a differential is being run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest
from evals.harness.dataset import LabelledEstate, select
from evals.harness.gates import cost_figure_accuracy, injection_resistance, safety_recall
from evals.harness.runner import run_pipeline
from tests.test_exemption_pipeline import (
    D4S_V5_MONTHLY,
    D8S_V5_MONTHLY,
    _idle_vm,
    _run,
)
from tests.test_exemption_pipeline import _settings as _pipeline_settings

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.report import ClientReport, ReportFinding
from costsentinel.domain.state import ScanState

DATASET = "adversarial-lookalikes"

#: The two resources the client's tags protect, and what the pipeline would otherwise
#: have proposed saving on each. Hand-derived from the committed catalogue: the
#: standby is a D8s_v5 whose whole compute charge would cease on deallocation, and the
#: snapshot is 512 GB at the committed 0.132 per GB.
EXEMPT_SAVINGS: dict[str, Decimal] = {
    "vm-dr-standby": Decimal("280.32"),
    "snap-clinical-retained": Decimal("67.58"),
}


def _settings(state_dir: Path, rules: Path | None = None) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=state_dir,
        exemption_rules_path=rules,
    )


def _empty_ruleset(path: Path) -> Path:
    """A valid ruleset with no rules, which is how exemption is switched off.

    Configuration, not a code path. A production module that knew it was being
    evaluated would be the thing this whole test file is guarding against.
    """
    path.write_text(
        json.dumps({"ruleset": "no-exemptions", "defaults": [], "clients": {}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def dataset() -> LabelledEstate:
    return select(dataset_id=DATASET)[0]


@pytest.fixture(scope="module")
def _runs(
    dataset: LabelledEstate, tmp_path_factory: pytest.TempPathFactory
) -> tuple[tuple[ClientReport, ScanState], tuple[ClientReport, ScanState]]:
    directory = tmp_path_factory.mktemp("differential")
    rules = _empty_ruleset(directory / "no-exemptions.json")
    with_exemption = run_pipeline(dataset, _settings(directory / "on"))
    without = run_pipeline(dataset, _settings(directory / "off", rules))
    return with_exemption, without


@pytest.fixture
def on(
    _runs: tuple[tuple[ClientReport, ScanState], tuple[ClientReport, ScanState]],
) -> ClientReport:
    return _runs[0][0]


@pytest.fixture
def off(
    _runs: tuple[tuple[ClientReport, ScanState], tuple[ClientReport, ScanState]],
) -> ClientReport:
    return _runs[1][0]


# ---------------------------------------------------------------------------


def test_switching_the_rules_off_really_does_switch_exemption_off(
    on: ClientReport, off: ClientReport
) -> None:
    """Otherwise the whole comparison below is between a run and itself."""
    assert {item.resource_name for item in on.exempt_findings} == set(EXEMPT_SAVINGS)
    assert off.exempt_findings == ()
    assert off.totals.exempt_monthly_cost is None
    assert len(off.findings) == len(on.findings) + len(EXEMPT_SAVINGS)


def test_only_the_exempt_resources_moved(on: ClientReport, off: ClientReport) -> None:
    """The set difference is exactly the two tagged resources.

    Nothing else appeared, and nothing else vanished.
    """
    before = {finding.resource_name for finding in off.findings}
    after = {finding.resource_name for finding in on.findings}
    assert before - after == set(EXEMPT_SAVINGS)
    assert after - before == set()


def _comparable(finding: ReportFinding) -> Mapping[str, object]:
    """Everything about a finding that exemption must not touch.

    A whole-model comparison was the first attempt and it was wrong in an instructive
    way: ``MoneyAmount.undetermined`` stamps a fresh ``retrieved_at``, so two runs
    minutes apart differ on a timestamp that means nothing here. Money is compared by
    amount, currency and rendered form, which is what a client sees and what the
    cost-figure gate checks.

    The rank and the ranking fields are excluded deliberately: a rank is a position in
    a list, and removing an entry necessarily renumbers what is left. The ordering is
    checked separately below.
    """
    return {
        "signal_id": finding.signal_id,
        "waste_kind": finding.waste_kind,
        "subscription_id": finding.subscription_id,
        "resource_id": finding.resource_id,
        "proposed_action": finding.proposed_action,
        "action_class": finding.action_class,
        "requires_approval": finding.requires_approval,
        "confidence": finding.confidence,
        "rationale": finding.rationale,
        "root_cause": finding.root_cause,
        "root_cause_factors": finding.root_cause_factors,
        "evidence": finding.evidence,
        "preconditions": finding.preconditions,
        "savings_basis": finding.savings_basis,
        "savings_is_estimated": finding.savings_is_estimated,
        "current_monthly_cost": (
            finding.current_monthly_cost.amount,
            finding.current_monthly_cost.currency,
            finding.current_monthly_cost.display(),
        ),
        "monthly_saving": (
            finding.monthly_saving.amount,
            finding.monthly_saving.currency,
            finding.monthly_saving.display(),
        ),
        "annual_saving": (
            finding.annual_saving.amount,
            finding.annual_saving.currency,
            finding.annual_saving.display(),
        ),
    }


def test_every_other_finding_is_identical_field_for_field(
    on: ClientReport, off: ClientReport
) -> None:
    """Every field a client reads, not just the money."""
    before = {finding.resource_name: finding for finding in off.findings}
    assert on.findings, "nothing survived exemption, so this asserts nothing"

    for finding in on.findings:
        original = before[finding.resource_name]
        assert _comparable(finding) == _comparable(original), finding.resource_name


def test_no_money_figure_moves_for_a_non_exempt_resource(
    on: ClientReport, off: ClientReport
) -> None:
    """Stated separately from the field-for-field check, because it is the one that matters.

    A savings figure that drifted because an unrelated resource was exempted would be
    the worst possible outcome of this phase: a number a client acts on, changed by
    something that has nothing to do with it.
    """
    before = {finding.resource_name: finding for finding in off.findings}
    for finding in on.findings:
        original = before[finding.resource_name]
        assert finding.monthly_saving.amount == original.monthly_saving.amount
        assert finding.annual_saving.amount == original.annual_saving.amount
        assert finding.current_monthly_cost.amount == original.current_monthly_cost.amount
        assert finding.savings_basis == original.savings_basis


def test_the_total_becomes_unknown_rather_than_zero(on: ClientReport, off: ClientReport) -> None:
    """The unknown-is-never-zero discipline, surviving a structural change.

    This dataset's only non-exempt finding is ``vm-claims-batch``, whose saving is
    genuinely unquantifiable -- no smaller same-family SKU covers its peak with the
    configured headroom. So exempting the two priced findings leaves a report with one
    unpriced finding, and the honest headline is **unknown**.

    Not ``0.00``, which would claim there is nothing to save. Not the old total, which
    would promise savings now withheld. And the count of unpriced findings is what
    says why, rather than the reader being left to infer it.
    """
    assert off.totals.projected_monthly_savings.amount == sum(EXEMPT_SAVINGS.values())

    assert on.totals.projected_monthly_savings.amount is None
    assert on.totals.projected_annual_savings.amount is None
    assert on.totals.findings_count == 1
    assert on.totals.unpriced_findings_count == 1
    assert "unknown" in on.totals.projected_monthly_savings.display().lower()


def test_the_total_drops_by_exactly_the_exempt_savings(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The subtractive arithmetic, on an estate where every finding is priced.

    Over the lookalikes dataset the on-run total is honestly unknown, which is
    correct and makes it the wrong place to check a subtraction. So this uses an
    estate of three priced idle machines and tags one: the total must fall by that
    machine's saving to the cent, and by nothing else.
    """
    settings = _pipeline_settings(tmp_path_factory)
    plain = [
        _idle_vm("vm-standby", tags={"owner": "a"}),
        _idle_vm("vm-reporting", tags={"owner": "b"}, monthly=D4S_V5_MONTHLY),
    ]
    off = _run(plain, settings)
    on = _run(
        [_idle_vm("vm-standby", tags={"role": "disaster-recovery-standby"}), plain[1]],
        settings,
    )
    assert off.report is not None
    assert on.report is not None

    off_total = off.report.totals.projected_monthly_savings.amount
    on_total = on.report.totals.projected_monthly_savings.amount
    assert off_total == D8S_V5_MONTHLY + D4S_V5_MONTHLY
    assert on_total == D4S_V5_MONTHLY
    assert off_total is not None
    assert on_total is not None
    assert off_total - on_total == D8S_V5_MONTHLY
    assert on.report.totals.projected_annual_savings.amount == on_total * 12

    # And the withheld saving reappears nowhere: the exempt total is a cost.
    exempt_cost = on.report.totals.exempt_monthly_cost
    assert exempt_cost is not None
    assert exempt_cost.amount == D8S_V5_MONTHLY


def test_the_exempt_resources_cost_what_they_always_cost(
    on: ClientReport, off: ClientReport
) -> None:
    """A provider fact does not move because a tag was read.

    The saving is withheld; the cost is not revised. For these two the two figures
    happen to coincide -- deallocating the standby and deleting the snapshot both save
    the whole charge -- which makes it a clean check that the exempt section reports
    the real number rather than a zero or a recomputation.
    """
    before = {finding.resource_name: finding for finding in off.findings}
    for item in on.exempt_findings:
        original = before[item.resource_name]
        assert item.current_monthly_cost.amount == original.current_monthly_cost.amount
        assert item.current_monthly_cost.amount == EXEMPT_SAVINGS[item.resource_name]
        assert item.waste_kind is original.waste_kind
        assert item.confidence == original.confidence

    exempt_cost = on.totals.exempt_monthly_cost
    assert exempt_cost is not None
    assert exempt_cost.amount == sum(EXEMPT_SAVINGS.values())


def test_the_relative_order_of_the_survivors_is_unchanged(
    on: ClientReport, off: ClientReport
) -> None:
    """Ranks renumber; they do not reshuffle."""
    survivors = [
        finding.resource_name
        for finding in sorted(off.findings, key=lambda f: f.rank)
        if finding.resource_name not in EXEMPT_SAVINGS
    ]
    assert [f.resource_name for f in sorted(on.findings, key=lambda f: f.rank)] == survivors


def test_every_gate_holds_with_exemption_on(
    dataset: LabelledEstate,
    _runs: tuple[tuple[ClientReport, ScanState], tuple[ClientReport, ScanState]],
) -> None:
    """The safety gate in particular. Exemption must not buy a pass by hiding a case."""
    on_report = _runs[0][0]
    for gate in (safety_recall(dataset, on_report), cost_figure_accuracy(dataset, on_report)):
        assert gate.passed, [failure.describe() for failure in gate.failures]
        assert gate.checked > 0, f"{gate.name} checked nothing"


def test_exemption_does_not_weaken_the_injection_gate(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The differential twins carry hostile tags now, so the gate must still compare them."""
    directory = tmp_path_factory.mktemp("injection")
    hostile = select(dataset_id="injection-hostile")[0]
    control = select(dataset_id="injection-control")[0]
    hostile_report, _ = run_pipeline(hostile, _settings(directory))
    control_report, _ = run_pipeline(control, _settings(directory))
    gate = injection_resistance(hostile, hostile_report, control_report)
    assert gate.passed, [failure.describe() for failure in gate.failures]
    assert gate.checked > 0
