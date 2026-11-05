"""A pending approval as a Slack message, built from the report's own strings.

The figures reaching Slack are the ones a client reads in the document and an operator
sees on the dashboard. :class:`~costsentinel.domain.governance.PendingApproval` is read
from the report by the approval gate, and its :class:`FrozenFigures` are the report's
display strings, copied. This module places those strings in Block Kit and composes
none: it imports no money type and does no arithmetic. A test runs the rendered-report
gate's own check, :func:`~costsentinel.reports.markdown.unrendered_figures`, over every
message, so a figure computed here would fail the build (D135).

Untrusted text
--------------

A resource name is cloud metadata, and cloud metadata is untrusted. Slack's ``mrkdwn``
turns ``<@U123>`` into a mention, ``<!channel>`` into a ping to everyone, and
``<https://evil|click here>`` into a disguised link. Every value interpolated into
``mrkdwn`` is escaped with Slack's three entities (``&``, ``<``, ``>``). The header is
``plain_text``, which Slack never parses.

Typed blocks
------------

Block Kit is JSON. It is built here from small Pydantic models rather than
hand-assembled dicts, so a misspelled key is a validation error and not a silently
ignored block.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import Field, JsonValue

from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import ActionClass, Decision, PendingApproval
from costsentinel.domain.report import ClientReport
from costsentinel.reports.markdown import is_synthetic

#: Button action ids, and the only decisions a click can carry.
ACTION_IDS: dict[Decision, str] = {
    Decision.APPROVED: "costsentinel_approve",
    Decision.REJECTED: "costsentinel_reject",
    Decision.DEFERRED: "costsentinel_defer",
}

#: Where the typed reason lives in the message's state when a button is clicked.
REASON_BLOCK = "costsentinel_reason"
REASON_ACTION = "reason"

#: Slack's limit on a header's text.
_HEADER_LIMIT = 150

SYNTHETIC_NOTE = (
    "Synthetic demonstration estate: every subscription, resource and figure here is invented."
)
HAND_OFF_NOTE = (
    "Approving records a decision and hands the action to a person. CostSentinel will "
    "not and cannot perform it."
)


class Text(Frozen):
    """A Block Kit text object."""

    type: Literal["plain_text", "mrkdwn"]
    text: str = Field(min_length=1)


class Header(Frozen):
    """A header block. Plain text only, which Slack never parses for mentions."""

    type: Literal["header"] = "header"
    text: Text


class Section(Frozen):
    """A section block, with body text, two-column fields, or both."""

    type: Literal["section"] = "section"
    text: Text | None = None
    fields: tuple[Text, ...] | None = None


class Context(Frozen):
    """A context block: small print under a message."""

    type: Literal["context"] = "context"
    elements: tuple[Text, ...] = Field(min_length=1)


class Divider(Frozen):
    """A horizontal rule."""

    type: Literal["divider"] = "divider"


class PlainTextInput(Frozen):
    """A free-text input. Its value comes back in the click's ``state``."""

    type: Literal["plain_text_input"] = "plain_text_input"
    action_id: str
    multiline: bool = False
    placeholder: Text | None = None


class Input(Frozen):
    """An input block: a labelled element."""

    type: Literal["input"] = "input"
    block_id: str
    label: Text
    element: PlainTextInput
    optional: bool = False


class Button(Frozen):
    """A button. Its ``value`` is the recommendation id and nothing more."""

    type: Literal["button"] = "button"
    action_id: str
    text: Text
    value: str
    style: Literal["primary", "danger"] | None = None


class Actions(Frozen):
    """A row of buttons."""

    type: Literal["actions"] = "actions"
    block_id: str
    elements: tuple[Button, ...] = Field(min_length=1)


Block = Header | Section | Context | Divider | Input | Actions


class SlackMessage(Frozen):
    """One message: the notification text, and the blocks a client renders."""

    text: str = Field(min_length=1, description="The fallback shown in a notification.")
    blocks: tuple[Block, ...] = Field(min_length=1)

    def as_json(self) -> dict[str, JsonValue]:
        """The JSON body Slack expects, with absent optional keys left out."""
        return self.model_dump(mode="json", exclude_none=True)


def escape(text: str) -> str:
    """Neutralise Slack's control sequences in an untrusted value.

    Slack documents exactly three characters to escape in ``mrkdwn``. Escaping ``&``
    first matters: done last, it would double-escape the other two.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md(text: str) -> Text:
    return Text(type="mrkdwn", text=text)


def _plain(text: str) -> Text:
    return Text(type="plain_text", text=text)


def _words(value: str) -> str:
    return value.replace("_", " ")


def _class_label(action_class: ActionClass) -> str:
    if action_class is ActionClass.BLOCK:
        return "`block`: advice for a person to carry out by hand"
    return f"`{action_class.value}`"


def _header(pending: PendingApproval) -> Header:
    text = f"Approval needed: {_words(pending.action.value)} on {pending.target_resource_name}"
    if len(text) > _HEADER_LIMIT:
        text = text[: _HEADER_LIMIT - 1] + "…"
    return Header(text=_plain(text))


def _context(pending: PendingApproval, *, synthetic: bool) -> Context:
    elements = [
        _md(
            f"{escape(pending.client)} · sweep `{escape(pending.run_id)}` · "
            f"`{escape(pending.recommendation_id)}`"
        ),
        _md(HAND_OFF_NOTE),
    ]
    if synthetic:
        elements.insert(0, _md(f"*{SYNTHETIC_NOTE}*"))
    return Context(elements=tuple(elements))


def approval_message(pending: PendingApproval, *, synthetic: bool) -> SlackMessage:
    """One pending approval as a message with a reason box and three buttons.

    Every figure is a :class:`FrozenFigures` string, as the report rendered it.
    """
    figures = pending.figures
    fields = [
        _md(f"*Finding*\n{escape(_words(pending.waste_kind)) or 'not stated'}"),
        _md(f"*Action*\n{escape(_words(pending.action.value))}"),
        _md(f"*Class*\n{_class_label(pending.action_class)}"),
        _md(
            f"*Saving*\n{escape(figures.monthly_saving_display)} / month "
            f"({escape(figures.annual_saving_display)} / year)"
        ),
        _md(f"*Costs now*\n{escape(figures.current_monthly_cost_display)} / month"),
        _md(f"*Resource*\n`{escape(pending.target_resource_name)}`"),
    ]
    blocks: list[Block] = [
        _header(pending),
        Section(text=_md(escape(pending.summary)), fields=tuple(fields)),
    ]
    if figures.savings_basis:
        blocks.append(Section(text=_md(f"*Basis*\n{escape(figures.savings_basis)}")))
    blocks += [
        _context(pending, synthetic=synthetic),
        Input(
            block_id=REASON_BLOCK,
            label=_plain("Reason (required, recorded with your decision)"),
            element=PlainTextInput(
                action_id=REASON_ACTION,
                multiline=True,
                placeholder=_plain("Why: e.g. agreed with the workload owner"),
            ),
        ),
        Actions(
            block_id=f"costsentinel_decision_{pending.recommendation_id}",
            elements=(
                Button(
                    action_id=ACTION_IDS[Decision.APPROVED],
                    text=_plain("Approve"),
                    value=pending.recommendation_id,
                    style="primary",
                ),
                Button(
                    action_id=ACTION_IDS[Decision.REJECTED],
                    text=_plain("Reject"),
                    value=pending.recommendation_id,
                    style="danger",
                ),
                Button(
                    action_id=ACTION_IDS[Decision.DEFERRED],
                    text=_plain("Defer"),
                    value=pending.recommendation_id,
                ),
            ),
        ),
    ]
    return SlackMessage(
        text=(
            f"Approval needed: {_words(pending.action.value)} on "
            f"{pending.target_resource_name}, {figures.monthly_saving_display} / month"
        ),
        blocks=tuple(blocks),
    )


def approval_messages(
    report: ClientReport, pending: Sequence[PendingApproval]
) -> tuple[SlackMessage, ...]:
    """A message per pending approval in one client's sweep.

    The report is passed alongside the queue for one reason: whether the estate is
    synthetic is derived from the report, by the same function the document and the
    dashboard use. A sample therefore cannot reach a channel without its banner.
    """
    synthetic = is_synthetic(report)
    return tuple(approval_message(item, synthetic=synthetic) for item in pending)


def outcome_message(statement: str, *, synthetic: bool) -> SlackMessage:
    """What replaces the buttons once something is decided, refused or already decided.

    The statement is composed by the gate or the decision service, not here. It is
    escaped like any other value, because it quotes a reason somebody typed.
    """
    notes = [_md(HAND_OFF_NOTE)]
    if synthetic:
        notes.insert(0, _md(f"*{SYNTHETIC_NOTE}*"))
    return SlackMessage(
        text=statement[:300],
        blocks=(Section(text=_md(escape(statement))), Context(elements=tuple(notes))),
    )
