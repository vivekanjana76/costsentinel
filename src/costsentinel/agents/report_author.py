"""Report Author: compose the client-ready report.

The split that matters: the model writes *prose*, the node assembles *figures*.
Every monetary value on the report is a
:class:`~costsentinel.domain.common.MoneyAmount` carried through from state --
which, because that type refuses LLM provenance, means no number on a client report
can have come from a model. Per-client totals are summed from the findings, so the
headline and the detail cannot disagree. The executive summary is model-derived and
says so through its own :class:`~costsentinel.domain.common.Provenance`.

The report also carries the things a client needs in order to *disagree* with it:
the root cause behind each finding, the computed ranking components, the evidence,
and the preconditions that would be re-checked before anything changed.

**Spend anomalies are a third section, and they carry no money the report can add
up.** An anomaly's ``delta`` is a change in spend, not a recoverable amount, so it
never reaches a savings total and never enters the approval queue -- there is nothing
to approve. The model may write a note explaining a change and cannot alter the
numbers or the verdict (ARCHITECTURE.md D106).

**Actionable savings and flagged-but-exempt waste are separate sections with
separate figures.** A single total that quietly absorbed exempt waste would misstate
both: it would promise savings nobody can realise, and it would hide the fact that a
decision was taken on the client's behalf. So ``projected_monthly_savings`` is the
sum of the actionable findings and nothing else, and the exempt items carry their
*current cost* -- a provider fact, never a saving -- alongside the reason nothing is
proposed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.memory_consult import summarise_recalls
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
    ExemptFinding,
    ReportAnomaly,
    ReportFinding,
    ReportPeriod,
    ReportSeverity,
    ReportTotals,
    WasteKindSummary,
)
from costsentinel.domain.signals import (
    AttributionOutcome,
    CostAnomaly,
    CostAttribution,
    WasteKind,
)
from costsentinel.domain.state import ScanState
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import ReportNarrative
from costsentinel.llm.fake import LABEL_ANOMALY, LABEL_RECOMMENDATION, LABEL_TOTALS
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

For each spend_change block, write one line explaining what a change of that shape
usually means and what the client should look at. Rules:
- A level_shift means the daily run rate moved and stayed moved; a spike means one day
  stood out and then reverted. Do not swap them and do not dispute them.
- Do not restate, adjust or recompute any figure. The baseline, the observed value and
  the difference are final.
- Do not propose a remediation. A spend change is something to investigate, not
  something to action.
""".strip()

_OUTPUT_CONTRACT = (
    "ReportNarrative: {executive_summary, notes[] of {recommendation_id, note}, "
    "anomaly_notes[] of {anomaly_id, note}}. Prose only -- no numeric fields."
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


def _waste_breakdown(findings: tuple[ReportFinding, ...]) -> tuple[WasteKindSummary, ...]:
    """Group findings by waste kind, summing each group deterministically.

    Ordered by monthly saving so the biggest category leads. A group whose savings
    are all unknown is still reported, with unknown totals, rather than dropped.
    """
    kinds: dict[WasteKind, list[ReportFinding]] = {}
    for finding in findings:
        kinds.setdefault(finding.waste_kind, []).append(finding)

    summaries: list[WasteKindSummary] = []
    for kind, group in kinds.items():
        monthly_amounts = [
            f.monthly_saving.amount for f in group if f.monthly_saving.amount is not None
        ]
        annual_amounts = [
            f.annual_saving.amount for f in group if f.annual_saving.amount is not None
        ]
        if monthly_amounts:
            currency = next(
                f.monthly_saving.currency for f in group if f.monthly_saving.amount is not None
            )
            monthly = MoneyAmount.of(
                sum(monthly_amounts, start=Decimal(0)),
                currency=currency,
                provenance=Provenance.calculated(
                    f"sum of {len(monthly_amounts)} priced {kind.value} finding(s), monthly"
                ),
            )
            annual = MoneyAmount.of(
                sum(annual_amounts, start=Decimal(0)),
                currency=currency,
                provenance=Provenance.calculated(
                    f"sum of {len(annual_amounts)} priced {kind.value} finding(s), annual"
                ),
            )
        else:
            reason = f"no {kind.value} finding could be priced"
            monthly = MoneyAmount.undetermined(reason)
            annual = MoneyAmount.undetermined(reason)
        summaries.append(
            WasteKindSummary(
                waste_kind=kind,
                findings_count=len(group),
                monthly_saving=monthly,
                annual_saving=annual,
            )
        )

    return tuple(
        sorted(
            summaries,
            key=lambda s: (
                -(s.monthly_saving.amount or Decimal(-1)),
                s.waste_kind.value,
            ),
        )
    )


def _exempt_findings(state: ScanState) -> tuple[ExemptFinding, ...]:
    """The waste that is real and deliberately not actioned.

    Built from the signals rather than from the recommendations, because an exempt
    resource has no recommendation -- which is the whole point. Ordered by cost, so
    the most expensive thing the client has told us to leave alone leads: that is the
    one they are most likely to want to revisit.
    """
    items: list[ExemptFinding] = []
    for signal in state.exempt_signals:
        verdict = state.exemption_for(signal.resource_id)
        if verdict is None:  # pragma: no cover -- exempt_signals is derived from these
            continue
        items.append(
            ExemptFinding(
                signal_id=signal.signal_id,
                waste_kind=signal.kind,
                subscription_id=signal.subscription_id,
                resource_id=signal.resource_id,
                resource_name=signal.resource_name,
                current_monthly_cost=signal.monthly_cost,
                confidence=signal.confidence,
                detector=signal.detector,
                exemption_reason=verdict.reason,
                exemption_rule=verdict.rule_reference,
                exemption_statement=verdict.statement,
                matched_tag=verdict.matched_tag,
                evidence=tuple(f"{obs.name}: {obs.value}" for obs in signal.evidence),
            )
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (-(item.current_monthly_cost.amount or Decimal(-1)), item.signal_id),
        )
    )


def _exempt_cost(items: Sequence[ExemptFinding]) -> MoneyAmount | None:
    """What the client spends on the resources they have told us to leave alone.

    ``None`` when nothing was exempt, which is an absence rather than an unknown
    amount. An unknown would render as "unknown spend on exempt resources", which is
    a different and considerably more alarming claim.

    A sum of provider-reported costs, so it is a calculation over verified inputs --
    and deliberately a cost rather than a saving, because nobody is proposing to
    recover it.
    """
    if not items:
        return None
    known = [
        item.current_monthly_cost.amount
        for item in items
        if item.current_monthly_cost.amount is not None
    ]
    if not known:
        return MoneyAmount.undetermined(
            f"none of the {len(items)} exempt finding(s) could be priced from provider data"
        )
    currency = next(
        item.current_monthly_cost.currency
        for item in items
        if item.current_monthly_cost.amount is not None
    )
    return MoneyAmount.of(
        sum(known, start=Decimal(0)),
        currency=currency,
        provenance=Provenance.calculated(
            f"sum of the current monthly cost of {len(known)} exempt resource(s)"
        ),
    )


def _anomaly_block(anomaly: CostAnomaly) -> EvidenceBlock:
    """One spend change, rendered for the model to *explain*.

    Deliberately includes the finished statement and the figures, and asks for prose.
    The model has nothing to decide here: the kind, the direction and every number are
    already computed, and :class:`~costsentinel.llm.contracts.AnomalyNote` has no field
    in which a different answer could be returned.
    """
    return EvidenceBlock(
        label=LABEL_ANOMALY,
        fields=(
            EvidenceField(name="anomaly_id", value=anomaly.anomaly_id),
            EvidenceField(name="scope", value=anomaly.subscription_id),
            EvidenceField(name="kind", value=anomaly.kind.value),
            EvidenceField(name="direction", value=anomaly.direction.value),
            EvidenceField(name="baseline", value=anomaly.baseline.display()),
            EvidenceField(name="observed", value=anomaly.observed.display()),
            EvidenceField(name="difference", value=anomaly.delta.display()),
            EvidenceField(name="observed_from", value=anomaly.observed_from.isoformat()),
            EvidenceField(name="observed_to", value=anomaly.observed_to.isoformat()),
            EvidenceField(name="statement", value=anomaly.statement()),
        ),
    )


#: What a report anomaly says when no attribution was computed for it at all. Distinct
#: from every :class:`UnattributedReason`, which are all answers to "we looked": this
#: one says we did not, which a client is entitled to be able to tell apart.
NO_ATTRIBUTION_ATTEMPTED = (
    "What accounted for this change was not examined, so nothing is claimed about its "
    "cause."
)


def _one_report_anomaly(
    anomaly: CostAnomaly,
    attribution: CostAttribution | None,
    narrative: ReportNarrative,
) -> ReportAnomaly:
    """One spend change, with whatever could be said about its cause.

    The attribution is optional and its absence is explicit. A missing attribution
    renders as "not examined" rather than as nothing at all, because a silent gap in
    the cause line would read as "no cause" to anybody who had seen an attributed
    anomaly in the same report.
    """
    return ReportAnomaly(
        anomaly_id=anomaly.anomaly_id,
        scope=anomaly.subscription_id,
        subscription_id=anomaly.subscription_id,
        service=anomaly.service,
        kind=anomaly.kind,
        direction=anomaly.direction,
        baseline=anomaly.baseline,
        observed=anomaly.observed,
        delta=anomaly.delta,
        observed_from=anomaly.observed_from,
        observed_to=anomaly.observed_to,
        deviation_score=anomaly.deviation_score,
        confidence=anomaly.confidence,
        detector=anomaly.detector,
        attribution_outcome=(
            attribution.outcome if attribution else AttributionOutcome.UNATTRIBUTED
        ),
        attribution_statement=(
            attribution.statement() if attribution else NO_ATTRIBUTION_ATTEMPTED
        ),
        attributed_components=attribution.components if attribution else (),
        statement=anomaly.statement(),
        note=narrative.anomaly_note_for(anomaly.anomaly_id),
        evidence=tuple(f"{obs.name}: {obs.value}" for obs in anomaly.evidence),
    )


def _report_anomalies(state: ScanState, narrative: ReportNarrative) -> tuple[ReportAnomaly, ...]:
    """Spend changes, in a deterministic order with the model's prose attached.

    Ordered by deviation score then by id: the furthest-from-normal change leads,
    because that is the one a client should look at first, and the id keeps the order
    total. Not ordered by the size of the delta -- a large change on a large
    subscription can be ordinary, which is what the score already accounts for.
    """
    by_anomaly = {one.anomaly_id: one for one in state.attributions}
    return tuple(
        sorted(
            (
                _one_report_anomaly(anomaly, by_anomaly.get(anomaly.anomaly_id), narrative)
                for anomaly in state.anomalies
            ),
            key=lambda item: (-item.deviation_score, item.anomaly_id),
        )
    )


def _recommendation_block(state: ScanState, recommendation: Recommendation) -> EvidenceBlock:
    classification = state.classification_for(recommendation.recommendation_id)
    requires_approval = (
        classification.requires_approval
        if classification
        else recommendation.risk_class.requires_approval
    )
    cause = state.root_cause_for(recommendation.signal_id)
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
            EvidenceField(name="root_cause", value=cause.narrative if cause else ""),
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
        blocked = sum(
            1
            for rec in state.recommendations
            if (c := state.classification_for(rec.recommendation_id)) is not None
            and not c.is_automatable
        )

        exempt_items = _exempt_findings(state)
        exempt_cost = _exempt_cost(exempt_items)

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
                EvidenceField(name="blocked_count", value=str(blocked)),
                EvidenceField(name="unpriced_findings_count", value=str(unpriced)),
                EvidenceField(name="anomaly_count", value=str(len(state.anomalies))),
                EvidenceField(
                    name="unassessed_scope_count", value=str(len(state.unassessed_scopes))
                ),
                EvidenceField(name="exempt_findings_count", value=str(len(exempt_items))),
                EvidenceField(
                    name="exempt_monthly_cost",
                    value=exempt_cost.display() if exempt_cost is not None else "none",
                ),
                EvidenceField(name="period_days", value=str(period.days)),
            ),
        )

        narrative = llm.structured(
            task=TASK_REPORT_PROSE,
            prompt=Prompt(
                instruction=_INSTRUCTION,
                output_contract=_OUTPUT_CONTRACT,
                evidence=(
                    totals_block,
                    *(_recommendation_block(state, rec) for rec in state.recommendations),
                    *(_anomaly_block(anomaly) for anomaly in state.anomalies),
                ),
            ),
            schema=ReportNarrative,
        )

        findings = tuple(
            _finding(state, rec, narrative.note_for(rec.recommendation_id))
            for rec in state.recommendations
        )
        anomalies = _report_anomalies(state, narrative)
        unassessed = tuple(
            f"{assessment.scope}: {assessment.detail}" for assessment in state.unassessed_scopes
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
            "CostSentinel has no execution path in this phase, so nothing in this "
            "report has been or can be applied automatically.",
            "Findings are ranked by a weighted composite of savings, safety and "
            "detector confidence, not by savings alone. Each finding shows its own "
            "breakdown.",
        ]
        if unpriced:
            notes.append(
                f"{unpriced} finding(s) could not be priced from provider data. They are "
                f"excluded from the projected savings rather than assumed to be zero, and "
                f"they rank below every priced finding -- we will not tell you that "
                f"something we cannot cost is your bigger opportunity. Each one says why "
                f"no figure could be derived."
            )
        if anomalies:
            # Node-authored. The one sentence a client must not read as a savings
            # opportunity is the one introducing a figure that is not a saving.
            notes.append(
                f"{len(anomalies)} spend change(s) were detected and are listed "
                f"separately from the findings. A spend change is something to "
                f"investigate, not something to act on: its difference is what you are "
                f"now paying differently, not an amount anyone has identified a way to "
                f"recover, and none of it appears in the projected savings above."
            )
        if unassessed:
            notes.append(
                f"{len(unassessed)} subscription(s) could not be assessed for spend "
                f"changes -- too little history, or days missing from the cost series. "
                f"Those days are unknown rather than zero, so no conclusion was drawn. "
                f"Each one says why."
            )
        if exempt_items:
            # Node-authored rather than model-authored, like the unpriced note above
            # it. The reason a client is not being advised to act on something is not
            # a sentence worth letting a model paraphrase.
            exempt_spend = exempt_cost.display() if exempt_cost is not None else "unknown"
            notes.append(
                f"{len(exempt_items)} finding(s) are flagged but exempt: real waste "
                f"signals on resources your own tags mark as protected -- a "
                f"disaster-recovery standby, a retention requirement, or an explicit "
                f"cost exemption. Each is listed with the tag that caused it and the "
                f"reason nothing is proposed, and all of them are excluded from the "
                f"projected savings above. Their current monthly cost is {exempt_spend}, "
                f"which is what you spend on them rather than a saving available to "
                f"you."
            )
        if blocked:
            notes.append(
                f"{blocked} finding(s) are classified as blocked: they are reported as "
                f"advice for a person to carry out by hand, and CostSentinel will not "
                f"perform them however they are approved."
            )
        if state.errors:
            notes.append(
                f"{len(state.errors)} processing note(s) were recorded during this "
                f"sweep; see the run's audit trail."
            )
        recall_line = summarise_recalls(state)
        if recall_line:
            notes.append(recall_line)

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
                blocked_count=blocked,
                unpriced_findings_count=unpriced,
                anomaly_count=len(anomalies),
                unassessed_scope_count=len(unassessed),
                exempt_findings_count=len(exempt_items),
                exempt_monthly_cost=exempt_cost,
            ),
            findings=findings,
            anomalies=anomalies,
            unassessed_spend=unassessed,
            exempt_findings=exempt_items,
            waste_breakdown=_waste_breakdown(findings),
            approval_queue=queue,
            incomplete_reasons=state.escalations,
            memory_recall=recall_line,
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
                    "blocked": str(blocked),
                    "unpriced": str(unpriced),
                    "anomalies": str(len(anomalies)),
                    "unassessed_scopes": str(len(unassessed)),
                    "exempt": str(len(exempt_items)),
                    "exempt_monthly_cost": (
                        exempt_cost.display() if exempt_cost is not None else "none"
                    ),
                    "projected_monthly_savings": monthly.display(),
                    "projected_annual_savings": annual.display(),
                    "observed_monthly_spend": spend.display(),
                    "severity": report.severity.value,
                    "waste_kinds": str(len(report.waste_breakdown)),
                    "complete": str(report.is_complete).lower(),
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
                "blocked": blocked,
                "unpriced": unpriced,
                "exempt": len(exempt_items),
                "anomalies": len(anomalies),
                "severity": report.severity.value,
                "complete": report.is_complete,
            },
        )

        return {"report": report, "audit": with_audit(state, events)}

    return report_author


def _resource_count(state: ScanState) -> int:
    if state.estate is None:
        return 0
    return len(state.estate.resources) + len(state.estate.reservations)


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
    cause = state.root_cause_for(recommendation.signal_id)
    score = state.ranking_for(recommendation.recommendation_id)
    recall = state.recall_for(recommendation.recommendation_id)
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
        confidence=recommendation.confidence,
        rationale=recommendation.rationale,
        note=note,
        root_cause=cause.narrative if cause else "",
        root_cause_factors=tuple(f.statement for f in cause.factors) if cause else (),
        ranking_rationale=score.rationale if score else "",
        ranking_breakdown=score.explain() if score else "",
        ranking_composite=score.composite if score else None,
        evidence=tuple(f"{obs.name}: {obs.value}" for obs in (signal.evidence if signal else ())),
        preconditions=recommendation.preconditions,
        monthly_saving=recommendation.savings.monthly,
        annual_saving=recommendation.savings.annual,
        savings_basis=recommendation.savings.basis,
        savings_is_estimated=recommendation.savings.is_estimated,
        memory_note=recall.annotation if recall else "",
        base_rank=recall.base_rank if recall else None,
    )
