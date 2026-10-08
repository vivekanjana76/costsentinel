"""Supervisor: the only place routing decisions are made.

CLAUDE.md section 5 puts the supervisor at the centre of the graph, and
ARCHITECTURE.md D13 explains why: keeping every conditional in one node leaves the
specialists as pure state transformers that can be tested without a graph, and puts
retry, escalation and approval routing somewhere a reviewer can read in one sitting.

Four decisions live here:

* **short-circuit** -- an estate with no signals goes straight to the report, rather
  than running four nodes over an empty list to produce the same empty report;
* **retry** -- a node that produced nothing usable is retried up to a bounded
  number of attempts, with the attempt counted in ``state.retries``;
* **escalate** -- when retries are exhausted, the run proceeds with what it has and
  records *why* it is incomplete, rather than either looping or dying silently;
* **require-approval** -- gated actions route to the approval seam.

Phase 7 turns that last branch into a durable interrupt. This phase builds the seam
and the records it will act on, which is why :class:`RouteAction` already has a
``REQUIRE_APPROVAL`` member that currently routes onward to the report.
"""

from __future__ import annotations

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.config import Settings
from costsentinel.domain.governance import AuditEventType, RouteAction, SupervisorDecision
from costsentinel.domain.state import ScanState
from costsentinel.observability.logging import get_logger

ACTOR = "supervisor"

#: How many times one node may be retried before the run escalates instead.
#: Two is deliberate: one retry covers a transient malformed response, and a second
#: failure is a real problem that a human should see rather than something to grind
#: against. A higher limit would mostly buy a longer wait before the same outcome.
MAX_ATTEMPTS = 2

_log = get_logger("agents.supervisor")


def _record(
    *,
    action: RouteAction,
    to_node: str,
    reason: str,
    attempt: int = 1,
) -> SupervisorDecision:
    return SupervisorDecision(
        from_node=ACTOR,
        action=action,
        to_node=to_node,
        reason=reason,
        attempt=attempt,
    )


def make_supervisor(*, settings: Settings) -> Node:
    """Build the supervisor node.

    The node itself records the decision; :func:`route_from_supervisor` reads it
    back to pick the edge. Splitting it that way keeps the decision in state -- and
    therefore in the checkpoint and the audit trail -- rather than living only in a
    routing function LangGraph calls and forgets.
    """
    _ = settings

    def supervisor(state: ScanState) -> NodeUpdate:
        """Decide what should happen next, and record why."""
        decision = _decide(state)
        retries = dict(state.retries)
        if decision.action is RouteAction.RETRY:
            retries[decision.to_node] = decision.attempt

        escalations = list(state.escalations)
        if decision.action is RouteAction.ESCALATE:
            escalations.append(decision.reason)

        _log.info(
            "routing decision",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "action": decision.action.value,
                "to_node": decision.to_node,
                "attempt": decision.attempt,
                "reason": decision.reason,
            },
        )

        return {
            "supervisor_decisions": (*state.supervisor_decisions, decision),
            "retries": retries,
            "escalations": tuple(escalations),
            "audit": with_audit(
                state,
                [
                    audit(
                        state,
                        actor=ACTOR,
                        event_type=(
                            AuditEventType.APPROVAL_REQUESTED
                            if decision.action is RouteAction.REQUIRE_APPROVAL
                            else AuditEventType.SCAN_STARTED
                        ),
                        subject=decision.to_node,
                        detail={
                            "route": decision.action.value,
                            "reason": decision.reason,
                            "attempt": str(decision.attempt),
                        },
                    )
                ],
            ),
        }

    return supervisor


def _decide(state: ScanState) -> SupervisorDecision:
    """Work out the next step from the state alone.

    Deliberately a pure function of state, so a routing decision can be tested by
    constructing a state rather than by running a graph.
    """
    # Nothing was read: the scout has not run yet.
    if state.estate is None:
        return _record(
            action=RouteAction.PROCEED,
            to_node="anomaly_scout",
            reason="no estate has been read yet, so the sweep starts with discovery",
        )

    # Read, but nothing wasteful found. There is nothing for four nodes to do.
    if not state.signals:
        return _record(
            action=RouteAction.SHORT_CIRCUIT,
            to_node="report_author",
            reason=(
                f"{len(state.estate.resources)} resource(s) and "
                f"{len(state.estate.reservations)} reservation(s) examined with no "
                f"waste detected, so there is nothing to diagnose, price or classify"
            ),
        )

    # Signals exist but nothing was diagnosed: the analyst has not run, or failed.
    if not state.root_causes:
        attempts = state.attempts("root_cause_analyst")
        if attempts >= MAX_ATTEMPTS:
            return _record(
                action=RouteAction.ESCALATE,
                to_node="savings_estimator",
                reason=(
                    f"root_cause_analyst produced no diagnosis after {attempts} "
                    f"attempt(s); continuing without root causes and flagging the "
                    f"report as incomplete"
                ),
                attempt=attempts,
            )
        return _record(
            action=RouteAction.RETRY if attempts else RouteAction.PROCEED,
            to_node="root_cause_analyst",
            reason=(
                f"{len(state.signals)} signal(s) await diagnosis"
                if not attempts
                else f"retrying diagnosis, attempt {attempts + 1}"
            ),
            attempt=attempts + 1,
        )

    # Diagnosed but not priced.
    if not state.priced_options:
        return _record(
            action=RouteAction.PROCEED,
            to_node="savings_estimator",
            reason=f"{len(state.signals)} signal(s) await pricing",
        )

    # Priced but not planned.
    if not state.recommendations:
        attempts = state.attempts("optimization_planner")
        if attempts >= MAX_ATTEMPTS:
            return _record(
                action=RouteAction.ESCALATE,
                to_node="report_author",
                reason=(
                    f"optimization_planner produced no recommendations after "
                    f"{attempts} attempt(s) despite {len(state.signals)} signal(s); "
                    f"reporting the findings without remediations"
                ),
                attempt=attempts,
            )
        return _record(
            action=RouteAction.RETRY if attempts else RouteAction.PROCEED,
            to_node="optimization_planner",
            reason=(
                f"{len(state.priced_options)} priced option(s) await a plan"
                if not attempts
                else f"retrying planning, attempt {attempts + 1}"
            ),
            attempt=attempts + 1,
        )

    # Planned but not classified: the Policy Guard is the mandatory next step.
    if not state.classifications:
        return _record(
            action=RouteAction.PROCEED,
            to_node="policy_guard",
            reason=(
                f"{len(state.recommendations)} recommendation(s) must be classified "
                f"before anything is reported as actionable"
            ),
        )

    # Classified. Anything gated routes through the approval seam.
    gated = [c for c in state.classifications if c.requires_approval]
    undecided = [c for c in gated if state.decision_for(c.recommendation_id) is None]
    if undecided:
        return _record(
            action=RouteAction.REQUIRE_APPROVAL,
            to_node="report_author",
            reason=(
                f"{len(undecided)} of {len(state.classifications)} action(s) need a "
                f"recorded human decision before they can be executed; none has an "
                f"execution path in this phase, so they are reported for approval"
            ),
        )

    if state.report is None:
        return _record(
            action=RouteAction.PROCEED,
            to_node="report_author",
            reason="every action is classified and decided; the report can be written",
        )

    return _record(
        action=RouteAction.COMPLETE,
        to_node="__end__",
        reason="the report has been produced",
    )


def route_from_supervisor(state: ScanState) -> str:
    """The LangGraph conditional edge: which node runs next.

    Reads the decision the supervisor already recorded, so the edge and the audit
    trail cannot disagree about what happened.
    """
    if not state.supervisor_decisions:  # pragma: no cover -- supervisor always records
        return "anomaly_scout"
    return state.supervisor_decisions[-1].to_node
