"""The client-facing deliverable.

Every number on a :class:`ClientReport` is a
:class:`~costsentinel.domain.common.MoneyAmount` carried through from state, not text
produced by a model. The executive summary is prose and is model-derived; it is a
separate field with its own provenance so the two can never be confused.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import Frozen, MoneyAmount, Provenance, utc_now
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
    rationale: str
    note: str = Field(default="", description="Model-authored commentary for the client.")
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
    approval_queue: tuple[ApprovalQueueItem, ...]
    notes: tuple[str, ...] = ()


class ScanResult(Frozen):
    """The outcome of a sweep, which may cover several clients.

    A sweep with no client specified runs one *separate* graph run per client and
    returns their reports side by side. It does not merge them: there is no
    multi-client report, so a sweep over several clients cannot accidentally become
    one (ARCHITECTURE.md D12).
    """

    reports: tuple[ClientReport, ...]
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

    def for_client(self, client: str) -> ClientReport | None:
        """One client's report, or ``None``."""
        return next((r for r in self.reports if r.client == client), None)
