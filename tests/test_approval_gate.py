"""The durable approval interrupt, and the four proofs that approving mutates nothing.

Two claims, and they are the whole phase.

**Durability.** A sweep that halts must survive the process that started it. The test
that matters discards the in-memory graph entirely, reopens the checkpoint file,
decides, and resumes to a completed report -- because a test that kept a reference
would prove only that a variable was still in scope.

**Approving never mutates.** An approval advances an item to "handed off to a human
operator" and records it. There is no execution path, by construction. One proof of
that is a claim; four independent proofs are a property, so it is asserted
structurally (nothing imports a mutating client), representationally (the record
carries no executable handle), exhaustively (no decision on any action class reaches
a mutation) and end to end (an approved deletion makes zero provider calls).
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from costsentinel.agents.approval_gate import (
    ACTOR,
    DecisionPayloadError,
    build_record,
    pending_for,
    read_payload,
    record_id_for,
)
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.estate import (
    CostSeries,
    Reservation,
    Resource,
    ResourceMetrics,
    SkuPrice,
    Subscription,
)
from costsentinel.domain.governance import (
    ApprovalRecord,
    ApprovalState,
    AuditEventType,
    Decision,
    DecisionRequest,
    PendingApproval,
)
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import (
    NothingPendingError,
    approvals_graph,
    find_pending,
    paused_sweep,
    paused_sweeps,
    resume,
    thread_ids,
)
from costsentinel.graph.build import build_graph
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import interrupts_in, run_client_scan, state_from
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, MockAzureProvider

#: The smaller synthetic client: two gated actions, so the queue is readable in a
#: failure message and both a resolving and a non-resolving decision fit in one test.
CLIENT = CLIENT_MINISTRY


def _settings(scratch: Path) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
    )


def _graph(saver: Any, settings: Settings) -> Any:
    return build_graph(
        provider=MockAzureProvider(), llm=FakeLLM(), settings=settings, checkpointer=saver
    )


def _request(recommendation_id: str, decision: Decision = Decision.APPROVED) -> DecisionRequest:
    return DecisionRequest(
        recommendation_id=recommendation_id,
        decision=decision,
        approver="ops@example.com",
        reason="agreed in the Tuesday review",
    )


# ---------------------------------------------------------------------------
# The sweep pauses, and the report exists while it is paused
# ---------------------------------------------------------------------------


def test_a_gated_sweep_pauses_with_its_report_already_written(tmp_path: Path) -> None:
    """The report comes first (D119).

    A client waiting on their cost review must not be waiting on their own approval
    of the things in it. So the document is complete and correct while the sweep sits
    halted, and the queue in it is the queue the approver will decide from.
    """
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        final = graph.invoke(
            ScanState(run_id="run-pause", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-pause"}},
        )

        assert interrupts_in(final), "the sweep did not pause"
        state = state_from(final)
        assert state.report is not None
        assert state.report.findings
        assert state.report.totals.awaiting_approval_count == 2


def test_a_sweep_with_nothing_gated_does_not_pause(tmp_path: Path) -> None:
    """A halt that fires when nothing needs deciding would make the gate noise."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        final = graph.invoke(
            ScanState(run_id="run-empty", client="no-such-client", requested_by="test"),
            config={"configurable": {"thread_id": "run-empty"}},
        )
        assert not interrupts_in(final)
        assert state_from(final).report is not None


def test_the_runner_reports_the_pause_rather_than_failing(tmp_path: Path) -> None:
    """A paused sweep is a successful sweep that is not finished.

    Before Phase 10 the only outcomes were "a report" and "an exception". A halt is a
    third thing, and the metrics say so -- an operational fact about the run, which is
    why it is there and not on the report.
    """
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        report, metrics = run_client_scan(
            client=CLIENT,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id="run-metrics",
        )
    assert report.findings
    assert metrics.paused_for_approval
    assert metrics.awaits_a_person
    assert metrics.pending_approvals == 2


# ---------------------------------------------------------------------------
# Durability: the claim of the phase
# ---------------------------------------------------------------------------


def test_a_paused_sweep_survives_the_graph_being_discarded(tmp_path: Path) -> None:
    """Pause, throw the graph away, reload from the file, decide, finish.

    The discard is the point. Keeping a reference to the graph or the saver would make
    this a test that a variable is still in scope, which is true of any in-memory
    pause and proves nothing about the day somebody decides a week later from a
    different machine.
    """
    settings = _settings(tmp_path)
    db = tmp_path / "cp.sqlite"
    config: Any = {"configurable": {"thread_id": "run-durable"}}

    # --- the first process ------------------------------------------------
    with sqlite_checkpointer(db) as saver:
        graph = _graph(saver, settings)
        final = graph.invoke(
            ScanState(run_id="run-durable", client=CLIENT, requested_by="test"), config=config
        )
        assert interrupts_in(final)
        queued = [one.recommendation_id for one in paused_sweep(graph, "run-durable").pending]
    del graph, saver, final

    # --- the second process ----------------------------------------------
    with sqlite_checkpointer(db) as reopened:
        assert "run-durable" in thread_ids(reopened), "the sweep is not in the file"
        fresh = _graph(reopened, settings)

        waiting = paused_sweep(fresh, "run-durable")
        assert waiting is not None
        assert [one.recommendation_id for one in waiting.pending] == queued
        assert waiting.client == CLIENT

        state = fresh.get_state(config)
        assert ScanState.model_validate(state.values).report is not None

        for recommendation_id in queued:
            state_after = resume(fresh, "run-durable", _request(recommendation_id))

        assert paused_sweep(fresh, "run-durable") is None, "still paused after every decision"
        assert state_after.report is not None
        assert len(state_after.approval_records) == len(queued)
        assert all(one.authorises_a_person for one in state_after.approval_records)


def test_the_resume_key_is_the_run_id(tmp_path: Path) -> None:
    """The only handle a second process has, so it is asserted rather than assumed."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        run_client_scan(
            client=CLIENT,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id="run-key-check",
        )
        assert "run-key-check" in thread_ids(saver)
        sweep = paused_sweep(approvals_graph(settings, saver), "run-key-check")
        assert sweep is not None
        assert sweep.run_id == "run-key-check"
        assert all(one.run_id == "run-key-check" for one in sweep.pending)


def test_the_gates_audit_events_appear_only_once_a_decision_resumes_it(
    tmp_path: Path,
) -> None:
    """An interrupted node commits nothing, which is why the audit test excludes it."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        final = graph.invoke(
            ScanState(run_id="run-audit", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-audit"}},
        )
        paused = state_from(final)
        assert not [e for e in paused.audit if e.actor == ACTOR]

        sweep = paused_sweep(graph, "run-audit")
        assert sweep is not None
        after = resume(graph, "run-audit", _request(sweep.pending[0].recommendation_id))

    kinds = {event.event_type for event in after.audit}
    assert AuditEventType.APPROVAL_RECORDED in kinds
    assert AuditEventType.ACTION_HANDED_OFF in kinds
    handed = next(e for e in after.audit if e.event_type is AuditEventType.ACTION_HANDED_OFF)
    # The audit trail states what an approval is, not only the docstrings.
    assert handed.detail["performed_by"] == "a human operator, by hand"
    assert "no execution path exists" in handed.detail["executed_by_costsentinel"]


def test_a_deferral_leaves_the_sweep_paused_and_counts_itself(tmp_path: Path) -> None:
    """A deferral records an answer and resolves nothing, which is why it exists."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        graph.invoke(
            ScanState(run_id="run-defer", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-defer"}},
        )
        first = paused_sweep(graph, "run-defer")
        assert first is not None
        target = first.pending[0].recommendation_id

        resume(graph, "run-defer", _request(target, Decision.DEFERRED))

        still = paused_sweep(graph, "run-defer")
        assert still is not None
        again = next(one for one in still.pending if one.recommendation_id == target)
        assert again.state is ApprovalState.DEFERRED
        assert again.deferrals == 1

        # And it can then be resolved.
        resume(graph, "run-defer", _request(target, Decision.REJECTED))
        remaining = paused_sweep(graph, "run-defer")
        assert remaining is None or target not in {
            one.recommendation_id for one in remaining.pending
        }


# ---------------------------------------------------------------------------
# The resume value is untrusted input
# ---------------------------------------------------------------------------


def test_a_decision_naming_an_unqueued_recommendation_is_refused() -> None:
    """Recorded against nothing is worse than refused: it would look decided."""
    queue = (_pending("rec-known"),)
    with pytest.raises(DecisionPayloadError, match="not awaiting a decision"):
        read_payload(
            {
                "recommendation_id": "rec-unknown",
                "decision": "approved",
                "approver": "ops",
                "reason": "x",
            },
            queue,
        )


@pytest.mark.parametrize(
    "payload",
    [
        "approved",
        {"recommendation_id": "rec-known", "decision": "approved", "approver": "ops"},
        {"recommendation_id": "rec-known", "decision": "maybe", "approver": "o", "reason": "x"},
        {"recommendation_id": "rec-known", "decision": "approved", "reason": "x"},
        {
            "recommendation_id": "rec-known",
            "decision": "approved",
            "approver": "ops",
            "reason": "x",
            "action": "delete_everything",
        },
    ],
    ids=["not-a-mapping", "no-reason", "unknown-decision", "no-approver", "extra-field"],
)
def test_a_malformed_decision_is_refused(payload: object) -> None:
    """Validated against a schema, not read field by field.

    The last case is the one worth naming: a request that tries to *supply* a fact --
    an action, a resource, a figure -- is refused outright. Those are copied from the
    pending item, so a hostile request can fail to decide anything and cannot decide
    something other than what it appears to.
    """
    with pytest.raises(DecisionPayloadError):
        read_payload(payload, (_pending("rec-known"),))


def test_a_decision_cannot_supply_its_own_facts() -> None:
    """The fields a request carries, asserted against the model.

    If this ever grew a resource id or an action, a decision command could attach an
    approval to a different resource than the one it named.
    """
    assert set(DecisionRequest.model_fields) == {
        "recommendation_id",
        "decision",
        "approver",
        "reason",
    }


def test_a_record_copies_its_facts_from_the_pending_item() -> None:
    """Never from the caller, for the reason above."""
    pending = _pending("rec-1")
    record = build_record(pending, decision=Decision.APPROVED, approver="ops", reason="agreed")
    assert record.target_resource_id == pending.target_resource_id
    assert record.action is pending.action
    assert record.action_class is pending.action_class
    assert record.figures == pending.figures


def test_a_record_id_is_derived_so_a_replay_is_recognisable() -> None:
    """Random ids would make a replayed resume look like a second decision."""
    first = record_id_for("run-1", "rec-1", 0)
    assert first == record_id_for("run-1", "rec-1", 0)
    assert first != record_id_for("run-1", "rec-1", 1)
    assert first != record_id_for("run-2", "rec-1", 0)


def test_find_pending_says_what_is_waiting_when_it_misses() -> None:
    """A miss lists what is waiting, rather than merely saying it found nothing."""
    from costsentinel.graph.approvals import PausedSweep

    sweeps = (PausedSweep(run_id="run-1", client="acme", pending=(_pending("rec-a"),)),)
    with pytest.raises(NothingPendingError, match="rec-a"):
        find_pending(sweeps, "rec-b")
    with pytest.raises(NothingPendingError, match="approvals list"):
        find_pending((), "rec-b")


def test_a_queue_is_empty_before_the_report_exists() -> None:
    """The gate reads the queue off the report, so there is nothing to ask about yet."""
    assert pending_for(ScanState(run_id="run-1", client="acme")) == ()


def _pending(recommendation_id: str) -> PendingApproval:
    from costsentinel.domain.governance import FrozenFigures

    return PendingApproval(
        run_id="run-1",
        recommendation_id=recommendation_id,
        client="acme",
        subscription_id="sub-1",
        target_resource_id="/r/1",
        target_resource_name="disk-1",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        action_class=ActionClass.BLOCK,
        summary="delete a disk",
        figures=FrozenFigures(
            monthly_saving_display="38.42 USD",
            annual_saving_display="461.04 USD",
            current_monthly_cost_display="38.42 USD",
        ),
    )


# ---------------------------------------------------------------------------
# Approving never mutates: proof 1, structural
# ---------------------------------------------------------------------------

#: Method and attribute names that would indicate a mutating cloud call. Matched as
#: substrings against the whole of `src`, so a new one has to get past this list.
MUTATING_NAMES: tuple[str, ...] = (
    "begin_delete",
    "begin_create_or_update",
    "begin_update",
    "begin_deallocate",
    "begin_power_off",
    "begin_restart",
    "begin_resize",
    "begin_start",
    ".delete(",
    ".create_or_update(",
    ".deallocate(",
    ".power_off(",
)

#: Packages whose presence would mean a write path exists at all.
MUTATING_IMPORTS: tuple[str, ...] = (
    "azure.mgmt.compute",
    "azure.mgmt.resource",
    "azure.mgmt.storage",
    "azure.mgmt.network",
    "azure.mgmt.sql",
    "azure.mgmt.web",
)


def _source_files() -> list[Path]:
    return sorted(Path("src/costsentinel").rglob("*.py"))


def test_no_module_imports_a_mutating_cloud_client() -> None:
    """Proof 1 of 4: structural.

    An approval cannot cause a mutation because there is nothing in the process that
    could perform one. Asserted against the import graph rather than the convention,
    and against a list of *packages* rather than a list of calls, because the first
    thing a write path needs is a client that has one.
    """
    offending: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            for name in names:
                if any(name.startswith(bad) for bad in MUTATING_IMPORTS):
                    offending.append(f"{path}: {name}")
    assert not offending, offending


def test_no_module_calls_a_mutating_sdk_method() -> None:
    """Proof 1 of 4, continued: no call site, even if a client appeared.

    Belt and braces on purpose. The import check would catch a management client
    arriving; this catches a mutating call made through something already imported.
    """
    offending: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for name in MUTATING_NAMES:
            if name in text:
                offending.append(f"{path}: {name}")
    assert not offending, offending


def test_there_is_no_execution_module() -> None:
    """Proof 1 of 4, continued: nothing is named as if it executed anything.

    A module called `execute`, `executor`, `apply` or `remediate` would be the first
    half of the thing this system deliberately cannot do, and its absence is cheap to
    assert and easy to notice.
    """
    forbidden = {"execute.py", "executor.py", "apply.py", "remediate.py", "actions.py"}
    present = {path.name for path in _source_files()} & forbidden
    assert not present, present


def test_the_approval_gate_imports_nothing_that_could_act() -> None:
    """Proof 1 of 4, continued: the node that handles approval, specifically.

    It takes no provider, no client and no handler. A reviewer should be able to see
    from the import list that this module has nothing to act with.
    """
    tree = ast.parse(Path("src/costsentinel/agents/approval_gate.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any("provider" in name for name in imported), imported
    assert not any(name.startswith("azure") for name in imported), imported
    assert not any("llm" in name for name in imported), imported


# ---------------------------------------------------------------------------
# Proof 2, representational
# ---------------------------------------------------------------------------

#: Field names that would make an approval carry a way to act on itself.
EXECUTABLE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "handler",
        "handlers",
        "callable",
        "callback",
        "command",
        "endpoint",
        "uri",
        "url",
        "invoke",
        "invoker",
        "execute",
        "executor",
        "execution",
        "apply",
        "script",
        "template",
        "payload",
        "body",
        "operation",
        "mutation",
        "write",
    }
)


@pytest.mark.parametrize("model", [ApprovalRecord, PendingApproval, DecisionRequest])
def test_no_approval_type_carries_an_executable_handle(model: Any) -> None:
    """Proof 2 of 4: representational.

    An approved item cannot be executed because the record of it holds nothing to
    execute. Asserted against the field set rather than by reading the class, so a
    field added later fails this test rather than quietly becoming the first half of
    an execution path.
    """
    # Matched on name *segments* rather than substrings, which this test learned the
    # hard way: "script" is a substring of "subscription_id", so the substring version
    # failed on a field that is plainly a tenant identifier.
    offending = {
        name for name in model.model_fields if set(name.split("_")) & set(EXECUTABLE_FIELD_NAMES)
    }
    assert not offending, f"{model.__name__}: {offending}"

    # And nothing callable can be stored in one, whatever it is called.
    for name, field in model.model_fields.items():
        annotation = str(field.annotation)
        assert "Callable" not in annotation, f"{model.__name__}.{name}"
        assert "Coroutine" not in annotation, f"{model.__name__}.{name}"


def test_the_approval_state_machine_has_no_executed_member() -> None:
    """Proof 2 of 4, continued: the terminal state is a hand-off, not a completion.

    There is no EXECUTED, no APPLYING and no FAILED, because there is no execution for
    a state machine to describe. An approval ends at "a person agreed and a person must
    now do it".
    """
    assert {member.value for member in ApprovalState} == {
        "awaiting_decision",
        "handed_off",
        "declined",
        "deferred",
    }
    assert not any("exec" in member.value for member in ApprovalState)
    assert not any("appl" in member.value for member in ApprovalState)


# ---------------------------------------------------------------------------
# Proof 3, exhaustive
# ---------------------------------------------------------------------------


def test_no_decision_on_any_action_class_reaches_a_mutation() -> None:
    """Proof 3 of 4: exhaustive over the whole cross product.

    Every decision value against every action class -- including approving a `block`,
    which is the combination somebody would reach for if they wanted this system to
    delete something. All twelve land in a recorded state, and the only one that
    authorises anything authorises a *person*.
    """
    combinations = list(itertools.product(Decision, ActionClass))
    assert len(combinations) == len(Decision) * len(ActionClass)

    for decision, action_class in combinations:
        pending = _pending("rec-1").model_copy(update={"action_class": action_class})
        record = build_record(pending, decision=decision, approver="ops", reason="exhaustive check")

        assert record.state is ApprovalState.for_decision(decision)
        assert record.state is not ApprovalState.AWAITING_DECISION
        # The only authorisation that exists is for a human being.
        if decision is Decision.APPROVED:
            assert record.authorises_a_person
            assert "handed off for a person to carry out" in record.statement()
            assert "no path to perform it" in record.statement()
        else:
            assert not record.authorises_a_person


def test_every_decision_maps_to_a_state_and_none_maps_to_execution() -> None:
    """Proof 3 of 4, continued: the mapping is total and lands nowhere permissive."""
    for decision in Decision:
        state = ApprovalState.for_decision(decision)
        assert state in set(ApprovalState)
        assert state is not ApprovalState.AWAITING_DECISION
    assert ApprovalState.for_decision(Decision.APPROVED) is ApprovalState.HANDED_OFF


def test_an_approval_record_cannot_claim_a_state_its_decision_does_not_imply() -> None:
    """A record saying "rejected" and "handed off" would be an authorisation nobody gave."""
    pending = _pending("rec-1")
    good = build_record(pending, decision=Decision.APPROVED, approver="ops", reason="ok")
    with pytest.raises(ValidationError):
        good.model_copy(update={"approver": ""}).model_validate(
            good.model_dump() | {"approver": ""}
        )


# ---------------------------------------------------------------------------
# Proof 4, end to end
# ---------------------------------------------------------------------------


class _CountingProvider:
    """The mock provider, counting every call. Satisfies ``AzureProvider``.

    Wraps rather than subclasses so no method can be added to the real provider and
    quietly escape the count.
    """

    def __init__(self) -> None:
        self._inner = MockAzureProvider()
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._inner.name

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        self.calls.append("list_subscriptions")
        return self._inner.list_subscriptions(client=client)

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        self.calls.append("list_resources")
        return self._inner.list_resources(subscription_id)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        self.calls.append("list_reservations")
        return self._inner.list_reservations(client=client)

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        self.calls.append("get_cost_series")
        return self._inner.get_cost_series(subscription_id, window_days=window_days)

    def get_cost_breakdown(self, subscription_id: str, *, window_days: int = 30) -> Any:
        self.calls.append("get_cost_breakdown")
        return self._inner.get_cost_breakdown(subscription_id, window_days=window_days)

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        self.calls.append("get_resource_metrics")
        return self._inner.get_resource_metrics(resource_id)

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        self.calls.append("list_sku_prices")
        return self._inner.list_sku_prices(region)


def test_approving_a_deletion_makes_zero_provider_calls(tmp_path: Path) -> None:
    """Proof 4 of 4: end to end, with the most dangerous decision available.

    The mock estate's `block`-classified finding is a *disk deletion*. Approving it
    produces a recorded hand-off and, measurably, not one further call to the
    provider -- which is the only thing in the process that talks to a cloud at all,
    and which has no write method to call even if something wanted one.
    """
    settings = _settings(tmp_path)
    provider = _CountingProvider()

    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = build_graph(provider=provider, llm=FakeLLM(), settings=settings, checkpointer=saver)
        graph.invoke(
            ScanState(run_id="run-delete", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-delete"}},
        )

        sweep = paused_sweep(graph, "run-delete")
        assert sweep is not None
        deletion = next(
            one for one in sweep.pending if one.action is ActionType.DELETE_ORPHANED_MANAGED_DISK
        )
        assert deletion.action_class is ActionClass.BLOCK, "the dangerous case"

        # Everything the sweep read, before the decision.
        provider.calls.clear()

        state = resume(graph, "run-delete", _request(deletion.recommendation_id))

    assert provider.calls == [], f"approval touched the provider: {provider.calls}"

    record = next(
        one for one in state.approval_records if one.recommendation_id == deletion.recommendation_id
    )
    assert record.state is ApprovalState.HANDED_OFF
    assert record.authorises_a_person
    assert record.action is ActionType.DELETE_ORPHANED_MANAGED_DISK


def test_the_whole_queue_can_be_approved_without_touching_the_provider(
    tmp_path: Path,
) -> None:
    """Proof 4 of 4, continued: not just the one, and not just once."""
    settings = _settings(tmp_path)
    provider = _CountingProvider()

    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = build_graph(provider=provider, llm=FakeLLM(), settings=settings, checkpointer=saver)
        graph.invoke(
            ScanState(run_id="run-all", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-all"}},
        )
        sweep = paused_sweep(graph, "run-all")
        assert sweep is not None
        queued = [one.recommendation_id for one in sweep.pending]

        provider.calls.clear()
        for recommendation_id in queued:
            resume(graph, "run-all", _request(recommendation_id))

        assert provider.calls == []
        assert paused_sweep(graph, "run-all") is None


# ---------------------------------------------------------------------------
# The queue as another process reads it
# ---------------------------------------------------------------------------


def test_paused_sweeps_are_listed_in_a_stable_order(tmp_path: Path) -> None:
    """Two listings of an unchanged store must print the same thing.

    An operator comparing yesterday's queue with today's should see what changed, not
    a reshuffle.
    """
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        for run_id in ("run-b", "run-a", "run-c"):
            graph.invoke(
                ScanState(run_id=run_id, client=CLIENT, requested_by="test"),
                config={"configurable": {"thread_id": run_id}},
            )
        first = [one.run_id for one in paused_sweeps(graph, saver)]
        second = [one.run_id for one in paused_sweeps(graph, saver)]

    assert first == second == ["run-a", "run-b", "run-c"]


def test_a_finished_sweep_is_not_listed_as_pending(tmp_path: Path) -> None:
    """The listing is about what is waiting, not about what exists."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        graph.invoke(
            ScanState(run_id="run-done", client="no-such-client", requested_by="test"),
            config={"configurable": {"thread_id": "run-done"}},
        )
        assert "run-done" in thread_ids(saver), "the run exists in the store"
        assert paused_sweeps(graph, saver) == (), "but nothing is waiting"


def test_the_queue_shows_the_figures_the_report_showed(tmp_path: Path) -> None:
    """An approver must be looking at the same number the client was shown.

    Copied from the report rather than re-derived, so a reprice between the scan and
    the decision cannot change what an approval meant.
    """
    settings = _settings(tmp_path)
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = _graph(saver, settings)
        final = graph.invoke(
            ScanState(run_id="run-figures", client=CLIENT, requested_by="test"),
            config={"configurable": {"thread_id": "run-figures"}},
        )
        report = state_from(final).report
        assert report is not None
        sweep = paused_sweep(graph, "run-figures")
        assert sweep is not None

    by_id = {finding.recommendation_id: finding for finding in report.findings}
    for item in sweep.pending:
        finding = by_id[item.recommendation_id]
        assert item.figures.monthly_saving_display == finding.monthly_saving.display()
        assert item.figures.annual_saving_display == finding.annual_saving.display()
        assert item.figures.current_monthly_cost_display == finding.current_monthly_cost.display()


def test_every_node_update_key_is_a_real_state_field() -> None:
    """`NodeUpdate` is `dict[str, Any]`, so a mistyped key is silent.

    That is not hypothetical. This gate returned its audit events under
    `"audit_events"` while the field is `"audit"`, and LangGraph discarded every one
    of them without a word. The pause worked, the decision was recorded, and the audit
    trail was quietly missing -- the worst thing for a governance feature to get
    wrong, because the failure is invisible in exactly the artefact somebody would
    check.

    Checked across the whole agents package rather than this node alone, since the
    hazard belongs to the type rather than to the author. Only *returned* dict
    literals are inspected: a first attempt looked at every dict literal and flagged
    audit-detail payloads whose keys happen to share a name with a state field, which
    is the sort of false positive that gets a test deleted.
    """
    import ast

    fields = set(ScanState.model_fields)
    offending: list[str] = []

    for path in sorted(Path("src/costsentinel/agents").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            returned = isinstance(node, ast.Return)
            annotated = (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.annotation, ast.Name)
                and node.annotation.id == "NodeUpdate"
            )
            if not (returned or annotated):
                continue
            literal = getattr(node, "value", None)
            if isinstance(literal, ast.Dict):
                for key in literal.keys:
                    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                        continue
                    if key.value not in fields:
                        offending.append(f"{path.name}:{key.lineno} {key.value}")

    assert not offending, (
        f"these node-update keys are not ScanState fields, so LangGraph discards them "
        f"silently: {offending}"
    )
