"""The ``decide`` and ``decisions`` commands, and the full learning loop.

The headline test here is the demo: scan, decline the top finding through the CLI,
scan again, and watch it come back annotated and demoted. Everything else is the
behaviour that makes that command safe to hand to an operator -- a mandatory reason,
an unknown id refused rather than recorded against nothing, and a decision never
silently overwritten.

``decide`` is deliberately not the approval gate. It records an outcome after a
report has been read and authorises nothing, which is what lets the learning loop be
demonstrated several phases before the Teams and Slack adapters exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from costsentinel.cli import build_parser, main
from costsentinel.config import MemoryBackend, Settings
from costsentinel.domain.memory import Outcome
from costsentinel.memory import SqliteMemoryStore, get_memory


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings with a file-backed memory store in a temporary directory."""
    return Settings(state_dir=tmp_path, memory_path=tmp_path / "memory.sqlite")


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    """Point the CLI at the test settings."""
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: settings)
    return settings


def _first_recommendation_id(settings: Settings, client: str) -> str:
    store = get_memory(settings)
    try:
        entries = store.entries_for(client)
        assert entries, "the scan should have written a history"
        return min(entries, key=lambda entry: entry.rank).recommendation_id
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The learning loop, end to end through the CLI
# ---------------------------------------------------------------------------


def test_decline_a_finding_and_it_comes_back_annotated_and_demoted(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demo. Three commands, and the second scan knows about the first.

    This is the whole point of the phase: without it, a client is re-pitched the same
    declined advice every month and CostSentinel is an analyser rather than a system
    that learns.
    """
    assert main(["scan", "--client", "northwind-energy", "--summary"]) == 0
    first = capsys.readouterr().out
    assert "memory" not in first.split("findings")[1].split("\n")[1]

    target = _first_recommendation_id(cli, "northwind-energy")
    assert (
        main(
            [
                "decide",
                target,
                "--reject",
                "--reason",
                "owner confirmed this is a quarter-end batch host",
                "--by",
                "ops@example.com",
            ]
        )
        == 0
    )
    recorded = capsys.readouterr().out
    assert "rejected by ops@example.com" in recorded
    assert "quarter-end batch host" in recorded
    assert "It is not removed: the waste has not gone away." in recorded

    assert main(["scan", "--client", "northwind-energy", "--summary"]) == 0
    second = capsys.readouterr().out
    assert "1 previously declined" in second
    assert "Previously declined on" in second
    assert "quarter-end batch host" in second
    assert "was #1 on merit" in second


def test_the_decisions_command_lists_decided_and_pending(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator needs a list of ids that are actually decidable."""
    assert main(["scan", "--client", "northwind-energy"]) == 0
    capsys.readouterr()

    assert main(["decisions", "--client", "northwind-energy"]) == 0
    before = capsys.readouterr().out
    assert "awaiting a decision" in before
    assert "decided" not in before
    assert "costsentinel decide" in before

    target = _first_recommendation_id(cli, "northwind-energy")
    main(["decide", target, "--defer", "--reason", "January maintenance window"])
    capsys.readouterr()

    assert main(["decisions", "--client", "northwind-energy"]) == 0
    after = capsys.readouterr().out
    assert "decided (1)" in after
    assert "deferred" in after
    assert "January maintenance window" in after
    assert target not in after.split("awaiting a decision")[1]


def test_decisions_says_so_when_nothing_has_been_recorded(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["decisions", "--client", "nobody"]) == 0
    assert "nothing recorded yet; run a scan first." in capsys.readouterr().out


def test_decisions_covers_every_client_by_default(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan"]) == 0
    capsys.readouterr()
    assert main(["decisions"]) == 0
    output = capsys.readouterr().out
    assert "northwind-energy" in output
    assert "ministry-of-transport" in output


# ---------------------------------------------------------------------------
# What the command refuses
# ---------------------------------------------------------------------------


def test_an_unknown_recommendation_is_refused_not_recorded_against_nothing(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """An outcome whose recommendation does not exist would match nothing forever.

    And the operator who mistyped the id would believe they had recorded a decision.
    """
    assert main(["decide", "rec-does-not-exist", "--reject", "--reason", "x"]) == 1
    error = capsys.readouterr().err
    assert "no recommendation 'rec-does-not-exist' has been proposed" in error
    assert "Run a scan first" in error


def test_a_reason_is_mandatory() -> None:
    """An outcome with no reason can suppress a repeat but cannot explain it."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["decide", "rec-1", "--reject"])


def test_exactly_one_outcome_may_be_given() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["decide", "rec-1", "--reason", "x"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["decide", "rec-1", "--accept", "--reject", "--reason", "x"])


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--accept", Outcome.ACCEPTED),
        ("--reject", Outcome.REJECTED),
        ("--defer", Outcome.DEFERRED),
    ],
)
def test_each_flag_records_its_own_outcome(
    flag: str, expected: Outcome, cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", "--client", "northwind-energy"]) == 0
    capsys.readouterr()
    target = _first_recommendation_id(cli, "northwind-energy")
    assert main(["decide", target, flag, "--reason", "because"]) == 0
    capsys.readouterr()

    store = get_memory(cli)
    try:
        recorded = store.outcome_for(target)
        assert recorded is not None
        assert recorded.outcome is expected
    finally:
        store.close()


def test_deciding_twice_is_refused_with_what_was_said_the_first_time(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Overwriting would lose the fact that somebody changed their mind."""
    assert main(["scan", "--client", "northwind-energy"]) == 0
    capsys.readouterr()
    target = _first_recommendation_id(cli, "northwind-energy")
    assert main(["decide", target, "--reject", "--reason", "not this quarter"]) == 0
    capsys.readouterr()

    assert main(["decide", target, "--accept", "--reason", "changed our minds"]) == 1
    error = capsys.readouterr().err
    assert "already rejected" in error
    assert "not this quarter" in error
    assert "changed their mind" in error


def test_deciding_with_memory_off_is_refused_rather_than_silently_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A person recording a decision deserves to be told it went nowhere.

    A graph node writing history degrades quietly; a human command must not.
    """
    off = Settings(state_dir=tmp_path, memory_backend=MemoryBackend.OFF)
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: off)

    assert main(["decide", "rec-1", "--reject", "--reason", "x"]) == 1
    error = capsys.readouterr().err
    assert "memory is switched off" in error
    assert "MEMORY_BACKEND=sqlite" in error

    assert main(["decisions"]) == 1
    assert "nothing has been recorded" in capsys.readouterr().err


def test_the_decision_is_attributed_to_the_os_user_when_no_one_is_named(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A weak identity, deliberately.

    It records who typed the command on a machine somebody already has access to.
    Authenticated approver identity is the approval gate's problem, and pretending
    otherwise here would overstate what the record proves.
    """
    import getpass

    assert main(["scan", "--client", "northwind-energy"]) == 0
    capsys.readouterr()
    target = _first_recommendation_id(cli, "northwind-energy")
    assert main(["decide", target, "--reject", "--reason", "x"]) == 0
    assert getpass.getuser() in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What it records
# ---------------------------------------------------------------------------


def test_the_outcome_copies_the_resource_and_action_from_what_was_proposed(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not from the caller.

    A typo in those would attach the decision to the wrong resource and suppress the
    wrong advice next month, which is worse than a rejected command.
    """
    assert main(["scan", "--client", "northwind-energy"]) == 0
    capsys.readouterr()
    target = _first_recommendation_id(cli, "northwind-energy")
    main(["decide", target, "--reject", "--reason", "x"])
    capsys.readouterr()

    store = get_memory(cli)
    try:
        entry = store.entry_for(target)
        outcome = store.outcome_for(target)
        assert entry is not None and outcome is not None
        assert outcome.target_resource_id == entry.target_resource_id
        assert outcome.action is entry.action
        assert outcome.waste_kind is entry.waste_kind
        assert outcome.client == entry.client
        assert outcome.run_id == entry.run_id
        assert outcome.monthly_saving_display == entry.monthly_saving_display
    finally:
        store.close()


def test_recording_a_decision_emits_an_audit_event(cli: Settings) -> None:
    from costsentinel.domain.governance import AuditEventType
    from costsentinel.memory import record_decision

    store = SqliteMemoryStore(cli.memory_db_path)
    try:
        from costsentinel.graph.runner import run_scan

        run_scan(client="northwind-energy", settings=cli, memory=store, requested_by="test")
        entries = store.entries_for("northwind-energy")
        assert entries
        _, event = record_decision(
            store,
            recommendation_id=entries[0].recommendation_id,
            outcome=Outcome.REJECTED,
            reason="audited",
            decided_by="ops@example.com",
        )
        assert event.event_type is AuditEventType.APPROVAL_RECORDED
        assert event.actor == "ops@example.com"
        assert event.detail["outcome"] == "rejected"
        assert event.detail["reason"] == "audited"
        assert event.detail["client"] == "northwind-energy"
        assert event.run_id == entries[0].run_id
    finally:
        store.close()


def test_the_config_command_reports_the_memory_backend_and_embedder(
    cli: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config"]) == 0
    output = capsys.readouterr().out
    assert "memory" in output
    assert "sqlite" in output
    assert "embedder" in output
    assert "fake" in output
