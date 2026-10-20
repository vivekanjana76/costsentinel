"""`costsentinel report`, and the committed sample it produces.

Three claims, in order of how much damage getting them wrong would do:

1. the sample in the repository is the current renderer's output, so a visitor reading
   it is reading what the tool produces today rather than what it produced once;
2. regenerating the sample cannot reach a real estate, whatever is in the developer's
   environment;
3. a rendered document goes somewhere gitignored unless it is that one sample.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from costsentinel.cli import (
    DEFAULT_RENDER_DIR,
    SAMPLE_CLIENT,
    SAMPLE_REPORT,
    SAMPLE_RUN_ID,
    SAMPLE_STAMP,
    main,
    render_sample,
)
from costsentinel.config import LLMBackend, MemoryBackend, RunMode
from costsentinel.reports.markdown import SYNTHETIC_BANNER, is_synthetic
from costsentinel.reports.pdf import INSTALL_HINT, pdf_available


def test_the_committed_sample_is_the_current_renderers_output(tmp_path: Path) -> None:
    """Otherwise the repository advertises an output it no longer produces.

    Regenerate with `uv run costsentinel report --sample`. Checked rather than
    generated in CI because the file is the deliverable: a visitor should be able to
    read a real output without running anything, and a stale one is worse than none.
    """
    assert SAMPLE_REPORT.exists(), f"{SAMPLE_REPORT} is missing"
    committed = SAMPLE_REPORT.read_text(encoding="utf-8")
    fresh = render_sample(tmp_path / "sample-report.md").read_text(encoding="utf-8")
    assert committed == fresh, (
        f"{SAMPLE_REPORT} is out of date; regenerate with "
        f"`uv run costsentinel report --sample` and review the diff"
    )


def test_the_committed_sample_says_it_is_synthetic(tmp_path: Path) -> None:
    """A document that reads like a real client's cost review must say that it is not.

    The banner is derived from the provenance and the nil-prefixed subscription ids
    rather than passed in, so it cannot be omitted by forgetting an argument.
    """
    text = SAMPLE_REPORT.read_text(encoding="utf-8")
    assert SYNTHETIC_BANNER in text
    assert "**Synthetic data.**" in text


def test_regenerating_the_sample_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A safety property, not a convenience.

    A developer with `MODE=real` in their `.env` must not be able to render "the
    sample" from a client's estate and commit it. Every input is a constant, so the
    command can only ever produce the synthetic document.
    """
    monkeypatch.setenv("MODE", "real")
    monkeypatch.setenv("LLM_BACKEND", "azure_openai")
    monkeypatch.setenv("COSTSENTINEL_MODE", "real")

    written = render_sample(tmp_path / "sample.md")
    assert SYNTHETIC_BANNER in written.read_text(encoding="utf-8")


def test_the_samples_inputs_are_all_fixed() -> None:
    """Which is what makes the file byte-stable, and so reviewable as a diff."""
    assert SAMPLE_RUN_ID == "run-sample"
    assert SAMPLE_STAMP.tzinfo is not None
    assert SAMPLE_CLIENT


def test_the_sample_renders_byte_identically_twice(tmp_path: Path) -> None:
    """A sample that moved on every run would be a sample nobody reviewed."""
    first = render_sample(tmp_path / "a.md").read_text(encoding="utf-8")
    second = render_sample(tmp_path / "b.md").read_text(encoding="utf-8")
    assert first == second


def test_the_sample_subcommand_writes_the_committed_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run against a temporary copy of the path, so the test cannot dirty the tree."""
    target = tmp_path / "docs" / "sample-report.md"
    monkeypatch.setattr("costsentinel.cli.SAMPLE_REPORT", target)

    assert main(["report", "--sample"]) == 0
    assert target.exists()
    out = capsys.readouterr().out
    assert str(target) in out
    assert "Synthetic estate" in out


def test_a_mock_run_writes_markdown_for_each_client(tmp_path: Path) -> None:
    """The ordinary path. One file per client, named for the client and the run."""
    assert main(["report", "--out", str(tmp_path)]) == 0
    written = sorted(tmp_path.glob("*.md"))
    assert written, "nothing was written"
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# Cost review — ")
        assert "## Executive summary" in text


def test_one_client_can_be_rendered_alone(tmp_path: Path) -> None:
    assert main(["report", "--client", SAMPLE_CLIENT, "--out", str(tmp_path)]) == 0
    written = sorted(tmp_path.glob("*.md"))
    assert len(written) == 1
    assert SAMPLE_CLIENT in written[0].name


def test_stdout_prints_the_document_instead_of_writing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """For piping into a diff or a pager, which is how a renderer gets reviewed."""
    assert main(["report", "--client", SAMPLE_CLIENT, "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "## Executive summary" in out
    assert "## How this report was produced" in out


def test_the_default_output_directory_is_gitignored() -> None:
    """A rendered report is an artefact, and some of them describe a real estate.

    The one copy that belongs in the repository is the synthetic sample.
    """
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    assert f"{DEFAULT_RENDER_DIR.parts[0]}/" in ignored.split()
    assert DEFAULT_RENDER_DIR.parts[0] != "docs"


def test_a_rendered_mock_document_is_marked_synthetic(tmp_path: Path) -> None:
    """Every mock rendering, not only the committed one."""
    assert main(["report", "--out", str(tmp_path)]) == 0
    for path in tmp_path.glob("*.md"):
        assert SYNTHETIC_BANNER in path.read_text(encoding="utf-8")


@pytest.mark.skipif(pdf_available(), reason="the extra is installed, so nothing is missing")
def test_asking_for_a_pdf_without_the_extra_fails_with_the_install_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An optional dependency should fail early and say how to get it.

    Checked before the sweep runs, so a missing extra is not discovered after a long
    scan has already happened.
    """
    assert main(["report", "--out", str(tmp_path), "--pdf"]) == 1
    assert INSTALL_HINT in capsys.readouterr().err
    assert not list(tmp_path.glob("*"))


@pytest.mark.skipif(not pdf_available(), reason="needs the optional 'reports' extra")
def test_a_pdf_is_written_beside_the_markdown(tmp_path: Path) -> None:
    assert main(["report", "--client", SAMPLE_CLIENT, "--out", str(tmp_path), "--pdf"]) == 0
    markdown = next(iter(tmp_path.glob("*.md")))
    pdf = markdown.with_suffix(".pdf")
    assert pdf.exists()
    assert pdf.read_bytes().startswith(b"%PDF-")


def test_the_sample_report_is_recognised_as_synthetic_by_the_renderer() -> None:
    """The same marker the commit guard uses, so one definition rather than two."""
    from costsentinel.config import Settings
    from costsentinel.graph.runner import run_client_scan
    from costsentinel.llm.fake import FakeLLM
    from costsentinel.providers.mock import MockAzureProvider

    settings = Settings(
        mode=RunMode.MOCK, llm_backend=LLMBackend.FAKE, memory_backend=MemoryBackend.OFF
    )
    report, _ = run_client_scan(
        client=SAMPLE_CLIENT,
        provider=MockAzureProvider(seed=settings.mock_seed),
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
        run_id=SAMPLE_RUN_ID,
        requested_by="test",
    )
    assert is_synthetic(report)


def test_the_committed_sample_exercises_every_section() -> None:
    """What #76 was for.

    Phase 8 shipped a renderer with six sections and a sample that showed four,
    because no mock resource was both detector-flagged and tag-exempt and every mock
    finding happened to be priceable. A feature demonstrated only by a golden file is
    a feature no repository visitor can see.
    """
    text = SAMPLE_REPORT.read_text(encoding="utf-8")
    for heading in (
        "## Executive summary",
        "## Recommended actions",
        "## Worth investigating — saving unknown",
        "## Flagged but exempt — no action proposed",
        "## Spend changes — investigate",
        "## How this report was produced",
    ):
        assert heading in text, heading


def test_the_samples_exempt_and_unpriced_findings_add_nothing_to_the_total() -> None:
    """The claim those two sections exist to make, read off the committed document.

    A flagged-but-exempt resource and a finding that cannot be costed are both real
    and neither is money on the table. Adding them to the estate moved the headline
    savings by zero, and that is the property worth pinning -- if either ever started
    contributing, this is where it would show.
    """
    text = SAMPLE_REPORT.read_text(encoding="utf-8")
    assert "**Projected actionable savings** | **1,421.97 USD / month**" in text
    assert "Findings we could not price | 1 (listed below, excluded from the total)" in text
    assert "Flagged but exempt | 1 (costing 140.16 USD / month)" in text
    # The exempt resource's cost is never described as a saving.
    exempt = text.split("## Flagged but exempt")[1].split("\n## ")[0]
    assert "Saving if applied" not in exempt
    assert "**Costs now** | **140.16 USD / month**" in exempt
