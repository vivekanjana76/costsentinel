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
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    SupervisorDecision,
)
from costsentinel.domain.observability import RunMetrics
from costsentinel.domain.recommendations import ActionType, PricedOption, Recommendation
from costsentinel.domain.report import ClientReport
from costsentinel.domain.signals import CostAnomaly, WasteSignal


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
    anomalies: tuple[CostAnomaly, ...] = ()

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

    def ranking_for(self, recommendation_id: str) -> RankingScore | None:
        """The computed ranking breakdown for one recommendation, or ``None``."""
        return next((r for r in self.ranking if r.recommendation_id == recommendation_id), None)

    def attempts(self, node: str) -> int:
        """How many times a node has already been attempted."""
        return self.retries.get(node, 0)
