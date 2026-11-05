"""A Slack approval never executes, asserted the four usual ways (ARCHITECTURE.md D135).

Phase 5 proved an approval cannot mutate a resource from four directions
(tests/test_approval_gate.py). Slack adds a surface that is reachable from the internet,
so the same four proofs are made again for it, against its own modules and types:

1. **Structural**: nothing in the Slack adapter or the approval service imports
   anything that could act, and the only remote calls the adapter can make are Slack's.
2. **Representational**: what a click carries holds no handle, address or command.
3. **Exhaustive**: every decision a button can send, on every action class, lands in a
   recorded state, and the only authorisation is for a person.
4. **End to end**: a signed Slack approval of the most dangerous item in the estate,
   a disk deletion, makes zero provider calls.
"""

from __future__ import annotations

import ast
import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from costsentinel.approvals.identity import ApproverIdentity
from costsentinel.approvals.ledger import DecisionOutcome
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.governance import ActionClass, ApprovalState, Decision
from costsentinel.domain.recommendations import ActionType
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph, paused_sweep
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, MockAzureProvider
from costsentinel.slack.blocks import ACTION_IDS
from costsentinel.slack.client import API_BASE, FakeSlackClient
from costsentinel.slack.interaction import SlackClick
from costsentinel.slack.signature import sign
from costsentinel.slack.webhook import INTERACTIONS_PATH, mount

SECRET = "synthetic-test-signing-secret-not-a-real-slack-secret"  # noqa: S105 -- labelled fake
NOW = 1_730_600_000
TEAM, USER = "T0SYNTH01", "U0ALICE01"
RUN = "run-slack-delete"
SRC = Path("src/costsentinel")
FIXTURE = Path("tests/fixtures/slack/block_actions.json")

#: The same lists the Phase 5 proofs use, kept identical on purpose.
MUTATING_IMPORT_PREFIXES = ("azure", "costsentinel.providers", "costsentinel.llm")
EXECUTABLE_FIELD_NAMES = frozenset(
    {
        "handler", "handlers", "callable", "callback", "command", "endpoint", "uri",
        "url", "invoke", "invoker", "execute", "executor", "execution", "apply",
        "script", "template", "payload", "body", "operation", "mutation", "write",
    }
)  # fmt: skip


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


# ---------------------------------------------------------------------------
# Proof 1: structural
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    sorted([*(SRC / "slack").rglob("*.py"), *(SRC / "approvals").rglob("*.py")]),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_the_slack_path_imports_nothing_that_could_act(path: Path) -> None:
    """Proof 1 of 4: no provider, no cloud SDK and no model client in the adapter or service.

    The service reaches the graph, which is built with a provider, but it imports none:
    the graph package does, and the Phase 5 proofs cover what the graph can do with one.
    """
    offending = {
        name
        for name in _imports(path)
        if any(name == bad or name.startswith(bad + ".") for bad in MUTATING_IMPORT_PREFIXES)
    }
    assert not offending, f"{path}: {offending}"


def test_the_adapter_can_call_only_slack() -> None:
    """Proof 1 of 4, continued: every outbound address is Slack's, fixed in the source.

    The Web API base is a constant, the two method names are literals, and the only
    other address is a ReplyTarget, which validates its host as hooks.slack.com.
    """
    assert API_BASE == "https://slack.com/api/"
    tree = ast.parse((SRC / "slack" / "client.py").read_text(encoding="utf-8"))
    methods = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_api"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert methods == {"chat.postMessage", "usergroups.users.list"}
    # No other module in the adapter opens a connection of its own.
    for path in (SRC / "slack").rglob("*.py"):
        if path.name != "client.py":
            assert "urllib.request" not in _imports(path), path


# ---------------------------------------------------------------------------
# Proof 2: representational
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [SlackClick, ApproverIdentity, DecisionOutcome])
def test_nothing_a_click_carries_is_an_executable_handle(model: Any) -> None:
    """Proof 2 of 4: the click, the identity and the outcome hold nothing to execute.

    SlackInteraction is deliberately not in this list: it pairs a click with the
    ReplyTarget Slack asked the answer to go to, which is an address by nature and is
    host-checked instead. It never reaches the decision path; only the click does.
    """
    offending = {
        name for name in model.model_fields if set(name.split("_")) & EXECUTABLE_FIELD_NAMES
    }
    assert not offending, f"{model.__name__}: {offending}"
    for name, field in model.model_fields.items():
        annotation = str(field.annotation)
        assert "Callable" not in annotation, f"{model.__name__}.{name}"
        assert "ReplyTarget" not in annotation, f"{model.__name__}.{name}"


def test_the_webhook_passes_the_click_and_not_the_reply_to_the_decision() -> None:
    """Proof 2 of 4, continued: `decide` is called with the click's fields only."""
    tree = ast.parse((SRC / "slack" / "webhook.py").read_text(encoding="utf-8"))
    (call,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "decide"
    ]
    sources = {ast.unparse(keyword.value) for keyword in call.keywords}
    assert not any("reply" in source for source in sources), sources


# ---------------------------------------------------------------------------
# Shared set-up for proofs 3 and 4
# ---------------------------------------------------------------------------


class _Counting:
    """The mock provider, counting every method call, whatever it is called.

    Wraps through ``__getattr__`` so a method added to the provider later is counted
    too rather than quietly escaping.
    """

    def __init__(self) -> None:
        self._inner = MockAzureProvider()
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return counted


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        approver_allowlist=f"slack:{TEAM}/{USER}",
        slack_enabled=True,
        slack_signing_secret=SecretStr(SECRET),
    )


def _body(target: str, decision: Decision, action_ts: str) -> bytes:
    payload = json.loads(
        FIXTURE.read_text(encoding="utf-8")
        .replace("{recommendation_id}", target)
        .replace("{action_id}", ACTION_IDS[decision])
        .replace("{action_ts}", action_ts)
        .replace("{user_id}", USER)
        .replace("{user_name}", "alice")
        .replace("{reason}", "exhaustive check")
    )
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


@pytest.fixture
def slack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, _Counting, Callable[[bytes], int]]:
    """A paused sweep, a signed-request poster, and a provider that counts every call."""
    settings = _settings(tmp_path)
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        run_client_scan(
            client=CLIENT_MINISTRY,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id=RUN,
            requested_by="test",
        )
    counting = _Counting()
    built: list[object] = []

    def provider_for(_settings: Settings) -> _Counting:
        built.append(counting)
        return counting

    # The graph the decision service resumes is built with whatever this returns.
    monkeypatch.setattr("costsentinel.providers.get_provider", provider_for)

    app = FastAPI()
    mount(app, settings, client=FakeSlackClient(), clock=lambda: NOW)
    http = TestClient(app)

    def post(body: bytes) -> int:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(NOW),
            "X-Slack-Signature": sign(SECRET, str(NOW), body),
        }
        status = http.post(INTERACTIONS_PATH, content=body, headers=headers).status_code
        # Without this, an unwired patch would also show zero calls and prove nothing.
        assert built, "the decision service did not build its graph with the counted provider"
        return status

    return settings, counting, post


def _state(settings: Settings) -> ScanState:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        graph = approvals_graph(settings, saver)
        snapshot = graph.get_state({"configurable": {"thread_id": RUN}})
    return ScanState.model_validate(snapshot.values)


def _queue(settings: Settings) -> list[tuple[str, ActionClass, ActionType]]:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        sweep = paused_sweep(approvals_graph(settings, saver), RUN)
    assert sweep is not None
    return [(one.recommendation_id, one.action_class, one.action) for one in sweep.pending]


# ---------------------------------------------------------------------------
# Proof 3: exhaustive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", [Decision.APPROVED, Decision.REJECTED])
def test_every_button_on_every_gated_class_lands_in_a_recorded_state(
    slack: tuple[Settings, _Counting, Callable[[bytes], int]], decision: Decision
) -> None:
    """Proof 3 of 4: each decision a click can send, on each class the queue holds.

    The synthetic queue holds one `block` item (the disk deletion) and one `review`
    item. Each is deferred and then decided, on a fresh sweep per resolving decision,
    so all three buttons meet both classes across the two parameters. Every click
    lands in a recorded state, and the provider is never called.
    """
    settings, counting, post = slack
    queue = _queue(settings)
    assert {cls for _, cls, _ in queue} == {ActionClass.BLOCK, ActionClass.REVIEW}

    counting.calls.clear()
    tick = 0
    for target, _, _ in queue:
        for step in (Decision.DEFERRED, decision):
            tick += 1
            assert post(_body(target, step, f"1730600000.{tick:06d}")) == 200

    assert counting.calls == [], f"a Slack decision touched the provider: {counting.calls}"
    records = _state(settings).approval_records
    for target, action_class, _ in queue:
        mine = [r for r in records if r.recommendation_id == target]
        assert [r.decision for r in mine] == [Decision.DEFERRED, decision], action_class
        for record in mine:
            assert record.action_class is action_class
            assert record.state is ApprovalState.for_decision(record.decision)
            assert record.authorises_a_person is (record.decision is Decision.APPROVED)
    assert not _queue_or_empty(settings), "every item resolved, so nothing waits"


def _queue_or_empty(settings: Settings) -> list[str]:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        sweep = paused_sweep(approvals_graph(settings, saver), RUN)
    return [one.recommendation_id for one in sweep.pending] if sweep else []


# ---------------------------------------------------------------------------
# Proof 4: end to end
# ---------------------------------------------------------------------------


def test_a_slack_approval_of_a_deletion_makes_zero_provider_calls(
    slack: tuple[Settings, _Counting, Callable[[bytes], int]],
) -> None:
    """Proof 4 of 4: the most dangerous click available, signed and authorised.

    The estate's `block`-classified disk deletion, approved from Slack by an
    allowlisted, verified user. The record is a hand-off to a person, and the
    provider -- the only thing that talks to a cloud -- is not called once.
    """
    settings, counting, post = slack
    deletion = next(
        rec for rec, _, action in _queue(settings)
        if action is ActionType.DELETE_ORPHANED_MANAGED_DISK
    )  # fmt: skip

    counting.calls.clear()
    assert post(_body(deletion, Decision.APPROVED, "1730600000.000001")) == 200

    assert counting.calls == [], f"approval touched the provider: {counting.calls}"
    record = next(r for r in _state(settings).approval_records if r.recommendation_id == deletion)
    assert record.state is ApprovalState.HANDED_OFF
    assert record.authorises_a_person
    assert record.action is ActionType.DELETE_ORPHANED_MANAGED_DISK
    assert record.action_class is ActionClass.BLOCK
    assert "no path to perform it" in record.statement()
