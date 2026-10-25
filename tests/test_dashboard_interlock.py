"""The decision-surface interlock, and the honesty of a typed approver name.

Phase 11 shipped a dashboard that could record an approval and said, in prose, "do not
expose this". Phase 12 replaces the sentence with a refusal (ARCHITECTURE.md D126):

- with decide on, a non-loopback bind and no verifying authenticator, the app is not
  built and the server does not start;
- a request that reaches a decision surface on a non-loopback socket is refused even
  when the declared bind said loopback, because a server can be bound more widely than
  its settings say;
- loopback with decide on, and display-only on any bind, serve exactly as before;
- a decision taken with no authenticator is recorded as self-asserted and unverified
  (D127).

The load-bearing test is `test_the_unsafe_combination_refuses_to_start`.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from costsentinel import cli
from costsentinel.api.app import create_app
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings, get_settings
from costsentinel.dashboard.app import BASE_PATH, mount, pending_queue
from costsentinel.dashboard.auth import (
    ApproverIdentity,
    NullAuthenticator,
    UnsafeDecisionSurfaceError,
    check_decision_surface,
    is_loopback,
)
from costsentinel.dashboard.html import SELF_ASSERTED_NOTE
from costsentinel.domain.governance import ApproverAssurance
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, MockAzureProvider

FORM = {"content-type": "application/x-www-form-urlencoded"}

#: The wildcard bind. The address under test, not one this suite listens on.
EVERYWHERE = "0.0.0.0"  # noqa: S104

#: Addresses another machine could reach, or that cannot be shown not to be.
REACHABLE = (EVERYWHERE, "::", "192.168.1.10", "10.0.0.5", "costsentinel.example.com", "")

#: Addresses only this machine can reach.
LOOPBACK = ("127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]")


def _settings(
    scratch: Path, *, bind: str = "127.0.0.1", decide: bool = True, enabled: bool = True
) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
        dashboard_enabled=enabled,
        dashboard_can_decide=decide,
        bind_host=bind,
    )


def _scanned(settings: Settings) -> Settings:
    """Put one paused sweep in the checkpoint, so there is something to decide."""
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        run_client_scan(
            client=CLIENT_MINISTRY,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id="run-interlock",
            requested_by="test",
        )
    return settings


def _decide_body(recommendation_id: str, approver: str = "ops@example.com") -> str:
    return (
        f"recommendation_id={recommendation_id}&decision=approved"
        f"&reason=agreed+in+review&approver={approver}"
    )


def _latest_record_state(settings: Settings) -> ScanState:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        graph = approvals_graph(settings, saver)
        snapshot = graph.get_state({"configurable": {"thread_id": "run-interlock"}})
    return ScanState.model_validate(snapshot.values)


class _VerifyingAuthenticator:
    """A stand-in for the Phase 10b authenticator: trusts a header the test sets.

    Exists to prove the seam. The interlock lifts on `verifies`, and the identity on
    the record comes from the authenticator, not from the typed name.
    """

    name = "test-sso"
    verifies = True

    def identify(self, request: Request, asserted: str) -> ApproverIdentity:
        _ = asserted
        return ApproverIdentity(
            name=request.headers.get("x-test-user", "nobody"),
            assurance=ApproverAssurance.VERIFIED,
            verified_by=self.name,
        )


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bind", REACHABLE)
def test_the_unsafe_combination_refuses_to_start(tmp_path: Path, bind: str) -> None:
    """Decide on, reachable bind, no authenticator: the app is never built.

    The same posture as the gate refusing to execute. A warning in a log is a
    configuration somebody can miss; an app that does not exist cannot be served.
    """
    settings = _settings(tmp_path, bind=bind)
    with pytest.raises(UnsafeDecisionSurfaceError) as refused:
        create_app(settings)

    message = str(refused.value)
    assert message.startswith("refusing to start")
    # Actionable: it names the three settings and both safe ways out.
    for needle in (
        "DASHBOARD_CAN_DECIDE=true",
        f"BIND_HOST={bind!r}",
        "no authenticator",
        "BIND_HOST=127.0.0.1",
        "DASHBOARD_CAN_DECIDE=false",
        "costsentinel approvals decide",
    ):
        assert needle in message, needle


def test_serve_refuses_and_never_reaches_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command an operator actually runs exits non-zero before uvicorn is called."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_CAN_DECIDE", "true")
    monkeypatch.setenv("BIND_HOST", "127.0.0.1")
    get_settings.cache_clear()

    def _must_not_serve(*_: object, **__: object) -> None:
        pytest.fail("uvicorn.run was reached on an unsafe configuration")

    monkeypatch.setattr("uvicorn.run", _must_not_serve)
    try:
        # The override is what makes it unsafe, so this also proves the override is the
        # address the interlock judges rather than BIND_HOST.
        assert cli.main(["serve", "--host", EVERYWHERE]) == 2
    finally:
        get_settings.cache_clear()
    assert "refusing to start" in capsys.readouterr().err


def test_serve_binds_the_address_the_interlock_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source for the bind, so the check and the socket cannot disagree."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_CAN_DECIDE", "true")
    monkeypatch.delenv("BIND_HOST", raising=False)
    get_settings.cache_clear()

    served: dict[str, object] = {}

    def _record(app: object, *, host: str, port: int) -> None:
        served.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", _record)
    try:
        assert cli.main(["serve", "--port", "8123"]) == 0
    finally:
        get_settings.cache_clear()
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8123
    assert isinstance(served["app"], FastAPI)


def test_importing_the_app_module_builds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal happens when the server asks for the app, not when anybody imports.

    An import-time build would make an unsafe environment break every importer,
    including `serve --host 127.0.0.1`, before the override could be read.
    """
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_CAN_DECIDE", "true")
    monkeypatch.setenv("BIND_HOST", EVERYWHERE)
    get_settings.cache_clear()
    try:
        module = importlib.import_module("costsentinel.api.app")  # no reload: nothing is built
        with pytest.raises(UnsafeDecisionSurfaceError):
            _ = module.app  # what `uvicorn costsentinel.api.app:app` resolves
        with pytest.raises(AttributeError):
            _ = module.no_such_thing
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# What still serves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bind", LOOPBACK)
def test_a_loopback_bind_with_decide_on_serves_and_decides(tmp_path: Path, bind: str) -> None:
    """The Phase 11 deployment -- one operator on their own machine -- is unchanged."""
    settings = _scanned(_settings(tmp_path, bind=bind))
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")

    page = client.get(f"{BASE_PATH}/approvals")
    assert page.status_code == 200
    assert "Approve" in page.text

    target = pending_queue(settings)[0].recommendation_id
    response = client.post(
        f"{BASE_PATH}/decide", content=_decide_body(target), headers=FORM, follow_redirects=False
    )
    assert response.status_code == 303
    assert target not in {one.recommendation_id for one in pending_queue(settings)}


@pytest.mark.parametrize("bind", REACHABLE)
def test_display_only_serves_on_any_bind(tmp_path: Path, bind: str) -> None:
    """Eyes on the estate without hands on the queue needs no authenticator."""
    settings = _scanned(_settings(tmp_path, bind=bind, decide=False))
    client = TestClient(create_app(settings))

    page = client.get(f"{BASE_PATH}/approvals")
    assert page.status_code == 200
    assert "Approve" not in page.text
    assert client.get(BASE_PATH).status_code == 200

    target = pending_queue(settings)[0].recommendation_id
    assert (
        client.post(f"{BASE_PATH}/decide", content=_decide_body(target), headers=FORM).status_code
        == 403
    )


def test_a_disabled_dashboard_is_never_judged(tmp_path: Path) -> None:
    """Nothing is served, so there is no surface to refuse."""
    create_app(_settings(tmp_path, bind=EVERYWHERE, enabled=False))


# ---------------------------------------------------------------------------
# Per request: the socket a decision arrived on
# ---------------------------------------------------------------------------


def test_a_decision_arriving_off_loopback_is_refused_even_when_the_bind_said_loopback(
    tmp_path: Path,
) -> None:
    """`uvicorn --host 0.0.0.0` without BIND_HOST: the app never saw the flag.

    So the route judges the socket the request was accepted on. The page offers no
    form and the POST is refused, and nothing is recorded.
    """
    settings = _scanned(_settings(tmp_path, bind="127.0.0.1"))
    app = FastAPI()
    assert mount(app, settings)
    remote = TestClient(app, base_url="http://10.0.0.5")

    page = remote.get(f"{BASE_PATH}/approvals")
    assert page.status_code == 200
    assert "Approve" not in page.text

    before = {one.recommendation_id for one in pending_queue(settings)}
    target = next(iter(before))
    refused = remote.post(f"{BASE_PATH}/decide", content=_decide_body(target), headers=FORM)
    assert refused.status_code == 403
    assert "loopback" in refused.json()["detail"]
    assert {one.recommendation_id for one in pending_queue(settings)} == before


def test_a_verifying_authenticator_lifts_the_restriction_and_owns_the_identity(
    tmp_path: Path,
) -> None:
    """The seam: an authenticator that verifies may serve off loopback.

    And the name on the record is the one it established, not the one typed into the
    form -- a verifying authenticator that simply echoed the typed name would verify
    nothing.
    """
    settings = _scanned(_settings(tmp_path, bind=EVERYWHERE))
    client = TestClient(create_app(settings, _VerifyingAuthenticator()), base_url="http://10.0.0.5")

    page = client.get(f"{BASE_PATH}/approvals")
    assert "Approve" in page.text
    assert SELF_ASSERTED_NOTE not in page.text

    target = pending_queue(settings)[0].recommendation_id
    response = client.post(
        f"{BASE_PATH}/decide",
        content=_decide_body(target, approver="someone-else"),
        headers={**FORM, "x-test-user": "alice@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    record = _latest_record_state(settings).approval_records[-1]
    assert record.approver == "alice@example.com"
    assert record.approver_assurance is ApproverAssurance.VERIFIED
    assert record.verified_by == "test-sso"


# ---------------------------------------------------------------------------
# A typed name is recorded as what it is (D127)
# ---------------------------------------------------------------------------


def test_a_dashboard_decision_without_an_authenticator_is_recorded_as_unverified(
    tmp_path: Path,
) -> None:
    """The record, and the page that took the decision, say the name was only typed."""
    settings = _scanned(_settings(tmp_path))
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    assert SELF_ASSERTED_NOTE in client.get(f"{BASE_PATH}/approvals").text

    target = pending_queue(settings)[0].recommendation_id
    client.post(f"{BASE_PATH}/decide", content=_decide_body(target), headers=FORM)

    state = _latest_record_state(settings)
    record = state.approval_records[-1]
    assert record.approver == "ops@example.com"
    assert record.approver_assurance is ApproverAssurance.SELF_ASSERTED
    assert record.verified_by is None
    assert "(self-asserted, unverified)" in record.statement()
    recorded = [e for e in state.audit if e.subject == target and "approver_assurance" in e.detail]
    assert recorded
    assert all(e.detail["approver_verified"] == "false" for e in recorded)


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_addresses_are_recognised(host: str) -> None:
    """Every spelling of this-machine-only, so a safe bind is never refused."""
    assert is_loopback(host)


@pytest.mark.parametrize("host", (*REACHABLE, None, "not an address", "127.0.0.1.example.com"))
def test_everything_else_fails_closed(host: str | None) -> None:
    """An address that cannot be shown to be loopback is treated as reachable."""
    assert not is_loopback(host)


def test_the_null_authenticator_verifies_nothing_and_says_so() -> None:
    """It labels the typed name for what it is. It does not refuse to record it."""
    auth = NullAuthenticator()
    assert auth.verifies is False
    request = Request({"type": "http", "headers": [], "server": ("127.0.0.1", 8000)})
    identity = auth.identify(request, "ops@example.com")
    assert identity.name == "ops@example.com"
    assert identity.assurance is ApproverAssurance.SELF_ASSERTED
    assert identity.verified_by is None


def test_the_interlock_reads_only_verifies_off_the_authenticator(tmp_path: Path) -> None:
    """The restriction lifts for an authenticator that verifies, and for no other reason."""
    settings = _settings(tmp_path, bind=EVERYWHERE)
    with pytest.raises(UnsafeDecisionSurfaceError):
        check_decision_surface(settings, NullAuthenticator())
    check_decision_surface(settings, _VerifyingAuthenticator())
