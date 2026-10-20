"""CostSentinel command line.

``argparse`` rather than typer or click (ARCHITECTURE.md D24): CLAUDE.md section 3
does not list a CLI framework, and Phase 1 needs two subcommands with three flags.
Revisit when the CLI surface grows.

Output is JSON by default, because that is the Phase 1 contract. ``--summary`` gives
a short digest for reading at a terminal; it is a digest, not a report. Rendered
Markdown and PDF reports are Phase 6.

``--real`` switches a single invocation to a live, read-only Azure estate. It is
deliberately awkward in three ways. It requires ``--subscription`` (or
``AZURE_SUBSCRIPTION_IDS``), because a sweep must read what the operator named
rather than everything a credential can reach. It prints the scope and waits for
nothing, so the operator sees what is about to be read before it is read. And it
writes its report to a gitignored directory rather than to stdout, because real
cost data pasted into a terminal, a ticket or a commit is the one irreversible
mistake available here. See SECURITY.md before using it.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from costsentinel.config import (
    LLMBackend,
    MemoryBackend,
    RunMode,
    Settings,
    get_settings,
)
from costsentinel.domain.memory import Outcome
from costsentinel.domain.report import ClientReport, ScanResult
from costsentinel.graph.runner import run_client_scan, run_scan
from costsentinel.llm.base import LLMError
from costsentinel.llm.fake import FakeLLM
from costsentinel.memory import get_memory
from costsentinel.memory.base import MemoryStore, MemoryStoreError
from costsentinel.memory.decisions import decided_entries, pending_entries, record_decision
from costsentinel.providers import get_provider
from costsentinel.providers.base import ProviderError, ProviderNotConfiguredError
from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider
from costsentinel.reports.markdown import render_markdown
from costsentinel.reports.pdf import EXTRA as PDF_EXTRA
from costsentinel.reports.pdf import INSTALL_HINT as PDF_INSTALL_HINT
from costsentinel.reports.pdf import PdfUnavailableError, pdf_available, render_pdf
from costsentinel.tools import describe_surface

PROGRAM = "costsentinel"

#: Where a real run's report lands by default. Under ``var/``, which is gitignored,
#: and checked by ``scripts/check_no_real_data.py`` as a belt to that brace. Real
#: cost data in a commit is the one irreversible mistake available here.
DEFAULT_REAL_OUTPUT_DIR = Path("var/real-runs")

#: Where `costsentinel eval` writes its scorecard. Committed, so a diff shows what
#: moved between runs.
DEFAULT_REPORT = Path("evals/REPORT.md")

#: Where a mock-estate rendering goes by default. Under ``tmp/`` and gitignored: a
#: rendered report is an artefact, and the one copy that belongs in the repository is
#: the committed sample.
DEFAULT_RENDER_DIR = Path("tmp/reports")

#: The committed sample, rendered from the synthetic estate so a repository visitor
#: can read a real output without running anything.
SAMPLE_REPORT = Path("docs/sample-report.md")

#: Generated-at stamp for the committed sample. Fixed, so regenerating it produces a
#: diff only when the renderer changed -- a sample that moved every day would be a
#: sample nobody reviewed.
SAMPLE_STAMP = datetime(2026, 10, 19, tzinfo=UTC)

#: The client the committed sample is rendered for. The larger of the two synthetic
#: clients, so the sample exercises every section the renderer has.
SAMPLE_CLIENT = CLIENT_NORTHWIND

#: Run id baked into the sample, so its report id does not change between renders.
SAMPLE_RUN_ID = "run-sample"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Autonomous multi-agent FinOps governance for multi-subscription Azure "
            "estates. Phase 1 runs read-only against a deterministic mock estate."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser(
        "scan",
        help="Scan the estate for waste and produce a client report as JSON.",
        description=(
            "Runs the agent graph over the estate. With no --client, each client is "
            "scanned as a separate run and gets its own report; reports are never "
            "merged across clients."
        ),
    )
    scan.add_argument(
        "--client",
        default=None,
        help="Scan only this client. Default: every client, each as its own run.",
    )
    scan.add_argument(
        "--summary",
        action="store_true",
        help="Print a short terminal digest instead of the full JSON report.",
    )
    scan.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON report (the default; accepted for explicitness).",
    )
    scan.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation. Use 0 for a single line. Default: 2.",
    )
    scan.add_argument(
        "--real",
        action="store_true",
        help=(
            "Scan a live Azure estate, read-only, via DefaultAzureCredential. "
            "Requires --subscription. Read SECURITY.md first."
        ),
    )
    scan.add_argument(
        "--subscription",
        action="append",
        default=None,
        metavar="ID",
        dest="subscriptions",
        help=(
            "A subscription a real run may read. Repeatable. Required with --real "
            "unless AZURE_SUBSCRIPTION_IDS is set. There is no wildcard."
        ),
    )
    scan.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help=(
            f"Directory for a real run's report. Default: {DEFAULT_REAL_OUTPUT_DIR} "
            f"(gitignored). Ignored for a mock run, which prints to stdout."
        ),
    )

    decide = subcommands.add_parser(
        "decide",
        help="Record what a human decided about one recommendation.",
        description=(
            "Writes an accept, reject or defer outcome into episodic memory, so the "
            "next sweep annotates and deprioritises advice this client has already "
            "considered instead of re-pitching it as new. This is not the approval "
            "gate: it records a decision after the fact and authorises nothing."
        ),
    )
    decide.add_argument(
        "recommendation_id",
        help="The recommendation id from a report. Must be one that was proposed.",
    )
    outcome_group = decide.add_mutually_exclusive_group(required=True)
    outcome_group.add_argument(
        "--accept", action="store_true", help="The client agreed to this remediation."
    )
    outcome_group.add_argument("--reject", action="store_true", help="The client declined it.")
    outcome_group.add_argument(
        "--defer",
        action="store_true",
        help="The client intends to act later. Not the same as declining.",
    )
    decide.add_argument(
        "--reason",
        required=True,
        help=(
            "Why. Mandatory: an outcome with no reason can suppress a repeat but "
            "cannot explain the suppression to the client next month."
        ),
    )
    decide.add_argument(
        "--by",
        default=None,
        help="Who decided. Defaults to the operating system user.",
    )

    decisions = subcommands.add_parser(
        "decisions",
        help="Show what has been decided, and what is still awaiting a decision.",
    )
    decisions.add_argument(
        "--client",
        default=None,
        help="One client. Default: every client the provider can see.",
    )

    evaluate = subcommands.add_parser(
        "eval",
        help="Score the pipeline against the labelled datasets and emit a scorecard.",
        description=(
            "Runs the real graph over the committed labelled estates and reports "
            "precision, recall, ranking concordance, savings calibration and report "
            "groundedness. Exits non-zero if a gate fails -- safety recall below "
            "100%, a money figure that does not match the ground truth, or a "
            "prompt injection that changed the advice. Fully offline: the fake "
            "model and the deterministic judge, no network and no credential."
        ),
    )
    tier_group = evaluate.add_mutually_exclusive_group()
    tier_group.add_argument(
        "--smoke",
        action="store_true",
        help="The fast subset CI runs on every commit. Still enforces every gate.",
    )
    tier_group.add_argument("--full", action="store_true", help="Every dataset. The default.")
    evaluate.add_argument("--dataset", default=None, metavar="ID", help="Run one dataset by id.")
    evaluate.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            f"Where to write the Markdown scorecard. Default: {DEFAULT_REPORT}. "
            f"Pass --no-write to skip it."
        ),
    )
    evaluate.add_argument(
        "--no-write",
        action="store_true",
        help="Print the summary without writing the Markdown report.",
    )

    report = subcommands.add_parser(
        "report",
        help="Render a sweep as a client-ready Markdown document, and a PDF if available.",
        description=(
            "Runs a sweep and renders the result as Markdown. This is the artefact a "
            "client reads, so every figure in it is checked: the evaluation harness "
            "asserts that no number appears in a rendered document that does not equal "
            "a figure on the report it came from. The Markdown needs no optional "
            "extras; the PDF needs `uv sync --extra reports` and is skipped with a "
            "note when it is absent."
        ),
    )
    report.add_argument(
        "--client",
        default=None,
        metavar="NAME",
        help="One client. Default: every client the provider can see.",
    )
    report.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            f"Directory to write into. Default: {DEFAULT_RENDER_DIR} for a mock run "
            f"and {DEFAULT_REAL_OUTPUT_DIR} for a real one. Both are gitignored."
        ),
    )
    report.add_argument(
        "--real",
        action="store_true",
        help="Read a real Azure estate. Read-only. See SECURITY.md first.",
    )
    report.add_argument(
        "--subscription",
        action="append",
        default=None,
        metavar="ID",
        help="Restrict a real run to one subscription. Repeatable.",
    )
    report.add_argument(
        "--pdf",
        action="store_true",
        help=(
            "Also render a PDF. Needs the `reports` extra, and names the install "
            "command if it is absent."
        ),
    )
    report.add_argument(
        "--stdout",
        action="store_true",
        help="Print the Markdown instead of writing it. Mock runs only.",
    )
    report.add_argument(
        "--sample",
        action="store_true",
        help=(
            f"Regenerate the committed sample at {SAMPLE_REPORT} from the synthetic "
            f"estate. Ignores every other flag and reads nothing from the "
            f"environment, so it can only produce the synthetic document."
        ),
    )

    subcommands.add_parser(
        "config",
        help="Show the active configuration and what it requires.",
    )
    return parser


def render_json(result: ScanResult, indent: int = 2) -> str:
    """Serialise a sweep as JSON.

    Monetary amounts serialise as decimal *strings* rather than floats, which is
    deliberate: binary floating point cannot represent a cent exactly, and a cost
    tool that silently rounds its own figures would undermine the provenance
    guarantees the domain model exists to provide.
    """
    return result.model_dump_json(indent=indent if indent > 0 else None)


def summarise_report(report: ClientReport) -> str:
    """A short digest of one client's report."""
    totals = report.totals
    lines = [
        f"{report.client}  [{report.severity.value}]",
        f"  period               {report.period.start} to {report.period.end} "
        f"({report.period.days} days)",
        f"  subscriptions        {len(report.subscriptions_in_scope)}",
        f"  resources examined   {report.resources_examined}",
        f"  observed spend       {totals.observed_monthly_spend.display()}",
        f"  projected savings    {totals.projected_monthly_savings.display()}/month, "
        f"{totals.projected_annual_savings.display()}/year",
        f"  findings             {totals.findings_count} "
        f"({totals.awaiting_approval_count} awaiting approval, "
        f"{totals.automatable_count} automatable)",
    ]
    if totals.anomaly_count or totals.unassessed_scope_count:
        unassessed = (
            f", {totals.unassessed_scope_count} scope(s) unassessed"
            if totals.unassessed_scope_count
            else ""
        )
        lines.append(f"  spend changes        {totals.anomaly_count} to investigate{unassessed}")
    if totals.exempt_findings_count:
        spend = (
            totals.exempt_monthly_cost.display()
            if totals.exempt_monthly_cost is not None
            else "unknown"
        )
        lines.append(
            f"  flagged but exempt   {totals.exempt_findings_count} "
            f"(costing {spend}/month, excluded from the savings above)"
        )
    if report.memory_recall:
        lines.append(f"  memory               {report.memory_recall}")
    lines.append("")
    for finding in report.findings:
        lines.append(
            f"  {finding.rank}. {finding.resource_name}  "
            f"[{finding.waste_kind.value}]  "
            f"{finding.proposed_action.value}  "
            f"({finding.action_class.value})  "
            f"saves {finding.monthly_saving.display()}/month"
        )
        if finding.memory_note:
            # Indented under the finding, with the rank it held on merit, so the
            # reordering is legible rather than mysterious.
            moved = (
                f" (was #{finding.base_rank} on merit)"
                if finding.base_rank is not None and finding.base_rank != finding.rank
                else ""
            )
            lines.append(f"       memory: {finding.memory_note}{moved}")
    if report.anomalies:
        lines += ["", "  Spend changes (investigate; no action proposed):"]
        for anomaly in report.anomalies:
            lines.append(
                f"    - {anomaly.scope}  [{anomaly.kind.value}]  "
                f"{anomaly.baseline.display()} -> {anomaly.observed.display()}/day  "
                f"({anomaly.deviation_score:.1f} sigma)"
            )
            lines.append(f"        {anomaly.statement}")
    if report.unassessed_spend:
        lines += ["", "  Spend not assessed:"]
        lines += [f"    - {line}" for line in report.unassessed_spend]
    if report.exempt_findings:
        lines += ["", "  Flagged but exempt (no action proposed):"]
        for item in report.exempt_findings:
            lines.append(
                f"    - {item.resource_name}  [{item.waste_kind.value}]  "
                f"costs {item.current_monthly_cost.display()}/month  "
                f"exempt: {item.exemption_reason.value}"
            )
            lines.append(f"        {item.exemption_statement}")
    if report.approval_queue:
        lines += ["", "  Awaiting approval:"]
        lines += [
            f"    - {item.resource_name}: {item.proposed_action.value} "
            f"[{item.action_class.value}] {item.monthly_saving_display}/month"
            for item in report.approval_queue
        ]
    if report.notes:
        lines += ["", *(f"  note: {note}" for note in report.notes)]
    return "\n".join(lines)


def summarise(result: ScanResult) -> str:
    """A short digest of a whole sweep."""
    header = (
        f"CostSentinel sweep: {len(result.reports)} client(s), "
        f"{result.total_findings} finding(s), "
        f"{result.total_awaiting_approval} awaiting approval."
    )
    return "\n\n".join([header, *(summarise_report(report) for report in result.reports)])


def _config_text(settings: Settings) -> str:
    credentials = (
        "none required (mock provider, fake model)"
        if settings.requires_no_credentials
        else "required for the selected backends"
    )
    memory = (
        f"{settings.memory_backend.value} ({settings.memory_db_path})"
        if settings.memory_backend is MemoryBackend.SQLITE
        else settings.memory_backend.value
    )
    tracing = "configured" if settings.tracing_enabled else "disabled (no-op)"
    return "\n".join(
        [
            f"mode                 {settings.mode.value}",
            f"llm_backend          {settings.llm_backend.value}",
            f"mock_seed            {settings.mock_seed}",
            f"state_dir            {settings.state_dir}",
            f"checkpoint_path      {settings.checkpoint_path}",
            f"log_level            {settings.log_level.value}",
            f"rightsize_headroom   {settings.rightsize_headroom_factor}x",
            f"memory               {memory}",
            f"embedder             {settings.embedder_backend.value}",
            f"tracing              {tracing}",
            f"credentials          {credentials}",
            f"subscription_scope   {_scope_text(settings.subscription_scope)}",
            f"client_tag           {settings.azure_client_tag}",
        ]
    )


def _scope_text(scope: Sequence[str]) -> str:
    return ", ".join(scope) if scope else "none (MODE=real would refuse to run)"


def real_run_banner(settings: Settings, scope: Sequence[str]) -> str:
    """What a real run is about to do, printed before it does any of it.

    An operator pointing this at an estate should not have to read the source to
    know what will be touched. Enumerating the tool surface rather than describing
    it means this cannot drift from what is actually exposed.
    """
    lines = [
        "CostSentinel real run -- READ ONLY",
        "  identity             DefaultAzureCredential (az login, managed identity "
        "or workload identity)",
        f"  subscriptions        {_scope_text(scope)}",
        f"  client tag           {settings.azure_client_tag}",
        f"  llm backend          {settings.llm_backend.value}",
        "  permissions needed   Reader, Cost Management Reader",
        "  operations performed  read only; this build has no write path at all",
        "",
        "  Azure tools this run may call:",
    ]
    lines += [f"    {line}" for line in describe_surface()]
    lines += [
        "",
        "  Pilot on a personal or sandbox subscription first. Obtain written "
        "approval before any client estate. Never commit the output.",
        "",
    ]
    return "\n".join(lines)


def resolve_real_scope(args: argparse.Namespace, settings: Settings) -> tuple[str, ...]:
    """The subscriptions a ``--real`` run may read.

    ``--subscription`` wins over the environment, so a shell with a lingering
    ``AZURE_SUBSCRIPTION_IDS`` cannot silently widen what a deliberate command asked
    for.

    Raises:
        ProviderNotConfiguredError: If no scope was given. A real run with no scope
            is refused rather than defaulted, because the default anybody would
            reach for -- everything the credential can see -- means reading other
            clients' estates on a managed-services tenant.
    """
    named = tuple(dict.fromkeys(args.subscriptions or ()))
    scope = named or settings.subscription_scope
    if not scope:
        msg = (
            "--real needs an explicit subscription scope. Pass --subscription ID "
            "(repeatable) or set AZURE_SUBSCRIPTION_IDS. There is no wildcard: "
            "CostSentinel will not read every subscription a credential can reach."
        )
        raise ProviderNotConfiguredError(msg)
    return scope


def write_real_report(result: ScanResult, directory: Path, indent: int) -> Path:
    """Write a real run's report under ``directory`` and return the path.

    Straight to a gitignored directory rather than to stdout. Real cost data pasted
    into a terminal, a ticket or a commit is the one irreversible mistake available
    here, and the shape of the command should make the safe thing the easy one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"scan-{stamp}.json"
    path.write_text(render_json(result, indent), encoding="utf-8")
    return path


def _outcome_from(args: argparse.Namespace) -> Outcome:
    """Which outcome the flags selected.

    argparse enforces exactly one through a required mutually-exclusive group, so
    there is no "none selected" case to handle here.
    """
    if args.accept:
        return Outcome.ACCEPTED
    if args.reject:
        return Outcome.REJECTED
    return Outcome.DEFERRED


def _decided_by(args: argparse.Namespace) -> str:
    """Who to attribute the decision to.

    The OS user is a weak identity and deliberately so: this records who typed the
    command on a machine somebody already has access to. Authenticated approver
    identity is the approval gate's problem (Phase 10), and pretending otherwise here
    would overstate what the record proves.
    """
    if args.by:
        return str(args.by)
    return getpass.getuser() or "unknown"


def run_decide(args: argparse.Namespace, settings: Settings) -> int:
    """Record one decision into episodic memory."""
    store = get_memory(settings)
    try:
        outcome, event = record_decision(
            store,
            recommendation_id=args.recommendation_id,
            outcome=_outcome_from(args),
            reason=args.reason,
            decided_by=_decided_by(args),
        )
    except MemoryStoreError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(
        f"Recorded: {outcome.recommendation_id} {outcome.outcome.value} by "
        f"{outcome.decided_by} on {outcome.decided_at.date().isoformat()}."
    )
    print(f"  client            {outcome.client}")
    print(f"  resource          {outcome.target_resource_name or outcome.target_resource_id}")
    print(f"  action            {outcome.action.value}")
    print(f"  monthly saving    {outcome.monthly_saving_display or 'unknown'}")
    print(f"  reason            {outcome.reason}")
    print(f"  audit event       {event.event_id}")
    if outcome.outcome.discourages_repitch:
        print(
            "\nThe next scan for this client will annotate this finding with the "
            "date and reason, and rank it below advice they have not already seen. "
            "It is not removed: the waste has not gone away."
        )
    else:
        print("\nThe next scan will note that this pattern was accepted.")
    return 0


def run_decisions(args: argparse.Namespace, settings: Settings) -> int:
    """Show what has been decided and what is still awaiting a decision."""
    store = get_memory(settings)
    try:
        if not store.is_enabled:
            print(
                f"{PROGRAM}: memory is switched off, so nothing has been recorded.",
                file=sys.stderr,
            )
            return 1
        clients = (
            (args.client,)
            if args.client
            else tuple(sorted({s.client for s in get_provider(settings).list_subscriptions()}))
        )
        print(_decisions_text(store, clients))
    except (ProviderError, MemoryStoreError) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _decisions_text(store: MemoryStore, clients: Sequence[str]) -> str:
    lines: list[str] = []
    for client in clients:
        decided = decided_entries(store, client)
        pending = pending_entries(store, client)
        lines.append(f"{client}")
        if not decided and not pending:
            lines.append("  nothing recorded yet; run a scan first.")
            lines.append("")
            continue
        if decided:
            lines.append(f"  decided ({len(decided)}):")
            lines += [
                f"    {outcome.outcome.value:<8} {entry.recommendation_id}  "
                f"{entry.target_resource_name}  {entry.action.value}  "
                f"{outcome.decided_at.date().isoformat()}  {outcome.reason}"
                for entry, outcome in decided
            ]
        if pending:
            lines.append(f"  awaiting a decision ({len(pending)}):")
            lines += [
                f"    {entry.recommendation_id}  {entry.target_resource_name}  "
                f"{entry.action.value}  saves {entry.monthly_saving_display or 'unknown'}/month"
                for entry in pending
            ]
            lines.append("")
            lines.append(
                f"  Record one with: {PROGRAM} decide "
                f'{pending[0].recommendation_id} --reject --reason "..."'
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def run_eval(args: argparse.Namespace) -> int:
    """Score the pipeline against the labelled datasets.

    Returns:
        ``0`` when every gate held, ``1`` otherwise. The quality metrics never
        affect the exit code: a precision of 0.80 is a number somebody should look
        at, not a reason to refuse a merge.
    """
    # Imported here rather than at module scope: the harness is a development tool
    # and the CLI should not pay for importing it on every `scan`. It also keeps the
    # dependency pointing one way -- evals imports costsentinel, never the reverse.
    from evals.harness.dataset import DatasetError, Tier  # noqa: PLC0415
    from evals.harness.runner import evaluate, write_report  # noqa: PLC0415

    try:
        card = evaluate(tier=Tier.SMOKE if args.smoke else None, dataset_id=args.dataset)
    except DatasetError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1

    print(card.terminal_summary())

    if not args.no_write:
        written = write_report(card, Path(args.out) if args.out else DEFAULT_REPORT)
        print(f"\nScorecard written to {written}")

    if not card.passed:
        failed = ", ".join(gate.name for gate in card.blocking if not gate.passed)
        print(
            f"\n{PROGRAM}: evaluation FAILED on {failed}. These are gates, not "
            f"scores: see the failures above.",
            file=sys.stderr,
        )
        return 1
    return 0


def report_filename(report: ClientReport) -> str:
    """A stable filename for one client's document.

    The client and the run id, so two sweeps of the same estate do not overwrite each
    other and a filename never carries a timestamp -- the document carries one
    generated-at and that is the only clock in the output.
    """
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in report.client)
    return f"{safe}-{report.run_id}.md"


def render_reports(
    result: ScanResult,
    directory: Path,
    *,
    want_pdf: bool,
    generated_at: datetime | None = None,
) -> list[Path]:
    """Render every client's report into a directory.

    Returns:
        The paths written, Markdown first for each client.

    Raises:
        PdfUnavailableError: If ``want_pdf`` and the extra is missing. Deliberately
            loud: somebody who asked for a PDF and silently got only Markdown would
            find out when they went looking for the file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for one in result.reports:
        markdown = render_markdown(one, generated_at=generated_at)
        path = directory / report_filename(one)
        path.write_text(markdown, encoding="utf-8")
        written.append(path)
        if want_pdf:
            written.append(
                render_pdf(markdown, path.with_suffix(".pdf"), title=f"Cost review — {one.client}")
            )
    return written


def render_sample(path: Path | None = None) -> Path:
    """Render the committed sample from the synthetic estate.

    Every input is fixed here and **nothing is read from the environment**. That is a
    safety property rather than a convenience: a developer with ``MODE=real`` in their
    ``.env`` must not be able to regenerate "the sample" from a client's estate and
    commit it. The provider, the model, the memory backend, the client, the run id and
    the generated-at stamp are all constants, so this command can only ever produce
    the synthetic document -- which also makes it byte-identical between runs, so a
    diff appears when the renderer changed and at no other time.
    """
    # Resolved here rather than as a default argument, so the module constant is
    # the single definition of where the sample lives.
    path = SAMPLE_REPORT if path is None else path
    with tempfile.TemporaryDirectory(prefix="costsentinel-sample-") as scratch:
        settings = Settings(
            mode=RunMode.MOCK,
            llm_backend=LLMBackend.FAKE,
            memory_backend=MemoryBackend.OFF,
            state_dir=Path(scratch),
        )
        report, _ = run_client_scan(
            client=SAMPLE_CLIENT,
            provider=MockAzureProvider(seed=settings.mock_seed),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=None,
            run_id=SAMPLE_RUN_ID,
            requested_by=PROGRAM,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report, generated_at=SAMPLE_STAMP), encoding="utf-8")
    return path


def run_report(args: argparse.Namespace, settings: Settings) -> int:
    """Render a sweep as a document."""
    if args.sample:
        written = render_sample()
        print(f"Written {written}")
        print("Synthetic estate. Commit this one; it is the repository's sample output.")
        return 0

    real = bool(args.real) or settings.mode is RunMode.REAL
    scope: tuple[str, ...] = ()
    if real:
        settings = settings.model_copy(update={"mode": RunMode.REAL})
        try:
            scope = resolve_real_scope(args, settings)
        except ProviderError as exc:
            print(f"{PROGRAM}: {exc}", file=sys.stderr)
            return 1
        print(real_run_banner(settings, scope), file=sys.stderr)

    if args.pdf and not pdf_available():
        print(f"{PROGRAM}: {PdfUnavailableError()}", file=sys.stderr)
        return 1

    try:
        result = run_scan(
            client=args.client,
            settings=settings,
            requested_by=PROGRAM,
            subscription_ids=scope or None,
        )
    except (ProviderError, LLMError) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1

    if args.stdout and not real:
        for one in result.reports:
            print(render_markdown(one))
        return 0

    default = DEFAULT_REAL_OUTPUT_DIR if real else DEFAULT_RENDER_DIR
    directory = Path(args.out) if args.out else default
    written = render_reports(result, directory, want_pdf=bool(args.pdf))

    for path in written:
        print(f"Written {path}")
    if real:
        print("That directory is gitignored. Do not commit it, and do not paste it.")
    elif not args.pdf and not pdf_available():
        print(
            f"PDF not rendered: the optional '{PDF_EXTRA}' extra is not installed. "
            f"Install it with `{PDF_INSTALL_HINT}` and pass --pdf."
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Returns:
        ``0`` on success, ``1`` on a provider or model failure. A report with zero
        findings is a success: finding no waste is a valid outcome, not an error.
    """
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "config":
        print(_config_text(settings))
        return 0

    if args.command == "decide":
        return run_decide(args, settings)

    if args.command == "decisions":
        return run_decisions(args, settings)

    if args.command == "eval":
        return run_eval(args)

    if args.command == "report":
        return run_report(args, settings)

    real = bool(args.real) or settings.mode is RunMode.REAL
    scope: tuple[str, ...] = ()
    if real:
        settings = settings.model_copy(update={"mode": RunMode.REAL})
        try:
            scope = resolve_real_scope(args, settings)
        except ProviderError as exc:
            print(f"{PROGRAM}: {exc}", file=sys.stderr)
            return 1
        print(real_run_banner(settings, scope), file=sys.stderr)

    try:
        result = run_scan(
            client=args.client,
            settings=settings,
            requested_by=PROGRAM,
            subscription_ids=scope or None,
        )
    except (ProviderError, LLMError) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1

    if real:
        directory = Path(args.out) if args.out else DEFAULT_REAL_OUTPUT_DIR
        written = write_real_report(result, directory, args.indent)
        print(summarise(result))
        print(f"\nReport written to {written}")
        print("That directory is gitignored. Do not commit it, and do not paste it.")
        return 0

    print(summarise(result) if args.summary else render_json(result, args.indent))
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised via the console script
    raise SystemExit(main())
