"""Domain model: every Pydantic v2 type that crosses a module boundary.

CLAUDE.md golden rule 6 is "typed everywhere, no untyped dicts crossing module
lines". This package is the vocabulary that makes that possible, and it depends on
nothing else in CostSentinel -- providers, agents, guardrails and reports all depend
inward on it.
"""

from costsentinel.domain.common import (
    Confidence,
    Currency,
    Frozen,
    MoneyAmount,
    Percentage,
    Provenance,
    ProvenanceSource,
    Verification,
    utc_now,
)
from costsentinel.domain.estate import (
    CostPoint,
    CostSeries,
    Environment,
    Estate,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
    Subscription,
)
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    AuditEventType,
    Decision,
)
from costsentinel.domain.recommendations import (
    MONTHS_PER_YEAR,
    ActionClass,
    ActionType,
    Recommendation,
    SavingsEstimate,
)
from costsentinel.domain.report import (
    ApprovalQueueItem,
    ClientReport,
    ReportFinding,
    ReportPeriod,
    ReportSeverity,
    ReportTotals,
    ScanResult,
)
from costsentinel.domain.signals import (
    AnomalyDirection,
    CostAnomaly,
    Observation,
    WasteKind,
    WasteSignal,
)
from costsentinel.domain.state import ScanState

__all__ = [
    "MONTHS_PER_YEAR",
    "ActionClass",
    "ActionClassification",
    "ActionType",
    "AnomalyDirection",
    "ApprovalDecision",
    "ApprovalQueueItem",
    "ApprovalRequest",
    "AuditEvent",
    "AuditEventType",
    "ClientReport",
    "Confidence",
    "CostAnomaly",
    "CostPoint",
    "CostSeries",
    "Currency",
    "Decision",
    "Environment",
    "Estate",
    "Frozen",
    "MoneyAmount",
    "Observation",
    "Percentage",
    "Provenance",
    "ProvenanceSource",
    "Recommendation",
    "ReportFinding",
    "ReportPeriod",
    "ReportSeverity",
    "ReportTotals",
    "Resource",
    "ResourceKind",
    "ResourceMetrics",
    "ResourceState",
    "SavingsEstimate",
    "ScanResult",
    "ScanState",
    "SkuPrice",
    "Subscription",
    "Verification",
    "WasteKind",
    "WasteSignal",
    "utc_now",
]
