"""Report Author: compose the client-ready report.

The split that matters: the model writes *prose*, the node assembles *figures*.
Every monetary value on the report is a
:class:`~costsentinel.domain.common.MoneyAmount` carried through from state --
which, because that type refuses LLM provenance, means no number on a client report
can have come from a model. The executive summary is model-derived and says so
through its own :class:`~costsentinel.domain.common.Provenance`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.savings import total_savings
from costsentinel.config import Settings
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.recommendations import Recommendation
from costsentinel.domain.report import (
    ApprovalQueueItem,
    ClientReport,
    ReportFinding,
    ReportPeriod,
    ReportSeverity,
    ReportTotals,
)
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import ReportNarrative
from costsentinel.llm.fake import LABEL_RECOMMENDATION, LABEL_TOTALS
from costsentinel.llm.routing import TASK_REPORT_PROSE
from costsentinel.observability.logging import get_logger

ACTOR = "report_author"

#: Savings as a share of observed spend, above which the headline escalates.
SEVERITY_HIGH_RATIO = Decimal("0.20")
SEVERITY_MODERATE_RATIO = Decimal("0.05")

_DEFAULT_WINDOW_DAYS = 30

_log = get_logger("agents.report_author")

_INSTRUCTION = """
You are the Report Author for a FinOps governance system. Write the executive
summary and a one-line note per finding for a client's finance and platform leads.

Rules you must follow:
- Every figure you mention must be copied verbatim from the totals or recommendation
  evidence below. Do not compute, round, convert or estimate any number.
- Do not introduce a finding, a resource or a claim that is not in the evidence.
- State plainly that nothing has been changed and that gated actions await the
  client's approval.
- Be direct and brief. No marketing language.
""".strip()

_OUTPUT_CONTRACT = (
    "ReportNarrative: {executive_summary, notes[] of {recommendation_id, note}}. "
    "Prose only -- no numeric fields."
)


def _severity(savings: MoneyAmount, spend: MoneyAmount, findings: int) -> ReportSeverity:
    """Headline severity from savings as a share of observed spend.

    Unknown spend with known findings yields ``LOW`` rather than ``NONE``: there is
    something to act on, and the share simply cannot be computed.
    """
    if findings == 0:
        return ReportSeverity.NONE
    if savings.amount is None or spend.amount is None or spend.amount == 0:
        return ReportSeverity.LOW
    ratio = savings.amount / spend.amount
    if ratio >= SEVERITY_HIGH_RATIO:
        return ReportSeverity.HIGH
    if ratio >= SEVERITY_MODERATE_RATIO:
        return ReportSeverity.MODERATE
    return ReportSeverity.LOW


def _period(state: ScanState) -> ReportPeriod:
    """The observation window, taken from the cost series the provider returned."""
    days: list[date] = [
        point.day
        for series in (state.estate.cost_series if state.estate else ())
        for point in series.points
    ]
    if days:
        start, end = min(days), max(days)
        return ReportPeriod(start=start, end=end, days=(end - start).days + 1)

    anchor = (state.estate.retrieved_at if state.estate else state.requested_at).date()
    return ReportPeriod(
        start=anchor - timedelta(days=_DEFAULT_WINDOW_DAYS),
        end=anchor,
        days=_DEFAULT_WINDOW_DAYS,
    )


def _recommendation_block(
    state: ScanState,
    recommendation: Recommendation,
) -> EvidenceBlock:
    classification = state.classification_for(recommendation.recommendation_id)
    requires_approval = (
        classification.requires_approval
        if classification
        else recommendation.risk_class.requires_approval
    )
    return EvidenceBlock(
        label=LABEL_RECOMMENDATION,
        fields=(
            EvidenceField(name="recommendation_id", value=recommendation.recommendation_id),
            EvidenceField(name="rank", value=str(recommendation.rank)),
            EvidenceField(name="resource_name", value=recommendation.target_resource_name),
            EvidenceField(name="subscription_id", value=recommendation.subscription_id),
            EvidenceField(name="action", value=recommendation.action.value),
            EvidenceField(name="action_class", value=recommendation.risk_class.value),
            EvidenceField(name="requires_approval", value=str(requires_approval).lower()),
            EvidenceField(name="monthly_saving", value=recommendation.savings.monthly.display()),
            EvidenceField(name="annual_saving", value=recommendation.savings.annual.display()),
            EvidenceField(name="rationale", value=recommendation.rationale),
        ),
    )


def make_report_author(*, llm: LLM, settings: Settings) -> Node:
    """Build the Report Author node."""
    _ = settings

    def report_author(state: ScanState) -> NodeUpdate:
        """Assemble the figures, ask the model for the prose, and emit the report."""
        spend = (
            state.estate.observed_spend()
            if state.estate
            else MoneyAmount.undetermined("no estate was read")
        )
        monthly, annual, unpriced = total_savings([rec.savings for rec in state.recommendations])

        gated = tuple(
            rec
            for rec in state.recommendations
            if (c := state.classification_for(rec.recommendation_id)) is not None
            and c.requires_approval
        )
        automatable = sum(
            1
            for rec in state.recommendations
            if (c := state.classification_for(rec.recommendation_id)) is not None
            and c.is_automatable
            and not c.requires_approval
        )

        period = _period(state)
        subscriptions = tuple(
            sub.subscription_id for sub in (state.estate.subscriptions if state.estate else ())
        )

        totals_block = EvidenceBlock(
            label=LABEL_TOTALS,
            fields=(
                EvidenceField(name="client", value=state.client),
                EvidenceField(name="subscriptions_in_scope", value=str(len(subscriptions))),
                EvidenceField(name="resources_examined", value=str(_resource_count(state))),
                EvidenceField(name="observed_monthly_spend", value=spend.display()),
                EvidenceField(name="projected_monthly_savings", value=monthly.display()),
                EvidenceField(name="projected_annual_savings", value=annual.display()),
                EvidenceField(name="findings_count", value=str(len(state.recommendations))),
                EvidenceField(name="awaiting_approval_count", value=str(len(gated))),
                EvidenceField(name="unpriced_findings_count", value=str(unpriced)),
                EvidenceField(name="period_days", value=str(period.days)),
            ),
        )

        prompt = Prompt(
            instruction=_INSTRUCTION,
            output_contract=_OUTPUT_CONTRACT,
            evidence=(
                totals_block,
                *(_recommendation_block(state, rec) for rec in state.recommendations),
            ),
        )
        narrative = llm.structured(task=TASK_REPORT_PROSE, prompt=prompt, schema=ReportNarrative)

        findings = tuple(
            _finding(state, rec, narrative.note_for(rec.recommendation_id))
            for rec in state.recommendations
        )
        queue = tuple(
            ApprovalQueueItem(
                recommendation_id=request.recommendation_id,
                resource_name=_resource_name(state, request.recommendation_id),
                proposed_action=request.action,
                action_class=request.action_class,
                monthly_saving_display=request.monthly_saving_display,
                reason=request.summary,
            )
            for request in state.approval_requests
        )

        notes: list[str] = [
            "Phase 1 is read-only: CostSentinel has no execution path, so nothing in "
            "this report has been or can be applied automatically.",
        ]
        if unpriced:
            notes.append(
                f"{unpriced} finding(s) could not be priced from provider data and are "
                f"excluded from the projected savings rather than assumed to be zero."
            )
        if state.errors:
            notes.append(
                f"{len(state.errors)} processing note(s) were recorded during this sweep; "
                f"see the run's audit trail."
            )

        report = ClientReport(
            report_id=f"rpt-{state.run_id}",
            run_id=state.run_id,
            client=state.client,
            period=period,
            subscriptions_in_scope=subscriptions,
            resources_examined=_resource_count(state),
            severity=_severity(monthly, spend, len(findings)),
            executive_summary=narrative.executive_summary,
            summary_provenance=Provenance(
                source=ProvenanceSource.LLM_INFERENCE,
                reference=f"{llm.name}:{TASK_REPORT_PROSE.name}",
                verification=Verification.UNVERIFIED,
            ),
            totals=ReportTotals(
                observed_monthly_spend=spend,
                projected_monthly_savings=monthly,
                projected_annual_savings=annual,
                findings_count=len(findings),
                awaiting_approval_count=len(gated),
                automatable_count=automatable,
            ),
            findings=findings,
            approval_queue=queue,
            notes=tuple(notes),
        )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.REPORT_GENERATED,
                subject=report.report_id,
                detail={
                    "findings": str(len(findings)),
                    "awaiting_approval": str(len(gated)),
                    "projected_monthly_savings": monthly.display(),
                    "projected_annual_savings": annual.display(),
                    "observed_monthly_spend": spend.display(),
                    "severity": report.severity.value,
                    "model": llm.name,
                },
            ),
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.SCAN_COMPLETED,
                subject=state.client,
                detail={"report_id": report.report_id, "errors": str(len(state.errors))},
                offset=1,
            ),
        ]

        _log.info(
            "report generated",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "findings": len(findings),
                "awaiting_approval": len(gated),
                "severity": report.severity.value,
            },
        )

        return {"report": report, "audit": with_audit(state, events)}

    return report_author


def _resource_count(state: ScanState) -> int:
    return len(state.estate.resources) if state.estate else 0


def _resource_name(state: ScanState, recommendation_id: str) -> str:
    return next(
        (
            rec.target_resource_name
            for rec in state.recommendations
            if rec.recommendation_id == recommendation_id
        ),
        "unknown",
    )


def _finding(state: ScanState, recommendation: Recommendation, note: str) -> ReportFinding:
    signal = state.signal(recommendation.signal_id)
    classification = state.classification_for(recommendation.recommendation_id)
    current_cost = (
        signal.monthly_cost
        if signal
        else MoneyAmount.undetermined("the originating signal is no longer in state")
    )
    return ReportFinding(
        rank=recommendation.rank,
        recommendation_id=recommendation.recommendation_id,
        signal_id=recommendation.signal_id,
        waste_kind=signal.kind if signal else WasteKind.STALE_NON_PRODUCTION_RESOURCE,
        subscription_id=recommendation.subscription_id,
        resource_id=recommendation.target_resource_id,
        resource_name=recommendation.target_resource_name,
        current_monthly_cost=current_cost,
        proposed_action=recommendation.action,
        action_class=(classification.action_class if classification else recommendation.risk_class),
        requires_approval=(
            classification.requires_approval
            if classification
            else recommendation.risk_class.requires_approval
        ),
        rationale=recommendation.rationale,
        note=note,
        evidence=tuple(f"{obs.name}: {obs.value}" for obs in (signal.evidence if signal else ())),
        preconditions=recommendation.preconditions,
        monthly_saving=recommendation.savings.monthly,
        annual_saving=recommendation.savings.annual,
        savings_basis=recommendation.savings.basis,
        savings_is_estimated=recommendation.savings.is_estimated,
    )
