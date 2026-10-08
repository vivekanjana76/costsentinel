"""Specialist agents: the graph's nodes.

Phase 1 implements four of the six specialists from ARCHITECTURE.md section 2.2.
The Root-Cause Analyst and the Savings Estimator arrive in Phase 3, along with the
supervisor that owns conditional routing. Each node here is built by a factory that
closes over its dependencies, so each is testable without a graph.
"""

from costsentinel.agents.anomaly_scout import make_anomaly_scout
from costsentinel.agents.base import Node, NodeUpdate
from costsentinel.agents.detectors import (
    DETECTORS,
    IDLE_CPU_MAX_PCT,
    MIN_OBSERVATION_DAYS,
    OVERSIZED_CPU_AVG_PCT,
    OVERSIZED_CPU_MAX_PCT,
    run_detectors,
    signal_id_for,
)
from costsentinel.agents.optimization_planner import make_optimization_planner
from costsentinel.agents.policy_guard import make_policy_guard
from costsentinel.agents.report_author import make_report_author
from costsentinel.agents.savings import (
    choose_rightsize_target,
    estimate_savings,
    total_savings,
)

__all__ = [
    "DETECTORS",
    "IDLE_CPU_MAX_PCT",
    "MIN_OBSERVATION_DAYS",
    "OVERSIZED_CPU_AVG_PCT",
    "OVERSIZED_CPU_MAX_PCT",
    "Node",
    "NodeUpdate",
    "choose_rightsize_target",
    "estimate_savings",
    "make_anomaly_scout",
    "make_optimization_planner",
    "make_policy_guard",
    "make_report_author",
    "run_detectors",
    "signal_id_for",
    "total_savings",
]
