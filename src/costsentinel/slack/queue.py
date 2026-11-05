# pyright: reportUnknownMemberType=false
# Reads sweep state through a compiled LangGraph graph, whose generics are only
# partially typed -- the same per-file allowance the graph package makes.
"""The pending queue, as Slack messages, read from the checkpoint.

Like the dashboard, this reads what a sweep left behind rather than running one, so
rendering or posting the queue costs no provider call. Each message is built from that
sweep's own stored :class:`~costsentinel.domain.report.ClientReport` and its pending
items, which the gate read from the same report (:mod:`.blocks`).
"""

from __future__ import annotations

from costsentinel.config import Settings
from costsentinel.domain.common import Frozen
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph, paused_sweeps
from costsentinel.graph.checkpointer import checkpointer_for
from costsentinel.slack.blocks import SlackMessage, approval_messages
from costsentinel.slack.client import PostedMessage, SlackClient


class QueuedMessage(Frozen):
    """One pending approval's message, and which sweep and item it is for."""

    run_id: str
    recommendation_id: str
    message: SlackMessage


def queued_messages(settings: Settings, *, run_id: str | None = None) -> tuple[QueuedMessage, ...]:
    """A message per pending approval, in the queue's stable order.

    Args:
        settings: Where the checkpoint lives.
        run_id: One sweep only. Default: every paused sweep.
    """
    queued: list[QueuedMessage] = []
    with checkpointer_for(settings) as saver:
        graph = approvals_graph(settings, saver)
        for sweep in paused_sweeps(graph, saver):
            if run_id is not None and sweep.run_id != run_id:
                continue
            snapshot = graph.get_state({"configurable": {"thread_id": sweep.run_id}})
            report = ScanState.model_validate(snapshot.values).report
            if report is None:  # pragma: no cover -- the gate pauses after the report
                continue
            messages = approval_messages(report, sweep.pending)
            queued.extend(
                QueuedMessage(
                    run_id=sweep.run_id,
                    recommendation_id=item.recommendation_id,
                    message=message,
                )
                for item, message in zip(sweep.pending, messages, strict=True)
            )
    return tuple(queued)


def post_queue(
    queued: tuple[QueuedMessage, ...], client: SlackClient, channel: str
) -> tuple[PostedMessage, ...]:
    """Send each message to one channel. Sends; decides nothing."""
    return tuple(client.post_message(channel, one.message) for one in queued)
