# pyright: reportMissingModuleSource=false
# ReportLab is an optional extra and CI does not install it, so pyright resolves its
# bundled stubs without the source behind them. Suppressed for this file only -- the
# one module that touches the extra -- rather than relaxed across src.
"""Typesetting the Markdown report as PDF.

**The PDF is a typesetting of the Markdown, not a second rendering of the report.**
That is the whole design. Two renderers reading the same `ClientReport` would be two
places a figure could be formatted, and they would drift the first time somebody fixed
one of them -- so this one never sees the report at all. It takes the Markdown string
and copies spans of text out of it, which makes "the PDF cannot contain a figure the
Markdown does not" true by construction (ARCHITECTURE.md D111).

**No Markdown library.** A general parser would be a second dependency and would also
be answering a harder question than this needs: the Markdown here is *ours*, with six
constructs and no nesting, because :mod:`costsentinel.reports.markdown` wrote it.
:func:`blocks` is a line reader over a grammar we own. The gain is not brevity -- it is
that the reader has no behaviour the renderer did not ask for.

**The parse is testable with zero extras.** :func:`blocks` imports nothing optional,
so the part that could lose or mangle a figure is exercised in CI, and only the final
"write bytes to a file" step needs the ``reports`` extra. The risky half is the half
that is always tested.

**ReportLab rather than an HTML pipeline.** WeasyPrint produces better-looking
documents and needs Pango and Cairo installed at the system level, which makes
"generate the client's PDF" a platform problem on a laptop. ReportLab is pure Python,
installs anywhere a wheel does, and never touches the network -- and a document
containing a client's spend must not be assembled by a hosted service.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from costsentinel.domain.common import Frozen

if TYPE_CHECKING:  # pragma: no cover - typing only
    from reportlab.platypus import Flowable

#: The optional extra that supplies the toolchain, named in the error a user sees.
EXTRA = "reports"

#: Install command quoted back when the extra is missing. A message that says
#: "reportlab is not installed" leaves somebody guessing at the extra's name.
INSTALL_HINT = "uv sync --extra reports"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^-\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|[\s\-|]+\|$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_CODE = re.compile(r"`([^`]+)`")
_DIGITS = re.compile(r"\d[\d,.]*")


class PdfUnavailableError(RuntimeError):
    """The PDF toolchain is not installed.

    Raised with the install command rather than letting an ``ImportError`` surface
    from three frames down, because the fix is one command and the user should not
    have to read a traceback to find it.
    """

    def __init__(self) -> None:
        """Name the extra and the command that installs it."""
        super().__init__(
            f"PDF rendering needs the optional '{EXTRA}' extra, which is not "
            f"installed. Install it with `{INSTALL_HINT}`. The Markdown report needs "
            f"no extras and is always available."
        )


class BlockKind(StrEnum):
    """The six things the Markdown report is made of.

    Closed, like every other vocabulary in the system, and for the same reason: a
    construct the renderer did not emit is a construct this reader should refuse to
    invent a meaning for.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    BULLET = "bullet"
    TABLE = "table"
    QUOTE = "quote"
    RULE = "rule"


class Block(Frozen):
    """One typesettable piece of the document."""

    kind: BlockKind
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()

    @property
    def strings(self) -> tuple[str, ...]:
        """Every piece of text this block will put on the page.

        What the figure-preservation test reads: a block's strings must all come from
        the Markdown, so the typesetting cannot introduce a number.
        """
        if self.kind is BlockKind.TABLE:
            return tuple(cell for row in self.rows for cell in row)
        return (self.text,) if self.text else ()


def blocks(markdown: str) -> tuple[Block, ...]:
    """Read our own Markdown into typesettable blocks.

    Deliberately strict about what it recognises and deliberately lossless about text:
    a line it does not recognise becomes a paragraph rather than being dropped, because
    silently losing a sentence from a client's report is worse than typesetting it
    plainly.

    Consecutive non-blank prose lines join into one paragraph with single spaces, which
    is the only transformation applied to text -- and it cannot change a digit.
    """
    found: list[Block] = []
    paragraph: list[str] = []
    table: list[tuple[str, ...]] = []

    def flush_paragraph() -> None:
        if paragraph:
            found.append(Block(kind=BlockKind.PARAGRAPH, text=" ".join(paragraph)))
            paragraph.clear()

    def flush_table() -> None:
        if table:
            found.append(Block(kind=BlockKind.TABLE, rows=tuple(table)))
            table.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()

        if not line:
            flush_paragraph()
            flush_table()
            continue

        if line.startswith("|"):
            flush_paragraph()
            if _TABLE_SEPARATOR.match(line):
                continue
            cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
            table.append(cells)
            continue

        flush_table()

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            found.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=heading.group(2).strip(),
                    level=len(heading.group(1)),
                )
            )
            continue

        if line == "---":
            flush_paragraph()
            found.append(Block(kind=BlockKind.RULE))
            continue

        if line.startswith(">"):
            flush_paragraph()
            found.append(Block(kind=BlockKind.QUOTE, text=line.lstrip("> ").strip()))
            continue

        bullet = _BULLET.match(line)
        if bullet:
            flush_paragraph()
            found.append(Block(kind=BlockKind.BULLET, text=bullet.group(1).strip()))
            continue

        paragraph.append(line)

    flush_paragraph()
    flush_table()
    return tuple(found)


def inline(text: str) -> str:
    """Markdown emphasis as the mini-markup ReportLab's paragraphs understand.

    Escapes first, then marks up, so a resource named ``<script>`` is text rather than
    tags. Only emphasis is translated and digits are never touched, which is what
    keeps :func:`figures_in` able to compare the typeset text against the Markdown.
    """
    escaped = html.escape(text, quote=False)
    marked = _BOLD.sub(r"<b>\1</b>", escaped)
    marked = _ITALIC.sub(r"<i>\1</i>", marked)
    return _CODE.sub(r'<font face="Courier">\1</font>', marked)


def figures_in(text: str) -> set[str]:
    """Every numeric run in a piece of text.

    Broader than the Markdown renderer's money pattern on purpose. This one is asked a
    different question -- "did the typesetting invent or mangle a digit anywhere?" --
    and for that, every number is in scope, including the dates and the counts.
    """
    return set(_DIGITS.findall(text))


def pdf_available() -> bool:
    """Whether the optional toolchain is importable.

    Used by the CLI to decide whether to offer a PDF, and by the tests to skip the
    ones that genuinely cannot run. A skip with a reason is honest; pretending the
    feature was exercised would not be.
    """
    from importlib.util import find_spec  # noqa: PLC0415 -- optional extra, imported lazily

    try:
        return find_spec("reportlab") is not None
    except ModuleNotFoundError:  # pragma: no cover - a partially installed parent
        return False


def _flowables(parsed: Sequence[Block], *, title: str) -> list[Flowable]:
    """Turn blocks into ReportLab flowables.

    Imported lazily inside the function: this module must be importable, and
    :func:`blocks` fully usable, with the extra absent.
    """
    from reportlab.lib import colors  # noqa: PLC0415 -- optional extra, imported lazily
    from reportlab.lib.enums import TA_LEFT  # noqa: PLC0415 -- optional extra, imported lazily
    from reportlab.lib.styles import (  # noqa: PLC0415 -- optional extra, imported lazily
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import mm  # noqa: PLC0415 -- optional extra, imported lazily
    from reportlab.platypus import (  # noqa: PLC0415 -- optional extra, imported lazily
        HRFlowable,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    sheet = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body",
        parent=sheet["BodyText"],
        fontSize=9.5,
        leading=13,
        alignment=TA_LEFT,
        spaceAfter=4,
    )
    quote = ParagraphStyle(
        "Quote",
        parent=body,
        leftIndent=8,
        borderPadding=4,
        backColor=colors.HexColor("#fff4e5"),
        spaceBefore=4,
        spaceAfter=8,
    )
    bullet = ParagraphStyle("Bullet", parent=body, leftIndent=12, bulletIndent=2)
    headings = {
        1: ParagraphStyle("H1", parent=sheet["Heading1"], fontSize=17, spaceAfter=8),
        2: ParagraphStyle("H2", parent=sheet["Heading2"], fontSize=13, spaceBefore=10),
        3: ParagraphStyle("H3", parent=sheet["Heading3"], fontSize=11, spaceBefore=8),
    }
    cell = ParagraphStyle("Cell", parent=body, fontSize=9, leading=11, spaceAfter=0)

    table_style = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d0d0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f7f7f7")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )

    out: list[Flowable] = []
    for block in parsed:
        if block.kind is BlockKind.HEADING:
            style = headings.get(min(block.level, 3), headings[3])
            out.append(Paragraph(inline(block.text), style))
        elif block.kind is BlockKind.PARAGRAPH:
            out.append(Paragraph(inline(block.text), body))
        elif block.kind is BlockKind.QUOTE:
            out.append(Paragraph(inline(block.text), quote))
        elif block.kind is BlockKind.BULLET:
            out.append(Paragraph(inline(block.text), bullet, bulletText="•"))
        elif block.kind is BlockKind.TABLE:
            columns = max(len(row) for row in block.rows)
            data = [
                [Paragraph(inline(value), cell) for value in (*row, *[""] * (columns - len(row)))]
                for row in block.rows
            ]
            widths = [62 * mm] + [(160 - 62) / max(1, columns - 1) * mm] * (columns - 1)
            out.append(Table(data, colWidths=widths[:columns], style=table_style, hAlign="LEFT"))
            out.append(Spacer(1, 5))
        elif block.kind is BlockKind.RULE:
            out.append(Spacer(1, 4))
            out.append(HRFlowable(width="100%", thickness=0.4, color=colors.HexColor("#cccccc")))
            out.append(Spacer(1, 4))
    _ = title
    return out


def render_pdf(markdown: str, path: Path, *, title: str) -> Path:
    """Typeset a Markdown report to a PDF file.

    Args:
        markdown: The document from
            :func:`costsentinel.reports.markdown.render_markdown`. The only source --
            nothing here reads the report object.
        path: Where to write. Parent directories are created.
        title: Document title for the PDF metadata.

    Returns:
        The path written.

    Raises:
        PdfUnavailableError: If the ``reports`` extra is not installed.
    """
    if not pdf_available():
        raise PdfUnavailableError
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415 -- optional extra, imported lazily
    from reportlab.lib.units import mm  # noqa: PLC0415 -- optional extra, imported lazily
    from reportlab.platypus import SimpleDocTemplate  # noqa: PLC0415 -- optional extra

    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        title=title,
        author="CostSentinel",
        subject="Cost review",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    document.build(_flowables(blocks(markdown), title=title))
    return path
