"""The client-facing deliverable.

Every number on a :class:`ClientReport` is a
:class:`~costsentinel.domain.common.MoneyAmount` carried through from state, not text
produced by a model. The executive summary is prose and is model-derived; it is a
separate field with its own provenance so the two can never be confused.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import (
    Confidence,
    Frozen,
    MoneyAmount,
    Provenance,
    utc_now,
)
from costsentinel.domain.exemption import ExemptionReason
from costsentinel.domain.observability import RunMetrics
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import (
    AnomalyDirection,
    AnomalyKind,
    AttributedComponent,
    AttributionOutcome,
    WasteKind,
)


class ReportFinding(Frozen):
    """One finding as the client sees it: what, where, why, worth, and who must sign off."""

    rank: int = Field(ge=1)
    recommendation_id: str
    signal_id: str
    waste_kind: WasteKind
    subscription_id: str
    resource_id: str
    resource_name: str
    current_monthly_cost: MoneyAmount
    proposed_action: ActionType
    action_class: ActionClass
    requires_approval: bool
    confidence: Confidence
    rationale: str
    note: str = Field(default="", description="Model-authored commentary for the client.")
    root_cause: str = Field(
        default="", description="Why this waste exists, from the Root-Cause Analyst."
    )
    root_cause_factors: tuple[str, ...] = Field(
        default=(),
        description="Contributing factors, each already verified against the evidence.",
    )
    ranking_rationale: str = Field(default="", description="Why this finding sits at this rank.")
    ranking_breakdown: str = Field(
        default="",
        description=(
            "The computed ranking components, so a client can see the trade-off "
            "rather than trust the order."
        ),
    )
    ranking_composite: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(1))
    evidence: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    monthly_saving: MoneyAmount
    annual_saving: MoneyAmount
    savings_basis: str
    savings_is_estimated: bool
    memory_note: str = Field(
        default="",
        description=(
            "What this client previously decided about this advice, with the date "
            "and their stated reason. Empty when nothing matched. Affects the rank "
            "and this line only; every figure above it is unchanged."
        ),
    )
    base_rank: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Where this finding ranked on merit alone, before memory was consulted. "
            "Carried so a client can see that an ordering changed and why -- an "
            "order that moved for reasons nobody can see is worse than no "
            "reordering at all."
        ),
    )


class ExemptFinding(Frozen):
    """A real waste signal the client's own tags say not to act on.

    Separate from :class:`ReportFinding` rather than a flag on it, and the separation
    is the point. A :class:`ReportFinding` is a *proposal*: it has a rank, an action,
    a class, a saving and a set of preconditions. An exempt item has none of those
    and never will, so giving it the shape of a proposal would mean inventing a rank
    for something nobody is being asked to do and an action nobody may take.

    What it does carry is the one figure that is real: the resource's **current
    monthly cost**, straight from the provider. That is not a saving and is never
    added to one. It is what the client is spending on something they have told us to
    leave alone, which is a fact worth putting in front of them -- they may decide
    the standby is worth less than its bill, and that decision is theirs.
    """

    signal_id: str
    waste_kind: WasteKind
    subscription_id: str
    resource_id: str
    resource_name: str
    current_monthly_cost: MoneyAmount
    confidence: Confidence
    detector: str
    exemption_reason: ExemptionReason
    exemption_rule: str = Field(
        description="The rule that withheld remediation, so it can be disputed against a rule."
    )
    exemption_statement: str = Field(
        description=(
            "Why nothing is proposed, in words a client reads. Composed by the "
            "exemption rule from fixed text plus the matched tag, never by a model."
        )
    )
    matched_tag: str = Field(
        default="",
        description="The deciding tag as ``key=value``, already rendered safe for a document.",
    )
    evidence: tuple[str, ...] = ()


class ReportAnomaly(Frozen):
    """A change in spend, surfaced for investigation.

    Separate from :class:`ReportFinding` and from :class:`ExemptFinding`, because it is
    a third kind of claim. A finding says *this resource is wasteful, act*; an exempt
    item says *this is waste we are not acting on, and here is why*; an anomaly says
    *spend changed, investigate*.

    Carries **no action, no action class and no saving**, and the absence is the point:
    there is no field through which an anomaly could acquire a remediation, and
    ``delta`` is a change in spend rather than a recoverable amount. Presenting it as a
    saving would promise money nobody has identified a way to recover
    (ARCHITECTURE.md D106).

    ``statement`` is composed by the detector from its own arithmetic. ``note`` is the
    only model-derived field here, and it may explain the change without being able to
    alter the numbers or the verdict.
    """

    anomaly_id: str
    scope: str = Field(description="What changed: a subscription id today, a resource id later.")
    subscription_id: str
    service: str | None = None
    kind: AnomalyKind
    direction: AnomalyDirection
    baseline: MoneyAmount
    observed: MoneyAmount
    delta: MoneyAmount
    observed_from: date
    observed_to: date
    deviation_score: Decimal = Field(ge=Decimal(0))
    confidence: Confidence
    detector: str
    attribution_outcome: AttributionOutcome = Field(
        default=AttributionOutcome.UNATTRIBUTED,
        description=(
            "Whether what caused this change could be named. Defaults to "
            "unattributed, so an anomaly built by a caller that did not attribute it "
            "claims nothing."
        ),
    )
    attribution_statement: str = Field(
        default="",
        description=(
            "What accounted for the change, or why it could not be said, in plain "
            "numbers. Composed by the domain rather than a model, like `statement`."
        ),
    )
    attributed_components: tuple[AttributedComponent, ...] = Field(
        default=(),
        description=(
            "The named components, largest first, each with its before, after, "
            "difference and share. Empty when unattributed -- a ranked list that "
            "explains nothing in particular reads as an answer."
        ),
    )
    statement: str = Field(
        min_length=1,
        description=(
            "The finding in plain numbers, composed by the detector. Never model-authored."
        ),
    )
    note: str = Field(
        default="", description="Model-authored commentary. Explains; decides nothing."
    )
    evidence: tuple[str, ...] = ()


class ApprovalQueueItem(Frozen):
    """A finding that cannot proceed without a human, surfaced for the client."""

    recommendation_id: str
    resource_name: str
    proposed_action: ActionType
    action_class: ActionClass
    monthly_saving_display: str
    reason: str


class WasteKindSummary(Frozen):
    """Findings and savings grouped by kind of waste.

    Gives a client the shape of their problem rather than only a list: "most of this
    is idle compute" is actionable in a way that twelve individual findings are not.
    """

    waste_kind: WasteKind
    findings_count: int = Field(ge=1)
    monthly_saving: MoneyAmount
    annual_saving: MoneyAmount


class ReportPeriod(Frozen):
    """The observation window the report covers."""

    start: date
    end: date
    days: int = Field(ge=1)


class ReportTotals(Frozen):
    """Headline figures. All provider-derived or arithmetic; none model-derived."""

    observed_monthly_spend: MoneyAmount
    projected_monthly_savings: MoneyAmount
    projected_annual_savings: MoneyAmount
    findings_count: int = Field(ge=0)
    awaiting_approval_count: int = Field(ge=0)
    automatable_count: int = Field(ge=0)
    blocked_count: int = Field(
        default=0, ge=0, description="Findings CostSentinel will never execute itself."
    )
    unpriced_findings_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Findings excluded from the totals because they could not be priced from "
            "provider data. Counted, never treated as zero."
        ),
    )
    exempt_findings_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Real waste signals on resources the client's tags protect. Surfaced and "
            "excluded from every savings figure above, because a total that quietly "
            "absorbed them would promise savings nobody can realise."
        ),
    )
    anomaly_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Spend changes surfaced for investigation. Counted separately from the "
            "findings because they are a different claim and carry no saving."
        ),
    )
    unassessed_scope_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Scopes whose spend could not be assessed -- too little history, or too "
            "many missing days. Reported rather than silently omitted, so a clean "
            "anomaly section is distinguishable from an unexamined one."
        ),
    )
    exempt_monthly_cost: MoneyAmount | None = Field(
        default=None,
        description=(
            "What the client currently spends on the exempt resources. ``None`` when "
            "nothing was exempt -- which is an absence rather than an unknown amount, "
            "and the two should not render as the same thing. Deliberately a *cost* "
            "and not a saving: nobody is proposing to recover it."
        ),
    )


class ReportSeverity(StrEnum):
    """A coarse headline for the client, derived from savings as a share of spend."""

    NONE = "none"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ClientReport(Frozen):
    """A client-ready savings report for one client's estate.

    Scoped to a single client by construction: there is no multi-client report type,
    which is how ARCHITECTURE.md D12 is enforced at the output edge.
    """

    report_id: str
    run_id: str
    client: str
    generated_at: datetime = Field(default_factory=utc_now)
    period: ReportPeriod
    subscriptions_in_scope: tuple[str, ...]
    resources_examined: int = Field(ge=0)
    severity: ReportSeverity
    executive_summary: str
    summary_provenance: Provenance
    totals: ReportTotals
    findings: tuple[ReportFinding, ...]
    exempt_findings: tuple[ExemptFinding, ...] = Field(
        default=(),
        description=(
            "Flagged but exempt: waste that is real and deliberately not actioned. "
            "Listed separately from the findings so a client can see both what we "
            "propose and what we found and chose not to propose, with the reason. "
            "The alternative -- dropping them -- would make a thin report "
            "indistinguishable from a clean estate."
        ),
    )
    anomalies: tuple[ReportAnomaly, ...] = Field(
        default=(),
        description=(
            "Spend changes, listed separately from the findings. An anomaly routes to "
            "an investigation and never to a remediation, so it is not in the "
            "approval queue and contributes nothing to the savings total."
        ),
    )
    unassessed_spend: tuple[str, ...] = Field(
        default=(),
        description=(
            "One line per scope whose spend could not be assessed, with the reason. "
            "A gap in a cost series is unknown, not zero -- and an anomaly section "
            "that was silent about it would read as a clean bill of health."
        ),
    )
    waste_breakdown: tuple[WasteKindSummary, ...] = ()
    approval_queue: tuple[ApprovalQueueItem, ...]
    incomplete_reasons: tuple[str, ...] = Field(
        default=(),
        description=(
            "Why this report may be incomplete, from the supervisor's escalations. "
            "Stated plainly rather than omitted, so a thin report is not mistaken "
            "for a clean estate."
        ),
    )
    memory_recall: str = Field(
        default="",
        description=(
            "One line on how much of this report the client has seen before. Empty "
            "when memory is off or nothing matched -- which is also what a first "
            "sweep of a new estate correctly produces."
        ),
    )
    notes: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether the run completed without escalating anything."""
        return not self.incomplete_reasons

    @property
    def has_exemptions(self) -> bool:
        """Whether anything was flagged and deliberately left alone."""
        return bool(self.exempt_findings)

    @property
    def has_anomalies(self) -> bool:
        """Whether any spend change was surfaced for investigation."""
        return bool(self.anomalies)


class ScanResult(Frozen):
    """The outcome of a sweep, which may cover several clients.

    A sweep with no client specified runs one *separate* graph run per client and
    returns their reports side by side. It does not merge them: there is no
    multi-client report, so a sweep over several clients cannot accidentally become
    one (ARCHITECTURE.md D12).
    """

    reports: tuple[ClientReport, ...]
    metrics: tuple[RunMetrics, ...] = Field(
        default=(),
        description=(
            "Per-run token, cost and latency records, one per report. Carried "
            "alongside the reports rather than inside them: a client report is a "
            "deliverable about their estate, not about what our model calls cost."
        ),
    )
    started_at: datetime = Field(default_factory=utc_now)

    @property
    def clients(self) -> tuple[str, ...]:
        """The clients covered, in report order."""
        return tuple(report.client for report in self.reports)

    @property
    def run_ids(self) -> tuple[str, ...]:
        """The run (and LangGraph thread) id of each report."""
        return tuple(report.run_id for report in self.reports)

    @property
    def total_findings(self) -> int:
        """Findings across every report in the sweep."""
        return sum(report.totals.findings_count for report in self.reports)

    @property
    def total_awaiting_approval(self) -> int:
        """Findings across every report that cannot proceed without a human."""
        return sum(report.totals.awaiting_approval_count for report in self.reports)

    @property
    def total_llm_calls(self) -> int:
        """Model calls across the whole sweep."""
        return sum(m.call_count for m in self.metrics)

    @property
    def total_tokens(self) -> int:
        """Tokens across the whole sweep."""
        return sum(m.total_usage.total_tokens for m in self.metrics)

    def for_client(self, client: str) -> ClientReport | None:
        """One client's report, or ``None``."""
        return next((r for r in self.reports if r.client == client), None)

    def metrics_for(self, run_id: str) -> RunMetrics | None:
        """One run's metrics, or ``None``."""
        return next((m for m in self.metrics if m.run_id == run_id), None)
