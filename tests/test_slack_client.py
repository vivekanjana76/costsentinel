"""The outbound Slack seam, and the `costsentinel slack` commands.

The real client is exercised against a fake opener that records the request and returns
a canned body, so every byte it would send is checked and no socket is opened.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from costsentinel.cli import build_parser, run_slack
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider
from costsentinel.slack.blocks import SlackMessage, outcome_message
from costsentinel.slack.client import (
    API_BASE,
    FakeSlackClient,
    HttpSlackClient,
    PostedMessage,
    ReplyTarget,
    SlackError,
    client_from_settings,
)

TOKEN = "xoxb-synthetic-not-a-real-token"  # noqa: S105 -- labelled fake
REPLY = ReplyTarget(address="https://hooks.slack.com/actions/T0SYNTH01/0/synthetic")
MESSAGE: SlackMessage = outcome_message("Approved.", synthetic=True)


class Opener:
    """Stands in for the network: records each request, answers with a canned body."""

    def __init__(self, *answers: bytes | Exception) -> None:
        """Queue the answers, one per request."""
        self.answers = list(answers)
        self.sent: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        assert timeout > 0
        self.sent.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def body(self, index: int = 0) -> dict[str, object]:
        data = self.sent[index].data
        assert isinstance(data, bytes)
        loaded: dict[str, object] = json.loads(data)
        return loaded


# ---------------------------------------------------------------------------
# The reply target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "https://evil.example/x",
        "http://hooks.slack.com/x",
        "https://hooks.slack.com.evil.example/x",
        "https://evil.example/?https://hooks.slack.com/",
        "https://a:b@hooks.slack.com/x",
        "https://hooks.slack.com:444/x",
        "file:///etc/passwd",
        "",
    ],
)
def test_a_reply_goes_only_to_slacks_hooks_host(address: str) -> None:
    """A payload names the reply address; only Slack's own host is accepted."""
    with pytest.raises(ValidationError):
        ReplyTarget(address=address)


def test_slacks_own_reply_address_is_accepted() -> None:
    """The real shape of a response_url passes."""
    assert REPLY.address.startswith("https://hooks.slack.com/")


# ---------------------------------------------------------------------------
# The real client, against a fake network
# ---------------------------------------------------------------------------


def test_posting_a_message_calls_chat_post_message_with_the_bot_token() -> None:
    """The request is exactly Slack's: fixed URL, bearer token, channel plus blocks."""
    opener = Opener(b'{"ok": true, "channel": "C0SYNTH01", "ts": "1730600000.000100"}')
    posted = HttpSlackClient(TOKEN, opener=opener).post_message("C0SYNTH01", MESSAGE)

    (request,) = opener.sent
    assert request.full_url == API_BASE + "chat.postMessage"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert opener.body() == {"channel": "C0SYNTH01", **MESSAGE.as_json()}
    assert (posted.channel, posted.ts) == ("C0SYNTH01", "1730600000.000100")


def test_a_reply_replaces_or_whispers_and_carries_no_token() -> None:
    """A resolved item replaces the buttons; anything else is said to the clicker only."""
    opener = Opener(b"ok", b"ok")
    client = HttpSlackClient(TOKEN, opener=opener)
    client.respond(REPLY, MESSAGE, replace=True)
    client.respond(REPLY, MESSAGE, replace=False)

    assert [r.full_url for r in opener.sent] == [REPLY.address, REPLY.address]
    assert all(r.get_header("Authorization") is None for r in opener.sent)
    assert opener.body(0)["replace_original"] is True
    assert opener.body(1) == {
        "replace_original": False,
        "response_type": "ephemeral",
        **MESSAGE.as_json(),
    }


def test_group_membership_is_asked_of_slack() -> None:
    """usergroups.users.list, with the group id as a query parameter."""
    opener = Opener(b'{"ok": true, "users": ["U0BOB0001", "U0ALICE01"]}')
    members = HttpSlackClient(TOKEN, opener=opener).usergroup_members("S0APPROVE1")
    assert opener.sent[0].full_url == API_BASE + "usergroups.users.list?usergroup=S0APPROVE1"
    assert opener.sent[0].get_method() == "GET"
    assert members == frozenset({"U0BOB0001", "U0ALICE01"})


@pytest.mark.parametrize(
    "answer",
    [
        b'{"ok": false, "error": "channel_not_found"}',
        b"<html>gateway timeout</html>",
        b'{"ok": true}',
        OSError("unreachable"),
    ],
    ids=["refused", "not-json", "no-location", "unreachable"],
)
def test_every_failure_is_one_error_type(answer: bytes | Exception) -> None:
    """The caller handles SlackError and nothing else, whatever went wrong."""
    with pytest.raises(SlackError):
        HttpSlackClient(TOKEN, opener=Opener(answer)).post_message("C0SYNTH01", MESSAGE)


def test_an_empty_token_is_refused_before_anything_is_sent() -> None:
    """A tokenless client would only ever fail; it fails at construction instead."""
    with pytest.raises(SlackError):
        HttpSlackClient("")


def _settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        **extra,  # type: ignore[arg-type]
    )


def test_no_token_means_no_client(tmp_path: Path) -> None:
    """Off is the default: nothing can reach Slack until a token is configured."""
    assert client_from_settings(_settings(tmp_path)) is None
    configured = _settings(tmp_path, slack_bot_token=SecretStr(TOKEN))
    assert isinstance(client_from_settings(configured), HttpSlackClient)


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _scanned(tmp_path: Path, **extra: object) -> Settings:
    settings = _settings(tmp_path, **extra)
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id="run-slack-sample",
            requested_by="test",
        )
    return settings


def test_render_prints_the_queue_and_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline by construction; its first message is the committed sample."""
    settings = _scanned(tmp_path)
    args = build_parser().parse_args(["slack", "render"])

    assert run_slack(args, settings) == 0

    rendered = json.loads(capsys.readouterr().out)
    sample = json.loads(Path("docs/slack/approval-message.json").read_text(encoding="utf-8"))
    assert rendered and rendered[0] == sample


def test_render_with_nothing_stored_prints_an_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty queue is a valid answer, not an error."""
    assert run_slack(build_parser().parse_args(["slack", "render"]), _settings(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_post_sends_each_pending_item_through_the_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One message per pending approval, to the configured channel."""
    settings = _scanned(tmp_path, slack_channel="C0SYNTH01")
    fake = FakeSlackClient()
    args = build_parser().parse_args(["slack", "post", "--run-id", "run-slack-sample"])

    assert run_slack(args, settings, client=fake) == 0

    assert fake.posted and {channel for channel, _ in fake.posted} == {"C0SYNTH01"}
    assert capsys.readouterr().out.count("Posted ") == len(fake.posted)


def test_post_without_a_channel_or_token_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is half-sent: missing configuration is named, and nothing goes out."""
    args = build_parser().parse_args(["slack", "post"])
    assert run_slack(args, _scanned(tmp_path)) == 2
    assert "SLACK_BOT_TOKEN and SLACK_CHANNEL" in capsys.readouterr().err


def test_post_reports_a_slack_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A refused post is an error exit with Slack's reason, not a traceback."""

    class Refusing(FakeSlackClient):
        def post_message(self, channel: str, message: SlackMessage) -> PostedMessage:
            raise SlackError("Slack refused chat.postMessage: not_in_channel")

    settings = _scanned(tmp_path, slack_channel="C0SYNTH01")
    assert run_slack(build_parser().parse_args(["slack", "post"]), settings, Refusing()) == 1
    assert "not_in_channel" in capsys.readouterr().err


def test_post_with_an_empty_queue_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing pending, nothing posted, and it says so."""
    fake = FakeSlackClient()
    settings = _settings(tmp_path, slack_channel="C0SYNTH01")
    assert run_slack(build_parser().parse_args(["slack", "post"]), settings, fake) == 0
    assert fake.posted == []
    assert "nothing was posted" in capsys.readouterr().out
