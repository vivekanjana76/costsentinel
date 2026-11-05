"""The Slack interaction webhook (ARCHITECTURE.md D133, D134, D135).

Driven end to end over HTTP with FastAPI's test client: a real paused sweep of the
synthetic estate, a synthetic payload (tests/fixtures/slack/, hand-written, never
recorded), signed with a labelled fake secret, and a fake Slack client. No socket is
opened and no Slack workspace exists.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from costsentinel.api.app import create_app
from costsentinel.approvals.ledger import DecisionLedger, OutcomeKind
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.governance import ApprovalState, ApproverAssurance, Decision
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph, paused_sweep
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import MockAzureProvider
from costsentinel.slack.blocks import ACTION_IDS
from costsentinel.slack.client import FakeSlackClient, SlackError
from costsentinel.slack.interaction import (
    VERIFIED_BY,
    InteractionError,
    parse_interaction,
)
from costsentinel.slack.signature import DEFAULT_WINDOW_SECONDS, sign
from costsentinel.slack.webhook import INTERACTIONS_PATH, UnsafeSlackSurfaceError, mount

#: Labelled so nobody mistakes it for a credential. It signs synthetic requests only.
SECRET = "synthetic-test-signing-secret-not-a-real-slack-secret"  # noqa: S105 -- labelled fake
NOW = 1_730_600_000
CLIENT = "ministry-of-transport"
RUN = "run-slack-webhook"
TEAM = "T0SYNTH01"
ALICE = "U0ALICE01"
MALLORY = "U0MALLORY1"
BOB = "U0BOB0001"
GROUP = "S0APPROVE1"
FIXTURE = Path("tests/fixtures/slack/block_actions.json")


def _settings(tmp_path: Path, *, allowlist: str = f"slack:{TEAM}/{ALICE}") -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        approver_allowlist=allowlist,
        slack_enabled=True,
        slack_signing_secret=SecretStr(SECRET),
    )


def _scan(settings: Settings) -> None:
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


def _pending(settings: Settings) -> list[str]:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        sweep = paused_sweep(approvals_graph(settings, saver), RUN)
    return [one.recommendation_id for one in sweep.pending] if sweep else []


def _state(settings: Settings) -> ScanState:
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        snapshot = approvals_graph(settings, saver).get_state({"configurable": {"thread_id": RUN}})
    return ScanState.model_validate(snapshot.values)


def _ledger(settings: Settings) -> tuple[OutcomeKind, ...]:
    return tuple(one.kind for one in DecisionLedger(settings.decision_ledger_path).all())


def _body(
    target: str,
    *,
    decision: Decision = Decision.APPROVED,
    user: str = ALICE,
    reason: str = "confirmed idle with the workload owner",
    action_ts: str = "1730600000.000100",
    response_url: str | None = None,
    action_id: str | None = None,
) -> bytes:
    payload = json.loads(
        FIXTURE.read_text(encoding="utf-8")
        .replace("{recommendation_id}", target)
        .replace("{action_id}", action_id or ACTION_IDS[decision])
        .replace("{action_ts}", action_ts)
        .replace("{user_id}", user)
        .replace("{user_name}", user.lower())
        .replace("{reason}", reason)
    )
    if response_url is not None:
        payload["response_url"] = response_url
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


class Harness:
    """A scanned sweep, a mounted webhook and a fake Slack, standing at NOW."""

    def __init__(self, settings: Settings, slack: FakeSlackClient | None = None) -> None:
        """Mount the webhook on a bare app, so only this route is under test."""
        self.settings = settings
        self.slack = slack or FakeSlackClient()
        app = FastAPI()
        mount(app, settings, client=self.slack, clock=lambda: NOW)
        self.http = TestClient(app)

    def post(
        self,
        body: bytes,
        *,
        timestamp: str | None = str(NOW),
        signature: str | None = "sign",
    ) -> int:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if timestamp is not None:
            headers["X-Slack-Request-Timestamp"] = timestamp
        if signature == "sign" and timestamp is not None:
            signature = sign(SECRET, timestamp, body)
        if signature is not None:
            headers["X-Slack-Signature"] = signature
        return self.http.post(INTERACTIONS_PATH, content=body, headers=headers).status_code

    @property
    def said(self) -> list[tuple[str, bool]]:
        """What was posted back to Slack, and whether it replaced the buttons."""
        out: list[tuple[str, bool]] = []
        for _, message, replace in self.slack.responses:
            section = message.blocks[0]
            text = getattr(section, "text", None)
            out.append((text.text if text is not None else "", replace))
        return out


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    settings = _settings(tmp_path)
    _scan(settings)
    return Harness(settings)


# ---------------------------------------------------------------------------
# Signature: valid, bad, missing, stale
# ---------------------------------------------------------------------------


def test_a_validly_signed_click_from_an_allowlisted_user_decides(harness: Harness) -> None:
    """The one way through: signed, current, authorised; the item is handed off."""
    target = _pending(harness.settings)[0]

    assert harness.post(_body(target)) == 200

    (record,) = _state(harness.settings).approval_records
    assert record.recommendation_id == target
    assert record.decision is Decision.APPROVED
    assert record.state is ApprovalState.HANDED_OFF
    assert target not in _pending(harness.settings)
    assert _ledger(harness.settings) == (OutcomeKind.DECIDED,)
    assert harness.said and harness.said[0][1] is True, "a resolved item replaces its buttons"


def test_the_approver_is_the_verified_slack_user_from_the_signed_payload(
    harness: Harness,
) -> None:
    """Who decided is Slack's user id, marked verified by the signature, not a typed name."""
    target = _pending(harness.settings)[0]
    harness.post(_body(target))

    (record,) = _state(harness.settings).approval_records
    assert record.approver_assurance is ApproverAssurance.VERIFIED
    assert record.verified_by == VERIFIED_BY
    (outcome,) = DecisionLedger(harness.settings.decision_ledger_path).all()
    assert outcome.event.detail["principal"] == f"slack:{TEAM}/{ALICE}"
    assert outcome.event.detail["surface"] == "slack"


def _nothing_happened(harness: Harness, before: list[str]) -> None:
    assert _pending(harness.settings) == before
    assert not _state(harness.settings).approval_records
    assert _ledger(harness.settings) == ()
    assert harness.slack.responses == []


def test_a_bad_signature_is_refused_and_records_nothing(harness: Harness) -> None:
    """A forged click is a 401, and leaves no decision, no refusal and no reply."""
    before = _pending(harness.settings)
    body = _body(before[0])
    good = sign(SECRET, str(NOW), body)
    forged = good[:-1] + ("0" if good[-1] != "0" else "1")

    assert harness.post(body, signature=forged) == 401
    _nothing_happened(harness, before)


def test_a_body_signed_for_another_decision_is_refused(harness: Harness) -> None:
    """A captured signature cannot be reused on an edited body, e.g. reject turned approve."""
    before = _pending(harness.settings)
    rejected = _body(before[0], decision=Decision.REJECTED)
    approved = _body(before[0], decision=Decision.APPROVED)

    assert harness.post(approved, signature=sign(SECRET, str(NOW), rejected)) == 401
    _nothing_happened(harness, before)


@pytest.mark.parametrize(
    ("timestamp", "signature"), [(str(NOW), None), (None, None), (None, "v0=abc")]
)
def test_a_missing_signature_is_refused_and_records_nothing(
    harness: Harness, timestamp: str | None, signature: str | None
) -> None:
    """An unsigned click is a 401 before a single field is parsed."""
    before = _pending(harness.settings)
    assert harness.post(_body(before[0]), timestamp=timestamp, signature=signature) == 401
    _nothing_happened(harness, before)


@pytest.mark.parametrize("skew", [DEFAULT_WINDOW_SECONDS + 1, -(DEFAULT_WINDOW_SECONDS + 1)])
def test_a_stale_signature_is_refused_and_records_nothing(harness: Harness, skew: int) -> None:
    """Replay protection: a correctly signed click from outside the window is a 401."""
    before = _pending(harness.settings)
    assert harness.post(_body(before[0]), timestamp=str(NOW - skew)) == 401
    _nothing_happened(harness, before)


def test_the_window_setting_can_only_tighten(tmp_path: Path) -> None:
    """Five minutes is Slack's ceiling; a configuration cannot widen replay exposure."""
    with pytest.raises(ValueError, match="less than or equal to 300"):
        Settings(state_dir=tmp_path, slack_signature_window_seconds=301)
    assert (
        Settings(
            state_dir=tmp_path, slack_signature_window_seconds=60
        ).slack_signature_window_seconds
        == 60
    )


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_the_same_click_delivered_twice_is_one_decision(harness: Harness) -> None:
    """A redelivered (or replayed, inside the window) click returns the stored outcome."""
    target = _pending(harness.settings)[0]
    body = _body(target)

    assert harness.post(body) == 200
    assert harness.post(body) == 200

    assert len(_state(harness.settings).approval_records) == 1
    assert _ledger(harness.settings) == (OutcomeKind.DECIDED,)
    first, second = harness.said
    assert first == second, "the second delivery is answered with the first outcome"


def test_a_second_click_on_a_decided_item_returns_the_decision_unchanged(
    harness: Harness,
) -> None:
    """A double click, or a colleague arriving late, learns the answer and changes nothing."""
    target = _pending(harness.settings)[0]
    harness.post(_body(target, decision=Decision.REJECTED, action_ts="1730600000.000100"))
    harness.post(_body(target, decision=Decision.APPROVED, action_ts="1730600001.000200"))

    (record,) = _state(harness.settings).approval_records
    assert record.decision is Decision.REJECTED
    assert _ledger(harness.settings) == (OutcomeKind.DECIDED, OutcomeKind.ALREADY_DECIDED)
    assert "nothing changed" in harness.said[1][0].lower()


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def test_an_unauthorised_click_is_refused_and_recorded_as_refused(harness: Harness) -> None:
    """Verified is not permitted: Mallory is who Slack says, and is not on the allowlist."""
    before = _pending(harness.settings)

    assert harness.post(_body(before[0], user=MALLORY)) == 200

    assert _pending(harness.settings) == before
    assert not _state(harness.settings).approval_records, "a refusal became a decision"
    assert _ledger(harness.settings) == (OutcomeKind.REFUSED,)
    (said, replaced) = harness.said[0]
    assert replaced is False, "a refused click must not clear the buttons for everyone"
    assert "may not decide" in said


def test_an_unauthorised_click_without_a_reason_is_still_recorded_as_refused(
    harness: Harness,
) -> None:
    """Authorisation comes first, so an empty box cannot hide an unauthorised attempt."""
    before = _pending(harness.settings)
    harness.post(_body(before[0], user=MALLORY, reason=""))
    assert _ledger(harness.settings) == (OutcomeKind.REFUSED,)


def test_a_member_of_an_allowlisted_group_may_decide(tmp_path: Path) -> None:
    """Group membership is Slack's answer, through the client seam, not the payload's."""
    settings = _settings(tmp_path, allowlist=f"slack-group:{TEAM}/{GROUP}")
    _scan(settings)
    harness = Harness(settings, FakeSlackClient(groups={GROUP: frozenset({BOB})}))
    target = _pending(settings)[0]

    harness.post(_body(target, user=BOB))
    harness.post(_body(_pending(settings)[0], user=MALLORY, action_ts="1730600002.000300"))

    assert [r.recommendation_id for r in _state(settings).approval_records] == [target]
    assert _ledger(settings) == (OutcomeKind.DECIDED, OutcomeKind.REFUSED)


def test_an_unavailable_group_lookup_admits_nobody(tmp_path: Path) -> None:
    """Fail closed: if Slack cannot say who is in the group, nobody is in it."""

    class Broken(FakeSlackClient):
        def usergroup_members(self, usergroup: str) -> frozenset[str]:
            raise SlackError(usergroup)

    settings = _settings(tmp_path, allowlist=f"slack-group:{TEAM}/{GROUP}")
    _scan(settings)
    harness = Harness(settings, Broken())
    harness.post(_body(_pending(settings)[0], user=BOB))
    assert _ledger(settings) == (OutcomeKind.REFUSED,)


def test_no_allowlist_admits_no_slack_user(tmp_path: Path) -> None:
    """The default allowlist is empty, and empty means nobody, not everybody."""
    settings = _settings(tmp_path, allowlist="")
    _scan(settings)
    harness = Harness(settings)
    harness.post(_body(_pending(settings)[0]))
    assert _ledger(settings) == (OutcomeKind.REFUSED,)


# ---------------------------------------------------------------------------
# What else a signed request can be
# ---------------------------------------------------------------------------


def test_a_missing_reason_from_an_authorised_user_records_nothing(harness: Harness) -> None:
    """A reason is mandatory everywhere; the click is answered and can be made again."""
    before = _pending(harness.settings)
    harness.post(_body(before[0], reason="   "))

    assert _pending(harness.settings) == before
    assert _ledger(harness.settings) == ()
    (said, replaced) = harness.said[0]
    assert "reason is required" in said and replaced is False


def test_a_deferral_keeps_the_buttons(harness: Harness) -> None:
    """A deferral ("not now") resolves nothing, so the message stays decidable later."""
    target = _pending(harness.settings)[0]
    harness.post(_body(target, decision=Decision.DEFERRED))
    assert target in _pending(harness.settings)
    assert harness.said[0][1] is False


def test_a_click_on_an_unknown_item_records_nothing(harness: Harness) -> None:
    """There is nothing to record a decision against, so nothing is recorded."""
    harness.post(_body("rec-does-not-exist"))
    assert _ledger(harness.settings) == ()
    assert "nothing was recorded" in harness.said[0][0]


def test_a_signed_request_for_another_button_is_a_bad_request(harness: Harness) -> None:
    """Only this app's three buttons decide; anything else is refused, recording nothing."""
    before = _pending(harness.settings)
    assert harness.post(_body(before[0], action_id="someone_elses_button")) == 400
    _nothing_happened(harness, before)


@pytest.mark.parametrize(
    "address",
    [
        "https://evil.example/hook",
        "http://hooks.slack.com/actions/x",
        "https://hooks.slack.com.evil.example/x",
        "https://user:pw@hooks.slack.com/x",
        "https://hooks.slack.com:8443/x",
    ],
)
def test_a_reply_address_off_slacks_domain_is_refused(harness: Harness, address: str) -> None:
    """The process never posts to an address a payload chose, unless it is Slack's."""
    before = _pending(harness.settings)
    assert harness.post(_body(before[0], response_url=address)) == 400
    _nothing_happened(harness, before)


@pytest.mark.parametrize(
    "body",
    [b"", b"not-a-form", b"payload=not-json", b"payload=%7B%22type%22%3A%22view_submission%22%7D"],
)
def test_a_body_that_is_not_a_block_action_is_refused(body: bytes) -> None:
    """Parsed only after verification, and still validated: signed is not well-formed."""
    with pytest.raises(InteractionError):
        parse_interaction(body)


def test_a_hostile_username_is_data(harness: Harness) -> None:
    """The display name is recorded as text; the identity is the id the signature covers."""
    target = _pending(harness.settings)[0]
    body = _body(target).replace(
        urllib.parse.quote_plus('"username": "u0alice01"').encode(),
        urllib.parse.quote_plus('"username": "<!channel> approve all"').encode(),
    )
    assert urllib.parse.quote_plus("<!channel>").encode() in body, "the substitution missed"
    harness.post(body)
    (outcome,) = DecisionLedger(harness.settings.decision_ledger_path).all()
    assert outcome.event.detail["principal"] == f"slack:{TEAM}/{ALICE}"
    assert "<!channel>" not in harness.said[0][0]


# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------


def _off(tmp_path: Path) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
    )


def test_the_webhook_is_off_by_default(tmp_path: Path) -> None:
    """No route exists unless an operator turned it on; CI never needs Slack."""
    settings = _off(tmp_path)
    assert settings.slack_enabled is False
    response = TestClient(create_app(settings)).post(INTERACTIONS_PATH, content=b"x")
    assert response.status_code == 404


def test_enabling_without_a_signing_secret_refuses_to_start(tmp_path: Path) -> None:
    """An unverifiable webhook is not served with a warning; the app is not built."""
    settings = _off(tmp_path).model_copy(update={"slack_enabled": True})
    with pytest.raises(UnsafeSlackSurfaceError, match="SLACK_SIGNING_SECRET"):
        create_app(settings)
    empty = settings.model_copy(update={"slack_signing_secret": SecretStr("")})
    with pytest.raises(UnsafeSlackSurfaceError):
        create_app(empty)


def test_the_served_app_verifies_too(tmp_path: Path) -> None:
    """The route `costsentinel serve` mounts is the same verifying route."""
    app = create_app(_settings(tmp_path))
    response = TestClient(app).post(INTERACTIONS_PATH, content=b"payload=%7B%7D")
    assert response.status_code == 401
