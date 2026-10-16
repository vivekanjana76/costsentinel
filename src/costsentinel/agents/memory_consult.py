"""Memory Consult: ask what this client has already said about this advice.

Sits between the Policy Guard and the Report Author. By the time it runs, detection
has happened, every option has been priced, one has been chosen, the deterministic
ranking has been computed and the policy class assigned. This node changes exactly
two things: the integer ``rank`` on each recommendation, and the annotations the
report will carry.

It is the only node in the graph that reads from memory, and it imports no language
model -- a test asserts that. Both facts matter for the same reason: the one thing
memory must never do is move a number. Everything monetary has already been computed
by the time this node is reached, and this node has no way to recompute it.

What it does not do is remove anything. A client declining advice does not make the
waste disappear, and a report that quietly dropped it would misstate the estate. The
finding stays, annotated with the date and reason it was declined, ranked below the
things the client has not already considered.
"""

from __future__ import annotations

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.config import Settings
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.memory import MemoryMetrics, Outcome
from costsentinel.domain.state import ScanState
from costsentinel.memory.base import MemoryStore
from costsentinel.memory.episodic import latest_outcomes
from costsentinel.memory.recall import consult, recall_summary
from costsentinel.observability.logging import get_logger

ACTOR = "memory_consult"

_log = get_logger("agents.memory_consult")


def make_memory_consult(*, memory: MemoryStore, settings: Settings) -> Node:
    """Build the Memory Consult node.

    Args:
        memory: The store to consult. A :class:`NullMemoryStore` is fine and makes
            this node a recorded no-op, which is why no caller needs a conditional.
        settings: Resolved configuration, for the recall limit.
    """

    def memory_consult(state: ScanState) -> NodeUpdate:
        """Re-rank and annotate against what this client previously decided."""
        if not state.recommendations:
            # Nothing to consult about. Still marks itself done, so the supervisor
            # does not route here again.
            return {
                "memory_consulted": True,
                "audit": with_audit(
                    state,
                    [
                        audit(
                            state,
                            actor=ACTOR,
                            event_type=AuditEventType.ACTION_CLASSIFIED,
                            subject=state.client,
                            detail={
                                "recalls": "0",
                                "reason": "no recommendations to consult memory about",
                                "store": memory.name,
                            },
                        )
                    ],
                ),
            }

        memory.reset_accesses()
        outcomes = latest_outcomes(memory.outcomes_for(state.client))
        reranked, recalls = consult(
            recommendations=state.recommendations,
            signals=state.signals,
            ranking=state.ranking,
            outcomes=outcomes,
        )
        metrics = MemoryMetrics(accesses=tuple(memory.accesses()))

        demoted = [recall for recall in recalls if recall.was_demoted]
        _log.info(
            "memory consulted",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "store": memory.name,
                "enabled": memory.is_enabled,
                "prior_outcomes": len(outcomes),
                "recommendations": len(reranked),
                "recalls": len(recalls),
                "demoted": len(demoted),
                "memory_hits": metrics.hit_count,
                "memory_misses": metrics.miss_count,
                "memory_latency_ms": metrics.total_latency_ms,
            },
        )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.ACTION_CLASSIFIED,
                subject=recall.recommendation_id,
                detail={
                    "recall": recall.kind.value,
                    "prior_outcome": recall.prior_outcome.value,
                    "prior_recommendation": recall.prior_recommendation_id,
                    "decided_at": recall.decided_at.isoformat(),
                    "penalty": str(recall.penalty),
                    "base_rank": str(recall.base_rank),
                    "adjusted_rank": str(recall.adjusted_rank),
                },
                offset=offset,
            )
            for offset, recall in enumerate(recalls)
        ]
        if not events:
            events = [
                audit(
                    state,
                    actor=ACTOR,
                    event_type=AuditEventType.ACTION_CLASSIFIED,
                    subject=state.client,
                    detail={
                        "recalls": "0",
                        "prior_outcomes": str(len(outcomes)),
                        "store": memory.name,
                        "reason": (
                            "no prior outcome matched"
                            if memory.is_enabled
                            else "memory is switched off"
                        ),
                    },
                )
            ]

        # ``errors`` is deliberately not touched. It feeds the report's "may be
        # incomplete" wording, and a client having declined something last month has
        # not made this report incomplete. The recall summary reaches the report
        # through its own field instead.
        return {
            "recommendations": reranked,
            "recalls": recalls,
            "memory_consulted": True,
            "memory_metrics": metrics,
            "audit": with_audit(state, events),
        }

    _ = settings
    return memory_consult


def summarise_recalls(state: ScanState) -> str:
    """The recall line the report carries, or an empty string."""
    return recall_summary(state.recalls)


def declined_count(state: ScanState) -> int:
    """How many findings this client has previously declined or deferred."""
    return sum(1 for recall in state.recalls if recall.prior_outcome.discourages_repitch)


def accepted_count(state: ScanState) -> int:
    """How many findings match a pattern this client previously accepted."""
    return sum(1 for recall in state.recalls if recall.prior_outcome is Outcome.ACCEPTED)
