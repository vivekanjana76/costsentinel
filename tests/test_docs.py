"""Guards on the documents a first-time reader checks.

The README is the front door, and every claim on it links to a file or a test. A link
that stops resolving, a test that gets renamed, or a transcript that no longer matches
what the code prints all turn a backed claim into an unbacked one without anybody
editing the claim. These tests make that drift a failure rather than a surprise.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "docs" / "demo"

# What legitimately differs between two runs of the same demo: the sweep's run id, the
# audit event id (which hashes the moment of the decision), and the decision date.
# Everything else -- every id, figure, rank and annotation -- must match exactly.
_VOLATILE = (
    (re.compile(r"\brun-[0-9a-f]{12}\b"), "run-<id>"),
    (re.compile(r"\boutcome-[0-9a-f]{16}-audit\b"), "outcome-<id>-audit"),
    (re.compile(r"(?<=declined on )\d{4}-\d{2}-\d{2}"), "<date>"),
    (re.compile(r"(?<= on )\d{4}-\d{2}-\d{2}(?=[.:])"), "<date>"),
)


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return text


def _run_cli(command: str, state_dir: Path) -> str:
    """Run one transcript command as its own OS process, as `scripts/demo.sh` does.

    The working directory is the empty state directory's parent, so a developer's
    `.env` cannot leak into the comparison.
    """
    argv = shlex.split(command)
    assert argv[0] == "costsentinel", command
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COSTSENTINEL_")
        and key not in {"MODE", "LLM_BACKEND", "STATE_DIR", "LOG_LEVEL"}
    }
    env |= {
        "MODE": "mock",
        "LLM_BACKEND": "fake",
        "LOG_LEVEL": "warning",
        "STATE_DIR": str(state_dir),
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(  # noqa: S603 -- argv from a committed transcript, interpreter fixed
        [sys.executable, "-m", "costsentinel.cli", *argv[1:]],
        cwd=state_dir.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout
    return done.stdout


def _replay(transcript: str, state_dir: Path) -> str:
    """Rebuild a transcript by running its `$` lines and keeping its `#` comments.

    The layout is the one `scripts/demo.sh` writes: comment lines, a blank line after
    the header, and per command `$ command`, its output, and a blank line. Output lines
    never start with `#`, which is how the end of one command's output is found.
    """
    rebuilt: list[str] = []
    in_output = False
    for line in transcript.replace("\r\n", "\n").splitlines():
        if line.startswith("$ "):
            rebuilt.append(line + "\n" + _run_cli(line[2:], state_dir) + "\n")
            in_output = True
        elif line.startswith("#"):
            rebuilt.append(line + "\n")
            in_output = False
        elif not in_output and not line:
            rebuilt.append("\n")
    return "".join(rebuilt)


# ---------------------------------------------------------------------------
# The committed demo transcripts are what the code prints today
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["approval-resume.txt", "learning-loop.txt"])
def test_a_committed_transcript_is_what_separate_processes_print_today(
    name: str, tmp_path: Path
) -> None:
    """A transcript nobody re-runs goes stale the first time an id or a figure moves.

    Each `$` line runs as its own subprocess sharing only a state directory, so for
    the approval transcript this is also the cross-process proof the README cites: a
    sweep paused by one process is listed and resumed by later ones.
    """
    committed = (DEMO / name).read_text(encoding="utf-8")
    state_dir = tmp_path / "demo-state"

    rebuilt = _replay(committed, state_dir)

    assert _normalise(rebuilt) == _normalise(committed), (
        f"docs/demo/{name} no longer matches what the CLI prints; "
        "regenerate it with `sh scripts/demo.sh` and review the diff"
    )


def test_the_learning_loop_transcript_demotes_the_decline_and_moves_no_figure() -> None:
    """The README quotes this transcript as the learning claim, so it must show it.

    The declined finding must rank lower on the second sweep with its annotation,
    while every saving and the headline totals are unchanged.
    """
    transcript = (DEMO / "learning-loop.txt").read_text(encoding="utf-8")
    first, second = transcript.split("$ costsentinel scan")[1:3]

    def ranked(sweep: str) -> list[tuple[str, str]]:
        return re.findall(r"^\s+\d+\. (\S+) .* saves (\S+) USD/month$", sweep, re.MULTILINE)

    def headline(sweep: str) -> list[str]:
        return re.findall(r"^\s+projected savings\s+(.+)$", sweep, re.MULTILINE)

    before, after = ranked(first), ranked(second)
    assert sorted(before) == sorted(after), "a figure moved, or a finding vanished"
    assert headline(first) == headline(second)
    assert before[0][0] == "vm-analytics-01"
    assert after.index(before[0]) > 0, "the declined finding was not demoted"
    assert "Previously declined on" in second
    assert "(was #1 on merit)" in second


def test_the_demo_script_writes_the_transcripts_that_are_committed() -> None:
    """The README says "regenerate with scripts/demo.sh"; the script must agree on where."""
    script = (ROOT / "scripts" / "demo.sh").read_text(encoding="utf-8")
    for transcript in sorted(DEMO.glob("*.txt")):
        assert f'"$OUT/{transcript.name}"' in script, transcript.name


# ---------------------------------------------------------------------------
# The licence is stated once
# ---------------------------------------------------------------------------


def test_the_licence_file_and_the_package_metadata_agree() -> None:
    """The package said "Proprietary" while the repository was public with no licence.

    A wheel's metadata and the LICENSE file are both read as the terms, so they must
    say the same thing.
    """
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert licence.startswith("MIT License")
    assert pyproject["project"]["license"] == {"text": "MIT"}
    assert re.search(r"^Copyright \(c\) \d{4} \S", licence, re.MULTILINE)


# ---------------------------------------------------------------------------
# The README is a front door, and every door on it opens
# ---------------------------------------------------------------------------

README = ROOT / "README.md"
GUIDE = ROOT / "docs" / "guide.md"

_LINK = re.compile(r"\]\(([^)\s]+)\)")
_CITED_TEST = re.compile(r"\[`(test_\w+)`\]\((tests/[\w/]+\.py)\)")


def _relative_links(doc: Path) -> list[str]:
    return [
        target
        for target in _LINK.findall(doc.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]


@pytest.mark.parametrize("doc", [README, GUIDE], ids=lambda p: p.name)
def test_every_relative_link_resolves_to_a_committed_file(doc: Path) -> None:
    """A claim whose evidence link 404s is an unbacked claim that still looks backed."""
    links = _relative_links(doc)
    assert links, f"{doc.name} has no relative links; the parser is wrong"
    missing = [
        target for target in links if not (doc.parent / target.split("#", 1)[0]).resolve().exists()
    ]
    assert not missing, f"{doc.name} links to files that do not exist: {missing}"


def test_every_test_the_readme_cites_exists_where_it_says() -> None:
    """A renamed test silently orphans the README claim that cites it as proof."""
    cited = _CITED_TEST.findall(README.read_text(encoding="utf-8"))
    assert len(cited) >= 10, "the README cites fewer tests than it should; parser broken?"
    missing = [
        f"{path}::{name}"
        for name, path in cited
        if not re.search(rf"^def {name}\(", (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert not missing, f"the README cites tests that do not exist: {missing}"


def test_the_readme_names_exactly_the_gates_that_block_a_build() -> None:
    """The gate list is the README's strongest claim; it must not drift from the harness."""
    from evals.harness.scorecard import BLOCKING_GATES

    text = README.read_text(encoding="utf-8")
    named = set(re.findall(r"\*\*`([a-z_]+\.[a-z_]+)`\*\*", text))
    assert named == set(BLOCKING_GATES)
    count = {3: "three", 4: "four", 5: "five", 6: "six"}[len(BLOCKING_GATES)]
    assert f"{count} build-blocking gates" in text


@pytest.mark.parametrize(
    "target",
    [
        "docs/sample-report.md",
        "evals/REPORT.md",
        "docs/dashboard/",
        "ARCHITECTURE.md",
        "docs/guide.md",
        "docs/demo/approval-resume.txt",
        "docs/demo/learning-loop.txt",
        "SECURITY.md",
        "LICENSE",
    ],
)
def test_nothing_load_bearing_is_more_than_one_click_from_the_readme(target: str) -> None:
    """A first-time reader should reach every reviewable artefact from the front door."""
    assert target in _relative_links(README)


def test_the_readme_quotes_the_learning_transcript_verbatim() -> None:
    """The README's transcript excerpt must be the real output, not a tidied version of it."""
    readme = README.read_text(encoding="utf-8")
    excerpt = readme.split("```text\n", 1)[1].split("```", 1)[0]
    transcript = _normalise((DEMO / "learning-loop.txt").read_text(encoding="utf-8"))
    lines = [line for line in excerpt.splitlines() if line.strip() and line.strip() != "..."]
    assert lines
    for line in lines:
        assert _normalise(line) in transcript, line
