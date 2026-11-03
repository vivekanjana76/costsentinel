"""The dashboard's real authenticator: approver tokens (ARCHITECTURE.md D136).

A labelled fake secret and synthetic approvers only. The end-to-end tests post to a
dashboard that a TestClient reaches on a non-loopback address, so they exercise exactly
the case the loopback interlock exists for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from costsentinel.api.app import create_app
from costsentinel.approvals import token
from costsentinel.approvals.ledger import DecisionLedger, OutcomeKind
from costsentinel.cli import build_parser, run_approvers
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.dashboard.app import BASE_PATH, pending_queue
from costsentinel.dashboard.auth import (
    NullAuthenticator,
    TokenAuthenticator,
    UnsafeDecisionSurfaceError,
    authenticator_from_settings,
)
from costsentinel.domain.governance import ApproverAssurance
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, MockAzureProvider

#: Labelled so nobody mistakes it for a credential.
SECRET = "synthetic-approver-token-secret-for-tests-only"  # noqa: S105 -- labelled fake
NOW = 1_730_600_000
DAY = 86_400
RUN = "run-token"
FORM = {"content-type": "application/x-www-form-urlencoded"}
REMOTE = "http://10.0.0.5"


def _mint(name: str = "alice", *, now: int = NOW, lifetime: int = DAY) -> str:
    return token.mint(SECRET, name, now=now, lifetime_seconds=lifetime)


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_a_genuine_current_token_names_its_approver() -> None:
    """The one way through: this secret, this name, not yet expired."""
    assert token.verify(SECRET, _mint(), now=NOW + 60) == "alice"


@pytest.mark.parametrize(
    "forged",
    [
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),  # one digit of the signature
        lambda t: t.replace(".alice.", ".mallory."),  # someone else's name
        lambda t: t.replace(f".{NOW + DAY}.", f".{NOW + 90 * DAY}."),  # a later expiry
        lambda t: "cst2" + t[4:],  # another format
        lambda t: t.rsplit(".", 1)[0],  # no signature
        lambda _: "",
        lambda _: "not a token at all",
    ],
)
def test_an_altered_token_does_not_verify(forged: object) -> None:
    """Every field is covered by the signature, and a malformed one fails the same way."""
    altered = forged(_mint())  # type: ignore[operator]
    with pytest.raises(token.TokenError, match="not valid"):
        token.verify(SECRET, altered, now=NOW)


def test_a_token_signed_with_another_secret_does_not_verify() -> None:
    """Rotating the secret revokes every token, which is the only revocation there is."""
    other = token.mint("x" * 40, "alice", now=NOW, lifetime_seconds=DAY)
    with pytest.raises(token.TokenError):
        token.verify(SECRET, other, now=NOW)


def test_an_expired_token_does_not_verify() -> None:
    """A credential stops working when it says it does."""
    with pytest.raises(token.TokenError):
        token.verify(SECRET, _mint(), now=NOW + DAY + 1)


@pytest.mark.parametrize(
    ("secret", "name", "lifetime"),
    [
        ("short", "alice", DAY),
        (SECRET, "alice.admin", DAY),
        (SECRET, "a@b", DAY),
        (SECRET, "alice", 0),
        (SECRET, "alice", 91 * DAY),
    ],
)
def test_minting_refuses_what_it_could_not_safely_sign(
    secret: str, name: str, lifetime: int
) -> None:
    """A weak key, an ambiguous name or an unbounded lifetime never becomes a token."""
    with pytest.raises(token.TokenError):
        token.mint(secret, name, now=NOW, lifetime_seconds=lifetime)


def test_the_signature_is_compared_in_constant_time() -> None:
    """`==` on a signature leaks, through timing, how much of a forgery was right."""
    tree = ast.parse(Path("src/costsentinel/approvals/token.py").read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in calls


# ---------------------------------------------------------------------------
# Choosing an authenticator, and the interlock
# ---------------------------------------------------------------------------


def _settings(
    tmp_path: Path, *, secret: str | None = SECRET, allowlist: str = "token:alice"
) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        dashboard_enabled=True,
        dashboard_can_decide=True,
        bind_host="0.0.0.0",  # noqa: S104 -- the off-loopback case under test
        approver_allowlist=allowlist,
        approver_token_secret=SecretStr(secret) if secret is not None else None,
    )


def test_the_default_authenticator_verifies_nothing(tmp_path: Path) -> None:
    """Off by default: with no secret, the dashboard takes a typed, self-asserted name."""
    assert isinstance(
        authenticator_from_settings(_settings(tmp_path, secret=None)), NullAuthenticator
    )
    assert isinstance(authenticator_from_settings(_settings(tmp_path)), TokenAuthenticator)


def test_by_default_a_remote_decision_surface_still_refuses_to_start(tmp_path: Path) -> None:
    """The interlock is never relaxed by default, allowlist or not."""
    with pytest.raises(UnsafeDecisionSurfaceError, match="APPROVER_TOKEN_SECRET"):
        create_app(_settings(tmp_path, secret=None))


def test_a_verifying_authenticator_without_an_allowlist_refuses_to_start(tmp_path: Path) -> None:
    """Verification alone admits nobody, so serving it remotely would be a misconfiguration."""
    with pytest.raises(UnsafeDecisionSurfaceError, match="APPROVER_ALLOWLIST is empty"):
        create_app(_settings(tmp_path, allowlist=""))


def test_a_weak_secret_refuses_to_start(tmp_path: Path) -> None:
    """A guessable HMAC key would make every token forgeable."""
    with pytest.raises(token.TokenError):
        create_app(_settings(tmp_path, secret="too-short"))  # noqa: S106 -- the point


# ---------------------------------------------------------------------------
# End to end, off loopback
# ---------------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Settings, TestClient, str]:
    """A token-verifying dashboard reached from another address, with a sweep paused."""
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
    app = create_app(settings, TokenAuthenticator(SECRET, clock=lambda: NOW))
    return settings, TestClient(app, base_url=REMOTE), pending_queue(settings)[0].recommendation_id


def _records(settings: Settings) -> ScanState:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        snapshot = approvals_graph(settings, saver).get_state({"configurable": {"thread_id": RUN}})
    return ScanState.model_validate(snapshot.values)


def _post(client: TestClient, target: str, *, credential: str = "", bearer: str = "") -> int:
    body = f"recommendation_id={target}&decision=approved&reason=agreed+in+review"
    if credential:
        body += f"&approver_token={credential}"
    headers = {**FORM, **({"authorization": f"Bearer {bearer}"} if bearer else {})}
    return client.post(
        f"{BASE_PATH}/decide", content=body, headers=headers, follow_redirects=False
    ).status_code


def test_a_valid_token_decides_from_off_the_machine_as_a_verified_approver(
    remote: tuple[Settings, TestClient, str],
) -> None:
    """The point of #130: a real authenticator lifts the loopback-only rule, and no more."""
    settings, client, target = remote

    assert _post(client, target, credential=_mint()) == 303

    record = _records(settings).approval_records[-1]
    assert record.approver == "alice"
    assert record.approver_assurance is ApproverAssurance.VERIFIED
    assert record.verified_by == "approver-token"
    (outcome,) = DecisionLedger(settings.decision_ledger_path).all()
    assert outcome.kind is OutcomeKind.DECIDED
    assert outcome.event.detail["principal"] == "token:alice"


def test_a_bearer_header_works_as_well_as_the_form_field(
    remote: tuple[Settings, TestClient, str],
) -> None:
    """For a scripted caller or a fronting proxy, the header carries the token."""
    settings, client, target = remote
    assert _post(client, target, bearer=_mint()) == 303
    assert _records(settings).approval_records


@pytest.mark.parametrize(
    "credential",
    ["", "cst1.alice.1.deadbeef", "wrong"],
    ids=["none", "forged", "garbage"],
)
def test_a_missing_or_bad_token_is_401_and_records_nothing(
    remote: tuple[Settings, TestClient, str], credential: str
) -> None:
    """No verified author, no record: the same rule as an unsigned Slack request."""
    settings, client, target = remote
    assert _post(client, target, credential=credential) in {400, 401}
    assert not _records(settings).approval_records
    assert DecisionLedger(settings.decision_ledger_path).all() == ()


def test_an_expired_token_is_401(remote: tuple[Settings, TestClient, str]) -> None:
    """Minted a day before it was used, with an hour to live."""
    settings, client, target = remote
    stale = _mint(now=NOW - DAY, lifetime=3600)
    assert _post(client, target, credential=stale) == 401
    assert not _records(settings).approval_records


def test_a_genuine_token_off_the_allowlist_is_refused_and_recorded(
    remote: tuple[Settings, TestClient, str],
) -> None:
    """Who someone is and whether they may approve are separate questions, asked once each."""
    settings, client, target = remote
    assert _post(client, target, credential=_mint("mallory")) == 403
    assert not _records(settings).approval_records
    (outcome,) = DecisionLedger(settings.decision_ledger_path).all()
    assert outcome.kind is OutcomeKind.REFUSED


def test_the_page_asks_for_a_token_rather_than_a_name(
    remote: tuple[Settings, TestClient, str],
) -> None:
    """A verifying deployment does not offer the self-asserted name box at all."""
    _, client, _ = remote
    page = client.get(f"{BASE_PATH}/approvals").text
    assert 'name="approver_token"' in page and 'type="password"' in page
    assert 'name="approver"' not in page


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def test_the_cli_mints_a_token_the_dashboard_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What `approvers token` prints is what `TokenAuthenticator` verifies."""
    args = build_parser().parse_args(["approvers", "token", "alice", "--days", "1"])
    assert run_approvers(args, _settings(tmp_path), now=lambda: NOW) == 0
    printed = capsys.readouterr().out.strip()
    assert token.verify(SECRET, printed, now=NOW) == "alice"


def test_the_cli_will_not_mint_without_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing to sign with, so nothing is printed that could be mistaken for a token."""
    args = build_parser().parse_args(["approvers", "token", "alice"])
    assert run_approvers(args, _settings(tmp_path, secret=None)) == 2
    assert capsys.readouterr().out == ""


def test_the_cli_refuses_an_unbounded_lifetime(tmp_path: Path) -> None:
    """Ninety days is the ceiling; a longer-lived credential is refused, not clipped."""
    args = build_parser().parse_args(["approvers", "token", "alice", "--days", "365"])
    assert run_approvers(args, _settings(tmp_path)) == 2
