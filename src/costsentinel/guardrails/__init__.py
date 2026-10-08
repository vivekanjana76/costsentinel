"""Safety and governance: the policy store, action classes and the approval gate.

This package is the chokepoint CLAUDE.md golden rule 3 depends on. Nothing
destructive may reach an execution path without being classified here first.
"""

from costsentinel.guardrails.approval import ApprovalGate, NoDecisionGate
from costsentinel.guardrails.policy import (
    ACTION_CLASS_FLOOR,
    CANDIDATE_ACTIONS,
    PRECONDITIONS,
    PolicyStore,
)

__all__ = [
    "ACTION_CLASS_FLOOR",
    "CANDIDATE_ACTIONS",
    "PRECONDITIONS",
    "ApprovalGate",
    "NoDecisionGate",
    "PolicyStore",
]
