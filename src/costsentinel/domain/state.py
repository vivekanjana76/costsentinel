"""``ScanState`` -- the single object threaded through every graph node.

Defined in ``domain`` because it is a Pydantic domain type (CLAUDE.md section 6) and
re-exported from ``costsentinel.graph`` for the graph package's convenience
(CLAUDE.md section 4).

Nodes receive the whole state and return only the fields they changed, which keeps
merge semantics explicit, keeps checkpoint diffs small, and makes it obvious in
review which fields a node is allowed to touch (ARCHITECTURE.md D14). This is the
one mutable type in the domain; everything it holds is frozen.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from costsentinel.domain.analysis import RankingScore, RootCause
from costsentinel.domain.common import utc_now
from costsentinel.domain.estate import Estate
from costsentinel.domain.exemption import ExemptionVerdict
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    AuditEvent,
    SupervisorDecision,
)
from costsentinel.domain.memory import MemoryMetrics, MemoryRecall
from costsentinel.domain.observability import RunMetrics
from costsentinel.domain.recommendations import ActionType, PricedOption, Recommendation
from costsentinel.domain.report import ClientReport
from costsentinel.domain.signals import (
    AnomalyAssessment,
    CostAnomaly,
    CostAttribution,
    WasteSignal,
)


class ScanState(BaseModel):
    """The state of one client's scan.

    ``run_id`` doubles as the LangGraph thread id, the log correlation id and the
    trace id, so one identifier ties a checkpoint, a log line and a span together.
    """

    model_config = ConfigDict(extra="forbid")

    # --- request ---------------------------------------------------------
    run_id: str
    client: str
    requested_at: datetime = Field(default_factory=utc_now)
    requested_by: str = "system"

    # --- gathered by the Anomaly Scout -----------------------------------
    estate: Estate | None = None

    # --- detection -------------------------------------------------------
    signals: tuple[WasteSignal, ...] = ()
    anomalies: tuple[CostAnomaly, ...] = Field(
        default=(),
        description=(
            "Spend changes. A separate stream from the waste signals on purpose: an "
            "anomaly says 'investigate', never 'act', and nothing downstream turns one "
            "into a recommendation."
        ),
    )
    anomaly_assessments: tuple[AnomalyAssessment, ...] = Field(
        default=(),
        description=(
            "One per scope examined, including the clean ones and the inconclusive "
            "ones. 'We looked and spend is steady' and 'nine days are missing so we "
            "cannot tell' are both worth saying, and neither is an empty tuple."
        ),
    )
    attributions: tuple[CostAttribution, ...] = Field(
        default=(),
        description=(
            "What accounted for each anomaly, or why it could not be said. One per "
            "anomaly whose scope had a series, including the unattributable ones -- an "
            "absent attribution would read as 'nothing changed' rather than 'we cannot "
            "say what did'."
        ),
    )

    # --- exemption -------------------------------------------------------
    exemptions: tuple[ExemptionVerdict, ...] = Field(
        default=(),
        description=(
            "Verdicts for the signalled resources a tag convention marks as idle, "
            "stale or oversized on purpose. Only the exempt ones are carried: an "
            "estate's worth of 'we looked and found nothing' would bloat every "
            "checkpoint, so the count examined is recorded on the estate-read audit "
            "event instead. An exemption withholds a recommendation and nothing "
            "else -- it classifies, prices and ranks nothing."
        ),
    )

    # --- diagnosis -------------------------------------------------------
    root_causes: tuple[RootCause, ...] = ()

    # --- costing ---------------------------------------------------------
    priced_options: tuple[PricedOption, ...] = ()

    # --- proposal --------------------------------------------------------
    recommendations: tuple[Recommendation, ...] = ()
    ranking: tuple[RankingScore, ...] = ()

    # --- governance ------------------------------------------------------
    classifications: tuple[ActionClassification, ...] = ()
    approval_requests: tuple[ApprovalRequest, ...] = ()
    decisions: tuple[ApprovalDecision, ...] = ()
    approval_records: tuple[ApprovalRecord, ...] = Field(
        default=(),
        description=(
            "Every decision recorded against this sweep while it was paused, in the "
            "order they were made -- including deferrals, which resolve nothing. The "
            "audit record, as distinct from `decisions`, which is the minimal "
            "authorisation the gate protocol passes around."
        ),
    )

    # --- memory ----------------------------------------------------------
    recalls: tuple[MemoryRecall, ...] = Field(
        default=(),
        description=(
            "What episodic memory had to say about each recommendation. Affects "
            "ordering and annotation only; no monetary figure is derived from it."
        ),
    )
    memory_consulted: bool = Field(
        default=False,
        description=(
            "Whether the consult step has run. The supervisor routes on this rather "
            "than on whether any recalls were produced, because finding nothing is a "
            "legitimate outcome and would otherwise loop."
        ),
    )
    memory_metrics: MemoryMetrics | None = None

    # --- output ----------------------------------------------------------
    report: ClientReport | None = None

    # --- routing ---------------------------------------------------------
    supervisor_decisions: tuple[SupervisorDecision, ...] = ()
    escalations: tuple[str, ...] = Field(
        default=(),
        description="Reasons a run was escalated for human attention rather than retried.",
    )

    # --- cross-cutting ---------------------------------------------------
    audit: tuple[AuditEvent, ...] = ()
    errors: tuple[str, ...] = ()
    run_metrics: RunMetrics | None = None
    retries: Mapping[str, int] = Field(
        default_factory=dict,
        description="Per-node retry counters, consumed by the supervisor.",
    )

    def classification_for(self, recommendation_id: str) -> ActionClassification | None:
        """The Policy Guard's verdict for one recommendation, or ``None``."""
        return next(
            (c for c in self.classifications if c.recommendation_id == recommendation_id),
            None,
        )

    def decision_for(self, recommendation_id: str) -> ApprovalDecision | None:
        """The recorded human decision for one recommendation, or ``None``."""
        return next(
            (d for d in self.decisions if d.recommendation_id == recommendation_id),
            None,
        )

    def exemption_for(self, resource_id: str) -> ExemptionVerdict | None:
        """The exemption held against one resource, or ``None``."""
        return next((e for e in self.exemptions if e.resource_id == resource_id), None)

    @property
    def exempt_resource_ids(self) -> frozenset[str]:
        """Resources whose waste signals are deliberately not acted on."""
        return frozenset(exemption.resource_id for exemption in self.exemptions)

    @property
    def exempt_signals(self) -> tuple[WasteSignal, ...]:
        """Signals that are real and deliberately not actioned.

        Surfaced, not suppressed. A cost tool that silently dropped an idle machine
        would be lying by omission, and the client would lose the one fact they
        might act on later -- that the standby costs them money every month whether
        or not they choose to keep it.
        """
        exempt = self.exempt_resource_ids
        return tuple(signal for signal in self.signals if signal.resource_id in exempt)

    @property
    def actionable_signals(self) -> tuple[WasteSignal, ...]:
        """Signals a remediation may be proposed for.

        What the diagnosis, pricing and planning nodes work from. Exempt signals are
        excluded here rather than inside each node, so there is one definition of
        "actionable" rather than four that can drift.
        """
        exempt = self.exempt_resource_ids
        return tuple(signal for signal in self.signals if signal.resource_id not in exempt)

    @property
    def unassessed_scopes(self) -> tuple[AnomalyAssessment, ...]:
        """Scopes whose spend could not be assessed, with the reason each time."""
        return tuple(a for a in self.anomaly_assessments if not a.verdict.is_conclusive)

    def signal(self, signal_id: str) -> WasteSignal | None:
        """One signal by id, or ``None``."""
        return next((s for s in self.signals if s.signal_id == signal_id), None)

    def options_for(self, signal_id: str) -> tuple[PricedOption, ...]:
        """Every priced remediation option for one signal."""
        return tuple(o for o in self.priced_options if o.signal_id == signal_id)

    def option(self, signal_id: str, action: ActionType) -> PricedOption | None:
        """One priced option by signal and action, or ``None``."""
        return next(
            (o for o in self.priced_options if o.signal_id == signal_id and o.action is action),
            None,
        )

    def root_cause_for(self, signal_id: str) -> RootCause | None:
        """The diagnosis for one signal, or ``None`` if none was produced."""
        return next((r for r in self.root_causes if r.signal_id == signal_id), None)

    def recall_for(self, recommendation_id: str) -> MemoryRecall | None:
        """What memory had to say about one recommendation, or ``None``."""
        return next((r for r in self.recalls if r.recommendation_id == recommendation_id), None)

    def ranking_for(self, recommendation_id: str) -> RankingScore | None:
        """The computed ranking breakdown for one recommendation, or ``None``."""
        return next((r for r in self.ranking if r.recommendation_id == recommendation_id), None)

    def attempts(self, node: str) -> int:
        """How many times a node has already been attempted."""
        return self.retries.get(node, 0)
