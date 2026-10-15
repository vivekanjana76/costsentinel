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
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from costsentinel.config import RunMode, Settings, get_settings
from costsentinel.domain.report import ClientReport, ScanResult
from costsentinel.graph.runner import run_scan
from costsentinel.llm.base import LLMError
from costsentinel.providers.base import ProviderError, ProviderNotConfiguredError
from costsentinel.tools import describe_surface

PROGRAM = "costsentinel"

#: Where a real run's report lands by default. Under ``var/``, which is gitignored,
#: and checked by ``scripts/check_no_real_data.py`` as a belt to that brace. Real
#: cost data in a commit is the one irreversible mistake available here.
DEFAULT_REAL_OUTPUT_DIR = Path("var/real-runs")


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
        "",
    ]
    lines += [
        f"  {finding.rank}. {finding.resource_name}  "
        f"[{finding.waste_kind.value}]  "
        f"{finding.proposed_action.value}  "
        f"({finding.action_class.value})  "
        f"saves {finding.monthly_saving.display()}/month"
        for finding in report.findings
    ]
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
    memory = "configured" if settings.memory_enabled else "disabled (Phase 4)"
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
