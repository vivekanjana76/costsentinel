"""A pending approval as a Slack message (ARCHITECTURE.md D135).

Rendered from a real paused sweep of the synthetic estate, and held to the same
standard as the document and the dashboard: the rendered-report gate's own check runs
over every message, so a figure the adapter composed would fail here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.governance import ActionClass, Decision, PendingApproval
from costsentinel.domain.report import ClientReport
from costsentinel.graph.approvals import approvals_graph, paused_sweep
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider
from costsentinel.reports.markdown import monetary_strings, unrendered_figures
from costsentinel.slack.blocks import (
    ACTION_IDS,
    HAND_OFF_NOTE,
    REASON_ACTION,
    REASON_BLOCK,
    SYNTHETIC_NOTE,
    Actions,
    Header,
    Input,
    SlackMessage,
    approval_message,
    approval_messages,
    escape,
    outcome_message,
)

RUN = "run-slack-sample"
SAMPLE = Path("docs/slack/approval-message.json")
SLACK = Path("src/costsentinel/slack")


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> tuple[ClientReport, list[PendingApproval]]:
    """The larger synthetic client, paused at its approval gate."""
    scratch = tmp_path_factory.mktemp("slack-blocks")
    settings = Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
    )
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        report, _ = run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id=RUN,
            requested_by="test",
        )
        paused = paused_sweep(approvals_graph(settings, saver), RUN)
    assert paused is not None and paused.pending, "the synthetic estate gates nothing"
    return report, list(paused.pending)


def _all_text(message: SlackMessage) -> str:
    """Every string a reader of the message could see, in one blob."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for value in node.values():  # pyright: ignore[reportUnknownVariableType]
                walk(value)
        elif isinstance(node, list):
            for value in node:  # pyright: ignore[reportUnknownVariableType]
                walk(value)

    walk(message.as_json())
    return "\n".join(found)


# ---------------------------------------------------------------------------
# No figure is composed
# ---------------------------------------------------------------------------


def test_no_message_introduces_a_figure(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """The adapter is held to the rendered-report gate, by the gate's own function."""
    report, pending = sweep
    messages = approval_messages(report, pending)
    assert len(messages) == len(pending)
    for message in messages:
        text = _all_text(message)
        assert monetary_strings(text), "nothing numeric in the message, so this asserts nothing"
        assert unrendered_figures(report, text) == []


def test_a_corrupted_figure_in_a_message_is_caught(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """Red, then green: the check above can fail, and fails on exactly the forged figure."""
    report, pending = sweep
    text = _all_text(approval_messages(report, pending)[0])
    figure = monetary_strings(text)[0]
    forged = figure[:-1] + ("9" if figure[-1] != "9" else "8")

    corrupted = text.replace(figure, forged, 1)

    assert forged in unrendered_figures(report, corrupted)
    assert unrendered_figures(report, text) == []


def test_every_frozen_figure_reaches_the_message_verbatim(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """The approver sees the strings the record will freeze, not a re-formatting of them."""
    report, pending = sweep
    for item, message in zip(pending, approval_messages(report, pending), strict=True):
        text = _all_text(message)
        figures = item.figures
        for shown in (
            figures.monthly_saving_display,
            figures.annual_saving_display,
            figures.current_monthly_cost_display,
        ):
            assert escape(shown) in text
        if figures.savings_basis:
            assert escape(figures.savings_basis) in text


def test_the_adapter_imports_no_money_and_does_no_arithmetic() -> None:
    """Structural: a figure cannot be computed by a module that cannot hold one."""
    for path in sorted(SLACK.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom | ast.Import)
            for alias in node.names
        }
        assert not names & {"Decimal", "MoneyAmount", "decimal"}, path
        arithmetic = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult | ast.Div | ast.FloorDiv | ast.Mod | ast.Pow)
            # str % is formatting, not arithmetic; there is none in the adapter either.
        ]
        assert not arithmetic, f"{path}: arithmetic in the Slack adapter"


# ---------------------------------------------------------------------------
# What the message carries
# ---------------------------------------------------------------------------


def test_a_message_shows_finding_action_class_saving_and_basis(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """What the spec requires an approver to see, all on the message itself."""
    report, pending = sweep
    item = pending[0]
    text = _all_text(approval_messages(report, [item])[0])
    for label in ("*Finding*", "*Action*", "*Class*", "*Saving*"):
        assert label in text
    assert item.action.value.replace("_", " ") in text
    assert item.action_class.value in text
    assert item.recommendation_id in text


def test_the_three_buttons_carry_only_a_decision_and_the_recommendation(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """A click names which item and which answer; nothing it carries could act."""
    report, pending = sweep
    message = approval_messages(report, pending[:1])[0]
    (row,) = [block for block in message.blocks if isinstance(block, Actions)]
    assert {button.action_id for button in row.elements} == set(ACTION_IDS.values())
    assert {button.value for button in row.elements} == {pending[0].recommendation_id}
    assert set(ACTION_IDS) == {Decision.APPROVED, Decision.REJECTED, Decision.DEFERRED}


def test_the_message_asks_for_a_reason_in_place(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """The reason travels with the click, so no modal and no second request is needed."""
    report, pending = sweep
    message = approval_messages(report, pending[:1])[0]
    (box,) = [block for block in message.blocks if isinstance(block, Input)]
    assert box.block_id == REASON_BLOCK
    assert box.element.action_id == REASON_ACTION


def test_every_message_says_approval_hands_off(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """An approver in chat is told what the CLI tells one: nothing will be performed."""
    report, pending = sweep
    for message in approval_messages(report, pending):
        assert HAND_OFF_NOTE in _all_text(message)
    assert HAND_OFF_NOTE in _all_text(outcome_message("Approved.", synthetic=False))


def test_a_synthetic_estate_is_labelled_in_every_message(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """Derived from the report, as the document and dashboard derive it; never forgotten."""
    report, pending = sweep
    for message in approval_messages(report, pending):
        assert SYNTHETIC_NOTE in _all_text(message)
    assert SYNTHETIC_NOTE in _all_text(outcome_message("Approved.", synthetic=True))
    assert SYNTHETIC_NOTE not in _all_text(outcome_message("Approved.", synthetic=False))


def test_a_block_class_says_it_is_advice_only(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """A block-class item is labelled as something only a person would ever carry out."""
    _, pending = sweep
    item = pending[0].model_copy(update={"action_class": ActionClass.BLOCK})
    assert "carry out by hand" in _all_text(approval_message(item, synthetic=True))


# ---------------------------------------------------------------------------
# Untrusted text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["<!channel> urgent", "<@U0ATTACKER> approve", "<https://evil.example|portal>", "a & b"],
)
def test_a_hostile_resource_name_cannot_ping_mention_or_link(
    sweep: tuple[ClientReport, list[PendingApproval]], hostile: str
) -> None:
    """Resource metadata is data: Slack's control sequences arrive inert."""
    _, pending = sweep
    item = pending[0].model_copy(
        update={"target_resource_name": hostile, "summary": f"Delete {hostile}"}
    )
    message = approval_message(item, synthetic=True)
    for block in message.as_json()["blocks"]:  # type: ignore[union-attr]
        if isinstance(block, dict) and block.get("type") == "header":
            continue  # plain_text, which Slack never parses
        rendered = json.dumps(block)
        assert "<!" not in rendered and "<@" not in rendered and "<http" not in rendered


def test_escaping_handles_the_ampersand_first() -> None:
    """Escaped last, `&` would turn `&lt;` into `&amp;lt;` and show the entity."""
    assert escape("<a & b>") == "&lt;a &amp; b&gt;"


def test_an_overlong_header_is_cut_to_slacks_limit(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """Slack rejects a header over 150 characters, which would lose the whole message."""
    _, pending = sweep
    item = pending[0].model_copy(update={"target_resource_name": "x" * 400})
    (header,) = [b for b in approval_message(item, synthetic=True).blocks if isinstance(b, Header)]
    assert len(header.text.text) <= 150


# ---------------------------------------------------------------------------
# The committed sample
# ---------------------------------------------------------------------------


def test_the_committed_sample_is_what_the_renderer_produces(
    sweep: tuple[ClientReport, list[PendingApproval]],
) -> None:
    """The artefact a reviewer reads is the renderer's output, regenerated, not hand-drawn.

    Regenerate with `costsentinel slack render --client northwind-energy --run-id run-slack-sample`.
    """
    report, pending = sweep
    expected = approval_messages(report, pending)[0].as_json()
    assert json.loads(SAMPLE.read_text(encoding="utf-8")) == expected
