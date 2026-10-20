"""The rendered document, which is the only artefact a client actually reads.

Until this phase every check in the project stopped at the internal `ClientReport`.
The figures on it were asserted against committed ground truth and every model-written
sentence was traced back to state -- and the Markdown a client opens was checked by
nothing. A figure can be right in the object and wrong in the output: reformatted,
rounded, pulled from the wrong field, or interpolated into a sentence.

So the claims here are about the *text*:

* every decimal in the document comes from the report -- rendering may copy and may
  never compose;
* the three headline figures appear exactly as ``display()`` renders them;
* an unknown prints as "unknown", never as zero and never as blank;
* an exempt item shows a cost and never a saving;
* an anomaly's difference is labelled a change in spend, never an available saving;
* the same input renders byte-identically, pinned by a golden file.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.exemption import ExemptionReason
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.report import (
    ApprovalQueueItem,
    ClientReport,
    ExemptFinding,
    ReportAnomaly,
    ReportFinding,
    ReportPeriod,
    ReportSeverity,
    ReportTotals,
)
from costsentinel.domain.signals import AnomalyDirection, AnomalyKind, WasteKind
from costsentinel.reports.markdown import (
    SYNTHETIC_BANNER,
    headline_displays,
    is_synthetic,
    model_authored,
    monetary_strings,
    render_markdown,
    rendered_figures,
    unrendered_figures,
)

#: The golden file: one report exercising all five sections, rendered byte for byte.
GOLDEN = Path("tests/golden/full-report.md")

STAMP = datetime(2026, 10, 19, tzinfo=UTC)
SUB = "00000000-0000-4000-8000-00000000c001"
REAL_SUB = "7f2c1b40-9a3e-4f21-8c55-1d4e6b9a7c32"


def _retail() -> Provenance:
    return Provenance(
        source=ProvenanceSource.AZURE_RETAIL_PRICES,
        retrieved_at=datetime(2026, 10, 1, tzinfo=UTC),
        reference="meter 00000000-0000-0000-0000-000000000001",
        verification=Verification.VERIFIED,
    )


def _money(amount: str) -> MoneyAmount:
    return MoneyAmount.of(Decimal(amount), provenance=_retail())


def _finding(
    *,
    rank: int,
    name: str,
    saving: str | None,
    action_class: ActionClass = ActionClass.REVIEW,
    estimated: bool = False,
) -> ReportFinding:
    monthly = (
        _money(saving)
        if saving is not None
        else MoneyAmount.undetermined("GP_Gen5_2 is the smallest tier the catalogue carries")
    )
    annual = (
        _money(str(Decimal(saving) * 12))
        if saving is not None
        else MoneyAmount.undetermined("monthly saving unknown, so annual saving is unknown")
    )
    return ReportFinding(
        rank=rank,
        recommendation_id=f"rec-{name}",
        signal_id=f"ws-{name}",
        waste_kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        subscription_id=SUB,
        resource_id=f"/subscriptions/{SUB}/vm/{name}",
        resource_name=name,
        current_monthly_cost=_money("280.32"),
        proposed_action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        action_class=action_class,
        requires_approval=action_class is not ActionClass.ALLOW,
        confidence=Decimal("0.90"),
        rationale=f"{name} has been idle for the whole window.",
        note=f"A model's note about {name}.",
        root_cause=f"{name} outlived the workload it was provisioned for.",
        root_cause_factors=("cpu max pct was observed as 2.1",),
        ranking_rationale=f"{name} sits here because its saving is the largest available.",
        ranking_breakdown="quantified; savings 1.000, safety 0.600, confidence 0.900 -> 0.885",
        ranking_composite=Decimal("0.885"),
        evidence=("cpu_max_pct: 2.1", "observation_days: 30"),
        preconditions=("Virtual machine still exists and is still running.",),
        monthly_saving=monthly,
        annual_saving=annual,
        savings_basis=(
            f"Deallocating {name} removes the whole 280.32 USD monthly charge."
            if saving is not None
            else f"{name}: no cheaper tier exists, so no saving can be quantified."
        ),
        savings_is_estimated=estimated,
    )


def _exempt() -> ExemptFinding:
    return ExemptFinding(
        signal_id="ws-standby",
        waste_kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        subscription_id=SUB,
        resource_id=f"/subscriptions/{SUB}/vm/vm-dr-standby",
        resource_name="vm-dr-standby",
        current_monthly_cost=_money("140.16"),
        confidence=Decimal("0.90"),
        detector="idle_virtual_machine/v1",
        exemption_reason=ExemptionReason.DISASTER_RECOVERY,
        exemption_rule="builtin-exemptions-v1:disaster_recovery",
        exemption_statement=(
            "This resource is tagged as a disaster-recovery standby. Being idle is "
            "what a standby is for. Tag: role=disaster-recovery-standby."
        ),
        matched_tag="role=disaster-recovery-standby",
        evidence=("cpu_max_pct: 2.1",),
    )


def _anomaly() -> ReportAnomaly:
    return ReportAnomaly(
        anomaly_id="ca-levelshift-abc1234567",
        scope=SUB,
        subscription_id=SUB,
        kind=AnomalyKind.LEVEL_SHIFT,
        direction=AnomalyDirection.INCREASE,
        baseline=_money("120.24"),
        observed=_money("209.79"),
        delta=_money("89.55"),
        observed_from=date(2026, 9, 25),
        observed_to=date(2026, 10, 1),
        deviation_score=Decimal("25.31"),
        confidence=Decimal("0.85"),
        detector="spend_level_shift/v1",
        statement=(
            "Daily spend rose from a baseline of 120.24 USD to 209.79 USD over "
            "2026-09-25 to 2026-10-01, a difference of 89.55 USD."
        ),
        note="A model's note about the level shift.",
        evidence=("baseline_daily_spend: 120.24", "weekday_median.mon: 120.10"),
    )


def _report(
    *,
    findings: tuple[ReportFinding, ...],
    exempt: tuple[ExemptFinding, ...] = (),
    anomalies: tuple[ReportAnomaly, ...] = (),
    unassessed: tuple[str, ...] = (),
    subscriptions: tuple[str, ...] = (SUB,),
) -> ClientReport:
    priced = [f.monthly_saving.amount for f in findings if f.monthly_saving.amount is not None]
    monthly = sum(priced, start=Decimal(0))
    exempt_cost = (
        MoneyAmount.of(
            sum((i.current_monthly_cost.amount or Decimal(0) for i in exempt), start=Decimal(0)),
            provenance=Provenance.calculated("sum of the current monthly cost of exempt resources"),
        )
        if exempt
        else None
    )
    return ClientReport(
        report_id="rpt-run-golden",
        run_id="run-golden",
        client="acme-industrial",
        generated_at=STAMP,
        period=ReportPeriod(start=date(2026, 9, 2), end=date(2026, 10, 1), days=30),
        subscriptions_in_scope=subscriptions,
        resources_examined=12,
        severity=ReportSeverity.HIGH,
        executive_summary=(
            "CostSentinel identified 2 actionable finding(s) worth 560.64 USD per "
            "month against observed spend of 2,400.00 USD."
        ),
        summary_provenance=Provenance(
            source=ProvenanceSource.LLM_INFERENCE,
            retrieved_at=datetime(2026, 10, 1, tzinfo=UTC),
            reference="fake-llm:report_prose",
            verification=Verification.UNVERIFIED,
        ),
        totals=ReportTotals(
            observed_monthly_spend=_money("2400.00"),
            projected_monthly_savings=MoneyAmount.of(
                monthly, provenance=Provenance.calculated("sum of priced findings, monthly")
            ),
            projected_annual_savings=MoneyAmount.of(
                monthly * 12,
                provenance=Provenance.calculated("sum of priced findings, annual"),
            ),
            findings_count=len(findings),
            awaiting_approval_count=sum(1 for f in findings if f.requires_approval),
            automatable_count=0,
            blocked_count=sum(1 for f in findings if f.action_class is ActionClass.BLOCK),
            unpriced_findings_count=sum(1 for f in findings if not f.monthly_saving.is_known),
            anomaly_count=len(anomalies),
            unassessed_scope_count=len(unassessed),
            exempt_findings_count=len(exempt),
            exempt_monthly_cost=exempt_cost,
        ),
        findings=findings,
        exempt_findings=exempt,
        anomalies=anomalies,
        unassessed_spend=unassessed,
        approval_queue=tuple(
            ApprovalQueueItem(
                recommendation_id=f.recommendation_id,
                resource_name=f.resource_name,
                proposed_action=f.proposed_action,
                action_class=f.action_class,
                monthly_saving_display=f.monthly_saving.display(),
                reason=f"{f.resource_name} needs your approval.",
            )
            for f in findings
            if f.requires_approval
        ),
        notes=("Nothing in this report has been applied.",),
    )


def section(text: str, heading: str) -> str:
    """One `##` section of the document, up to the next one.

    Splitting on the horizontal rule looks simpler and is wrong: a Markdown table's
    separator row is also `---`, so it truncates every section that contains a table,
    which is all of them.
    """
    assert heading in text, heading
    after = text.split(heading, 1)[1]
    return after.split("\n## ", 1)[0]


@pytest.fixture
def full() -> ClientReport:
    """A report with all five sections populated.

    Hand-built rather than taken from a pipeline run, deliberately: the golden file
    below should move when the *renderer* changes and at no other time. Deriving it
    from the mock estate would make every unrelated fixture change a golden-file diff,
    which is how a golden file stops being read.
    """
    return _report(
        findings=(
            _finding(rank=1, name="vm-reporting-03", saving="280.32"),
            _finding(rank=2, name="vm-etl-02", saving="280.32", estimated=True),
            _finding(rank=3, name="snap-old", saving="67.58", action_class=ActionClass.BLOCK),
            _finding(rank=4, name="sql-reporting", saving=None),
        ),
        exempt=(_exempt(),),
        anomalies=(_anomaly(),),
        unassessed=(f"{SUB}: 5 of 30 day(s) have no data, which is unknown, not zero.",),
    )


@pytest.fixture
def minimal() -> ClientReport:
    """One priced finding and nothing else, so the empty-section paths are exercised."""
    return _report(findings=(_finding(rank=1, name="vm-only", saving="100.00"),))


# ---------------------------------------------------------------------------
# The five sections
# ---------------------------------------------------------------------------


def test_every_section_appears_when_it_has_content(full: ClientReport) -> None:
    """Five sections because there are five different claims a client must not conflate."""
    text = render_markdown(full)
    for heading in (
        "## Executive summary",
        "## Recommended actions",
        "## Worth investigating — saving unknown",
        "## Flagged but exempt — no action proposed",
        "## Spend changes — investigate",
        "## Spend we could not assess",
        "## Awaiting your decision",
        "## How this report was produced",
    ):
        assert heading in text, heading


def test_an_empty_section_is_omitted_rather_than_left_hanging(minimal: ClientReport) -> None:
    """A heading with nothing under it reads as a missing section, not an empty one."""
    text = render_markdown(minimal)
    assert "## Recommended actions" in text
    for heading in (
        "## Worth investigating",
        "## Flagged but exempt",
        "## Spend changes",
        "## Spend we could not assess",
    ):
        assert heading not in text, heading


def test_an_unquantified_finding_gets_its_own_section_rather_than_the_bottom_of_a_list(
    full: ClientReport,
) -> None:
    """The point of the fifth section.

    The ranking rule puts unquantified findings last (D104), which is right for an
    order and wrong for a document -- the bottom of a long ranked list is exactly where
    an unknown quietly becomes a zero. Named section, named resource.
    """
    text = render_markdown(full)
    body = section(text, "## Worth investigating — saving unknown")
    assert "sql-reporting" in body
    assert "unknown USD" in body
    assert "no cheaper tier exists" in body
    # And it is not in the ranked list.
    ranked = section(text, "## Recommended actions")
    assert "sql-reporting" not in ranked


# ---------------------------------------------------------------------------
# Every figure equals its source
# ---------------------------------------------------------------------------


def test_no_decimal_in_the_document_was_introduced_by_rendering(full: ClientReport) -> None:
    """Rendering may copy and may never compose (D109).

    The invariant the whole phase exists for. A rounded total, a reformatted amount or
    a sum the renderer computed itself would appear in neither the report's figures nor
    any string it carries.
    """
    text = render_markdown(full)
    assert monetary_strings(text), "nothing numeric in the document, so this asserts nothing"
    assert unrendered_figures(full, text) == []


def test_the_headline_figures_appear_exactly_as_the_domain_renders_them(
    full: ClientReport,
) -> None:
    """Verbatim, not by membership.

    "Some figure from the report appears somewhere" is a weak claim about a headline. A
    reformatted or swapped total is the error a client would act on.
    """
    text = render_markdown(full)
    for display in headline_displays(full):
        assert display in text, display
    assert full.totals.projected_monthly_savings.display() == "628.22 USD"


def test_a_corrupted_figure_is_caught(full: ClientReport) -> None:
    """Red, then green. A check nobody has seen fail is a check nobody should trust.

    One digit changed in the rendered text -- the kind of thing a careless reformat
    would do -- and the check names the offending figure.
    """
    text = render_markdown(full)
    assert unrendered_figures(full, text) == []

    corrupted = text.replace("628.22", "628.23", 1)
    assert corrupted != text
    offending = unrendered_figures(full, corrupted)
    assert offending == ["628.23"]


def test_a_recomputed_total_is_caught(full: ClientReport) -> None:
    """The realistic failure: a renderer that adds up the findings itself.

    Summing the four savings as a *float* gives 628.2199999999999, which rounds to the
    same cents here -- so the test uses the other realistic slip, rounding the total to
    whole pounds, which is what a template filter would do.
    """
    text = render_markdown(full).replace("628.22 USD", "628.00 USD")
    assert unrendered_figures(full, text) == ["628.00"]


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


def test_an_unknown_renders_as_unknown_and_never_as_zero(full: ClientReport) -> None:
    """A blank cell in a savings column reads as zero to every human who sees it."""
    text = render_markdown(full)
    body = section(text, "## Worth investigating — saving unknown")
    assert "unknown USD" in body
    assert "0.00 USD" not in body
    # And the summary counts it rather than absorbing it.
    assert "Findings we could not price" in text
    assert "excluded from the total" in text


def test_an_exempt_item_shows_a_cost_and_never_a_saving(full: ClientReport) -> None:
    """Its figure is what the client is paying, not an amount available to them."""
    text = render_markdown(full)
    body = section(text, "## Flagged but exempt — no action proposed")
    assert "**Costs now**" in body
    assert "140.16 USD / month" in body
    assert "Saving if applied" not in body, "an exempt item is not an opportunity"
    assert "role=disaster-recovery-standby" in body
    assert "excluded from the savings total" in body
    assert "what you are currently paying, not a saving available to you" in body


def test_an_anomalys_difference_is_labelled_a_change_not_a_saving(full: ClientReport) -> None:
    """An anomaly routes to an investigation, and the document must say so."""
    text = render_markdown(full)
    body = section(text, "## Spend changes — investigate")
    assert "**Difference**" in body
    assert "89.55 USD / day" in body
    assert "rather than an amount anyone has identified a way to recover" in body
    assert "CostSentinel proposes nothing here" in body
    # The headline savings does not include it.
    assert full.totals.projected_monthly_savings.amount == Decimal("628.22")


def test_the_report_states_where_its_figures_came_from(full: ClientReport) -> None:
    """Derived from the provenance flags, not from a hardcoded sentence.

    A hardcoded "prices from Azure Retail Prices" line would keep saying so after
    somebody ran the mock provider, which is the sort of claim a client would be right
    to stop trusting.
    """
    text = render_markdown(full)
    assert "Figures derived from | Azure Retail Prices" in text
    assert "Data retrieved | 2026-10-01" in text


def test_the_report_distinguishes_estimated_from_observed(full: ClientReport) -> None:
    """A rightsizing target is a judgement; a deleted disk's cost is a measurement."""
    text = render_markdown(full)
    assert "Savings that are observed costs | 2" in text
    assert "Savings that are modelled estimates | 1" in text
    assert "**observed**" in text
    assert "**estimated**" in text
    assert "only the first is a measurement" in text


def test_a_blocked_action_is_explained_rather_than_named(full: ClientReport) -> None:
    """`block` tells a client nothing; what happens next tells them something."""
    text = render_markdown(full)
    assert "CostSentinel will never perform this itself" in text
    assert "advice for a person to carry out by hand" in text


# ---------------------------------------------------------------------------
# Synthetic data says so
# ---------------------------------------------------------------------------


def test_a_synthetic_estate_is_banner_marked(full: ClientReport) -> None:
    """A synthetic estate says so, and the renderer works it out rather than being told.

    Detected from the nil-prefixed subscription ids, the same marker the commit guard
    uses, so a sample cannot be published without its banner by forgetting an argument.
    """
    assert is_synthetic(full)
    text = render_markdown(full)
    assert SYNTHETIC_BANNER in text
    assert "**Synthetic data.**" in text
    assert "correct about nothing" in text


def test_a_real_estate_carries_no_synthetic_banner() -> None:
    """The banner must not become decoration, or it stops being read."""
    real = _report(
        findings=(_finding(rank=1, name="vm-prod-01", saving="100.00"),),
        subscriptions=(REAL_SUB,),
    )
    assert not is_synthetic(real)
    text = render_markdown(real)
    assert SYNTHETIC_BANNER not in text
    assert "**Synthetic data.**" not in text


def test_the_synthetic_marker_matches_the_commit_guard() -> None:
    """One definition of "not a real tenant", rather than two that could drift."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_no_real_data", Path("scripts/check_no_real_data.py")
    )
    assert spec is not None
    assert spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    from costsentinel.reports.markdown import SYNTHETIC_SUBSCRIPTION_PREFIX

    assert SYNTHETIC_SUBSCRIPTION_PREFIX == guard.SYNTHETIC_PREFIX


# ---------------------------------------------------------------------------
# Model prose is quoted, not paraphrased
# ---------------------------------------------------------------------------


def test_every_declared_model_span_appears_verbatim(full: ClientReport) -> None:
    """The link that makes the object-level groundedness check mean anything.

    If the renderer paraphrased a model sentence, grading the report object would say
    nothing about the document. So the renderer declares what it emitted and this
    asserts it is there, word for word.
    """
    text = render_markdown(full)
    spans = model_authored(full)
    assert spans
    for span in spans:
        assert span.text in text, span.where


def test_the_declared_spans_cover_every_model_written_field(full: ClientReport) -> None:
    """A span the renderer forgot to declare is a sentence nothing grades."""
    where = {span.where for span in model_authored(full)}
    assert "executive_summary" in where
    assert any(name.startswith("root_cause[") for name in where)
    assert any(name.startswith("ranking_rationale[") for name in where)
    assert any(name.startswith("anomaly_note[") for name in where)


def test_the_summary_is_labelled_as_a_models_reading_of_the_figures(
    full: ClientReport,
) -> None:
    """Table first, prose second, prose labelled.

    Putting a model-written paragraph above the numbers invites a reader to take the
    paragraph as the finding.
    """
    text = render_markdown(full)
    table_at = text.index("| Observed spend over the period")
    prose_at = text.index(full.executive_summary)
    assert table_at < prose_at
    assert "written by a language model" in text
    assert "no number in it came from a language model" in text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_report_renders_byte_identically(full: ClientReport) -> None:
    """A document that differs on every render makes every diff noise."""
    assert render_markdown(full) == render_markdown(full)


def test_the_only_clock_in_the_body_is_the_injected_stamp(full: ClientReport) -> None:
    """So a golden file is possible and a diff means something."""
    first = render_markdown(full, generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = render_markdown(full, generated_at=datetime(2027, 6, 30, tzinfo=UTC))
    assert first != second
    assert first.replace("2026-01-01", "2027-06-30") == second


def test_the_golden_file_matches(full: ClientReport) -> None:
    """A full report, all five sections, byte for byte.

    The determinism tests above prove the renderer is stable within a run. This one
    proves it is stable across *changes*: a diff here is a change to the document a
    client reads, and it should be looked at rather than discovered by a client.

    Regenerate after a deliberate change with::

        UPDATE_GOLDEN=1 uv run pytest tests/test_reports.py -k golden

    and review the diff. Regeneration lives behind an environment variable rather than
    in a separate script so there is exactly one definition of the fixture -- a script
    that built its own copy would drift from the test and pin nothing.
    """
    rendered = render_markdown(full, generated_at=STAMP)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered, encoding="utf-8")
        pytest.skip(f"regenerated {GOLDEN}; re-run without UPDATE_GOLDEN to assert it")
    assert GOLDEN.exists(), (
        f"{GOLDEN} is missing; regenerate with "
        f"`UPDATE_GOLDEN=1 uv run pytest tests/test_reports.py -k golden`"
    )
    assert rendered == GOLDEN.read_text(encoding="utf-8"), (
        f"{GOLDEN} is out of date. If the change is intended, regenerate with "
        f"`UPDATE_GOLDEN=1 uv run pytest tests/test_reports.py -k golden` and review the diff."
    )


def test_the_permitted_figure_set_covers_the_non_monetary_decimals(
    full: ClientReport,
) -> None:
    """Confidences and deviation scores are the pipeline's arithmetic too.

    The object-level judge learned this in Phase 5: its first version collected money
    only and reported eight confidence scores as ungrounded. Worth not repeating one
    layer out, so the permitted set is every decimal the pipeline produced.
    """
    permitted = rendered_figures(full)
    assert "0.90" in permitted, "confidence"
    assert "25.31" in permitted, "deviation score"
    assert "0.88" in permitted, "ranking composite, quantised to two places"
