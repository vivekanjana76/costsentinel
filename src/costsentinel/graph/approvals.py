# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false
# The third module that touches LangGraph directly, and it gets the same treatment as
# the other two: the generics on CompiledStateGraph and Command are only partially
# typed, so strict mode reports *their* members as unknown. Disabled for this file
# only rather than relaxed across src, so everything CostSentinel owns stays strict.
"""Finding and resuming a paused sweep from another process.

The approval gate halts a sweep at a checkpoint and the process exits. Days later
somebody has to be able to ask "what is waiting on me?" and answer it, from a terminal
that shares nothing with the original run but a file on disk. That is what this module
does, and it is the half of durability that is easy to forget: a pause nobody can find
is not durable, it is just slow.

The resume key
--------------

A sweep's checkpoint thread id **is its run id**. That has been true since Phase 1
because :func:`~costsentinel.graph.runner.run_client_scan` passes the run id straight
through as ``thread_id``, and from Phase 10 it is load-bearing rather than incidental:
it is the only handle a second process has. Documented here, asserted by a test, and
printed in the log line the gate emits when it pauses.

Why a graph has to be built to read a queue
-------------------------------------------

Enumerating thread ids needs only the checkpointer, but reading *what a sweep is
waiting on* needs ``graph.get_state``, which needs a compiled graph. So listing the
queue builds one -- with the same provider and model the sweep would use, neither of
which it calls. A listing makes no provider request and no model call; it reads a file
and inspects a pending interrupt.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import Field

from costsentinel.config import Settings
from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import DecisionRequest, PendingApproval
from costsentinel.domain.state import ScanState
from costsentinel.observability.logging import get_logger

_log = get_logger("graph.approvals")


class ApprovalsError(RuntimeError):
    """A paused sweep could not be found, read or resumed."""


class NothingPendingError(ApprovalsError):
    """No sweep is waiting on a decision about this recommendation."""


class PausedSweep(Frozen):
    """One sweep halted at the approval gate, as another process sees it."""

    run_id: str = Field(description="The sweep, and the key a second process resumes it with.")
    client: str
    pending: tuple[PendingApproval, ...] = Field(min_length=1)

    @property
    def summary(self) -> str:
        """One line for a listing."""
        return f"{self.run_id}  {self.client}  {len(self.pending)} awaiting a decision"


def approvals_graph(
    settings: Settings, saver: BaseCheckpointSaver[str]
) -> CompiledStateGraph[ScanState, None, ScanState, ScanState]:
    """A graph bound to the checkpointer, for reading and resuming paused sweeps.

    Lives here rather than in the CLI so the LangGraph types stay in the three modules
    that already accept their partial typing, and so a second caller -- the A2A surface
    reads the same queue -- shares one definition of "a graph for the approval store".

    Built with the provider and model a sweep would use and calling neither: reading a
    queue inspects a pending interrupt in a file, and resuming one runs the nodes that
    were already going to run.
    """
    from costsentinel.graph.build import build_graph  # noqa: PLC0415 -- cycle
    from costsentinel.llm import get_llm  # noqa: PLC0415 -- cycle
    from costsentinel.providers import get_provider  # noqa: PLC0415 -- cycle

    return build_graph(
        provider=get_provider(settings),
        llm=get_llm(settings),
        settings=settings,
        checkpointer=saver,
    )


def _config(run_id: str) -> RunnableConfig:
    """The LangGraph config that identifies one sweep.

    One function rather than a dict literal at four call sites, because getting this
    key wrong means silently reading or resuming a different run.
    """
    return cast("RunnableConfig", {"configurable": {"thread_id": run_id}})


def thread_ids(saver: BaseCheckpointSaver[str]) -> tuple[str, ...]:
    """Every sweep the checkpointer has heard of, newest first, deduplicated.

    Includes finished sweeps: whether one is *paused* is a question for
    :func:`paused_sweep`, which has to look at the pending tasks to answer it.
    """
    seen: list[str] = []
    for tup in saver.list(None):
        configurable = cast("dict[str, Any]", tup.config).get("configurable", {})
        run_id = cast("dict[str, Any]", configurable).get("thread_id")
        if isinstance(run_id, str) and run_id not in seen:
            seen.append(run_id)
    return tuple(seen)


def paused_sweep(
    graph: CompiledStateGraph[ScanState, None, ScanState, ScanState], run_id: str
) -> PausedSweep | None:
    """What one sweep is waiting on, or ``None`` if it is not waiting.

    ``None`` covers three different situations on purpose -- the sweep finished, it
    never existed, or it stopped for some other reason -- because none of them is a
    pending approval and the caller's next move is the same for all three.
    """
    snapshot = graph.get_state(_config(run_id))
    if not snapshot.interrupts:
        return None

    pending: list[PendingApproval] = []
    for one in snapshot.interrupts:
        value = one.value
        if not isinstance(value, dict):
            continue
        queue = cast("dict[str, Any]", value).get("pending")
        if not isinstance(queue, list | tuple):
            continue
        for item in cast("Sequence[Any]", queue):
            pending.append(PendingApproval.model_validate(item))

    if not pending:
        return None

    state = ScanState.model_validate(snapshot.values)
    return PausedSweep(run_id=run_id, client=state.client, pending=tuple(pending))


def paused_sweeps(
    graph: CompiledStateGraph[ScanState, None, ScanState, ScanState],
    saver: BaseCheckpointSaver[str],
) -> tuple[PausedSweep, ...]:
    """Every sweep currently waiting on a human, in a stable order.

    Sorted by run id rather than left in checkpoint order, so two listings of an
    unchanged store print the same thing. An operator comparing yesterday's list with
    today's should see what changed, not a reshuffle.
    """
    found: list[PausedSweep] = []
    for run_id in thread_ids(saver):
        sweep = paused_sweep(graph, run_id)
        if sweep is not None:
            found.append(sweep)
    return tuple(sorted(found, key=lambda one: one.run_id))


def find_pending(
    sweeps: Sequence[PausedSweep], recommendation_id: str
) -> tuple[PausedSweep, PendingApproval]:
    """Locate one recommendation across every paused sweep.

    An operator decides about a *recommendation*, not about a sweep -- the id is what
    the report gave them, and which run produced it is bookkeeping they should not have
    to carry.

    Raises:
        NothingPendingError: If no paused sweep is waiting on it. The message lists
            what is waiting, because "not found" with no alternatives is the least
            useful thing a command can say.
    """
    for sweep in sweeps:
        for item in sweep.pending:
            if item.recommendation_id == recommendation_id:
                return sweep, item

    waiting = sorted(item.recommendation_id for sweep in sweeps for item in sweep.pending)
    detail = ", ".join(waiting) if waiting else "no sweep is paused for approval"
    msg = (
        f"nothing is awaiting a decision on {recommendation_id!r}. Waiting on: {detail}."
        f" Run `costsentinel approvals list` to see the queue."
    )
    raise NothingPendingError(msg)


def resume(
    graph: CompiledStateGraph[ScanState, None, ScanState, ScanState],
    run_id: str,
    request: DecisionRequest,
) -> ScanState:
    """Record one decision and let the sweep continue from where it stopped.

    The decision is passed as the interrupt's resume value and validated *inside* the
    graph, by the gate, against the queue that actually exists. This function does not
    pre-approve it: a check here and a check there would be two places for the rules
    to drift, and the one that matters is the one holding the state.

    Returns:
        The state after the sweep ran as far as it can. It may be paused again -- a
        sweep with three gated actions needs three decisions, and each one moves it
        along by one.
    """
    from costsentinel.graph.runner import state_from  # noqa: PLC0415 -- cycle

    _log.info(
        "resuming a paused sweep",
        extra={
            "run_id": run_id,
            "recommendation_id": request.recommendation_id,
            "decision": request.decision.value,
            "approver": request.approver,
        },
    )
    # Annotated because `Command` is generic in what it may route to, and this one
    # routes nowhere -- it carries a resume value for the interrupt that is waiting.
    command: Command[Any] = Command(resume=request.model_dump(mode="json"))
    final: object = graph.invoke(command, config=_config(run_id))
    return state_from(final)
