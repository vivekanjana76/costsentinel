"""The approval-gate seam.

Phase 1 ships the interface and the safe default only. The Policy Guard node
classifies every action and builds an :class:`ApprovalRequest` for anything gated,
but does not yet halt the graph -- and because Phase 1 has no execution path at all,
classifying without halting cannot cause a mutation.

Phase 3 turns this into a durable LangGraph interrupt with a local CLI gate
(ARCHITECTURE.md D7); Phase 7 adds the Teams and Slack adapters. All of them satisfy
this same protocol and produce the same
:class:`~costsentinel.domain.governance.ApprovalDecision`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from costsentinel.domain.governance import ApprovalDecision, ApprovalRequest


@runtime_checkable
class ApprovalGate(Protocol):
    """Puts a gated action to a human and reports the decision.

    Returning ``None`` means "no decision yet", which keeps the run halted. There is
    deliberately no way to express "timed out, therefore approved": absence of a
    decision is never authorisation.
    """

    @property
    def name(self) -> str:
        """Short identifier recorded in the audit trail."""
        ...

    def request(self, request: ApprovalRequest) -> ApprovalDecision | None:
        """Ask for a decision on one gated action.

        Args:
            request: What is being proposed, and what it is worth.

        Returns:
            The recorded decision, or ``None`` if no human has decided yet.
        """
        ...


class NoDecisionGate:
    """The Phase 1 default: records the request, never decides.

    This is the safe default by construction. If the gate were ever wired into the
    graph before a real adapter existed, every gated action would stay halted rather
    than quietly proceed.
    """

    def __init__(self) -> None:
        """Start with an empty record of requests seen."""
        self.requests: list[ApprovalRequest] = []

    @property
    def name(self) -> str:
        """Short identifier recorded in the audit trail."""
        return "no-decision-gate"

    def request(self, request: ApprovalRequest) -> ApprovalDecision | None:
        """Record the request and return no decision, leaving the action gated."""
        self.requests.append(request)
        return None
