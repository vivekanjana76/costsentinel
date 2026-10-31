"""Guards on the documents a first-time reader checks.

The README is the front door, and every claim on it links to a file or a test. A link
that stops resolving, a test that gets renamed, or a transcript that no longer matches
what the code prints all turn a backed claim into an unbacked one without anybody
editing the claim. These tests make that drift a failure rather than a surprise.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
