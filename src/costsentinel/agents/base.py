"""Shared plumbing for graph nodes.

Nodes are built by factories that close over their dependencies (a provider, a
model, the policy store) and return a plain function of
:class:`~costsentinel.domain.state.ScanState`. That keeps LangGraph's single-argument
node signature while leaving every dependency injectable, so a node can be tested
without a graph.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from costsentinel.domain.governance import AuditEvent, AuditEventType
from costsentinel.domain.state import ScanState

#: A partial state update: only the fields a node changed (ARCHITECTURE.md D14).
NodeUpdate = dict[str, Any]


class Node(Protocol):
    """What a built node looks like to LangGraph.

    A ``Protocol`` with a *named* ``state`` parameter rather than
    ``Callable[[ScanState], NodeUpdate]``: LangGraph may call a node by keyword, and
    a bare ``Callable`` is position-only, so the alias would not actually describe
    what LangGraph accepts.
    """

    def __call__(self, state: ScanState) -> NodeUpdate:
        """Transform the state, returning only the fields changed."""
        ...


def audit(
    state: ScanState,
    *,
    actor: str,
    event_type: AuditEventType,
    subject: str,
    detail: dict[str, str] | None = None,
    offset: int = 0,
) -> AuditEvent:
    """Build one audit event, numbered within the run.

    Event ids are ``<run_id>-<sequence>`` rather than random, so they are stable
    across a checkpoint resume and comparable between runs of the same estate.

    Args:
        state: The current state, used for the run id and the running count.
        actor: The node, adapter or person responsible.
        event_type: What happened.
        subject: What it happened to -- a resource, signal or report id.
        detail: Flat string fields safe to render in a client-facing trail.
        offset: Position within a batch of events emitted by one node call.
    """
    sequence = len(state.audit) + offset
    return AuditEvent(
        event_id=f"{state.run_id}-{sequence:04d}",
        run_id=state.run_id,
        event_type=event_type,
        actor=actor,
        subject=subject,
        detail=detail or {},
    )


def with_audit(state: ScanState, events: Sequence[AuditEvent]) -> tuple[AuditEvent, ...]:
    """Append events to the run's trail.

    Nodes return the whole new tuple rather than relying on a LangGraph reducer, so
    that what a node contributes to the audit trail is visible in its own return
    value.
    """
    return (*state.audit, *events)
