"""A verified Slack button click, parsed into the few facts a decision needs.

Called only on a body whose signature has already been checked (:mod:`.signature`).
The signature proves Slack sent it; it does not make the contents safe, so every field
read is validated and everything else in the payload is ignored. A click supplies the
recommendation id and the decision: the facts being decided about are copied from the
pending item by the gate, exactly as for the CLI (D120).

Two types, deliberately apart
-----------------------------

:class:`SlackClick` is what the decision path receives. It carries no address, handle or
command, and the never-executes test checks its fields the way it checks the approval
types. :class:`SlackInteraction` adds the :class:`~.client.ReplyTarget` that Slack asked
the outcome to be posted to, which is a URL by nature. It stays out of the decision,
and it is host-checked to Slack's own domain.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from costsentinel.approvals.identity import ApproverIdentity
from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import ApproverAssurance, Decision
from costsentinel.slack.blocks import ACTION_IDS, REASON_ACTION, REASON_BLOCK
from costsentinel.slack.client import ReplyTarget

#: Recorded as ``verified_by`` on every identity a Slack click establishes.
VERIFIED_BY = "slack-signature"

_DECISIONS = {action_id: decision for decision, action_id in ACTION_IDS.items()}
_SLACK_ID = r"^[A-Z0-9]{2,32}$"


class InteractionError(ValueError):
    """A signed request that is not a CostSentinel decision click."""


class _Loose(BaseModel):
    """Slack's payload as received: read what is needed, ignore the rest."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _Team(_Loose):
    id: str = Field(pattern=_SLACK_ID)


class _User(_Loose):
    id: str = Field(pattern=_SLACK_ID)
    username: str | None = None


class _Action(_Loose):
    action_id: str
    value: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    action_ts: str = Field(pattern=r"^[0-9]{1,12}\.[0-9]{1,9}$")


class _Input(_Loose):
    value: str | None = None


class _State(_Loose):
    values: dict[str, dict[str, _Input]] = Field(default_factory=dict[str, dict[str, _Input]])


class _Payload(_Loose):
    type: Literal["block_actions"]
    team: _Team
    user: _User
    actions: tuple[_Action, ...] = Field(min_length=1, max_length=1)
    state: _State = Field(default_factory=_State)
    response_url: str | None = None


class SlackClick(Frozen):
    """One decision click, as the decision path receives it."""

    team_id: str = Field(pattern=_SLACK_ID)
    user_id: str = Field(pattern=_SLACK_ID)
    user_name: str = Field(min_length=1, max_length=80)
    recommendation_id: str = Field(min_length=1)
    decision: Decision
    reason: str = Field(description="What was typed in the reason box. May be empty.")
    action_ts: str

    @property
    def principal(self) -> str:
        """The verified identity this click carries."""
        return f"slack:{self.team_id}/{self.user_id}"

    @property
    def interaction_key(self) -> str:
        """The same for every delivery of this click, and for no other click."""
        return f"slack:{self.team_id}:{self.user_id}:{self.action_ts}:{self.decision.value}"

    def identity(self, groups: tuple[str, ...] = ()) -> ApproverIdentity:
        """The approver: verified by the signature, named by Slack, not by the click."""
        return ApproverIdentity(
            name=self.user_name,
            assurance=ApproverAssurance.VERIFIED,
            verified_by=VERIFIED_BY,
            principal=self.principal,
            groups=groups,
        )


class SlackInteraction(Frozen):
    """A click, and where Slack asked for the answer to go."""

    click: SlackClick
    reply: ReplyTarget | None = None


def parse_interaction(body: bytes) -> SlackInteraction:
    """A signature-verified form body, as a decision click.

    Raises:
        InteractionError: If it is anything else: not form-encoded, not a block
            action, not one of this app's three buttons, or malformed in any field.
            Nothing is recorded for it.
    """
    try:
        form = urllib.parse.parse_qs(body.decode("utf-8"), strict_parsing=True)
        (raw,) = form["payload"]
        payload = _Payload.model_validate(json.loads(raw))
    except (UnicodeDecodeError, ValueError, KeyError, ValidationError) as exc:
        msg = "the request is not a Slack block action"
        raise InteractionError(msg) from exc

    (action,) = payload.actions
    decision = _DECISIONS.get(action.action_id)
    if decision is None:
        msg = f"{action.action_id!r} is not a CostSentinel decision button"
        raise InteractionError(msg)

    typed = payload.state.values.get(REASON_BLOCK, {}).get(REASON_ACTION)
    name = (payload.user.username or payload.user.id)[:80]
    try:
        reply = ReplyTarget(address=payload.response_url) if payload.response_url else None
    except ValidationError as exc:
        msg = "the reply address is not Slack's"
        raise InteractionError(msg) from exc

    return SlackInteraction(
        click=SlackClick(
            team_id=payload.team.id,
            user_id=payload.user.id,
            user_name=name,
            recommendation_id=action.value,
            decision=decision,
            reason=(typed.value or "").strip() if typed else "",
            action_ts=action.action_ts,
        ),
        reply=reply,
    )
