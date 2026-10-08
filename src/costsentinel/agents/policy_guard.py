"""Policy Guard: classify every proposed action, and build the approval queue.

This is the safety chokepoint. In Phase 1 it classifies and records but does not
halt -- which is safe because Phase 1 has no execution path at all, so a
classification without a gate cannot cause a mutation. What it produces is the
audit-grade record of *what class each action is*, plus an
:class:`~costsentinel.domain.governance.ApprovalRequest` for everything gated.

Phase 3 turns the gate into a durable LangGraph interrupt at exactly this point
(ARCHITECTURE.md D7): the graph halts, state persists, a human decides hours or days
later, and the run resumes on the recorded decision.
"""

from __future__ import annotations

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.config import Settings
from costsentinel.domain.estate import Environment
from costsentinel.domain.governance import (
    ActionClassification,
    ApprovalRequest,
    AuditEventType,
)
from costsentinel.domain.recommendations import ActionClass
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.observability.logging import get_logger

ACTOR = "policy_guard"

_log = get_logger("agents.policy_guard")


def _summary(
    *,
    resource_name: str,
    action_value: str,
    action_class: ActionClass,
    saving_display: str,
) -> str:
    """One line an approver can act on without opening the full report."""
    if action_class is ActionClass.BLOCK:
        return (
            f"{action_value.replace('_', ' ').capitalize()} on {resource_name} is "
            f"worth {saving_display} per month, but CostSentinel will not execute it. "
            f"It is reported for a human to perform by hand."
        )
    return (
        f"{action_value.replace('_', ' ').capitalize()} on {resource_name}, worth "
        f"{saving_display} per month. Requires your approval before anything changes."
    )


def make_policy_guard(*, settings: Settings, policy: PolicyStore | None = None) -> Node:
    """Build the Policy Guard node."""
    _ = settings
    store = policy or PolicyStore()

    def policy_guard(state: ScanState) -> NodeUpdate:
        """Classify each recommendation and queue whatever needs a human."""
        environments: dict[str, Environment] = (
            {sub.subscription_id: sub.environment for sub in state.estate.subscriptions}
            if state.estate
            else {}
        )

        classifications: list[ActionClassification] = []
        requests: list[ApprovalRequest] = []
        errors: list[str] = list(state.errors)

        for recommendation in state.recommendations:
            environment = environments.get(recommendation.subscription_id, Environment.UNKNOWN)
            classification = store.classification_for(
                recommendation_id=recommendation.recommendation_id,
                action=recommendation.action,
                environment=environment,
            )
            classifications.append(classification)

            # The planner reads the same policy store, so these must agree. If they
            # ever diverge, the Policy Guard's verdict is authoritative and the
            # divergence is recorded rather than silently resolved.
            if classification.action_class is not recommendation.risk_class:
                errors.append(
                    f"policy_guard: {recommendation.recommendation_id} was proposed as "
                    f"{recommendation.risk_class.value} but classifies as "
                    f"{classification.action_class.value}; the classification stands."
                )

            if classification.requires_approval:
                requests.append(
                    ApprovalRequest(
                        request_id=f"apr-{recommendation.recommendation_id.removeprefix('rec-')}",
                        run_id=state.run_id,
                        recommendation_id=recommendation.recommendation_id,
                        client=recommendation.client,
                        subscription_id=recommendation.subscription_id,
                        target_resource_id=recommendation.target_resource_id,
                        action=recommendation.action,
                        action_class=classification.action_class,
                        summary=_summary(
                            resource_name=recommendation.target_resource_name,
                            action_value=recommendation.action.value,
                            action_class=classification.action_class,
                            saving_display=recommendation.savings.monthly.display(),
                        ),
                        monthly_saving_display=recommendation.savings.monthly.display(),
                    )
                )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.ACTION_CLASSIFIED,
                subject=classification.recommendation_id,
                detail={
                    "action": classification.action.value,
                    "action_class": classification.action_class.value,
                    "floor_class": classification.floor_class.value,
                    "requires_approval": str(classification.requires_approval).lower(),
                    "is_automatable": str(classification.is_automatable).lower(),
                    "policy_reference": classification.policy_reference,
                },
                offset=offset,
            )
            for offset, classification in enumerate(classifications)
        ]
        events += [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.APPROVAL_REQUESTED,
                subject=request.recommendation_id,
                detail={
                    "request_id": request.request_id,
                    "action": request.action.value,
                    "action_class": request.action_class.value,
                    "monthly_saving": request.monthly_saving_display,
                    "gate": "not yet wired (Phase 3); no execution path exists",
                },
                offset=len(classifications) + offset,
            )
            for offset, request in enumerate(requests)
        ]

        gated = sum(1 for c in classifications if c.requires_approval)
        blocked = sum(1 for c in classifications if not c.is_automatable)
        _log.info(
            "actions classified",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "classified": len(classifications),
                "requires_approval": gated,
                "blocked": blocked,
            },
        )

        return {
            "classifications": tuple(classifications),
            "approval_requests": tuple(requests),
            "errors": tuple(errors),
            "audit": with_audit(state, events),
        }

    return policy_guard
