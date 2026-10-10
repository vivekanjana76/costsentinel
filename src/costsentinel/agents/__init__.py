"""Specialist agents: the graph's nodes.

All six specialists from ARCHITECTURE.md section 2.2, plus the supervisor that owns
every conditional edge. Each node is built by a factory that closes over its
dependencies, so each is testable without a graph -- which is what lets the routing
tests construct a state and assert a decision rather than running a sweep.
"""

from costsentinel.agents.anomaly_scout import make_anomaly_scout
from costsentinel.agents.base import Node, NodeUpdate
from costsentinel.agents.detectors import (
    DETECTORS,
    IDLE_CPU_MAX_PCT,
    IDLE_SQL_MAX_CONNECTIONS,
    MIN_OBSERVATION_DAYS,
    OVERSIZED_CPU_AVG_PCT,
    OVERSIZED_CPU_MAX_PCT,
    OVERSIZED_PLAN_UTILISATION_PCT,
    STALE_SNAPSHOT_AGE_DAYS,
    UNUSED_RESERVATION_UTILISATION_PCT,
    run_detectors,
    run_reservation_detectors,
    signal_id_for,
)
from costsentinel.agents.optimization_planner import make_optimization_planner
from costsentinel.agents.policy_guard import make_policy_guard
from costsentinel.agents.ranking import (
    WEIGHT_CONFIDENCE,
    WEIGHT_RISK,
    WEIGHT_SAVINGS,
    rank_order,
    score_recommendations,
)
from costsentinel.agents.report_author import make_report_author
from costsentinel.agents.root_cause_analyst import make_root_cause_analyst
from costsentinel.agents.savings import (
    SavingsTarget,
    choose_rightsize_target,
    estimate_savings,
    total_savings,
)
from costsentinel.agents.savings_estimator import make_savings_estimator
from costsentinel.agents.supervisor import (
    MAX_ATTEMPTS,
    make_supervisor,
    route_from_supervisor,
)

__all__ = [
    "DETECTORS",
    "IDLE_CPU_MAX_PCT",
    "IDLE_SQL_MAX_CONNECTIONS",
    "MAX_ATTEMPTS",
    "MIN_OBSERVATION_DAYS",
    "OVERSIZED_CPU_AVG_PCT",
    "OVERSIZED_CPU_MAX_PCT",
    "OVERSIZED_PLAN_UTILISATION_PCT",
    "STALE_SNAPSHOT_AGE_DAYS",
    "UNUSED_RESERVATION_UTILISATION_PCT",
    "WEIGHT_CONFIDENCE",
    "WEIGHT_RISK",
    "WEIGHT_SAVINGS",
    "Node",
    "NodeUpdate",
    "SavingsTarget",
    "choose_rightsize_target",
    "estimate_savings",
    "make_anomaly_scout",
    "make_optimization_planner",
    "make_policy_guard",
    "make_report_author",
    "make_root_cause_analyst",
    "make_savings_estimator",
    "make_supervisor",
    "rank_order",
    "route_from_supervisor",
    "run_detectors",
    "run_reservation_detectors",
    "score_recommendations",
    "signal_id_for",
    "total_savings",
]
