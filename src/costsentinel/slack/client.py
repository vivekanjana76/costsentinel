"""Every outbound Slack call, behind one seam.

:class:`SlackClient` is the protocol the CLI and the webhook use. :class:`FakeSlackClient`
records what would have been sent, and is what every test uses, so CI holds no token
and opens no socket. :class:`HttpSlackClient` is the real one: three Web API calls over
the standard library's ``urllib``, with the opener injectable so a test can stand in for
the network. No Slack SDK is added (CLAUDE.md section 3).

The three calls are the whole of what the adapter can do in Slack:

- ``chat.postMessage``, to send an approval message to a channel;
- the interaction's ``response_url``, to replace the buttons with the outcome;
- ``usergroups.users.list``, to learn whether a user is in an allowlisted group.

None of them reads or writes an Azure resource, and nothing here is reachable from the
approval gate (D135).

The reply target
----------------

``response_url`` arrives inside the request body. The signature proves Slack sent the
body, but the URL is still a URL somebody else chose, and posting to it is an outbound
request this process makes. So :class:`ReplyTarget` accepts only ``https`` on Slack's
own ``hooks.slack.com``, with no credentials and no port, and nothing else is ever
posted to.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from costsentinel.config import Settings
from costsentinel.domain.common import Frozen
from costsentinel.slack.blocks import SlackMessage

#: Slack's Web API. Fixed: the client never builds an API URL from input.
API_BASE = "https://slack.com/api/"

#: The only host an interaction reply is ever sent to.
REPLY_HOST = "hooks.slack.com"


class SlackError(RuntimeError):
    """A Slack call failed, or Slack said no."""


class ReplyTarget(Frozen):
    """Where Slack asked for the message that replaces an approval's buttons."""

    address: str = Field(description="An https://hooks.slack.com/ address, and nothing else.")

    @field_validator("address")
    @classmethod
    def _slack_only(cls, value: str) -> str:
        parts = urllib.parse.urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.hostname != REPLY_HOST
            or parts.port is not None
            or parts.username is not None
            or parts.password is not None
        ):
            msg = f"a reply may only go to https://{REPLY_HOST}/"
            raise ValueError(msg)
        return value


class PostedMessage(Frozen):
    """Where a message landed."""

    channel: str
    ts: str


class SlackClient(Protocol):
    """What the adapter may ask of Slack."""

    def post_message(self, channel: str, message: SlackMessage) -> PostedMessage:
        """Send a message to a channel."""
        ...

    def respond(self, target: ReplyTarget, message: SlackMessage, *, replace: bool) -> None:
        """Answer an interaction: replace the original, or reply to the clicker alone."""
        ...

    def usergroup_members(self, usergroup: str) -> frozenset[str]:
        """The user ids in one Slack user group."""
        ...


@dataclass
class FakeSlackClient:
    """Records every call and sends nothing. The only client a test uses."""

    groups: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    posted: list[tuple[str, SlackMessage]] = field(default_factory=list[tuple[str, SlackMessage]])
    responses: list[tuple[ReplyTarget, SlackMessage, bool]] = field(
        default_factory=list[tuple[ReplyTarget, SlackMessage, bool]]
    )

    def post_message(self, channel: str, message: SlackMessage) -> PostedMessage:
        """Record the message and return a deterministic timestamp."""
        self.posted.append((channel, message))
        return PostedMessage(channel=channel, ts=f"1730600000.{len(self.posted):06d}")

    def respond(self, target: ReplyTarget, message: SlackMessage, *, replace: bool) -> None:
        """Record the reply."""
        self.responses.append((target, message, replace))

    def usergroup_members(self, usergroup: str) -> frozenset[str]:
        """Membership as configured on the fake; unknown groups are empty."""
        return self.groups.get(usergroup, frozenset())


#: Sends one request and returns the response body. ``urllib`` by default.
Opener = Callable[[urllib.request.Request, float], bytes]


def _urlopen(request: urllib.request.Request, timeout: float) -> bytes:
    # The URL is API_BASE plus a fixed method name, or a host-checked ReplyTarget.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body: bytes = response.read()
    return body


class _ApiResponse(BaseModel):
    """The envelope every Web API call returns. Untrusted, so validated."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: bool
    error: str | None = None
    channel: str | None = None
    ts: str | None = None
    users: tuple[str, ...] | None = None


class HttpSlackClient:
    """The real client: the Web API over ``urllib``, authenticated with a bot token."""

    def __init__(self, token: str, *, opener: Opener = _urlopen, timeout: float = 10.0) -> None:
        """Hold the token; nothing is sent until a method is called."""
        if not token:
            msg = "SLACK_BOT_TOKEN is empty"
            raise SlackError(msg)
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def _send(self, request: urllib.request.Request) -> bytes:
        try:
            return self._opener(request, self._timeout)
        except OSError as exc:
            msg = f"Slack could not be reached: {exc.__class__.__name__}"
            raise SlackError(msg) from exc

    def _api(
        self,
        method: str,
        *,
        body: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
    ) -> _ApiResponse:
        address = API_BASE + method
        if query:
            address += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(  # noqa: S310 -- fixed https base
            address,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST" if body is not None else "GET",
        )
        raw = self._send(request)
        try:
            parsed = _ApiResponse.model_validate_json(raw)
        except ValidationError as exc:
            msg = f"Slack's {method} response was not the expected shape"
            raise SlackError(msg) from exc
        if not parsed.ok:
            msg = f"Slack refused {method}: {parsed.error or 'no reason given'}"
            raise SlackError(msg)
        return parsed

    def post_message(self, channel: str, message: SlackMessage) -> PostedMessage:
        """``chat.postMessage``."""
        reply = self._api("chat.postMessage", body={"channel": channel, **message.as_json()})
        if reply.channel is None or reply.ts is None:
            msg = "Slack accepted the message but did not say where it landed"
            raise SlackError(msg)
        return PostedMessage(channel=reply.channel, ts=reply.ts)

    def respond(self, target: ReplyTarget, message: SlackMessage, *, replace: bool) -> None:
        """POST to the interaction's ``response_url``, which needs no token."""
        envelope: dict[str, object] = (
            {"replace_original": True}
            if replace
            else {"replace_original": False, "response_type": "ephemeral"}
        )
        request = urllib.request.Request(  # noqa: S310 -- ReplyTarget is host-checked
            target.address,
            data=json.dumps({**envelope, **message.as_json()}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        self._send(request)

    def usergroup_members(self, usergroup: str) -> frozenset[str]:
        """``usergroups.users.list``."""
        reply = self._api("usergroups.users.list", query={"usergroup": usergroup})
        return frozenset(reply.users or ())


def client_from_settings(settings: Settings) -> SlackClient | None:
    """The configured client, or ``None`` when no bot token is set.

    ``None`` is a working configuration: the webhook still decides, it just cannot
    replace the buttons or resolve a group, and a group entry then admits nobody.
    """
    if settings.slack_bot_token is None:
        return None
    return HttpSlackClient(settings.slack_bot_token.get_secret_value())
