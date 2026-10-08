# pyright: reportUnknownMemberType=false
# LangGraph's StateGraph/CompiledStateGraph generics are only partially typed, so
# pyright's strict mode reports *its* members as unknown. The rule is disabled for
# this file only -- the two modules that touch LangGraph directly -- rather than
# relaxed across src, so everything CostSentinel owns stays strictly checked.
"""Running a sweep: one checkpointed graph run per client.

A sweep with no client specified runs each client's estate as its own graph run with
its own ``run_id`` and its own LangGraph thread. That is not an implementation
convenience -- it is how ARCHITECTURE.md D12 is enforced. A client is the
multi-tenancy boundary, so there is no code path in which one run, one checkpoint
thread or one report spans two clients.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

from langgraph.checkpoint.base import BaseCheckpointSaver

from costsentinel.config import Settings, get_settings
from costsentinel.domain.report import ClientReport, ScanResult
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import build_graph
from costsentinel.graph.checkpointer import checkpointer_for
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm import get_llm
from costsentinel.llm.base import LLM
from costsentinel.observability.logging import configure_logging, get_logger
from costsentinel.providers import get_provider
from costsentinel.providers.base import AzureProvider

_log = get_logger("graph.runner")


class ScanFailedError(RuntimeError):
    """A graph run completed without producing a report."""


def new_run_id() -> str:
    """A fresh run id, which doubles as the LangGraph thread id."""
    return f"run-{uuid.uuid4().hex[:12]}"


def clients_in_scope(provider: AzureProvider, client: str | None = None) -> tuple[str, ...]:
    """The clients a sweep should cover.

    Args:
        provider: Read-only estate access.
        client: One client, or ``None`` for every client the provider can see.

    Returns:
        Client identifiers, deduplicated and sorted so a sweep is reproducible.
    """
    if client is not None:
        return (client,)
    return tuple(sorted({sub.client for sub in provider.list_subscriptions()}))


def run_client_scan(
    *,
    client: str,
    provider: AzureProvider,
    llm: LLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str] | None,
    policy: PolicyStore | None = None,
    run_id: str | None = None,
    requested_by: str = "system",
) -> ClientReport:
    """Run one client's scan to completion and return its report.

    Raises:
        ScanFailedError: If the graph finished without a report, which means a node
            failed to populate state rather than that no waste was found -- a report
            with zero findings is a perfectly good result.
    """
    thread_id = run_id or new_run_id()
    graph = build_graph(
        provider=provider,
        llm=llm,
        settings=settings,
        policy=policy,
        checkpointer=checkpointer,
    )
    initial = ScanState(run_id=thread_id, client=client, requested_by=requested_by)

    _log.info(
        "scan started",
        extra={
            "run_id": thread_id,
            "client": client,
            "mode": settings.mode.value,
            "llm_backend": settings.llm_backend.value,
            "provider": provider.name,
            "model": llm.name,
        },
    )

    final = graph.invoke(initial, config={"configurable": {"thread_id": thread_id}})
    state = ScanState.model_validate(final)

    if state.report is None:
        msg = (
            f"The scan for client {client!r} (run {thread_id}) completed without a "
            f"report. Errors recorded: {state.errors or 'none'}."
        )
        raise ScanFailedError(msg)

    return state.report


def run_scan(
    *,
    client: str | None = None,
    settings: Settings | None = None,
    provider: AzureProvider | None = None,
    llm: LLM | None = None,
    policy: PolicyStore | None = None,
    requested_by: str = "system",
    run_id_factory: Callable[[], str] = new_run_id,
) -> ScanResult:
    """Run a sweep and return one report per client in scope.

    Every argument is injectable so the API, the CLI and the tests share one code
    path. With nothing passed, configuration comes from the environment -- which
    defaults to the mock provider and the fake model, needing no credentials.

    Args:
        client: One client, or ``None`` to sweep every client separately.
        settings: Resolved configuration. Defaults to the process settings.
        provider: Read-only estate access. Defaults to the configured provider.
        llm: Structured reasoning. Defaults to the configured backend.
        policy: The policy store. Defaults to the built-in one.
        requested_by: Recorded on state and in the audit trail.
        run_id_factory: Supplies each run's id. Injectable so a test can pin it.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)
    active_provider = provider or get_provider(resolved)
    active_llm = llm or get_llm(resolved)

    targets = clients_in_scope(active_provider, client)
    reports: list[ClientReport] = []

    with checkpointer_for(resolved) as checkpointer:
        for target in targets:
            reports.append(
                run_client_scan(
                    client=target,
                    provider=active_provider,
                    llm=active_llm,
                    settings=resolved,
                    checkpointer=checkpointer,
                    policy=policy,
                    run_id=run_id_factory(),
                    requested_by=requested_by,
                )
            )

    _log.info(
        "sweep complete",
        extra={
            "clients": len(reports),
            "findings": sum(r.totals.findings_count for r in reports),
            "mode": resolved.mode.value,
        },
    )
    return ScanResult(reports=tuple(reports))


def load_checkpointed_state(
    *,
    run_id: str,
    provider: AzureProvider,
    llm: LLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> ScanState | None:
    """Read a run's persisted state back out of the checkpointer.

    This is what makes resumability demonstrable rather than merely configured: a
    test reads a completed run's state back from SQLite in a fresh connection, and
    Phase 3 uses the same path to resume a run that halted for approval.

    Returns:
        The persisted state, or ``None`` if the thread has no checkpoint.
    """
    graph = build_graph(provider=provider, llm=llm, settings=settings, checkpointer=checkpointer)
    snapshot = graph.get_state(config={"configurable": {"thread_id": run_id}})
    if not snapshot.values:
        return None
    return ScanState.model_validate(snapshot.values)


def checkpointed_nodes(
    *,
    run_id: str,
    provider: AzureProvider,
    llm: LLM,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
) -> Sequence[str]:
    """The nodes a run checkpointed a resume point for, in execution order.

    LangGraph writes a checkpoint at every node boundary, and each snapshot's
    ``next`` names the node that would run were the thread resumed from it. So the
    ``next`` values across a run's history are exactly the set of nodes the run could
    have been resumed *into* -- which is the property a mid-sweep crash depends on,
    and the reason this reads ``next`` rather than a write log.

    The synthetic ``__start__`` boundary is excluded, and the terminal snapshot has an
    empty ``next``, so a completed run yields one entry per real node.
    """
    graph = build_graph(provider=provider, llm=llm, settings=settings, checkpointer=checkpointer)
    history = list(graph.get_state_history(config={"configurable": {"thread_id": run_id}}))
    return tuple(
        node
        for snapshot in reversed(history)
        for node in snapshot.next
        if not node.startswith("__")
    )
