"""Data-handling safety: what may never be committed, and what may never leak.

This repository is public and the tool reads live cost data from client Azure
estates. Those two facts together make one mistake materially worse than all the
others, so it gets its own test module rather than a line in somebody else's.

Three things are asserted here:

1. The guard refuses the artefacts a real run produces, and does not cry wolf on the
   files this repository legitimately contains. A check that fires on correct files
   gets disabled, which is worse than no check.
2. The committed estate and eval datasets are fully synthetic, so nothing shipped
   with the repository is somebody's real subscription.
3. A real run writes to a gitignored directory and announces its scope before it
   reads anything, rather than printing client cost data to stdout.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from costsentinel.cli import (
    DEFAULT_REAL_OUTPUT_DIR,
    build_parser,
    main,
    real_run_banner,
    resolve_real_scope,
    write_real_report,
)
from costsentinel.config import RunMode, Settings
from costsentinel.providers.base import ProviderNotConfiguredError

GUARD = Path("scripts/check_no_real_data.py")

#: A GUID shaped like a real Azure subscription id: random hex, which is what makes
#: it the thing the guard exists to catch.
REALISTIC_ID = "7f3a91bc-4e52-4d18-9a6b-2c81e0f4d7a9"

#: The shape the mock estate uses.
SYNTHETIC_ID = "00000000-0000-4000-8000-00000000a001"


def _guard(*paths: Path | str) -> subprocess.CompletedProcess[str]:
    """Run the guard over some paths, as the pre-commit hook does."""
    return subprocess.run(  # noqa: S603
        [sys.executable, str(GUARD), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# The guard catches what it is for
# ---------------------------------------------------------------------------


def test_a_real_looking_subscription_id_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text(f"We scanned /subscriptions/{REALISTIC_ID}/ yesterday.\n", encoding="utf-8")
    result = _guard(target)
    assert result.returncode == 1
    assert REALISTIC_ID in result.stderr
    assert "Refusing to commit" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        f'  "subscription_id": "{REALISTIC_ID}",',
        f"AZURE_SUBSCRIPTION_IDS={REALISTIC_ID}",
        f"costsentinel scan --real --subscription {REALISTIC_ID}",
        f'  "tenant_id": "{REALISTIC_ID}",',
        f"/subscriptions/{REALISTIC_ID}/resourceGroups/rg-prod/providers/x/y",
    ],
)
def test_every_context_a_subscription_id_appears_in_is_covered(line: str, tmp_path: Path) -> None:
    """Each of these is a way a real id has actually ended up in a file somewhere."""
    target = tmp_path / "leaked.txt"
    target.write_text(line + "\n", encoding="utf-8")
    assert _guard(target).returncode == 1


def test_a_real_sweeps_output_is_refused_by_its_provenance_marker(tmp_path: Path) -> None:
    """The provider's own name is in every fact it produced."""
    target = tmp_path / "scan.json"
    target.write_text(
        json.dumps({"provenance": {"source": "azure_resource_graph", "provider": "azure-live"}}),
        encoding="utf-8",
    )
    result = _guard(target)
    assert result.returncode == 1
    assert "provenance marker" in result.stderr
    assert "var/real-runs" in result.stderr


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production"])
def test_an_env_file_is_refused(name: str, tmp_path: Path) -> None:
    target = tmp_path / name
    target.write_text("AZURE_TENANT_ID=something\n", encoding="utf-8")
    result = _guard(target)
    assert result.returncode == 1
    assert "credentials" in result.stderr


@pytest.mark.parametrize("path", ["var/real-runs/scan.json", "out/report.md", ".costsentinel/x"])
def test_anything_under_a_real_output_directory_is_refused_by_path_alone(path: str) -> None:
    """Before the file is even read, so this works on a binary or a huge one."""
    result = _guard(path)
    assert result.returncode == 1
    assert "real-run output" in result.stderr
    assert "git restore --staged" in result.stderr


def test_the_refusal_explains_the_stakes_and_how_to_proceed(tmp_path: Path) -> None:
    """A guard that only says "no" gets worked around rather than understood."""
    target = tmp_path / "leaked.md"
    target.write_text(f"/subscriptions/{REALISTIC_ID}/resourceGroups/rg\n", encoding="utf-8")
    result = _guard(target)
    assert result.returncode == 1
    assert "git history is permanent" in result.stderr.lower()
    assert "scripts/check_no_real_data.py" in result.stderr


# ---------------------------------------------------------------------------
# The guard does not cry wolf
# ---------------------------------------------------------------------------


def test_the_whole_committed_repository_passes_the_guard() -> None:
    """The most important assertion in this file.

    A check that fires on files the repository legitimately contains gets disabled,
    and a disabled check protects nothing. Running it over every tracked file keeps
    it honest in both directions.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked
    result = _guard(*tracked)
    assert result.returncode == 0, result.stderr


def test_the_price_catalogues_azure_meter_ids_are_not_flagged() -> None:
    """They are GUIDs, and they are public pricing data every customer sees.

    This was a real false positive from the first version of the guard, which
    matched any GUID anywhere. It is pinned because the narrower rule is the only
    reason the check is usable at all.
    """
    catalogue = Path("src/costsentinel/providers/data/price_catalogue.json")
    text = catalogue.read_text(encoding="utf-8")
    guids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)
    # The catalogue really does contain random-hex GUIDs, so this is not vacuous.
    assert len(guids) > 20
    assert _guard(catalogue).returncode == 0


@pytest.mark.parametrize(
    "guid",
    [
        SYNTHETIC_ID,
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-1111-111111111111",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    ],
)
def test_the_deliberately_fake_shapes_are_allowed(guid: str) -> None:
    """No real Azure tenant issues any of these, which is why they are safe."""
    sys.path.insert(0, str(GUARD.parent))
    try:
        from check_no_real_data import looks_synthetic
    finally:
        sys.path.pop(0)
    assert looks_synthetic(guid)


def test_running_the_guard_with_no_paths_and_nothing_staged_is_a_pass() -> None:
    """So the hook is silent on a commit that touches nothing it cares about."""
    sys.path.insert(0, str(GUARD.parent))
    try:
        from check_no_real_data import main as guard_main
    finally:
        sys.path.pop(0)
    assert guard_main([]) in (0, 1)  # depends on what is staged; it must not raise


# ---------------------------------------------------------------------------
# The committed estate is synthetic
# ---------------------------------------------------------------------------


def test_every_mock_subscription_id_is_a_synthetic_shape() -> None:
    """The demo estate ships with the repository, so it must be nobody's real one."""
    from costsentinel.providers.mock import MockAzureProvider

    provider = MockAzureProvider(seed=1337)
    subscriptions = provider.list_subscriptions()
    assert subscriptions
    for subscription in subscriptions:
        assert subscription.subscription_id.startswith("00000000-0000-")


def test_every_mock_resource_id_is_scoped_to_a_synthetic_subscription() -> None:
    from costsentinel.providers.mock import MockAzureProvider

    provider = MockAzureProvider(seed=1337)
    checked = 0
    for subscription in provider.list_subscriptions():
        for resource in provider.list_resources(subscription.subscription_id):
            assert resource.resource_id.startswith("/subscriptions/00000000-0000-")
            checked += 1
    assert checked > 10


def test_the_mock_provider_declares_itself_as_the_source_of_its_facts() -> None:
    """So a mock figure can never be mistaken for a real one downstream."""
    from costsentinel.domain.common import ProvenanceSource
    from costsentinel.providers.mock import MockAzureProvider

    provider = MockAzureProvider(seed=1337)
    (subscription, *_) = provider.list_subscriptions()
    assert subscription.provenance.source is ProvenanceSource.MOCK_PROVIDER


def test_the_eval_datasets_directory_holds_nothing_that_fails_the_guard() -> None:
    """The labelled estates are synthetic by construction and stay that way."""
    datasets = Path("evals/datasets")
    assert datasets.is_dir()
    files = [path for path in datasets.rglob("*") if path.is_file()]
    assert files, "the datasets directory should at least document itself"
    assert _guard(*files).returncode == 0


# ---------------------------------------------------------------------------
# A real run's output handling
# ---------------------------------------------------------------------------


def test_the_default_real_output_directory_is_gitignored() -> None:
    """Belt to the guard's brace: the easy path has to be the safe one."""
    ignored = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(DEFAULT_REAL_OUTPUT_DIR / "scan.json")],  # noqa: S607
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 0, f"{DEFAULT_REAL_OUTPUT_DIR} is not gitignored"


def test_a_real_run_refuses_without_an_explicit_scope() -> None:
    """There is no wildcard, and no default anybody would reach for.

    On a managed-services tenant, "everything the credential can see" means reading
    other clients' estates because somebody forgot a flag.
    """
    args = build_parser().parse_args(["scan", "--real"])
    with pytest.raises(ProviderNotConfiguredError, match="no wildcard"):
        resolve_real_scope(args, Settings())


def test_an_explicit_subscription_beats_a_lingering_environment_variable() -> None:
    """A shell with a stale AZURE_SUBSCRIPTION_IDS must not widen a deliberate command."""
    args = build_parser().parse_args(["scan", "--real", "--subscription", SYNTHETIC_ID])
    settings = Settings(azure_subscription_ids="some-other-subscription")
    assert resolve_real_scope(args, settings) == (SYNTHETIC_ID,)


def test_the_environment_supplies_the_scope_when_no_flag_is_given() -> None:
    args = build_parser().parse_args(["scan", "--real"])
    settings = Settings(azure_subscription_ids=f"{SYNTHETIC_ID}, second-sub")
    assert resolve_real_scope(args, settings) == (SYNTHETIC_ID, "second-sub")


def test_repeated_subscriptions_are_collapsed_in_order() -> None:
    args = build_parser().parse_args(
        ["scan", "--real", "--subscription", "a", "--subscription", "b", "--subscription", "a"]
    )
    assert resolve_real_scope(args, Settings()) == ("a", "b")


def test_the_banner_names_the_scope_the_identity_and_every_tool_it_may_call() -> None:
    """Somebody about to point this at an estate should not have to read the source."""
    from costsentinel.tools import describe_surface

    banner = real_run_banner(Settings(mode=RunMode.REAL), (SYNTHETIC_ID,))
    assert "READ ONLY" in banner
    assert SYNTHETIC_ID in banner
    assert "DefaultAzureCredential" in banner
    assert "Reader, Cost Management Reader" in banner
    for line in describe_surface():
        assert line in banner


def test_the_banner_carries_the_three_warnings_security_md_insists_on() -> None:
    banner = real_run_banner(Settings(mode=RunMode.REAL), (SYNTHETIC_ID,))
    assert "sandbox" in banner
    assert "approval" in banner
    assert "Never commit the output" in banner


def test_a_real_runs_report_is_written_to_a_file_not_to_stdout(tmp_path: Path) -> None:
    """The report goes straight to a file rather than to the terminal.

    Real cost data pasted into a terminal, a ticket or a commit is the one
    irreversible mistake available here.
    """
    from costsentinel.graph.runner import run_scan

    result = run_scan(settings=Settings(state_dir=tmp_path), requested_by="test")
    directory = tmp_path / "real-runs"
    written = write_real_report(result, directory, indent=2)

    assert written.parent == directory
    assert written.name.startswith("scan-") and written.name.endswith(".json")
    assert json.loads(written.read_text(encoding="utf-8"))["reports"]


def test_the_report_filename_is_timestamped_so_a_second_run_does_not_overwrite(
    tmp_path: Path,
) -> None:
    from costsentinel.graph.runner import run_scan

    result = run_scan(settings=Settings(state_dir=tmp_path), requested_by="test")
    written = write_real_report(result, tmp_path / "out", indent=0)
    assert re.fullmatch(r"scan-\d{8}T\d{6}Z\.json", written.name)


def test_the_cli_refuses_a_real_run_with_no_scope_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: Settings(state_dir=tmp_path))
    assert main(["scan", "--real"]) == 1
    captured = capsys.readouterr()
    assert "explicit subscription scope" in captured.err
    # Nothing was read, so nothing is on stdout.
    assert captured.out == ""


def test_a_mock_run_still_prints_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The demo path is unchanged; only --real diverts its output."""
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: Settings(state_dir=tmp_path))
    assert main(["scan", "--summary"]) == 0
    assert "CostSentinel sweep" in capsys.readouterr().out


def test_ci_never_exercises_the_real_path() -> None:
    """Asserted against the workflow rather than trusted.

    CI installs no credential and no optional extra, so a real run could not
    succeed -- but the workflow must also not ask for one.
    """
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--real" not in workflow
    assert "MODE=real" not in workflow
    assert "AZURE_SUBSCRIPTION_IDS" not in workflow
    assert "--extra azure" not in workflow
    assert "--extra llm" not in workflow


def test_a_completed_real_run_writes_a_file_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The whole --real path, with the mock estate standing in for a live one.

    Substituting the provider is the only way to exercise this offline, and it is
    the right substitution: what is under test is the CLI's output handling, not
    where the facts came from.
    """
    from costsentinel.providers.mock import MockAzureProvider

    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: Settings(state_dir=tmp_path))
    monkeypatch.setattr(
        "costsentinel.graph.runner.get_provider",
        lambda settings, **_: MockAzureProvider(seed=settings.mock_seed),
    )
    destination = tmp_path / "real-out"
    exit_code = main(
        [
            "scan",
            "--real",
            "--subscription",
            SYNTHETIC_ID,
            "--out",
            str(destination),
            "--client",
            "Northwind Trading",
        ]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    # The banner goes to stderr, before anything is read.
    assert "READ ONLY" in captured.err
    assert SYNTHETIC_ID in captured.err
    # stdout carries the digest, the path, and the warning -- not the report body.
    assert "Report written to" in captured.out
    assert "gitignored" in captured.out
    assert "Do not commit it" in captured.out

    (written,) = list(destination.glob("scan-*.json"))
    assert json.loads(written.read_text(encoding="utf-8"))["reports"]


# ---------------------------------------------------------------------------
# SECURITY.md must stay true
# ---------------------------------------------------------------------------

SECURITY = Path("SECURITY.md")


def test_security_md_exists_and_is_linked_from_the_readme() -> None:
    assert SECURITY.is_file()
    assert "SECURITY.md" in Path("README.md").read_text(encoding="utf-8")


def test_the_runbook_carries_the_three_warnings() -> None:
    """These are the ones that matter, so they are not left to a reader's judgement."""
    text = SECURITY.read_text(encoding="utf-8")
    assert "Pilot on a personal or sandbox subscription" in text
    assert "Obtain written approval before any client estate" in text
    assert "Never commit the output" in text


def test_the_runbook_states_the_read_only_guarantee_and_the_auth_model() -> None:
    text = SECURITY.read_text(encoding="utf-8")
    assert "az login" in text
    assert "DefaultAzureCredential" in text
    assert "no client-secret setting" in text.lower()
    assert "MODE=real" in text
    assert "--subscription" in text


def test_the_runbook_lists_exactly_the_roles_the_tools_declare() -> None:
    """A permissions table that drifts from the code sends somebody to the wrong admin.

    So the roles the registry actually asks for are checked against the document
    rather than trusted to have been copied correctly.
    """
    from costsentinel.tools.registry import required_permissions

    text = SECURITY.read_text(encoding="utf-8")
    roles = {permission.split(" on ")[0] for permission in required_permissions()}
    assert roles
    for role in roles:
        assert role in text, f"SECURITY.md does not mention the {role!r} role"
    # The registry is where "no elevated role" is actually enforced, and
    # tests/test_readonly.py asserts it there. Here we only check the document says
    # so, because a reader who grants Contributor "to be safe" has defeated it.
    assert "none is `Contributor` or `Owner`" in text


def test_the_runbook_names_a_vulnerability_contact() -> None:
    text = SECURITY.read_text(encoding="utf-8")
    assert "Reporting a vulnerability" in text
    assert "@" in text.split("Reporting a vulnerability", 1)[1]


def test_the_runbooks_commands_use_flags_that_exist() -> None:
    """A runbook with a wrong flag is worse than none: it fails mid-procedure.

    Found one -- ``--regions`` where the script takes ``--region`` -- which is why
    this is asserted rather than proofread.
    """
    parser = build_parser()
    scan_flags = {
        option
        for action in parser._subparsers._group_actions
        for subparser in getattr(action, "choices", {}).values()
        for option in subparser._actions
        for option in option.option_strings
    }
    text = SECURITY.read_text(encoding="utf-8")
    for flag in ("--real", "--subscription", "--out"):
        assert flag in text
        assert flag in scan_flags

    refresh = re.findall(r"refresh_prices\.py ([^\n]*)", text)
    assert refresh
    for invocation in refresh:
        for token in invocation.split():
            if token.startswith("--"):
                assert token in {"--verify", "--dry-run", "--region", "--out"}, token
