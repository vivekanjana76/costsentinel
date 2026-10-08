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
from costsentinel.domain.observability import RunMetrics
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import WasteKind


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
    notes: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether the run completed without escalating anything."""
        return not self.incomplete_reasons


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
