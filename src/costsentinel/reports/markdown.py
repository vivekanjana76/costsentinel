"""Rendering a :class:`~costsentinel.domain.report.ClientReport` as Markdown.

This is the document a client reads, which makes it the one place where being
approximately right is worst. So the renderer is deliberately dull.

**No template engine.** A report with a fixed set of sections is a function, not a
template, and a template is one more place a figure could be reformatted on the way
out -- a stray ``{{ "%.0f" | format(saving) }}`` is a rounding error nobody reviews.
The standard library is enough, and it means the Markdown path works with zero
optional extras (ARCHITECTURE.md D108).

**Every monetary value goes through** :meth:`~costsentinel.domain.common.MoneyAmount.display`,
the same call the terminal digest and the audit trail use. "The number in the document
equals the number in the object" is then true by construction rather than by
discipline -- and the evaluation harness asserts it anyway, because a guarantee worth
having is worth checking at the layer the client actually sees (D109).

**Unknown stays unknown.** ``display()`` renders an undetermined amount as
``unknown USD``, never ``0.00`` and never blank. A blank cell in a savings column
reads as zero to every human who sees one, which is the specific failure the domain
model's unknown-is-not-zero rule exists to prevent; it would be a shame to reintroduce
it in the last hundred lines.

**Five sections, because there are five different claims**, and flattening them into
one ranked list would make three of them unreadable:

1. the recommended actions -- act on these;
2. worth investigating with an unknown saving -- real waste nobody can cost yet;
3. flagged but exempt -- waste the client's own tags protect;
4. spend changes -- investigate, never act;
5. what could not be assessed -- gaps, stated rather than omitted.

Section 2 exists because of the Phase 7 ranking rule. An unquantified finding ranks
below every priced one (D104), which is right for an *order* and wrong for a
*document*: a finding nobody can cost is not thereby unimportant, and the bottom of a
long ranked list is exactly where an unknown quietly becomes a zero. Giving it a
heading resolves that honestly -- the ranking still refuses to claim it is the bigger
opportunity, and the reader still sees it by name (D110).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from costsentinel.domain.common import (
    Frozen,
    MoneyAmount,
    Provenance,
    ProvenanceSource,
)
from costsentinel.domain.recommendations import ActionClass
from costsentinel.domain.report import (
    ClientReport,
    ExemptFinding,
    ReportAnomaly,
    ReportFinding,
)

#: The horizontal rule between sections.
_RULE = "---"

#: A monetary decimal as the document prints it, grouped or not. Bare integers are
#: deliberately excluded: "30 days" and "8 findings" are not money, and a check that
#: flagged them would be a check somebody turns off.
_DECIMAL = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")

#: How a provenance source is described to a client. A client should not have to read
#: an enum to find out where their numbers came from.
_SOURCE_LABELS: dict[ProvenanceSource, str] = {
    ProvenanceSource.AZURE_RETAIL_PRICES: "Azure Retail Prices",
    ProvenanceSource.AZURE_COST_MANAGEMENT: "Azure Cost Management",
    ProvenanceSource.AZURE_RESOURCE_GRAPH: "Azure Resource Graph",
    ProvenanceSource.AZURE_MONITOR: "Azure Monitor",
    ProvenanceSource.AZURE_ADVISOR: "Azure Advisor",
    ProvenanceSource.MOCK_PROVIDER: "a synthetic demonstration estate",
    ProvenanceSource.CALCULATION: "arithmetic over the figures above",
    ProvenanceSource.POLICY_STORE: "the policy store",
    ProvenanceSource.OPERATOR: "an operator",
    ProvenanceSource.LLM_INFERENCE: "a language model",
}

#: Prefix of a deliberately synthetic subscription id -- the nil-prefixed RFC 4122
#: form no real Azure tenant issues. The same marker ``scripts/check_no_real_data.py``
#: uses to decide whether an id may be committed, and a test asserts the two agree:
#: one definition of "this is not a real tenant" rather than two that could drift.
SYNTHETIC_SUBSCRIPTION_PREFIX = "00000000-0000-"

#: Marks a report built from the synthetic estate. Prominent, and in the document
#: rather than only in a commit message: a sample that could be mistaken for a real
#: client's spend is a liability rather than a convenience.
SYNTHETIC_BANNER = (
    "> **This report describes a synthetic demonstration estate.** Every "
    "subscription, resource and figure in it is invented. It is not any real "
    "client's data and must not be read as a statement about one."
)


def _h(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def _table(rows: Iterable[tuple[str, str]]) -> list[str]:
    """A two-column table.

    Deliberately a table rather than a prose paragraph for anything numeric: a figure
    in a labelled cell is a figure a reader can check, and a figure inside a sentence
    is one they have to trust.
    """
    lines = ["| | |", "| --- | --- |"]
    lines += [f"| {label} | {value} |" for label, value in rows]
    return lines


def _class_phrase(action_class: ActionClass, *, requires_approval: bool) -> str:
    """What an action class means for the reader, not what it is called.

    "block" tells a client nothing. "CostSentinel will not perform this -- it is
    advice for a person to carry out by hand" tells them what happens next.
    """
    if action_class is ActionClass.BLOCK:
        return (
            "`block` -- CostSentinel will never perform this itself, however it is "
            "approved. It is advice for a person to carry out by hand."
        )
    if requires_approval:
        return "`review` -- requires your explicit approval before anything changes."
    return "`allow` -- safe and reversible; no approval needed."


def _figure_basis(is_estimated: bool) -> str:
    """Whether a saving is an observation or a modelling assumption.

    The distinction a client deciding what to approve first most needs: a deleted
    disk's cost is measured, and a rightsizing target is a judgement about what will
    fit.
    """
    if is_estimated:
        return "**estimated** -- depends on a modelling assumption, stated in the basis"
    return "**observed** -- the provider's reported cost for something being removed"


def _price_sources(report: ClientReport) -> tuple[set[ProvenanceSource], datetime | None]:
    """Where this report's figures came from, read off the figures themselves.

    Derived rather than declared. A hardcoded "prices from Azure Retail Prices" line
    would keep saying so after someone ran the mock provider, which is the sort of
    claim a client would be right to stop trusting.
    """
    sources: set[ProvenanceSource] = set()
    retrieved: list[datetime] = []
    for money in _all_money(report):
        provenance = money.provenance
        sources.add(provenance.source)
        retrieved.append(provenance.retrieved_at)
    return sources, min(retrieved) if retrieved else None


def _all_money(report: ClientReport) -> list[MoneyAmount]:
    """Every monetary amount on the report, for provenance and for assertions."""
    amounts: list[MoneyAmount] = [
        report.totals.observed_monthly_spend,
        report.totals.projected_monthly_savings,
        report.totals.projected_annual_savings,
    ]
    if report.totals.exempt_monthly_cost is not None:
        amounts.append(report.totals.exempt_monthly_cost)
    for finding in report.findings:
        amounts += [finding.current_monthly_cost, finding.monthly_saving, finding.annual_saving]
    for item in report.exempt_findings:
        amounts.append(item.current_monthly_cost)
    for anomaly in report.anomalies:
        amounts += [anomaly.baseline, anomaly.observed, anomaly.delta]
        for part in anomaly.attributed_components:
            amounts += [part.baseline, part.observed, part.contribution]
    for summary in report.waste_breakdown:
        amounts += [summary.monthly_saving, summary.annual_saving]
    return amounts


class ProseSpan(Frozen):
    """One stretch of model-authored text the document contains.

    The renderer declares these rather than leaving the harness to find them, because
    finding them by parsing would mean guessing which sentences a model wrote -- and a
    guess that drifted would quietly stop grading the prose it was built to grade.
    """

    where: str = Field(description="Which field of the report it came from, for a failure message.")
    text: str


def model_authored(report: ClientReport) -> tuple[ProseSpan, ...]:
    """Every model-written span this renderer puts in the document.

    Exactly the spans :func:`render_markdown` emits, in document order, so the
    evaluation harness can assert each one appears in the rendered text *verbatim* and
    then grade it for groundedness. The verbatim check matters as much as the grading:
    it proves the renderer quoted the model rather than paraphrasing it, which is the
    one way rendering could launder an ungrounded claim past a check that only ever
    looked at the report object (ARCHITECTURE.md D109).

    Deterministic prose -- a saving's basis, an exemption's statement, an anomaly's
    statement -- is deliberately absent. Those are composed by arithmetic from their
    own figures, and grading them would be grading the detector rather than the model.
    """
    spans: list[ProseSpan] = []
    if report.executive_summary:
        spans.append(ProseSpan(where="executive_summary", text=report.executive_summary))
    for finding in report.findings:
        for field, text in (
            ("root_cause", finding.root_cause),
            ("ranking_rationale", finding.ranking_rationale),
            ("memory_note", finding.memory_note),
        ):
            if text:
                spans.append(ProseSpan(where=f"{field}[{finding.resource_name}]", text=text))
    for anomaly in report.anomalies:
        if anomaly.note:
            spans.append(ProseSpan(where=f"anomaly_note[{anomaly.scope}]", text=anomaly.note))
    return tuple(spans)


def rendered_figures(report: ClientReport) -> frozenset[str]:
    """Every decimal string this report is entitled to print.

    Money, and the other computed decimals the document quotes: a detector's
    confidence and an anomaly's deviation score. Both are the pipeline's own
    arithmetic, and leaving them out would have the check report the report's own
    honest numbers as ungrounded -- the same mistake the object-level judge made on its
    first outing, which is worth not repeating.

    Exported because the evaluation harness checks the *rendered document* against it:
    a decimal in the output that is not in this set did not come from the report, which
    is the one thing rendering must never introduce (D109).
    """
    figures: set[str] = set()

    def add(value: Decimal) -> None:
        grouped = f"{value:,.2f}"
        figures.add(grouped)
        figures.add(grouped.replace(",", ""))

    for money in _all_money(report):
        if money.amount is not None:
            add(money.amount)
    for finding in report.findings:
        add(finding.confidence)
        if finding.ranking_composite is not None:
            add(finding.ranking_composite)
    for item in report.exempt_findings:
        add(item.confidence)
    for anomaly in report.anomalies:
        add(anomaly.confidence)
        add(anomaly.deviation_score)
    return frozenset(figures)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(report: ClientReport, generated_at: datetime, *, synthetic: bool) -> list[str]:
    lines = [_h(1, f"Cost review — {report.client}"), ""]
    if synthetic:
        lines += [SYNTHETIC_BANNER, ""]
    lines += _table(
        [
            ("Period", f"{report.period.start} to {report.period.end} ({report.period.days} days)"),
            ("Generated", generated_at.date().isoformat()),
            ("Subscriptions in scope", str(len(report.subscriptions_in_scope))),
            ("Resources examined", str(report.resources_examined)),
            ("Headline", report.severity.value),
            ("Report id", f"`{report.report_id}`"),
        ]
    )
    return [*lines, ""]


def _summary(report: ClientReport) -> list[str]:
    """The figures first, then the prose, and the prose labelled as prose.

    Deliberately this way round. Putting a model-written paragraph above the numbers
    invites a reader to take the paragraph as the finding; putting the table first
    makes the paragraph what it is -- a reading of figures the reader has already seen.
    """
    totals = report.totals
    rows: list[tuple[str, str]] = [
        ("Observed spend over the period", totals.observed_monthly_spend.display()),
        (
            "**Projected actionable savings**",
            f"**{totals.projected_monthly_savings.display()} / month**",
        ),
        ("Annualised", f"{totals.projected_annual_savings.display()} / year"),
        ("Actionable findings", str(totals.findings_count)),
        ("Awaiting your approval", str(totals.awaiting_approval_count)),
        ("CostSentinel may apply automatically", str(totals.automatable_count)),
    ]
    if totals.blocked_count:
        rows.append(("Advice for a person to carry out by hand", str(totals.blocked_count)))
    if totals.unpriced_findings_count:
        rows.append(
            (
                "Findings we could not price",
                f"{totals.unpriced_findings_count} (listed below, excluded from the total)",
            )
        )
    if totals.exempt_findings_count:
        cost = (
            totals.exempt_monthly_cost.display()
            if totals.exempt_monthly_cost is not None
            else "unknown"
        )
        rows.append(
            ("Flagged but exempt", f"{totals.exempt_findings_count} (costing {cost} / month)")
        )
    if totals.anomaly_count:
        rows.append(("Spend changes to investigate", str(totals.anomaly_count)))
    if totals.unassessed_scope_count:
        rows.append(
            ("Subscriptions whose spend we could not assess", str(totals.unassessed_scope_count))
        )

    lines = [_h(2, "Executive summary"), "", *_table(rows), ""]
    if report.executive_summary:
        lines += [report.executive_summary, ""]
        lines += [
            f"_The paragraph above was written by {_describe(report.summary_provenance)} from "
            f"the figures in the table. Every figure in this report is arithmetic over "
            f"provider data; no number in it came from a language model._",
            "",
        ]
    return lines


def _describe(provenance: Provenance) -> str:
    return _SOURCE_LABELS.get(provenance.source, provenance.source.value)


def _finding_block(finding: ReportFinding, *, position: int) -> list[str]:
    action = finding.proposed_action.value.replace("_", " ")
    lines = [
        _h(3, f"{position}. {finding.resource_name} — {action}"),
        "",
        *_table(
            [
                ("What", finding.waste_kind.value.replace("_", " ")),
                ("Subscription", f"`{finding.subscription_id}`"),
                ("Costs now", finding.current_monthly_cost.display() + " / month"),
                (
                    "**Saving if applied**",
                    f"**{finding.monthly_saving.display()} / month** "
                    f"({finding.annual_saving.display()} / year)",
                ),
                ("Figure", _figure_basis(finding.savings_is_estimated)),
                (
                    "Approval",
                    _class_phrase(
                        finding.action_class, requires_approval=finding.requires_approval
                    ),
                ),
                ("Detector confidence", f"{finding.confidence}"),
            ]
        ),
        "",
        f"**How the saving was derived.** {finding.savings_basis}",
        "",
    ]
    if finding.root_cause:
        lines += [f"**Why this exists.** {finding.root_cause}", ""]
    if finding.root_cause_factors:
        lines += [f"- {factor}" for factor in finding.root_cause_factors]
        lines.append("")
    if finding.ranking_rationale:
        lines += [f"**Why it sits here.** {finding.ranking_rationale}", ""]
    if finding.memory_note:
        moved = (
            f" (ranked #{finding.base_rank} on merit alone)"
            if finding.base_rank is not None and finding.base_rank != finding.rank
            else ""
        )
        lines += [f"**You have seen this before.** {finding.memory_note}{moved}", ""]
    if finding.evidence:
        lines += ["**Evidence.**", ""]
        lines += [f"- {line}" for line in finding.evidence]
        lines.append("")
    if finding.preconditions:
        lines += ["**Re-checked immediately before anything changes.**", ""]
        lines += [f"- {line}" for line in finding.preconditions]
        lines.append("")
    return lines


def _recommendations(findings: Sequence[ReportFinding]) -> list[str]:
    if not findings:
        return []
    lines = [
        _h(2, "Recommended actions"),
        "",
        "Ordered by a weighted composite of the saving, how safe the action is and the "
        "detector's confidence — not by saving alone. A smaller, safer, more certain "
        "saving is often the better first move.",
        "",
        "**Nothing in this report has been applied.** CostSentinel has no execution "
        "path: every action below is a proposal, and the gated ones cannot proceed "
        "without a recorded decision from you.",
        "",
    ]
    for position, finding in enumerate(findings, start=1):
        lines += _finding_block(finding, position=position)
    return lines


def _unquantified(findings: Sequence[ReportFinding]) -> list[str]:
    """Real waste nobody can put a figure on, given its own heading.

    These rank below every priced finding (D104) and that is deliberate: a tool must
    not claim the thing it cannot cost is the bigger opportunity. But the bottom of a
    ranked list is where an unknown quietly becomes a zero, so they get a section of
    their own and are named rather than buried (D110).
    """
    if not findings:
        return []
    lines = [
        _h(2, "Worth investigating — saving unknown"),
        "",
        "These are real findings. CostSentinel could not derive a figure for them, so "
        "they are **excluded from the savings total rather than counted as zero**, and "
        "they rank below every priced finding because we will not claim something we "
        "cannot cost is your bigger opportunity. That is not the same as saying they "
        "do not matter — each one says why no figure could be derived, and several are "
        "worth a decision.",
        "",
    ]
    for finding in findings:
        lines += [
            _h(3, f"{finding.resource_name} — {finding.proposed_action.value.replace('_', ' ')}"),
            "",
            *_table(
                [
                    ("What", finding.waste_kind.value.replace("_", " ")),
                    ("Subscription", f"`{finding.subscription_id}`"),
                    ("Costs now", finding.current_monthly_cost.display() + " / month"),
                    ("Saving", f"{finding.monthly_saving.display()} — see below"),
                    (
                        "Approval",
                        _class_phrase(
                            finding.action_class, requires_approval=finding.requires_approval
                        ),
                    ),
                ]
            ),
            "",
            f"**Why no figure.** {finding.savings_basis}",
            "",
        ]
        if finding.root_cause:
            lines += [f"**Why this exists.** {finding.root_cause}", ""]
        if finding.ranking_rationale:
            lines += [f"**Why it sits here.** {finding.ranking_rationale}", ""]
        if finding.memory_note:
            lines += [f"**You have seen this before.** {finding.memory_note}", ""]
    return lines


def _exempt(items: Sequence[ExemptFinding]) -> list[str]:
    if not items:
        return []
    lines = [
        _h(2, "Flagged but exempt — no action proposed"),
        "",
        "Real waste signals on resources **your own tags** mark as protected: a "
        "disaster-recovery standby, a retention requirement, an explicit cost "
        "exemption. They are shown because the spend is real and you may want to "
        "revisit it, and they are **excluded from the savings total** because nobody "
        "is proposing to recover it. The figure beside each one is what you are "
        "currently paying, not a saving available to you.",
        "",
    ]
    for item in items:
        lines += [
            _h(3, f"{item.resource_name} — {item.exemption_reason.value.replace('_', ' ')}"),
            "",
            *_table(
                [
                    ("What we detected", item.waste_kind.value.replace("_", " ")),
                    ("Subscription", f"`{item.subscription_id}`"),
                    ("**Costs now**", f"**{item.current_monthly_cost.display()} / month**"),
                    ("Exempt because", f"`{item.matched_tag}`" if item.matched_tag else "—"),
                    ("Rule", f"`{item.exemption_rule}`"),
                ]
            ),
            "",
            item.exemption_statement,
            "",
        ]
    return lines


def _anomalies(anomalies: Sequence[ReportAnomaly]) -> list[str]:
    if not anomalies:
        return []
    lines = [
        _h(2, "Spend changes — investigate"),
        "",
        "Spend in these subscriptions moved. This is **not** a list of waste and not a "
        "list of actions: a change in spend is something to look into, and the "
        "difference shown is what you are now paying differently rather than an amount "
        "anyone has identified a way to recover. None of it appears in the savings "
        "total, and CostSentinel proposes nothing here.",
        "",
    ]
    for anomaly in anomalies:
        lines += [
            _h(3, f"{anomaly.scope} — {anomaly.kind.value.replace('_', ' ')}"),
            "",
            *_table(
                [
                    ("Baseline", anomaly.baseline.display() + " / day"),
                    ("Observed", anomaly.observed.display() + " / day"),
                    (
                        "**Difference**",
                        f"**{anomaly.delta.display()} / day** ({anomaly.direction.value})",
                    ),
                    ("Window", f"{anomaly.observed_from} to {anomaly.observed_to}"),
                    ("How unusual", f"{anomaly.deviation_score} robust standard deviations"),
                    ("Attributed to", _attribution_summary(anomaly)),
                    ("Detector confidence", f"{anomaly.confidence}"),
                ]
            ),
            "",
            anomaly.statement,
            "",
            *_attribution_block(anomaly),
        ]
        if anomaly.note:
            lines += [anomaly.note, ""]
    return lines


def _attribution_summary(anomaly: ReportAnomaly) -> str:
    """The one-line "what caused it" for the anomaly's own table.

    Present on every anomaly, including the unattributed ones. A row that simply
    vanished when nothing could be named would read as "no cause" to anybody who had
    seen an attributed anomaly in the same report -- which is the opposite of what an
    unattributed outcome means.
    """
    if not anomaly.attributed_components:
        return _UNATTRIBUTED_SHORT
    return ", ".join(part.label for part in anomaly.attributed_components)


def _attribution_block(anomaly: ReportAnomaly) -> list[str]:
    """What accounted for the change, in plain numbers a client can check.

    Every figure here is copied from the attribution, which computed it from the
    provider's own breakdown. The share is printed because the two numbers either side
    of it are not enough on their own: a meter rising 90 USD a day matters differently
    depending on whether that is the whole change or a tenth of it.
    """
    if not anomaly.attributed_components:
        return [f"**What changed.** {anomaly.attribution_statement}", ""]

    lines = ["**What changed.**", ""]
    lines += _table_with_header(
        ("Meter", "Was", "Now", "Difference", "Share of the change"),
        [
            (
                part.label,
                f"{part.baseline.display()} / day",
                f"{part.observed.display()} / day",
                f"{part.contribution.display()} / day ({part.direction.value})",
                f"{part.share * Decimal(100):.0f}%",
            )
            for part in anomaly.attributed_components
        ],
    )
    # The statement is deliberately *not* repeated here. It says exactly what the
    # table above says, in a sentence, and printing both made the section read as
    # though there were two findings. The sentence survives on the report object, where
    # the harness grades it, and the table is what a client reads.
    lines.append("")
    return lines


#: The short form for the anomaly table, where the full explanation would not fit. The
#: long form is always rendered below it, so this is a pointer and never the whole
#: claim.
_UNATTRIBUTED_SHORT = "no single cause identified — see below"


def _table_with_header(
    header: tuple[str, ...], rows: Sequence[tuple[str, ...]]
) -> list[str]:
    """A Markdown table with a header row.

    Distinct from :func:`_table`, which renders the headerless two-column form used
    for every "label: value" block in the document. A table whose columns each mean
    something different needs them named.
    """
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _unassessed(lines_in: Sequence[str]) -> list[str]:
    if not lines_in:
        return []
    return [
        _h(2, "Spend we could not assess"),
        "",
        "These subscriptions were examined and no conclusion was drawn. Days missing "
        "from a cost series are **unknown, not zero** — reading a gap as zero spend "
        "would produce a confident and entirely fictional drop, so nothing is claimed "
        "instead.",
        "",
        *[f"- {line}" for line in lines_in],
        "",
    ]


def _approval_queue(report: ClientReport) -> list[str]:
    if not report.approval_queue:
        return []
    lines = [
        _h(2, "Awaiting your decision"),
        "",
        "| Resource | Action | Class | Saving / month |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {item.resource_name} | {item.proposed_action.value.replace('_', ' ')} | "
        f"`{item.action_class.value}` | {item.monthly_saving_display} |"
        for item in report.approval_queue
    ]
    return [*lines, ""]


def _provenance_section(report: ClientReport, *, synthetic: bool) -> list[str]:
    sources, retrieved = _price_sources(report)
    described = sorted(
        {
            _SOURCE_LABELS.get(source, source.value)
            for source in sources
            if source is not ProvenanceSource.CALCULATION
        }
    )
    # Only priced findings are counted here. An unquantified saving is neither an
    # observed cost nor a modelled estimate, and folding it into either number would
    # overstate how much of this report is a measurement.
    priced = [finding for finding in report.findings if finding.monthly_saving.is_known]
    estimated = sum(1 for finding in priced if finding.savings_is_estimated)
    observed = len(priced) - estimated

    lines = [
        _h(2, "How this report was produced"),
        "",
        *_table(
            [
                ("Figures derived from", ", ".join(described) if described else "—"),
                (
                    "Data retrieved",
                    retrieved.date().isoformat() if retrieved is not None else "unknown",
                ),
                ("Savings that are observed costs", str(observed)),
                ("Savings that are modelled estimates", str(estimated)),
            ]
        ),
        "",
        "**Estimated or observed.** An *observed* saving is the provider's reported "
        "cost for something being removed outright — delete the disk, that charge "
        "stops. An *estimated* saving depends on a modelling assumption, such as which "
        "smaller SKU a workload will fit on; each one states its assumption in its "
        "basis. Both are arithmetic over provider data and neither is produced by a "
        "model, but only the first is a measurement.",
        "",
        "**Unknown is not zero.** Where CostSentinel cannot derive a figure it prints "
        "`unknown` and excludes the finding from the totals. It never prints a zero it "
        "has not measured.",
        "",
    ]
    if synthetic:
        lines += [
            "**Synthetic data.** The rates above are real published Azure prices, but "
            "the estate they are applied to is invented: these subscriptions, "
            "resources and utilisation figures describe no real client. Any total in "
            "this report is therefore arithmetic that is correct about nothing.",
            "",
        ]
    return lines


def _notes(report: ClientReport) -> list[str]:
    if not report.notes and not report.incomplete_reasons:
        return []
    lines = [_h(2, "Notes"), ""]
    lines += [f"- {note}" for note in report.notes]
    if report.incomplete_reasons:
        lines += ["", "**This report may be incomplete.**", ""]
        lines += [f"- {reason}" for reason in report.incomplete_reasons]
    return [*lines, ""]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def is_synthetic(report: ClientReport) -> bool:
    """Whether this report describes a demonstration estate rather than a real one.

    Derived rather than passed in, so a sample cannot be published without its banner
    by forgetting an argument.

    Two signals, because one is not enough. A figure provenanced to the mock provider
    is conclusive -- but since Phase 3 the mock estate is priced from the *real*
    committed Azure catalogue, so its money carries ``AZURE_RETAIL_PRICES`` provenance
    and that signal alone misses it. The second is the subscription ids: every one
    nil-prefixed means no real tenant issued them, which is exactly the test the
    commit guard applies before letting an id into the repository.
    """
    sources, _ = _price_sources(report)
    if ProvenanceSource.MOCK_PROVIDER in sources:
        return True
    return bool(report.subscriptions_in_scope) and all(
        subscription.startswith(SYNTHETIC_SUBSCRIPTION_PREFIX)
        for subscription in report.subscriptions_in_scope
    )


def render_markdown(report: ClientReport, *, generated_at: datetime | None = None) -> str:
    """Render one client's report as Markdown.

    Args:
        report: The report to render. Its own ordering is preserved: findings are
            already in rank order and every other section is sorted deterministically
            by the node that built it, so nothing here re-sorts and nothing iterates a
            set.
        generated_at: Overrides the report's own timestamp. The *only* variable in the
            body, and injectable for exactly one reason: a document that differs on
            every render makes every diff noise and a golden file impossible.

    Returns:
        A complete Markdown document. Byte-identical for the same input.
    """
    stamp = generated_at or report.generated_at
    synthetic = is_synthetic(report)

    quantified = [f for f in report.findings if f.monthly_saving.is_known]
    unquantified = [f for f in report.findings if not f.monthly_saving.is_known]

    sections: list[list[str]] = [
        _header(report, stamp, synthetic=synthetic),
        _summary(report),
        _recommendations(quantified),
        _unquantified(unquantified),
        _exempt(report.exempt_findings),
        _anomalies(report.anomalies),
        _unassessed(report.unassessed_spend),
        _approval_queue(report),
        _notes(report),
        _provenance_section(report, synthetic=synthetic),
    ]

    body: list[str] = []
    for section in sections:
        if not section:
            continue
        if body:
            body += [_RULE, ""]
        body += section

    # One trailing newline, like every other text file in the repository.
    return "\n".join(body).rstrip("\n") + "\n"


def source_text(report: ClientReport) -> str:
    """Every string the report carries, concatenated.

    The second half of the rendering guarantee. A decimal in the document is
    acceptable if it is a figure on the report *or* if it already appears in text the
    report holds -- a catalogue price quoted inside a savings basis, a figure inside a
    resource name, a score inside a ranking breakdown. All of those were composed by
    the pipeline or supplied by the provider, and reproducing them is the renderer
    doing its job.

    What this does *not* permit is a decimal the renderer produced itself: a rounded
    total, a reformatted amount, a sum it computed. Those appear in neither set, which
    is the claim worth making -- **rendering may copy and may never compose** (D109).
    """
    parts: list[str] = [
        report.client,
        report.executive_summary,
        report.memory_recall,
        report.period.start.isoformat(),
        report.period.end.isoformat(),
        *report.subscriptions_in_scope,
        *report.notes,
        *report.incomplete_reasons,
        *report.unassessed_spend,
    ]
    for finding in report.findings:
        parts += [
            finding.resource_name,
            finding.resource_id,
            finding.subscription_id,
            finding.rationale,
            finding.note,
            finding.root_cause,
            finding.ranking_rationale,
            finding.ranking_breakdown,
            finding.savings_basis,
            finding.memory_note,
            *finding.root_cause_factors,
            *finding.evidence,
            *finding.preconditions,
        ]
    for item in report.exempt_findings:
        parts += [
            item.resource_name,
            item.resource_id,
            item.subscription_id,
            item.exemption_statement,
            item.exemption_rule,
            item.matched_tag,
            *item.evidence,
        ]
    for anomaly in report.anomalies:
        parts += [
            anomaly.scope,
            anomaly.subscription_id,
            anomaly.statement,
            anomaly.attribution_statement,
            anomaly.note,
            anomaly.detector,
            *anomaly.evidence,
        ]
        for part in anomaly.attributed_components:
            parts += [part.key, part.label]
    for queued in report.approval_queue:
        parts += [queued.resource_name, queued.monthly_saving_display, queued.reason]
    for summary in report.waste_breakdown:
        parts.append(summary.waste_kind.value)
    return "\n".join(part for part in parts if part)


def headline_displays(report: ClientReport) -> tuple[str, ...]:
    """The three figures a client reads first, as the document must print them.

    Checked verbatim rather than by membership. "Some figure from the report appears
    somewhere" is a weak claim about a headline; "the observed spend, the monthly
    saving and the annual saving appear exactly as ``display()`` renders them" is the
    one that catches a reformatted or swapped total.
    """
    return (
        report.totals.observed_monthly_spend.display(),
        report.totals.projected_monthly_savings.display(),
        report.totals.projected_annual_savings.display(),
    )


def monetary_strings(text: str) -> list[str]:
    """Every decimal in a rendered document, for the figure-accuracy check.

    Kept here rather than in the harness so the renderer and the check agree on what
    counts as a figure. Bare integers are deliberately not matched: "30 days" and
    "8 findings" are not money, and a check that flagged them would be a check someone
    turns off.
    """
    return _DECIMAL.findall(text)


def unrendered_figures(report: ClientReport, text: str) -> list[str]:
    """Decimals in the document that the report did not supply.

    The whole point of the module, reduced to one function the harness can call. An
    empty list is the invariant: rendering may drop a figure, reorder figures or
    describe them, and may never *introduce* one.

    A decimal passes if it is a figure on the report, or if it already appears in a
    string the report carries (see :func:`source_text`). The second clause is not a
    loophole -- it is the difference between copying and composing, and a figure the
    renderer computed appears in neither.
    """
    permitted = rendered_figures(report)
    carried = source_text(report)
    offending: list[str] = []
    for figure in monetary_strings(text):
        if figure in permitted or figure.replace(",", "") in permitted:
            continue
        if figure in carried:
            continue
        offending.append(figure)
    return offending


def quantified_total(report: ClientReport) -> Decimal:
    """The sum of the priced findings' monthly savings.

    Exported so a test can assert the rendered headline against an independently
    computed sum rather than against the field the renderer printed.
    """
    return sum(
        (
            finding.monthly_saving.amount
            for finding in report.findings
            if finding.monthly_saving.amount is not None
        ),
        start=Decimal(0),
    )
