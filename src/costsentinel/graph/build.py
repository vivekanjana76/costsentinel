# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false
# LangGraph's StateGraph/CompiledStateGraph generics are only partially typed, so
# pyright's strict mode reports *its* members as unknown. The rule is disabled for
# this file only -- the two modules that touch LangGraph directly -- rather than
# relaxed across src, so everything CostSentinel owns stays strictly checked.
"""Graph assembly.

Phase 2 runs the full six-specialist graph with a supervisor owning every
conditional edge::

    START -> supervisor -+-> anomaly_scout --------+
                         |                         |
                         +-> root_cause_analyst ---+
                         |                         |
                         +-> savings_estimator ----+--> back to supervisor
                         |                         |
                         +-> optimization_planner -+
                         |                         |
                         +-> policy_guard ---------+
                         |
                         +-> report_author -> END

Every specialist returns to the supervisor, which re-reads the state and decides what
happens next. That star shape is what makes retry, escalation, short-circuit and the
approval seam one mechanism instead of four: the supervisor's decision is a recorded
fact in state, and :func:`route_from_supervisor` simply reads it back.

State is checkpointed after every node, so a sweep resumes from the last completed
node rather than re-reading the estate -- and from Phase 7 the same mechanism lets a
run halt for days awaiting a human approval.
"""

from __future__ import annotations

from collections.abc import Hashable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from costsentinel.agents.anomaly_scout import make_anomaly_scout
from costsentinel.agents.optimization_planner import make_optimization_planner
from costsentinel.agents.policy_guard import make_policy_guard
from costsentinel.agents.report_author import make_report_author
from costsentinel.agents.root_cause_analyst import make_root_cause_analyst
from costsentinel.agents.savings_estimator import make_savings_estimator
from costsentinel.agents.supervisor import make_supervisor, route_from_supervisor
from costsentinel.config import Settings
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.base import LLM
from costsentinel.providers.base import AzureProvider

NODE_SUPERVISOR = "supervisor"
NODE_ANOMALY_SCOUT = "anomaly_scout"
NODE_ROOT_CAUSE_ANALYST = "root_cause_analyst"
NODE_SAVINGS_ESTIMATOR = "savings_estimator"
NODE_OPTIMIZATION_PLANNER = "optimization_planner"
NODE_POLICY_GUARD = "policy_guard"
NODE_REPORT_AUTHOR = "report_author"

#: The six specialists from CLAUDE.md section 5, in the order a full sweep visits
#: them. Kept in one place so tests, docs and the diagram cannot drift from the code.
SPECIALIST_NODES: tuple[str, ...] = (
    NODE_ANOMALY_SCOUT,
    NODE_ROOT_CAUSE_ANALYST,
    NODE_SAVINGS_ESTIMATOR,
    NODE_OPTIMIZATION_PLANNER,
    NODE_POLICY_GUARD,
    NODE_REPORT_AUTHOR,
)

#: Every node in the graph: the six specialists plus the supervisor.
PHASE_2_NODES: tuple[str, ...] = (NODE_SUPERVISOR, *SPECIALIST_NODES)

#: Where the supervisor may send a run. Declaring the map explicitly means an
#: unroutable decision is a build-time error rather than a silent dead end.
_ROUTES: dict[Hashable, str] = {
    NODE_ANOMALY_SCOUT: NODE_ANOMALY_SCOUT,
    NODE_ROOT_CAUSE_ANALYST: NODE_ROOT_CAUSE_ANALYST,
    NODE_SAVINGS_ESTIMATOR: NODE_SAVINGS_ESTIMATOR,
    NODE_OPTIMIZATION_PLANNER: NODE_OPTIMIZATION_PLANNER,
    NODE_POLICY_GUARD: NODE_POLICY_GUARD,
    NODE_REPORT_AUTHOR: NODE_REPORT_AUTHOR,
    "__end__": END,
}


def build_graph(
    *,
    provider: AzureProvider,
    llm: LLM,
    settings: Settings,
    policy: PolicyStore | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[ScanState, None, ScanState, ScanState]:
    """Assemble and compile the scan graph.

    Args:
        provider: Read-only estate access. The Anomaly Scout and the Savings
            Estimator are its only callers.
        llm: Structured reasoning. Wrap it in
            :class:`~costsentinel.llm.traced.TracedLLM` to get per-call metrics.
        settings: Resolved configuration.
        policy: The policy store. Defaults to the built-in one.
        checkpointer: Durable state store. ``None`` compiles an unpersisted graph,
            which is useful for a unit test of the node sequence but is not how a
            real sweep runs.

    Returns:
        The compiled graph, ready to invoke with a :class:`ScanState`.
    """
    store = policy or PolicyStore()

    graph: StateGraph[ScanState, None, ScanState, ScanState] = StateGraph(ScanState)

    graph.add_node(NODE_SUPERVISOR, make_supervisor(settings=settings))
    graph.add_node(NODE_ANOMALY_SCOUT, make_anomaly_scout(provider=provider, settings=settings))
    graph.add_node(NODE_ROOT_CAUSE_ANALYST, make_root_cause_analyst(llm=llm, settings=settings))
    graph.add_node(
        NODE_SAVINGS_ESTIMATOR,
        make_savings_estimator(provider=provider, settings=settings, policy=store),
    )
    graph.add_node(
        NODE_OPTIMIZATION_PLANNER,
        make_optimization_planner(llm=llm, settings=settings, policy=store),
    )
    graph.add_node(NODE_POLICY_GUARD, make_policy_guard(settings=settings, policy=store))
    graph.add_node(NODE_REPORT_AUTHOR, make_report_author(llm=llm, settings=settings))

    graph.add_edge(START, NODE_SUPERVISOR)
    graph.add_conditional_edges(NODE_SUPERVISOR, route_from_supervisor, _ROUTES)

    # Every specialist except the report author returns to the supervisor, so a
    # single node decides every transition. The report author ends the run, which is
    # what terminates the loop despite the star shape.
    for node in (
        NODE_ANOMALY_SCOUT,
        NODE_ROOT_CAUSE_ANALYST,
        NODE_SAVINGS_ESTIMATOR,
        NODE_OPTIMIZATION_PLANNER,
        NODE_POLICY_GUARD,
    ):
        graph.add_edge(node, NODE_SUPERVISOR)
    graph.add_edge(NODE_REPORT_AUTHOR, END)

    return graph.compile(checkpointer=checkpointer)
