"""Refuse to commit a real-run artefact.

This repository is public and the tool reads live cost data from client Azure
estates. Those two facts together mean one mistake is materially worse than all the
others: a real sweep's output, or a real subscription id, reaching a commit. Git
history is effectively permanent and a force-push does not help once a clone exists.

``.gitignore`` already keeps the real-run output directory out. This is the second
layer, because ``git add -f``, a stray copy into ``src/``, or a pasted snippet in a
Markdown file all get past a gitignore. Run as a pre-commit hook over the staged
files, or by hand over any path.

Four things are refused:

1. A file inside a real-run output directory.
2. A ``.env`` file other than ``.env.example``.
3. An Azure subscription or tenant id that does not look deliberately synthetic.
4. A real-provider provenance marker in a data file, which is what a committed real
   sweep's JSON looks like.

The id rule is a heuristic and says so. A real Azure subscription id is random hex;
the ones this repository legitimately contains are nil-prefixed or
single-digit-repeated, which no real tenant issues. Being wrong in the cautious
direction is correct here: a false positive costs somebody thirty seconds and a
false negative is unrecoverable.

It matches ids *in context* -- after ``/subscriptions/``, beside a
``subscription_id`` key, after ``--subscription`` -- rather than any GUID anywhere.
The first version did the latter and flagged every Azure meter id in the committed
price catalogue, which is public pricing data every customer sees. A check that
cries wolf on correct files gets disabled, so the narrower rule is the safer one.

Usage::

    uv run python scripts/check_no_real_data.py [PATH ...]

With no paths, every staged file is checked. Exits non-zero on the first file that
fails, naming the file, the line and what to do about it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

#: Directories a real run writes to. Nothing inside them may ever be committed.
REAL_OUTPUT_DIRS: tuple[str, ...] = ("var/", "out/", ".costsentinel/")

#: Any Azure-shaped GUID, as a fragment of the context patterns below.
_GUID = r"(?P<guid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"

#: Where a GUID identifies an *estate* rather than something public.
#:
#: Scanning for any GUID at all was the first attempt and it was wrong: the
#: committed price catalogue is full of real Azure meter ids, which are GUIDs and are
#: public pricing data that every Azure customer sees. What must never be committed
#: is a subscription or tenant id, so the rule matches the contexts those appear in.
#: Narrower, and therefore actually usable.
GUID_CONTEXTS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"/subscriptions/{_GUID}"),
    re.compile(rf"subscription_?ids?\b[\s\"':=\[(,]+{_GUID}", re.IGNORECASE),
    re.compile(rf"tenant_?id\b[\s\"':=]+{_GUID}", re.IGNORECASE),
    re.compile(rf"--subscription[\s=]+{_GUID}"),
    re.compile(rf"/providers/Microsoft\.Capacity/reservationOrders/{_GUID}"),
)

#: GUID shapes this repository is allowed to contain, because no real Azure tenant
#: issues them. The mock estate uses the nil-prefixed form; tests and docs use the
#: single-character-repeated and ``x``-placeholder forms.
SYNTHETIC_PREFIX = "00000000-0000-"
PLACEHOLDER_CHARS = frozenset("x")

#: Markers that only appear in the output of a real sweep. ``azure-live`` is the real
#: provider's own name, which every fact it produced carries in its provenance.
REAL_RUN_MARKERS: tuple[str, ...] = (
    "azure-live",
    "azure_resource_graph",
    "azure_cost_management",
)

#: File types the marker scan applies to. Source and test files name these strings
#: legitimately -- the provider is called ``azure-live`` in its own module -- so the
#: scan covers the data formats a report is written in.
DATA_SUFFIXES: tuple[str, ...] = (".json", ".md", ".csv", ".txt", ".yaml", ".yml")

#: Paths exempt from the marker scan because describing the thing is their job.
MARKER_EXEMPT: tuple[str, ...] = (
    "scripts/check_no_real_data.py",
    "SECURITY.md",
    "ARCHITECTURE.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "README.md",
    "CLAUDE.md",
)

#: Paths exempt from the GUID scan. Only this file, which has to contain the
#: patterns in order to look for them.
GUID_EXEMPT: tuple[str, ...] = ("scripts/check_no_real_data.py",)

#: Skipped wholesale: not text, or not ours.
SKIP_DIRS: tuple[str, ...] = (".git/", ".venv/", "node_modules/", "__pycache__/")

MAX_BYTES = 4_000_000


class Finding(NamedTuple):
    """One reason a file may not be committed."""

    path: str
    line: int
    detail: str
    remedy: str

    def render(self) -> str:
        """A single line an operator can act on."""
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {location}\n      {self.detail}\n      {self.remedy}"


def staged_files() -> list[str]:
    """Paths staged for commit, as git reports them."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _normalise(path: str) -> str:
    return path.replace("\\", "/")


def looks_synthetic(guid: str) -> bool:
    """Whether a GUID is one of the deliberately fake shapes this repo may contain.

    A real Azure subscription id is random hex. These are not, which is why they are
    safe to commit and why the rule can be this blunt.
    """
    lowered = guid.lower()
    if lowered.startswith(SYNTHETIC_PREFIX):
        return True
    digits = set(lowered.replace("-", ""))
    # A single repeated character: 11111111-1111-..., aaaaaaaa-aaaa-..., and the
    # all-zero nil GUID.
    return len(digits) == 1 or digits <= PLACEHOLDER_CHARS


def check_path(path: str) -> list[Finding]:
    """Every reason one path may not be committed."""
    normalised = _normalise(path)
    findings: list[Finding] = []

    for directory in REAL_OUTPUT_DIRS:
        if normalised.startswith(directory):
            findings.append(
                Finding(
                    path=normalised,
                    line=0,
                    detail=f"{directory} holds real-run output and runtime state",
                    remedy=(
                        f"git restore --staged {normalised} -- and do not use "
                        f"git add -f on this directory"
                    ),
                )
            )
            return findings

    name = Path(normalised).name
    if name.startswith(".env") and name != ".env.example":
        findings.append(
            Finding(
                path=normalised,
                line=0,
                detail="a .env file holds credentials",
                remedy=f"git restore --staged {normalised} -- put the shape in .env.example",
            )
        )
        return findings

    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size > MAX_BYTES:
        return findings
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings

    findings += _scan_lines(normalised, text)
    return findings


def _estate_ids(line: str) -> list[str]:
    """Every subscription or tenant id one line mentions, in any of the contexts."""
    found: list[str] = []
    for pattern in GUID_CONTEXTS:
        found += [match.group("guid") for match in pattern.finditer(line)]
    return found


def _scan_lines(normalised: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    check_guids = normalised not in GUID_EXEMPT
    check_markers = (
        normalised not in MARKER_EXEMPT and Path(normalised).suffix.lower() in DATA_SUFFIXES
    )

    for number, line in enumerate(text.splitlines(), start=1):
        if check_guids:
            findings += [
                Finding(
                    path=normalised,
                    line=number,
                    detail=f"{guid} looks like a real Azure subscription or tenant id",
                    remedy=(
                        "if this is real, remove it; if it is an example, use the "
                        "00000000-0000-4000-8000-0000000000NN form the mock estate uses"
                    ),
                )
                for guid in _estate_ids(line)
                if not looks_synthetic(guid)
            ]
        if check_markers:
            findings += [
                Finding(
                    path=normalised,
                    line=number,
                    detail=f"{marker!r} is a real-provider provenance marker",
                    remedy=(
                        "this looks like the output of a real sweep; real output "
                        "belongs in var/real-runs, which is gitignored"
                    ),
                )
                for marker in REAL_RUN_MARKERS
                if marker in line
            ]
    return findings


def check(paths: list[str]) -> list[Finding]:
    """Check every path, skipping the directories that are never ours."""
    findings: list[Finding] = []
    for path in paths:
        normalised = _normalise(path)
        if any(normalised.startswith(skip) for skip in SKIP_DIRS):
            continue
        findings += check_path(path)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        ``0`` when nothing was found, ``1`` otherwise.
    """
    arguments = list(argv if argv is not None else sys.argv[1:])
    paths = arguments or staged_files()
    if not paths:
        return 0

    findings = check(paths)
    if not findings:
        return 0

    print(
        f"\nRefusing to commit: {len(findings)} possible real-estate artefact(s).\n",
        file=sys.stderr,
    )
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nThis repository is public and this tool reads live client cost data. "
        "Git history is permanent.\nIf a finding is a false positive, see "
        "scripts/check_no_real_data.py for the allowed synthetic forms.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover -- exercised via the hook and tests
    raise SystemExit(main())
