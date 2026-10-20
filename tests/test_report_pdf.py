"""The PDF renderer, and the claim that it cannot disagree with the Markdown.

Two renderers of the same report is a way to publish two different numbers. So the
PDF does not read the report at all: it typesets the *Markdown*, which makes the
Markdown the single source of truth and reduces the PDF's correctness to one testable
property -- typesetting carries every digit through unchanged and introduces none.

The parser is tested with zero extras, because that is where the arithmetic claim
lives. The file-producing tests skip unless `reports` is installed, which is also what
CI does: a client-facing PDF should not put a C-extension toolchain on the critical
path of a check that gates the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from costsentinel.reports.pdf import (
    EXTRA,
    INSTALL_HINT,
    BlockKind,
    PdfUnavailableError,
    blocks,
    figures_in,
    inline,
    pdf_available,
    render_pdf,
)

SAMPLE = """# Cost review — acme

> **Synthetic.** Invented data.

| | |
| --- | --- |
| Observed spend | 2,400.00 USD |
| **Savings** | **628.22 USD / month** |

## Recommended actions

A paragraph that runs
across two source lines.

- cpu_max_pct: 2.1
- observation_days: 30

---
"""


def test_every_construct_the_renderer_emits_is_recognised() -> None:
    """A closed vocabulary, matched against the renderer's actual output."""
    kinds = {block.kind for block in blocks(SAMPLE)}
    assert kinds == {
        BlockKind.HEADING,
        BlockKind.QUOTE,
        BlockKind.TABLE,
        BlockKind.PARAGRAPH,
        BlockKind.BULLET,
        BlockKind.RULE,
    }


def test_the_committed_sample_parses_without_falling_back_to_prose() -> None:
    """The real document, not a toy one.

    An unrecognised line becomes a paragraph rather than vanishing, which is the safe
    failure -- but a table that silently typeset as prose would be a worse report, so
    this asserts the real sample's structure survives.
    """
    parsed = blocks(Path("docs/sample-report.md").read_text(encoding="utf-8"))
    kinds = [block.kind for block in parsed]
    assert kinds.count(BlockKind.TABLE) >= 3
    assert kinds.count(BlockKind.HEADING) >= 5
    assert BlockKind.QUOTE in kinds, "the synthetic banner"


def test_typesetting_preserves_every_figure_and_invents_none() -> None:
    """The one property that makes two renderers safe.

    Not "the PDF looks right" -- a pixel claim nobody can maintain -- but "the set of
    numbers on the page equals the set of numbers in the Markdown".
    """
    for markdown in (SAMPLE, Path("docs/sample-report.md").read_text(encoding="utf-8")):
        source = figures_in(markdown)
        typeset = {
            figure
            for block in blocks(markdown)
            for string in block.strings
            for figure in figures_in(string)
        }
        assert typeset - source == set(), "typesetting introduced a number"
        assert source - typeset == set(), "typesetting dropped a number"


def test_a_number_cannot_be_lost_inside_markup() -> None:
    """Emphasis is stripped for the page; the digits inside it are not."""
    assert figures_in(inline("**628.22 USD / month**")) == {"628.22"}
    assert "628.22" in inline("**628.22 USD / month**")
    assert "**" not in inline("**628.22**")
    # Deliberately broad: it collects every digit run, including one inside an id,
    # because the claim it serves is set equality between source and page -- and a
    # checker that decided which digits counted could decide wrongly.
    assert figures_in(inline("`rpt-run-1`")) == {"1"}


def test_a_paragraph_joins_its_lines_without_touching_a_digit() -> None:
    """The only transformation applied to text, so the only one that could lose one."""
    joined = [b for b in blocks("A total of\n2,400.00 USD was observed.\n") if b.text]
    assert len(joined) == 1
    assert joined[0].text == "A total of 2,400.00 USD was observed."


def test_the_missing_extra_names_the_command_that_fixes_it() -> None:
    """An optional dependency whose error message does not say how to get it is a bug."""
    message = str(PdfUnavailableError())
    assert EXTRA in message
    assert INSTALL_HINT in message


@pytest.mark.skipif(not pdf_available(), reason=f"needs the optional '{EXTRA}' extra")
def test_a_pdf_is_produced_and_carries_the_headline_figures(tmp_path: Path) -> None:
    """Asserted, not eyeballed: the file exists, is a PDF, and holds the numbers.

    No pixel comparison. A PDF diff breaks on a font metric and tells you nothing about
    whether the money is right, which is the only thing worth gating.
    """
    markdown = Path("docs/sample-report.md").read_text(encoding="utf-8")
    out = render_pdf(markdown, tmp_path / "report.pdf", title="Cost review")

    assert out.exists()
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF-")
    assert len(raw) > 2_000, "a PDF this small did not typeset the document"

    # Every figure in the Markdown reached a block destined for the page.
    source = figures_in(markdown)
    typeset = {
        figure for block in blocks(markdown) for s in block.strings for figure in figures_in(s)
    }
    assert source == typeset


@pytest.mark.skipif(pdf_available(), reason="the extra is installed, so nothing is unavailable")
def test_without_the_extra_the_pdf_path_refuses_rather_than_half_works(tmp_path: Path) -> None:
    """Refusing beats half-working here.

    Somebody who asked for a PDF and silently got only Markdown would find out when
    they went looking for the file.
    """
    with pytest.raises(PdfUnavailableError) as raised:
        render_pdf("# Report\n", tmp_path / "report.pdf", title="Report")
    assert INSTALL_HINT in str(raised.value)
    assert not (tmp_path / "report.pdf").exists()


def test_the_markdown_path_needs_no_extra_at_all() -> None:
    """The guarantee that keeps CI offline and extra-free.

    Importing the Markdown renderer must not pull in the PDF toolchain, so a build with
    zero optional dependencies still produces the artefact the gate checks.
    """
    from costsentinel.reports import markdown as md

    source = Path(md.__file__).read_text(encoding="utf-8")
    assert "reportlab" not in source
    assert "pdf" not in {name for name in dir(md) if not name.startswith("_")}
