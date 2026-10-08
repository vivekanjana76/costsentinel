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

from costsentinel.domain.common import utc_now
from costsentinel.domain.estate import Estate
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
)
from costsentinel.domain.recommendations import Recommendation
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

    # --- proposal --------------------------------------------------------
    recommendations: tuple[Recommendation, ...] = ()

    # --- governance ------------------------------------------------------
    classifications: tuple[ActionClassification, ...] = ()
    approval_requests: tuple[ApprovalRequest, ...] = ()
    decisions: tuple[ApprovalDecision, ...] = ()

    # --- output ----------------------------------------------------------
    report: ClientReport | None = None

    # --- cross-cutting ---------------------------------------------------
    audit: tuple[AuditEvent, ...] = ()
    errors: tuple[str, ...] = ()
    retries: Mapping[str, int] = Field(
        default_factory=dict,
        description="Per-node retry counters, consumed by the supervisor in Phase 3.",
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
