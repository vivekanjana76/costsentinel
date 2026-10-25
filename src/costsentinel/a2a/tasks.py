"""The A2A task lifecycle: submit, poll, collect the artefact, or cancel.

A2A models a request as a *task* with a lifecycle, not as a call with a return value.
That suits this system: a sweep takes seconds to minutes, and a caller should not hold a
socket open through one.

The lifecycle
-------------

::

    submit ──> SUBMITTED ──run──> WORKING ──> COMPLETED
                   │                     └──> FAILED
                   └──cancel──> CANCELED

:meth:`TaskStore.submit` records the task and returns it **submitted**. The work runs in
:meth:`TaskStore.run`, which the server schedules after the response has been sent.
Phase 10 ran the work inside ``submit``, so a task was already finished when its id came
back, and cancellation had nothing to act on (D130).

Cancellation is honest about what it can stop. A **submitted** task is cancelled before
its read starts, so the read never runs. A **working** one is not cancelable: a sweep
has no safe midpoint to abandon, and claiming otherwise would describe a capability
that does not exist. A finished task is not cancelable either. A repeated cancel of a
cancelled task returns it unchanged, because spec 3.3.1 makes cancel idempotent.

What a task cannot do
---------------------

Every skill reads. No task type decides an approval and none changes a resource. The
store maps three skill ids to three read functions and has no fourth branch to add one
to. Cancel changes the *task*, an in-memory record this store owns, and never a sweep,
a checkpoint or a decision.

Typed artefacts
---------------

One Pydantic type per skill, discriminated on ``name``. Phase 10 returned
``payload: Mapping[str, Any]``, which is an untyped dict crossing the most external
boundary in the system (CLAUDE.md golden rule 6). A consumer can now validate what it
received against :data:`ARTIFACT_ADAPTER`, the same contract the server serialises
with.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter

from costsentinel.a2a.card import SkillId
from costsentinel.config import Settings
from costsentinel.domain.common import Frozen, utc_now
from costsentinel.domain.governance import PendingApproval
from costsentinel.domain.report import ClientReport
from costsentinel.observability.logging import get_logger

_log = get_logger("a2a.tasks")

#: Recorded as the requester on a sweep an external agent asked for, so the audit trail
#: distinguishes it from one a person started. An external caller is still a caller.
AGENT_REQUESTER = "external-agent"

READ_ONLY_NOTE = (
    "Read-only. Nothing in this report has been applied, and nothing in it can be: "
    "CostSentinel has no execution path."
)
STORED_NOTE = "Stored results. No cloud call and no model call was made."
QUEUE_NOTE = (
    "A decision is recorded by a person, through `costsentinel approvals decide`. This "
    "surface reports what is waiting and has no path to decide it or to perform it."
)


class TaskState(StrEnum):
    """Where a task has got to. Every member is reachable.

    No ``INPUT_REQUIRED``, ``AUTH_REQUIRED`` or ``REJECTED``. No skill asks for more
    input, nothing authenticates a caller yet (Phase 12b), and a request for an
    unadvertised skill is refused before any task exists. Declaring a state a consumer
    can never observe would misdescribe the surface.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """Whether the task will never change again."""
        return self in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}


class TaskError(RuntimeError):
    """A task could not be created, found, or run."""


class UnknownSkillError(TaskError):
    """A request named a skill this agent does not advertise."""


class UnknownTaskError(TaskError):
    """A request named a task this store has never issued."""


class TaskNotCancelableError(TaskError):
    """A cancel arrived for a task that is running or already finished."""


class _Artifact(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid")

    media_type: Literal["application/json"] = "application/json"
    note: str = Field(min_length=1)


class ScanArtifact(_Artifact):
    """``report.scan``: the report for each client a fresh read-only sweep covered."""

    name: Literal["cost-review"] = "cost-review"
    reports: tuple[ClientReport, ...]
    clients: tuple[str, ...]
    awaiting_approval: int = Field(ge=0)


class FindingsArtifact(_Artifact):
    """``report.findings``: stored reports, read off the checkpoint."""

    name: Literal["current-findings"] = "current-findings"
    reports: tuple[ClientReport, ...]
    sweeps: int = Field(ge=0)


class QueuedSweep(Frozen):
    """One paused sweep and what it is waiting on."""

    run_id: str
    client: str
    pending: tuple[PendingApproval, ...]


class QueueArtifact(_Artifact):
    """``approvals.queue``: what a person has yet to decide. Readable, not decidable."""

    name: Literal["approval-queue"] = "approval-queue"
    sweeps: tuple[QueuedSweep, ...]
    awaiting: int = Field(ge=0)


Artifact = Annotated[ScanArtifact | FindingsArtifact | QueueArtifact, Field(discriminator="name")]

#: What a consumer validates a returned artefact against.
ARTIFACT_ADAPTER: TypeAdapter[ScanArtifact | FindingsArtifact | QueueArtifact] = TypeAdapter(
    Artifact
)


class Task(Frozen):
    """One A2A task and where it has got to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    skill: SkillId
    state: TaskState
    client: str | None = None
    submitted_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    artifact: Artifact | None = None
    error: str = Field(
        default="",
        description="Why it failed, when it did. Empty otherwise, never a stray message.",
    )

    @property
    def is_finished(self) -> bool:
        """Whether a consumer should stop polling."""
        return self.state.is_terminal


class TaskStore:
    """Runs and remembers A2A tasks for one process.

    In-memory, which is the right scope. A task's artefact is derivable again by asking
    again, and the durable thing in this system, a paused sweep and its decisions,
    lives in the checkpoint. Persisting task metadata would add a second store with
    nothing in it worth recovering.

    Transitions happen under one lock. The server runs a task on a worker thread while
    a cancel may arrive on another, and "submitted" must not be read by both.
    """

    def __init__(self, settings: Settings) -> None:
        """Hold the configuration the read skills need, and nothing else."""
        self._settings = settings
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        """How many tasks this store has issued."""
        return len(self._tasks)

    def submit(self, skill: SkillId | str, params: Mapping[str, Any] | None = None) -> Task:
        """Record one read-only task, **submitted**. Nothing runs yet.

        Raises:
            UnknownSkillError: If the skill is not advertised on the card. The message
                lists what is, so a consumer that guessed wrong can correct itself
                without fetching the card again. No task is created.
        """
        try:
            resolved = SkillId(skill)
        except ValueError as exc:
            advertised = ", ".join(sorted(member.value for member in SkillId))
            msg = f"{skill!r} is not an advertised skill; this agent offers {advertised}"
            raise UnknownSkillError(msg) from exc

        client = (params or {}).get("client")
        task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            skill=resolved,
            state=TaskState.SUBMITTED,
            client=str(client) if client else None,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        _log.info(
            "a2a task submitted",
            extra={"task_id": task.task_id, "skill": resolved.value, "client": task.client},
        )
        return task

    def run(self, task_id: str) -> Task:
        """Run a submitted task to completion or failure, and return it.

        A task that is no longer submitted is returned as it is. That covers one
        cancelled before its turn, and one already run by a duplicate schedule.
        Running only from submitted is what makes cancel mean "this read never ran".
        """
        with self._lock:
            task = self._get(task_id)
            if task.state is not TaskState.SUBMITTED:
                return task
            task = self._put(task, state=TaskState.WORKING)

        try:
            artifact = self._dispatch(task.skill, task.client)
        except Exception as exc:
            _log.warning(
                "a2a task failed",
                extra={"task_id": task_id, "skill": task.skill.value, "error": str(exc)},
            )
            with self._lock:
                return self._put(task, state=TaskState.FAILED, error=str(exc))

        with self._lock:
            return self._put(task, state=TaskState.COMPLETED, artifact=artifact)

    def execute(self, skill: SkillId | str, params: Mapping[str, Any] | None = None) -> Task:
        """Submit and run in one call, for an in-process caller that will not poll."""
        return self.run(self.submit(skill, params).task_id)

    def cancel(self, task_id: str) -> Task:
        """Cancel a submitted task before its read starts.

        Raises:
            UnknownTaskError: If this store never issued it.
            TaskNotCancelableError: If it is working or finished. A cancelled task is
                returned unchanged instead, because a repeated cancel has the same effect
                as the first (spec 3.3.1).
        """
        with self._lock:
            task = self._get(task_id)
            if task.state is TaskState.CANCELED:
                return task
            if task.state is not TaskState.SUBMITTED:
                msg = (
                    f"task {task_id!r} is {task.state.value} and cannot be cancelled; only a "
                    f"submitted task can, before its read starts"
                )
                raise TaskNotCancelableError(msg)
            cancelled = self._put(task, state=TaskState.CANCELED)
        _log.info("a2a task cancelled", extra={"task_id": task_id, "skill": task.skill.value})
        return cancelled

    def status(self, task_id: str) -> Task:
        """One task by id.

        Raises:
            UnknownTaskError: If this store never issued it.
        """
        with self._lock:
            return self._get(task_id)

    def artifact(self, task_id: str) -> ScanArtifact | FindingsArtifact | QueueArtifact:
        """One completed task's artefact.

        Raises:
            UnknownTaskError: If the task is unknown.
            TaskError: If it has not completed. That is a different thing from being
                unknown, and it leads a consumer somewhere different.
        """
        task = self.status(task_id)
        if task.artifact is None:
            msg = (
                f"task {task_id!r} is {task.state.value} and has produced no artefact"
                f"{f': {task.error}' if task.error else ''}"
            )
            raise TaskError(msg)
        return task.artifact

    # -- bookkeeping, always under the lock ---------------------------------

    def _get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            msg = f"no task {task_id!r} was issued by this agent"
            raise UnknownTaskError(msg)
        return task

    def _put(self, task: Task, **changes: Any) -> Task:
        updated = task.model_copy(update={**changes, "updated_at": utc_now()})
        self._tasks[task.task_id] = updated
        return updated

    # -- the read skills ---------------------------------------------------

    def _dispatch(
        self, skill: SkillId, client: str | None
    ) -> ScanArtifact | FindingsArtifact | QueueArtifact:
        """Dispatch to one of three read functions. There is no fourth branch."""
        match skill:
            case SkillId.SCAN:
                return self._scan(client)
            case SkillId.FINDINGS:
                return self._findings(client)
            case SkillId.APPROVAL_QUEUE:
                return self._queue(client)

    def _scan(self, client: str | None) -> ScanArtifact:
        """Run a read-only sweep and return the report."""
        from costsentinel.graph.runner import run_scan  # noqa: PLC0415 -- cycle

        result = run_scan(
            client=client, settings=self._settings, requested_by=f"a2a:{AGENT_REQUESTER}"
        )
        return ScanArtifact(
            reports=tuple(result.reports),
            clients=tuple(result.clients),
            awaiting_approval=sum(len(report.approval_queue) for report in result.reports),
            note=READ_ONLY_NOTE,
        )

    def _findings(self, client: str | None) -> FindingsArtifact:
        """The findings from the most recent sweeps, without running a new one.

        Read off the checkpoint, not recomputed. "The current findings" means what was
        last reported, not what a fresh sweep would say today. Those are different
        answers, and a consumer asking this one wants the first.
        """
        from costsentinel.domain.state import ScanState  # noqa: PLC0415
        from costsentinel.graph.approvals import (  # noqa: PLC0415 -- cycle
            approvals_graph,
            thread_ids,
        )
        from costsentinel.graph.checkpointer import checkpointer_for  # noqa: PLC0415

        reports: list[ClientReport] = []
        with checkpointer_for(self._settings) as saver:
            graph = approvals_graph(self._settings, saver)
            for run_id in thread_ids(saver):
                snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
                if not snapshot.values:
                    continue
                state = ScanState.model_validate(snapshot.values)
                if state.report is None or (client and state.client != client):
                    continue
                reports.append(state.report)

        return FindingsArtifact(reports=tuple(reports), sweeps=len(reports), note=STORED_NOTE)

    def _queue(self, client: str | None) -> QueueArtifact:
        """The pending-approval queue, which this surface can read and cannot decide."""
        from costsentinel.graph.approvals import (  # noqa: PLC0415 -- cycle
            approvals_graph,
            paused_sweeps,
        )
        from costsentinel.graph.checkpointer import checkpointer_for  # noqa: PLC0415

        with checkpointer_for(self._settings) as saver:
            graph = approvals_graph(self._settings, saver)
            sweeps = tuple(
                QueuedSweep(run_id=sweep.run_id, client=sweep.client, pending=tuple(sweep.pending))
                for sweep in paused_sweeps(graph, saver)
                if not client or sweep.client == client
            )

        return QueueArtifact(
            sweeps=sweeps, awaiting=sum(len(sweep.pending) for sweep in sweeps), note=QUEUE_NOTE
        )
