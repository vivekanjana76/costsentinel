# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false
# LangGraph's StateGraph/CompiledStateGraph generics are only partially typed, so
# pyright's strict mode reports *its* members as unknown. The rule is disabled for
# this file only -- the two modules that touch LangGraph directly -- rather than
# relaxed across src, so everything CostSentinel owns stays strictly checked.
"""Graph assembly.

Phase 1 runs a linear four-node graph (ARCHITECTURE.md D20)::

    START -> anomaly_scout -> optimization_planner -> policy_guard -> report_author -> END

The supervisor, the conditional routing and the approval interrupt arrive in
Phase 3. Because every node is already an independent state transformer returning a
partial update, inserting them is additive -- edges change, nodes do not.

State is checkpointed after every node, so a sweep resumes from the last completed
node rather than re-reading the estate.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from costsentinel.agents.anomaly_scout import make_anomaly_scout
from costsentinel.agents.optimization_planner import make_optimization_planner
from costsentinel.agents.policy_guard import make_policy_guard
from costsentinel.agents.report_author import make_report_author
from costsentinel.config import Settings
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.base import LLM
from costsentinel.providers.base import AzureProvider

NODE_ANOMALY_SCOUT = "anomaly_scout"
NODE_OPTIMIZATION_PLANNER = "optimization_planner"
NODE_POLICY_GUARD = "policy_guard"
NODE_REPORT_AUTHOR = "report_author"

#: The Phase 1 node order, in one place so tests and docs cannot drift from it.
PHASE_1_NODES: tuple[str, ...] = (
    NODE_ANOMALY_SCOUT,
    NODE_OPTIMIZATION_PLANNER,
    NODE_POLICY_GUARD,
    NODE_REPORT_AUTHOR,
)


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
        provider: Read-only estate access. The Anomaly Scout is its only caller.
        llm: Structured reasoning for the planner's rationale and the report's prose.
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

    graph.add_node(NODE_ANOMALY_SCOUT, make_anomaly_scout(provider=provider, settings=settings))
    graph.add_node(
        NODE_OPTIMIZATION_PLANNER,
        make_optimization_planner(provider=provider, llm=llm, settings=settings, policy=store),
    )
    graph.add_node(NODE_POLICY_GUARD, make_policy_guard(settings=settings, policy=store))
    graph.add_node(NODE_REPORT_AUTHOR, make_report_author(llm=llm, settings=settings))

    graph.add_edge(START, NODE_ANOMALY_SCOUT)
    graph.add_edge(NODE_ANOMALY_SCOUT, NODE_OPTIMIZATION_PLANNER)
    graph.add_edge(NODE_OPTIMIZATION_PLANNER, NODE_POLICY_GUARD)
    graph.add_edge(NODE_POLICY_GUARD, NODE_REPORT_AUTHOR)
    graph.add_edge(NODE_REPORT_AUTHOR, END)

    return graph.compile(checkpointer=checkpointer)
