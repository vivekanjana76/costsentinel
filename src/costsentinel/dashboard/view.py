"""The view model: what the operator surface shows, and nothing it works out itself.

Everything on this page already exists on a
:class:`~costsentinel.domain.report.ClientReport`. The view layer decides **order and
emphasis** and never a figure. That is the whole rule, and it is the reason this module
exists as a separate layer rather than the HTML renderer reading the report directly:
the place where a recomputation would be introduced is a place a test can point at.

No arithmetic
-------------

There is no addition, no division, no rounding and no percentage in this file. The one
derivation that happens is a **count** of things already in a tuple, which is pinned by
a test so that "we only count" stays a claim somebody checks rather than a convention.

Every monetary value is carried as the string ``MoneyAmount.display()`` already
produced. That is not laziness about types -- it is the same reasoning as
:class:`~costsentinel.domain.governance.FrozenFigures`: what the operator must see is
what the report rendered, down to the grouping and the currency, and re-deriving it
here from an amount would be re-deriving it from data a second formatter could disagree
about.

The same six sections
---------------------

The sections are the Markdown renderer's sections, in the Markdown renderer's order, so
the document a client reads and the page an operator reads cannot show different things
about the same sweep (ARCHITECTURE.md D123).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import Field

from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import PendingApproval
from costsentinel.domain.recommendations import ActionClass
from costsentinel.domain.report import ClientReport, ReportAnomaly, ReportFinding
from costsentinel.reports.markdown import (
    SOURCE_LABELS,
    SYNTHETIC_BANNER,
    is_synthetic,
    price_sources,
)

#: What a value reads as when the pipeline could not determine it. Taken from the
#: domain's own rendering rather than written out here, so the page and the document
#: cannot disagree about what an unknown looks like.
UNKNOWN_SUFFIX = "unknown"


class Row(Frozen):
    """One label-and-value line. Both sides are already-rendered strings."""

    label: str
    value: str
    emphasis: bool = Field(
        default=False, description="Whether the view wants this read first. Not a figure."
    )


class FindingView(Frozen):
    """One actionable recommendation, as the page shows it."""

    rank: int
    recommendation_id: str
    resource_name: str
    subscription_id: str
    action: str
    action_class: ActionClass
    waste_kind: str
    requires_approval: bool
    monthly_saving: str
    annual_saving: str
    current_cost: str
    confidence: str
    basis: str
    figure_basis: str
    root_cause: str = ""
    ranking_rationale: str = ""
    evidence: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    memory_note: str = ""

    @property
    def saving_is_known(self) -> bool:
        """Whether a figure was derived. Read off the rendered string, not recomputed."""
        return UNKNOWN_SUFFIX not in self.monthly_saving


class ExemptView(Frozen):
    """One flagged-but-exempt resource.

    Carries ``current_cost`` and **no saving field at all**. The absence is the point:
    there is nowhere on this type to put an amount somebody could mistake for money on
    the table.
    """

    resource_name: str
    subscription_id: str
    waste_kind: str
    current_cost: str
    exemption_reason: str
    exemption_rule: str
    matched_tag: str
    statement: str


class AttributionView(Frozen):
    """One component that accounted for a spend change."""

    label: str
    before: str
    after: str
    difference: str
    direction: str
    share: str


class AnomalyView(Frozen):
    """One spend change.

    No action, no action class and no saving, exactly as
    :class:`~costsentinel.domain.report.ReportAnomaly` has none. A view that invented
    one would be the only place in the system where an anomaly could acquire an action.
    """

    scope: str
    kind: str
    direction: str
    baseline: str
    observed: str
    difference: str
    window: str
    deviation_score: str
    confidence: str
    statement: str
    attribution_summary: str
    attribution_statement: str
    attributed: tuple[AttributionView, ...] = ()
    note: str = ""

    @property
    def is_attributed(self) -> bool:
        """Whether a cause was named."""
        return bool(self.attributed)


class PendingView(Frozen):
    """One gated action awaiting a decision, as the queue shows it."""

    run_id: str
    recommendation_id: str
    client: str
    resource_name: str
    action: str
    action_class: ActionClass
    summary: str
    monthly_saving: str
    annual_saving: str
    current_cost: str
    deferrals: int


class ReportView(Frozen):
    """One client's report, arranged for a screen.

    Built by :func:`report_view`. Holds no :class:`~decimal.Decimal` and no
    :class:`~costsentinel.domain.common.MoneyAmount`: every figure arrives as the
    string the report already rendered, so there is nothing here a second formatter
    could disagree about.
    """

    client: str
    report_id: str
    run_id: str
    period: str
    generated: str
    severity: str
    synthetic: bool
    banner: str = ""
    summary_rows: tuple[Row, ...]
    executive_summary: str
    summary_is_model_written: bool
    headline_figures: tuple[str, ...] = Field(
        description=(
            "The observed spend, the monthly saving and the annual saving, exactly as "
            "MoneyAmount.display() rendered them. Carried so a test can assert they "
            "reach the page verbatim rather than by membership."
        )
    )
    findings: tuple[FindingView, ...] = ()
    unquantified: tuple[FindingView, ...] = ()
    exempt: tuple[ExemptView, ...] = ()
    anomalies: tuple[AnomalyView, ...] = ()
    unassessed: tuple[str, ...] = ()
    pending: tuple[PendingView, ...] = ()
    notes: tuple[str, ...] = ()
    basis_rows: tuple[Row, ...] = ()

    @property
    def has_pending(self) -> bool:
        """Whether this sweep is waiting on a person."""
        return bool(self.pending)


class EstateView(Frozen):
    """The overview: every client in scope, and what is waiting across all of them."""

    generated: str
    synthetic: bool
    banner: str = ""
    reports: tuple[ReportView, ...] = ()
    total_pending: int = 0

    @property
    def clients(self) -> tuple[str, ...]:
        """The clients on this page, in the order they are shown."""
        return tuple(report.client for report in self.reports)


def _finding_view(finding: ReportFinding) -> FindingView:
    return FindingView(
        rank=finding.rank,
        recommendation_id=finding.recommendation_id,
        resource_name=finding.resource_name,
        subscription_id=finding.subscription_id,
        action=finding.proposed_action.value.replace("_", " "),
        action_class=finding.action_class,
        waste_kind=finding.waste_kind.value.replace("_", " "),
        requires_approval=finding.requires_approval,
        monthly_saving=finding.monthly_saving.display(),
        annual_saving=finding.annual_saving.display(),
        current_cost=finding.current_monthly_cost.display(),
        confidence=f"{finding.confidence}",
        basis=finding.savings_basis,
        figure_basis="estimated" if finding.savings_is_estimated else "observed",
        root_cause=finding.root_cause,
        ranking_rationale=finding.ranking_rationale,
        evidence=finding.evidence,
        preconditions=finding.preconditions,
        memory_note=finding.memory_note,
    )


def _anomaly_view(anomaly: ReportAnomaly) -> AnomalyView:
    return AnomalyView(
        scope=anomaly.scope,
        kind=anomaly.kind.value.replace("_", " "),
        direction=anomaly.direction.value,
        baseline=anomaly.baseline.display(),
        observed=anomaly.observed.display(),
        difference=anomaly.delta.display(),
        window=f"{anomaly.observed_from} to {anomaly.observed_to}",
        deviation_score=f"{anomaly.deviation_score}",
        confidence=f"{anomaly.confidence}",
        statement=anomaly.statement,
        attribution_summary=(
            ", ".join(part.label for part in anomaly.attributed_components)
            if anomaly.attributed_components
            else "no single cause identified"
        ),
        attribution_statement=anomaly.attribution_statement,
        attributed=tuple(
            AttributionView(
                label=part.label,
                before=part.baseline.display(),
                after=part.observed.display(),
                difference=part.contribution.display(),
                direction=part.direction.value,
                # The share is already a fraction on the component; rendered with the
                # domain's own formatting rather than multiplied here.
                share=f"{part.share * 100:.0f}%",
            )
            for part in anomaly.attributed_components
        ),
        note=anomaly.note,
    )


def pending_view(item: PendingApproval) -> PendingView:
    """One queued item, arranged for a screen.

    Public because the queue page builds its own list from the checkpoint rather
    than from a report, and two copies of this mapping would be two chances for
    the queue page and the report page to describe one pending item differently.
    """
    return PendingView(
        run_id=item.run_id,
        recommendation_id=item.recommendation_id,
        client=item.client,
        resource_name=item.target_resource_name,
        action=item.action.value.replace("_", " "),
        action_class=item.action_class,
        summary=item.summary,
        monthly_saving=item.figures.monthly_saving_display,
        annual_saving=item.figures.annual_saving_display,
        current_cost=item.figures.current_monthly_cost_display,
        deferrals=item.deferrals,
    )


def _summary_rows(report: ClientReport) -> tuple[Row, ...]:
    """The headline table. Every value is a rendered figure or a count off a tuple."""
    totals = report.totals
    rows = [
        Row(label="Observed spend over the period", value=totals.observed_monthly_spend.display()),
        Row(
            label="Projected actionable savings",
            value=f"{totals.projected_monthly_savings.display()} / month",
            emphasis=True,
        ),
        Row(label="Annualised", value=f"{totals.projected_annual_savings.display()} / year"),
        Row(label="Actionable findings", value=str(totals.findings_count)),
        Row(label="Awaiting your approval", value=str(totals.awaiting_approval_count)),
        Row(label="CostSentinel may apply automatically", value=str(totals.automatable_count)),
    ]
    if totals.blocked_count:
        rows.append(
            Row(
                label="Advice for a person to carry out by hand",
                value=str(totals.blocked_count),
            )
        )
    if totals.unpriced_findings_count:
        rows.append(
            Row(
                label="Findings we could not price",
                value=f"{totals.unpriced_findings_count} (excluded from the total)",
            )
        )
    if totals.exempt_findings_count:
        cost = totals.exempt_monthly_cost
        rows.append(
            Row(
                label="Flagged but exempt",
                value=(
                    f"{totals.exempt_findings_count} (costing {cost.display()} / month)"
                    if cost is not None
                    else str(totals.exempt_findings_count)
                ),
            )
        )
    if totals.anomaly_count:
        rows.append(Row(label="Spend changes to investigate", value=str(totals.anomaly_count)))
    if totals.unassessed_scope_count:
        rows.append(
            Row(
                label="Subscriptions whose spend we could not assess",
                value=str(totals.unassessed_scope_count),
            )
        )
    return tuple(rows)


def _basis_rows(report: ClientReport) -> tuple[Row, ...]:
    """Where the figures came from, read off the provenance rather than asserted.

    A hardcoded "prices from Azure Retail Prices" line would keep saying so after
    somebody ran the mock provider -- the same reasoning the Markdown renderer's
    provenance block carries.
    """
    sources, retrieved = price_sources(report)
    described = sorted(
        {
            SOURCE_LABELS.get(source, source.value)
            for source in sources
            if source.value != "calculation"
        }
    )
    rows = [
        Row(label="Figures derived from", value=", ".join(described) if described else "—"),
        Row(
            label="Data retrieved",
            value=retrieved.date().isoformat() if retrieved is not None else "unknown",
        ),
    ]
    priced = [one for one in report.findings if one.monthly_saving.is_known]
    estimated = sum(1 for one in priced if one.savings_is_estimated)
    rows.append(Row(label="Savings that are observed costs", value=str(len(priced) - estimated)))
    rows.append(Row(label="Savings that are modelled estimates", value=str(estimated)))
    return tuple(rows)


def report_view(
    report: ClientReport,
    *,
    pending: Sequence[PendingApproval] = (),
    generated_at: datetime | None = None,
) -> ReportView:
    """Arrange one report for a screen.

    Args:
        report: The sweep's own report. Every figure comes from here.
        pending: What this sweep is waiting on, read from the checkpoint by the caller.
            Passed in rather than looked up, so this function stays pure and a test can
            build a queue without a checkpointer.
        generated_at: The one clock in the output, injectable for the same reason the
            Markdown renderer's is -- so a page is reproducible and a screenshot is
            reviewable.

    Returns:
        A :class:`ReportView` holding only strings, enums and counts.
    """
    synthetic = is_synthetic(report)
    stamp = generated_at or report.generated_at
    quantified = [one for one in report.findings if one.monthly_saving.is_known]
    unquantified = [one for one in report.findings if not one.monthly_saving.is_known]

    return ReportView(
        client=report.client,
        report_id=report.report_id,
        run_id=report.run_id,
        period=f"{report.period.start} to {report.period.end} ({report.period.days} days)",
        generated=stamp.date().isoformat(),
        severity=report.severity.value,
        synthetic=synthetic,
        banner=SYNTHETIC_BANNER if synthetic else "",
        summary_rows=_summary_rows(report),
        executive_summary=report.executive_summary,
        summary_is_model_written=True,
        headline_figures=(
            report.totals.observed_monthly_spend.display(),
            report.totals.projected_monthly_savings.display(),
            report.totals.projected_annual_savings.display(),
        ),
        findings=tuple(_finding_view(one) for one in quantified),
        unquantified=tuple(_finding_view(one) for one in unquantified),
        exempt=tuple(
            ExemptView(
                resource_name=item.resource_name,
                subscription_id=item.subscription_id,
                waste_kind=item.waste_kind.value.replace("_", " "),
                current_cost=item.current_monthly_cost.display(),
                exemption_reason=item.exemption_reason.value.replace("_", " "),
                exemption_rule=item.exemption_rule,
                matched_tag=item.matched_tag,
                statement=item.exemption_statement,
            )
            for item in report.exempt_findings
        ),
        anomalies=tuple(_anomaly_view(one) for one in report.anomalies),
        unassessed=report.unassessed_spend,
        pending=tuple(pending_view(one) for one in pending),
        notes=report.notes,
        basis_rows=_basis_rows(report),
    )


def estate_view(views: Sequence[ReportView], *, generated_at: datetime | None = None) -> EstateView:
    """The overview across every client, in the order given.

    The order is the caller's, not this function's: a view that sorted by savings would
    be deciding which client matters most, which is a judgement nobody asked it to make.
    """
    stamp = generated_at or datetime.now().astimezone()
    return EstateView(
        generated=stamp.date().isoformat(),
        synthetic=any(view.synthetic for view in views),
        banner=next((view.banner for view in views if view.banner), ""),
        reports=tuple(views),
        total_pending=sum(len(view.pending) for view in views),
    )
