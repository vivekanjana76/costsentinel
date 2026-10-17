"""The report tells a client both things: what to act on, and what we left alone.

The requirement being pinned here is arithmetic as much as presentation. A single
savings total that quietly absorbed exempt waste would misstate both numbers -- it
would promise savings nobody can realise, and it would hide that a decision was taken
on the client's behalf. So the headline total is the sum of the *actionable* findings
and nothing else, and the exempt items carry a **cost**, which is a provider fact,
rather than a saving, which would be a claim nobody is making.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from evals.harness.judge import FakeJudge
from tests.test_exemption_pipeline import (
    D4S_V5_MONTHLY,
    D8S_V5_MONTHLY,
    _idle_vm,
    _run,
    _settings,
)

from costsentinel.cli import summarise_report
from costsentinel.domain.exemption import ExemptionReason
from costsentinel.domain.report import ClientReport
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState


@pytest.fixture
def state(tmp_path_factory: pytest.TempPathFactory) -> ScanState:
    """A retention-exempt standby plus two ordinary idle machines."""
    return _run(
        [
            _idle_vm("vm-dr-standby", tags={"role": "disaster-recovery-standby"}),
            _idle_vm("vm-clinical-hold", tags={"retention": "HIPAA-6YR-REF-2291"}),
            _idle_vm("vm-reporting-03", tags={"owner": "bi-team"}, monthly=D4S_V5_MONTHLY),
        ],
        _settings(tmp_path_factory),
    )


@pytest.fixture
def report(state: ScanState) -> ClientReport:
    assert state.report is not None
    return state.report


def test_the_exempt_waste_is_in_the_report(report: ClientReport) -> None:
    """Surfaced, not suppressed. A thin report must not look like a clean estate."""
    assert report.has_exemptions
    names = {item.resource_name for item in report.exempt_findings}
    assert names == {"vm-dr-standby", "vm-clinical-hold"}
    assert all(item.waste_kind is WasteKind.IDLE_VIRTUAL_MACHINE for item in report.exempt_findings)


def test_each_exempt_item_shows_the_reason_and_the_tag(report: ClientReport) -> None:
    """A client must be able to see which of their own tags withheld the advice."""
    standby = next(i for i in report.exempt_findings if i.resource_name == "vm-dr-standby")
    held = next(i for i in report.exempt_findings if i.resource_name == "vm-clinical-hold")

    assert standby.exemption_reason is ExemptionReason.DISASTER_RECOVERY
    assert standby.matched_tag == "role=disaster-recovery-standby"
    assert standby.exemption_rule == "builtin-exemptions-v1:disaster_recovery"
    assert "standby is for" in standby.exemption_statement

    assert held.exemption_reason is ExemptionReason.RETENTION_REQUIRED
    assert held.matched_tag == "retention=HIPAA-6YR-REF-2291"
    assert "retention requirement" in held.exemption_statement


def test_an_exempt_item_keeps_its_evidence(report: ClientReport) -> None:
    """The client can check the detection as well as the exemption."""
    standby = next(i for i in report.exempt_findings if i.resource_name == "vm-dr-standby")
    assert any(line.startswith("cpu_max_pct: ") for line in standby.evidence)
    assert standby.detector == "idle_virtual_machine/v1"


def test_an_exempt_item_is_not_in_the_actionable_findings(report: ClientReport) -> None:
    """Which is the one thing the two adversarial cases turn on."""
    actionable = {finding.resource_name for finding in report.findings}
    assert actionable == {"vm-reporting-03"}
    assert report.totals.findings_count == 1


def test_the_savings_total_excludes_every_exempt_item(report: ClientReport) -> None:
    """The headline promises only what is actually available."""
    assert report.totals.projected_monthly_savings.amount == D4S_V5_MONTHLY
    assert report.totals.projected_annual_savings.amount == D4S_V5_MONTHLY * 12


def test_the_total_still_equals_the_sum_of_its_findings(report: ClientReport) -> None:
    """The arithmetic tie the cost-figure gate also checks, asserted locally."""
    priced = [
        finding.monthly_saving.amount
        for finding in report.findings
        if finding.monthly_saving.amount is not None
    ]
    assert report.totals.projected_monthly_savings.amount == sum(priced, start=Decimal(0))


def test_the_exempt_cost_is_a_cost_and_not_a_saving(report: ClientReport) -> None:
    """Two D8s_v5 machines. What the client spends on them, never offered as recoverable."""
    assert report.totals.exempt_findings_count == 2
    exempt_cost = report.totals.exempt_monthly_cost
    assert exempt_cost is not None
    assert exempt_cost.amount == D8S_V5_MONTHLY * 2
    # And it is nowhere in the savings.
    assert report.totals.projected_monthly_savings.amount != exempt_cost.amount
    assert exempt_cost.provenance.reference is not None
    assert "current monthly cost" in exempt_cost.provenance.reference


def test_no_exempt_item_renders_as_a_zero_saving(report: ClientReport) -> None:
    """Zero would be a claim that the standby is worth nothing, which is a different claim."""
    for item in report.exempt_findings:
        assert item.current_monthly_cost.amount is not None
        assert item.current_monthly_cost.amount > 0
    # There is no saving field on an exempt item at all.
    assert "monthly_saving" not in type(report.exempt_findings[0]).model_fields


def test_an_absent_exemption_is_none_rather_than_an_unknown_amount(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An absent exemption is not an unknown amount, and must not read as one."""
    state = _run([_idle_vm("vm-reporting-03", tags={})], _settings(tmp_path_factory))
    assert state.report is not None
    assert state.report.exempt_findings == ()
    assert state.report.totals.exempt_findings_count == 0
    assert state.report.totals.exempt_monthly_cost is None
    assert not state.report.has_exemptions


def test_the_exempt_items_lead_with_the_most_expensive(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The one the client is most likely to want to revisit."""
    state = _run(
        [
            _idle_vm("vm-small-standby", tags={"role": "standby"}, monthly=D4S_V5_MONTHLY),
            _idle_vm("vm-large-standby", tags={"role": "standby"}),
        ],
        _settings(tmp_path_factory),
    )
    assert state.report is not None
    assert [i.resource_name for i in state.report.exempt_findings] == [
        "vm-large-standby",
        "vm-small-standby",
    ]


def test_a_note_explains_the_exempt_section_without_a_model(report: ClientReport) -> None:
    """Node-authored, like the unpriced note. Not a sentence worth paraphrasing."""
    note = next(n for n in report.notes if "flagged but exempt" in n)
    assert "excluded from the projected savings" in note
    assert "rather than a saving available to you" in note
    assert "560.64" in note


def test_the_summary_prose_is_still_grounded(state: ScanState, report: ClientReport) -> None:
    """The exempt figures are the pipeline's own arithmetic, so the judge must accept them."""
    groundedness = FakeJudge().score(report, state)
    assert not groundedness.unsupported, [c.text for c in groundedness.unsupported]


def test_the_terminal_digest_shows_a_standby_outside_the_savings(report: ClientReport) -> None:
    """A sample a human reads: the DR standby, its reason, and where it does not count."""
    text = summarise_report(report)
    assert "flagged but exempt   2" in text
    assert "Flagged but exempt (no action proposed):" in text
    assert "vm-dr-standby" in text
    assert "exempt: disaster_recovery" in text
    assert "excluded from the savings above" in text
    # The standby's cost appears as a cost, and the savings line shows only the
    # actionable figure.
    assert f"costs {D8S_V5_MONTHLY:,.2f} USD/month" in text
    assert f"projected savings    {D4S_V5_MONTHLY:,.2f} USD/month" in text
