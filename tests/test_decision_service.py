"""The one decision path (ARCHITECTURE.md D133).

Every surface -- the CLI, the dashboard and the Slack webhook -- decides by calling
``approvals.service.decide``. These tests pin that it is the *only* path, in both
directions, and pin the three things it adds: a repeated interaction is one decision,
an already-decided item returns its existing decision unchanged, and a refusal is
recorded as a refusal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from costsentinel.approvals.identity import (
    ApproverIdentity,
    AuthorisationPolicy,
    Surface,
)
from costsentinel.approvals.ledger import DecisionLedger, DecisionOutcome, OutcomeKind
from costsentinel.approvals.service import decide
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.governance import ApprovalState, ApproverAssurance, Decision
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import NothingPendingError, approvals_graph, paused_sweep
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import MockAzureProvider

CLIENT = "ministry-of-transport"
RUN = "run-service"
ALICE = "slack:T0SYNTH01/U0ALICE01"

SRC = Path("src/costsentinel")


def _settings(tmp_path: Path, *, allowlist: str = "") -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        approver_allowlist=allowlist,
    )


def _scanned(settings: Settings) -> Settings:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        run_client_scan(
            client=CLIENT,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id=RUN,
            requested_by="test",
        )
    return settings


def _pending_ids(settings: Settings) -> list[str]:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        sweep = paused_sweep(approvals_graph(settings, saver), RUN)
    return [one.recommendation_id for one in sweep.pending] if sweep else []


def _state(settings: Settings) -> ScanState:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        snapshot = approvals_graph(settings, saver).get_state({"configurable": {"thread_id": RUN}})
    return ScanState.model_validate(snapshot.values)


def _alice() -> ApproverIdentity:
    return ApproverIdentity(
        name="alice",
        assurance=ApproverAssurance.VERIFIED,
        verified_by="slack-signature",
        principal=ALICE,
    )


def _local(
    settings: Settings, target: str, decision: Decision = Decision.APPROVED
) -> DecisionOutcome:
    return decide(
        settings,
        recommendation_id=target,
        decision=decision,
        reason="agreed in review",
        identity=ApproverIdentity.self_asserted("ops"),
        surface=Surface.CLI,
        local=True,
    )


# ---------------------------------------------------------------------------
# One path, asserted in both directions
# ---------------------------------------------------------------------------


def _calls(path: Path) -> set[str]:
    """Every function or method name called anywhere in one module."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_only_the_service_resumes_a_sweep_or_builds_a_decision_request() -> None:
    """The negative half: no surface keeps a private way to decide.

    `resume` and `DecisionRequest` are the two things a second decision path would
    need. Outside the service, only the module that defines `resume` and the gate that
    validates a request may mention them as calls.
    """
    allowed = {SRC / "approvals" / "service.py", SRC / "graph" / "approvals.py"}
    offending = sorted(
        str(path)
        for path in SRC.rglob("*.py")
        if path not in allowed and _calls(path) & {"resume", "DecisionRequest", "find_pending"}
    )
    assert not offending, f"a second decision path: {offending}"


@pytest.mark.parametrize(
    "surface",
    [
        SRC / "cli.py",
        SRC / "dashboard" / "app.py",
        pytest.param(
            SRC / "slack" / "webhook.py",
            marks=pytest.mark.xfail(
                reason="the Slack webhook is not built yet; closes #136", strict=True
            ),
        ),
    ],
    ids=lambda p: p.parent.name + "/" + p.name,
)
def test_every_surface_decides_through_the_service(surface: Path) -> None:
    """The positive half: each surface calls the shared function, by name."""
    text = surface.read_text(encoding="utf-8")
    assert "from costsentinel.approvals.service import decide" in text, surface


# ---------------------------------------------------------------------------
# A decision, and the answers that are not one
# ---------------------------------------------------------------------------


def test_a_local_decision_records_exactly_what_the_gate_records(tmp_path: Path) -> None:
    """The service adds no record of its own; the checkpoint holds the gate's."""
    settings = _scanned(_settings(tmp_path))
    target = _pending_ids(settings)[0]

    outcome = decide(
        settings,
        recommendation_id=target,
        decision=Decision.APPROVED,
        reason="agreed in review",
        identity=ApproverIdentity.self_asserted("ops"),
        surface=Surface.CLI,
        local=True,
    )

    assert outcome.kind is OutcomeKind.DECIDED
    (record,) = _state(settings).approval_records
    assert outcome.record == record
    assert record.state is ApprovalState.HANDED_OFF
    assert target not in _pending_ids(settings)
    assert outcome.remaining == len(_pending_ids(settings))


def test_the_same_interaction_twice_is_one_decision(tmp_path: Path) -> None:
    """Slack redelivers; the second delivery of a click is the same click."""
    settings = _scanned(_settings(tmp_path, allowlist=ALICE))
    target = _pending_ids(settings)[0]

    def once() -> DecisionOutcome:
        return decide(
            settings,
            recommendation_id=target,
            decision=Decision.APPROVED,
            reason="agreed in review",
            identity=_alice(),
            surface=Surface.SLACK,
            local=False,
            interaction_key="slack:T0SYNTH01:U0ALICE01:1730600000.000100",
        )

    first, second = once(), once()

    assert len(_state(settings).approval_records) == 1, "a redelivery decided twice"
    assert first.kind is second.kind is OutcomeKind.DECIDED
    assert second.record == first.record
    assert second.replayed and not first.replayed
    assert len(DecisionLedger(settings.decision_ledger_path).all()) == 1


def test_a_decision_on_an_already_decided_item_returns_it_unchanged(tmp_path: Path) -> None:
    """A second approver clicking a resolved item learns the answer; it does not change."""
    settings = _scanned(_settings(tmp_path, allowlist=ALICE))
    target = _pending_ids(settings)[0]
    first = decide(
        settings,
        recommendation_id=target,
        decision=Decision.REJECTED,
        reason="not this quarter",
        identity=ApproverIdentity.self_asserted("ops"),
        surface=Surface.CLI,
        local=True,
    )

    second = decide(
        settings,
        recommendation_id=target,
        decision=Decision.APPROVED,
        reason="changed my mind",
        identity=_alice(),
        surface=Surface.SLACK,
        local=False,
        interaction_key="slack:T0SYNTH01:U0ALICE01:1730600000.000200",
    )

    assert second.kind is OutcomeKind.ALREADY_DECIDED
    assert second.record == first.record
    assert second.record is not None and second.record.decision is Decision.REJECTED
    assert "nothing changed" in second.statement.lower()
    assert len(_state(settings).approval_records) == 1


def test_a_deferral_does_not_count_as_already_decided(tmp_path: Path) -> None:
    """A deferral ("not now") resolves nothing, so a later decision is a real one."""
    settings = _scanned(_settings(tmp_path))
    target = _pending_ids(settings)[0]

    _local(settings, target, Decision.DEFERRED)
    later = _local(settings, target, Decision.APPROVED)

    assert later.kind is OutcomeKind.DECIDED
    assert [r.decision for r in _state(settings).approval_records] == [
        Decision.DEFERRED,
        Decision.APPROVED,
    ]


def test_a_refusal_is_recorded_as_a_refusal_and_moves_nothing(tmp_path: Path) -> None:
    """An unauthorised approver leaves the sweep exactly where it was."""
    settings = _scanned(_settings(tmp_path))  # no allowlist: every verified identity refused
    before = _pending_ids(settings)

    outcome = decide(
        settings,
        recommendation_id=before[0],
        decision=Decision.APPROVED,
        reason="looks fine",
        identity=_alice(),
        surface=Surface.SLACK,
        local=False,
        interaction_key="slack:T0SYNTH01:U0ALICE01:1730600000.000300",
    )

    assert outcome.kind is OutcomeKind.REFUSED
    assert outcome.record is None
    assert outcome.event.event_type.value == "approval_refused"
    assert outcome.event.detail["decided"].startswith("false")
    assert _pending_ids(settings) == before
    assert not _state(settings).approval_records
    (stored,) = DecisionLedger(settings.decision_ledger_path).all()
    assert stored.kind is OutcomeKind.REFUSED


def test_a_self_asserted_name_from_off_the_machine_is_refused(tmp_path: Path) -> None:
    """The service enforces locality itself, whatever the calling surface believes."""
    settings = _scanned(_settings(tmp_path, allowlist=ALICE))
    before = _pending_ids(settings)

    outcome = decide(
        settings,
        recommendation_id=before[0],
        decision=Decision.APPROVED,
        reason="trust me",
        identity=ApproverIdentity.self_asserted("alice"),
        surface=Surface.DASHBOARD,
        local=False,
    )

    assert outcome.kind is OutcomeKind.REFUSED
    assert _pending_ids(settings) == before


def test_an_item_nobody_ever_saw_records_nothing(tmp_path: Path) -> None:
    """There is nothing a decision on a non-existent item could be recorded against."""
    settings = _scanned(_settings(tmp_path))

    with pytest.raises(NothingPendingError):
        _local(settings, "rec-does-not-exist")

    assert DecisionLedger(settings.decision_ledger_path).all() == ()


def test_the_refusal_check_uses_the_items_own_client(tmp_path: Path) -> None:
    """A client-scoped approver is judged against the client the item belongs to."""
    settings = _scanned(_settings(tmp_path))
    target = _pending_ids(settings)[0]

    outcome = decide(
        settings,
        recommendation_id=target,
        decision=Decision.APPROVED,
        reason="ok",
        identity=_alice(),
        surface=Surface.SLACK,
        local=False,
        policy=AuthorisationPolicy.parse(f"{ALICE}@northwind-energy"),
    )

    assert outcome.kind is OutcomeKind.REFUSED
    assert CLIENT in outcome.statement
