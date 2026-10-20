"""The A2A task lifecycle: submit, poll, collect the artefact.

A2A models a request as a *task* with a lifecycle rather than as a call with a return
value, which suits this system: a sweep takes seconds to minutes and a caller should
not be holding a socket open through one. Submit, get an id, poll, read the artefact.

Synchronous underneath, deliberately
------------------------------------

The work runs inside :meth:`TaskStore.submit`, so a task is ``COMPLETED`` or ``FAILED``
by the time the id comes back. The lifecycle is real and the concurrency is not, and
that is the honest order to build them in: the shape a consumer codes against is
settled, and nothing here pretends to an asynchrony that has not been tested. A worker
is a Phase 11 concern, and swapping one in changes this module and no consumer.

What a task cannot do
---------------------

Every skill reads. There is no task type that decides an approval, and none that
changes a resource -- the store maps three skill ids to three read functions and has no
fourth branch to add one to. A task that asks for something unadvertised fails with
the advertised list rather than being attempted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from costsentinel.a2a.card import SkillId
from costsentinel.config import Settings
from costsentinel.domain.common import Frozen, utc_now
from costsentinel.observability.logging import get_logger

_log = get_logger("a2a.tasks")


class TaskState(StrEnum):
    """Where a task has got to.

    No ``CANCELLED`` and no ``INPUT_REQUIRED``: neither is reachable while the work is
    synchronous, and declaring a state a consumer can never observe would be a lie
    about the surface.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskError(RuntimeError):
    """A task could not be created, found, or run."""


class UnknownSkillError(TaskError):
    """A request named a skill this agent does not advertise."""


class UnknownTaskError(TaskError):
    """A request named a task this store has never issued."""


class Artifact(Frozen):
    """What a completed task returns.

    The payload is the report or the queue as JSON -- the same structures the CLI and
    the HTTP API return, so there is one representation of a finding rather than an
    A2A-shaped copy that could drift from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    media_type: str = "application/json"
    payload: Mapping[str, Any]


class Task(Frozen):
    """One A2A task and its outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    skill: SkillId
    state: TaskState
    submitted_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    artifact: Artifact | None = None
    error: str = Field(
        default="",
        description="Why it failed, when it did. Empty otherwise, never a stray message.",
    )

    @property
    def is_finished(self) -> bool:
        """Whether a consumer should stop polling."""
        return self.state in {TaskState.COMPLETED, TaskState.FAILED}


class TaskStore:
    """Runs and remembers A2A tasks for one process.

    In-memory, which is the right scope for what this is: a task's *artefact* is
    derivable again by asking for it again, and the durable thing in this system -- a
    paused sweep and its decisions -- lives in the checkpoint, not here. Persisting
    task metadata would add a second store with nothing in it worth recovering.
    """

    def __init__(self, settings: Settings) -> None:
        """Hold the configuration the read skills need, and nothing else."""
        self._settings = settings
        self._tasks: dict[str, Task] = {}

    @property
    def count(self) -> int:
        """How many tasks this store has issued."""
        return len(self._tasks)

    def submit(self, skill: SkillId | str, params: Mapping[str, Any] | None = None) -> Task:
        """Run one read-only skill and return the finished task.

        Raises:
            UnknownSkillError: If the skill is not advertised on the card. The message
                lists what is, because a consumer that guessed wrong should be able to
                correct itself without fetching the card again.
        """
        try:
            resolved = SkillId(skill)
        except ValueError as exc:
            advertised = ", ".join(sorted(member.value for member in SkillId))
            msg = f"{skill!r} is not an advertised skill; this agent offers {advertised}"
            raise UnknownSkillError(msg) from exc

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        arguments = dict(params or {})
        _log.info(
            "a2a task submitted",
            extra={"task_id": task_id, "skill": resolved.value, "client": arguments.get("client")},
        )

        try:
            artifact = self._run(resolved, arguments)
        except Exception as exc:
            failed = Task(
                task_id=task_id,
                skill=resolved,
                state=TaskState.FAILED,
                completed_at=utc_now(),
                error=str(exc),
            )
            self._tasks[task_id] = failed
            _log.warning(
                "a2a task failed",
                extra={"task_id": task_id, "skill": resolved.value, "error": str(exc)},
            )
            return failed

        done = Task(
            task_id=task_id,
            skill=resolved,
            state=TaskState.COMPLETED,
            completed_at=utc_now(),
            artifact=artifact,
        )
        self._tasks[task_id] = done
        return done

    def status(self, task_id: str) -> Task:
        """One task by id.

        Raises:
            UnknownTaskError: If this store never issued it.
        """
        task = self._tasks.get(task_id)
        if task is None:
            msg = f"no task {task_id!r} was issued by this agent"
            raise UnknownTaskError(msg)
        return task

    def artifact(self, task_id: str) -> Artifact:
        """One task's artefact.

        Raises:
            UnknownTaskError: If the task is unknown.
            TaskError: If it has not completed, which is a different thing from being
                unknown and leads a consumer somewhere different.
        """
        task = self.status(task_id)
        if task.artifact is None:
            msg = (
                f"task {task_id!r} is {task.state.value} and has produced no artefact"
                f"{f': {task.error}' if task.error else ''}"
            )
            raise TaskError(msg)
        return task.artifact

    # -- the read skills ---------------------------------------------------

    def _run(self, skill: SkillId, params: Mapping[str, Any]) -> Artifact:
        """Dispatch to one of three read functions. There is no fourth branch."""
        match skill:
            case SkillId.SCAN:
                return self._scan(params)
            case SkillId.FINDINGS:
                return self._findings(params)
            case SkillId.APPROVAL_QUEUE:
                return self._queue(params)

    def _scan(self, params: Mapping[str, Any]) -> Artifact:
        """Run a read-only sweep and return the report."""
        from costsentinel.graph.runner import run_scan  # noqa: PLC0415 -- cycle

        client = params.get("client")
        result = run_scan(
            client=str(client) if client else None,
            settings=self._settings,
            requested_by=f"a2a:{AGENT_REQUESTER}",
        )
        return Artifact(
            name="cost-review",
            payload={
                "reports": [report.model_dump(mode="json") for report in result.reports],
                "clients": list(result.clients),
                "awaiting_approval": sum(len(report.approval_queue) for report in result.reports),
                "note": (
                    "Read-only. Nothing in this report has been applied, and nothing "
                    "in it can be: CostSentinel has no execution path."
                ),
            },
        )

    def _findings(self, params: Mapping[str, Any]) -> Artifact:
        """The findings from the most recent sweep, without running a new one.

        Read off the checkpoint rather than recomputed, so "the current findings" means
        what was last reported rather than what a fresh sweep would say today -- those
        are different answers and a consumer asking this one wants the first.
        """
        from costsentinel.domain.state import ScanState  # noqa: PLC0415
        from costsentinel.graph.approvals import (  # noqa: PLC0415 -- cycle
            approvals_graph,
            thread_ids,
        )
        from costsentinel.graph.checkpointer import checkpointer_for  # noqa: PLC0415

        client = params.get("client")
        reports: list[dict[str, Any]] = []
        with checkpointer_for(self._settings) as saver:
            graph = approvals_graph(self._settings, saver)
            for run_id in thread_ids(saver):
                snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
                if not snapshot.values:
                    continue
                state = ScanState.model_validate(snapshot.values)
                if state.report is None:
                    continue
                if client and state.client != str(client):
                    continue
                reports.append(state.report.model_dump(mode="json"))

        return Artifact(
            name="current-findings",
            payload={
                "reports": reports,
                "sweeps": len(reports),
                "note": "Stored results. No cloud call and no model call was made.",
            },
        )

    def _queue(self, params: Mapping[str, Any]) -> Artifact:
        """The pending-approval queue, which this surface can read and cannot decide."""
        from costsentinel.graph.approvals import (  # noqa: PLC0415 -- cycle
            approvals_graph,
            paused_sweeps,
        )
        from costsentinel.graph.checkpointer import checkpointer_for  # noqa: PLC0415

        client = params.get("client")
        with checkpointer_for(self._settings) as saver:
            graph = approvals_graph(self._settings, saver)
            sweeps = [
                sweep
                for sweep in paused_sweeps(graph, saver)
                if not client or sweep.client == str(client)
            ]

        return Artifact(
            name="approval-queue",
            payload={
                "sweeps": [
                    {
                        "run_id": sweep.run_id,
                        "client": sweep.client,
                        "pending": [item.model_dump(mode="json") for item in sweep.pending],
                    }
                    for sweep in sweeps
                ],
                "awaiting": sum(len(sweep.pending) for sweep in sweeps),
                "note": (
                    "A decision is recorded by a person, through `costsentinel "
                    "approvals decide`. This surface reports what is waiting and has "
                    "no path to decide it or to perform it."
                ),
            },
        )


#: Recorded as the requester on a sweep an external agent asked for, so the audit trail
#: distinguishes it from one a person started. An external caller is still a caller.
AGENT_REQUESTER = "external-agent"
