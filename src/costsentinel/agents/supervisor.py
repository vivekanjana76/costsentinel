"""Supervisor: the only place routing decisions are made.

CLAUDE.md section 5 puts the supervisor at the centre of the graph, and
ARCHITECTURE.md D13 explains why: keeping every conditional in one node leaves the
specialists as pure state transformers that can be tested without a graph, and puts
retry, escalation and approval routing somewhere a reviewer can read in one sitting.

Four decisions live here:

* **short-circuit** -- an estate with no signals goes straight to the report, rather
  than running five nodes over an empty list to produce the same empty report;
* **retry** -- a node that produced nothing usable is retried up to a bounded
  number of attempts, with the attempt counted in ``state.retries``;
* **escalate** -- when retries are exhausted, the run proceeds with what it has and
  records *why* it is incomplete, rather than either looping or dying silently;
* **require-approval** -- gated actions route to the approval seam.

Phase 10 turns that last branch into a durable interrupt. This phase builds the seam
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
    # --- the post-report phase ------------------------------------------
    #
    # Once the report is written the sweep is in its final phase, and the only
    # business left is a decision on anything gated. Checked first, and it has to be:
    # from Phase 10 the report author returns to the supervisor rather than ending the
    # run, so without this every earlier branch that routes *to* the report author
    # would fire again on the way back and loop forever. That is not hypothetical --
    # it is what happened, and a hanging test is how it was found.
    if state.report is not None:
        undecided = pending_recommendations(state)
        if undecided:
            return _record(
                action=RouteAction.REQUIRE_APPROVAL,
                to_node="approval_gate",
                reason=(
                    f"{len(undecided)} action(s) need a recorded human decision; the "
                    f"sweep halts at a durable checkpoint until one is given. "
                    f"Approval hands the action to a person -- CostSentinel has no "
                    f"execution path"
                ),
            )
        return _record(
            action=RouteAction.COMPLETE,
            to_node="__end__",
            reason=(
                "the report has been produced and every gated action has a recorded decision"
                if state.classifications
                else "the report has been produced"
            ),
        )

    # Nothing was read: the scout has not run yet.
    if state.estate is None:
        return _record(
            action=RouteAction.PROCEED,
            to_node="anomaly_scout",
            reason="no estate has been read yet, so the sweep starts with discovery",
        )

    # Read, but nothing *actionable* found. There is nothing for four nodes to do.
    #
    # Keyed on actionable signals rather than on signals, because an estate whose
    # every finding is exempt has nothing to diagnose, price or plan either -- and
    # routing it onward would send it round the retry path until the planner
    # escalated, for no reason. The reason line distinguishes the two cases, since
    # "nothing found" and "everything found is protected by your own tags" are very
    # different things to tell a client.
    if not state.actionable_signals:
        exempt = len(state.exemptions)
        # An anomaly is reported, not remediated, so it does not keep the sweep
        # running -- but a short-circuit that said "nothing found" while a spend change
        # was waiting in the report would be the wrong thing to record.
        found = (
            f" {len(state.anomalies)} spend change(s) were found and are reported for "
            f"investigation."
            if state.anomalies
            else ""
        )
        examined = (
            f"{len(state.estate.resources)} resource(s) and "
            f"{len(state.estate.reservations)} reservation(s) examined"
        )
        return _record(
            action=RouteAction.SHORT_CIRCUIT,
            to_node="report_author",
            reason=(
                f"{examined} with no waste detected, so there is nothing to "
                f"diagnose, price or classify.{found}"
                if not exempt
                else (
                    f"{examined}; every one of the {len(state.signals)} signal(s) "
                    f"found is on a resource the client's tags exempt, so all "
                    f"{exempt} are reported with their reason and none is "
                    f"actioned.{found}"
                )
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
                f"{len(state.actionable_signals)} signal(s) await diagnosis"
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
            reason=f"{len(state.actionable_signals)} signal(s) await pricing",
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
                    f"{attempts} attempt(s) despite {len(state.actionable_signals)} "
                    f"actionable signal(s); "
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

    # Classified, but memory has not been asked yet. The consult step reorders and
    # annotates against what this client already decided, and it has to run before
    # the report rather than after -- the report is the thing the client reads.
    #
    # Routed on ``memory_consulted`` rather than on whether any recalls exist,
    # because finding nothing is a legitimate outcome and testing for recalls would
    # loop forever on a client with no history.
    if not state.memory_consulted:
        return _record(
            action=RouteAction.PROCEED,
            to_node="memory_consult",
            reason=(
                f"{len(state.recommendations)} recommendation(s) are classified; "
                f"consulting what this client previously decided before reporting"
            ),
        )

    # Classified, memory consulted, and no report yet. The report is written *before*
    # the approval gate rather than after it (D119): a client waiting on their cost
    # review must not be waiting on their own approval of the things in it, and the
    # document is correct and complete either way. What happens next is decided by the
    # post-report branch at the top of this function, on the way back.
    gated = [c for c in state.classifications if c.requires_approval]
    return _record(
        action=RouteAction.PROCEED,
        to_node="report_author",
        reason=(
            f"every action is classified; writing the report, which will list "
            f"{len(gated)} action(s) awaiting a decision"
            if gated
            else "every action is classified and none is gated; the report can be written"
        ),
    )


def pending_recommendations(state: ScanState) -> tuple[str, ...]:
    """Gated recommendations the approval gate would actually ask about.

    Delegates to :func:`~costsentinel.agents.approval_gate.pending_for` rather than
    recomputing the condition, and that is not tidiness -- it is the fix for a hang.
    The first version counted every classified-and-gated recommendation, while the
    gate only queues the ones it can show an approver (present in the report, with a
    figure). A recommendation classified but absent from the report made the two
    disagree: the supervisor routed to the gate, the gate found nothing to ask, the
    supervisor routed to the gate again, forever.

    One definition of "pending", used by both the node that asks and the node that
    decides whether to ask. A *deferral* leaves an item pending, which is the whole
    difference between it and a rejection.
    """
    from costsentinel.agents.approval_gate import pending_for  # noqa: PLC0415 -- cycle

    return tuple(item.recommendation_id for item in pending_for(state))


def route_from_supervisor(state: ScanState) -> str:
    """The LangGraph conditional edge: which node runs next.

    Reads the decision the supervisor already recorded, so the edge and the audit
    trail cannot disagree about what happened.
    """
    if not state.supervisor_decisions:  # pragma: no cover -- supervisor always records
        return "anomaly_scout"
    return state.supervisor_decisions[-1].to_node
