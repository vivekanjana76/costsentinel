"""The operator surface: it recomputes nothing, and it decides through one path.

Two claims carry the phase.

**The view computes no figure.** Everything on a page already exists on a
`ClientReport`, so the same copy-never-compose rule the rendered-report gate enforces
(D109) applies to the HTML -- and it is enforced with the same functions, rather than a
second checker that could disagree with the first.

**There is one decision path.** The page can record an approval, and it does so by
calling the three things `costsentinel approvals decide` calls. Asserted structurally,
because "we reused the function" is the sort of claim that stays true right up until
somebody adds a shortcut.

Everything here runs offline with no browser: the view model and the renderer are
plain functions, and the routes are exercised through the FastAPI test client.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.dashboard import app as dashboard_app
from costsentinel.dashboard.app import (
    BASE_PATH,
    build_router,
    mount,
    pending_queue,
    record_decision,
    stored_reports,
)
from costsentinel.dashboard.html import (
    NO_EXECUTION_NOTE,
    content_of,
    esc,
    render_estate,
    render_queue,
    render_report,
)
from costsentinel.dashboard.static import APPROVALS, INDEX, client_filename, write_static
from costsentinel.dashboard.view import (
    ExemptView,
    ReportView,
    estate_view,
    report_view,
)
from costsentinel.domain.governance import Decision
from costsentinel.domain.report import ClientReport
from costsentinel.graph.approvals import approvals_graph, paused_sweep, paused_sweeps
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, CLIENT_NORTHWIND, MockAzureProvider
from costsentinel.reports.markdown import (
    SYNTHETIC_BANNER,
    headline_displays,
    monetary_strings,
    unrendered_figures,
)


def section_of(html: str, heading: str) -> str:
    """One ``<h2>`` section of a page.

    Splitting on the heading *text* alone looks simpler and is wrong: "Flagged but
    exempt" and "Spend changes to investigate" are also labels in the summary table
    above, so a plain split returns the summary rather than the section.
    """
    marker = f"<h2>{heading}"
    assert marker in html, heading
    return html.split(marker, 1)[1].split("</section>", 1)[0]


def _local(app: FastAPI) -> TestClient:
    """A client whose requests arrive on a loopback socket, as a local operator's do.

    The default test client reports its server as ``testserver``, which is not
    loopback, and the decision interlock rightly refuses a decision from it (D126).
    """
    return TestClient(app, base_url="http://127.0.0.1")


def _settings(scratch: Path, *, enabled: bool = False, decide: bool = False) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
        dashboard_enabled=enabled,
        dashboard_can_decide=decide,
    )


@pytest.fixture
def report(tmp_path: Path) -> ClientReport:
    """The larger synthetic client, which exercises every section."""
    built, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=MockAzureProvider(),
        llm=FakeLLM(),
        settings=_settings(tmp_path),
        checkpointer=None,
        run_id="run-view",
        requested_by="test",
    )
    return built


@pytest.fixture
def view(report: ClientReport) -> ReportView:
    return report_view(report)


def _scanned(scratch: Path, client: str = CLIENT_NORTHWIND) -> Settings:
    """A settings object whose checkpoint already holds a paused sweep."""
    settings = _settings(scratch, enabled=True, decide=True)
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        run_client_scan(
            client=client,
            provider=MockAzureProvider(),
            llm=FakeLLM(),
            settings=settings,
            checkpointer=saver,
            run_id=f"run-{client}",
            requested_by="test",
        )
    return settings


# ---------------------------------------------------------------------------
# The view computes nothing
# ---------------------------------------------------------------------------


def test_the_page_introduces_no_figure(view: ReportView, report: ClientReport) -> None:
    """The claim the whole phase rests on, checked with the gate's own machinery.

    `unrendered_figures` is the function the rendered-report gate calls. Reusing it
    rather than writing a second checker means the document and the page are held to
    one standard, and a weakening of that standard weakens both at once -- which is
    visible, unlike two checkers drifting apart.
    """
    text = content_of(render_report(view))
    assert monetary_strings(text), "nothing numeric on the page, so this asserts nothing"
    assert unrendered_figures(report, text) == []


def test_a_corrupted_figure_on_the_page_is_caught(view: ReportView, report: ClientReport) -> None:
    """Red, then green. A check nobody has seen fail is a check nobody should trust."""
    text = content_of(render_report(view))
    headline = report.totals.projected_monthly_savings.display().split(" ")[0]
    assert headline in text

    corrupted = text.replace(headline, headline[:-1] + "9", 1)
    assert corrupted != text
    assert unrendered_figures(report, corrupted) == [headline[:-1] + "9"]
    assert unrendered_figures(report, text) == []


def test_the_headline_figures_reach_the_page_verbatim(
    view: ReportView, report: ClientReport
) -> None:
    """Not by membership: a reformatted or swapped total is the error somebody acts on."""
    text = content_of(render_report(view))
    for display in headline_displays(report):
        assert display in text, display
    assert view.headline_figures == headline_displays(report)


def test_the_view_model_itself_holds_no_recomputable_type(view: ReportView) -> None:
    """Caught one layer up from the HTML, where a recomputation would be introduced.

    A `Decimal` or a `MoneyAmount` on the view would be a thing a renderer could
    format differently from the report. Strings cannot be re-rounded.
    """
    from decimal import Decimal

    from pydantic import BaseModel

    from costsentinel.domain.common import MoneyAmount

    def walk(value: object, where: str) -> None:
        assert not isinstance(value, Decimal), f"{where} is a Decimal"
        assert not isinstance(value, MoneyAmount), f"{where} is a MoneyAmount"
        # On the class, not the instance: accessing `model_fields` through an
        # instance is deprecated in Pydantic 2.11 and removed in 3.
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:  # pyright: ignore[reportUnknownArgumentType]
                walk(getattr(value, name), f"{where}.{name}")
        elif isinstance(value, tuple | list):
            for index, item in enumerate(value):  # pyright: ignore[reportUnknownVariableType]
                walk(item, f"{where}[{index}]")

    walk(view, "view")


def test_the_view_module_does_no_arithmetic() -> None:
    """Asserted against the syntax tree rather than against a convention.

    Exactly two expressions, both named here, because "no arithmetic" would be a claim
    this file does not quite meet and pretending otherwise would make the test a
    rubber stamp:

    * ``len(priced) - estimated`` splits a **count** of findings into observed and
      estimated. It is a count of items already in a tuple, which is the one derivation
      the module's docstring permits.
    * ``part.share * 100`` turns a fraction the pipeline computed into the percentage a
      reader expects. A unit conversion of an existing value, not a new figure.

    Neither touches money. Anything else appearing here is a recomputation, and the
    assertion on the exact count is what makes a third expression fail rather than
    blend in.
    """
    tree = ast.parse(Path("src/costsentinel/dashboard/view.py").read_text(encoding="utf-8"))
    arithmetic = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div | ast.FloorDiv)
    ]
    assert sorted(arithmetic) == ["len(priced) - estimated", "part.share * 100"], arithmetic


def test_the_renderer_does_no_arithmetic() -> None:
    """The same check one layer out, where there is no exception at all."""
    tree = ast.parse(Path("src/costsentinel/dashboard/html.py").read_text(encoding="utf-8"))
    arithmetic = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub | ast.Mult | ast.Div | ast.FloorDiv)
    ]
    assert arithmetic == [], [ast.unparse(one) for one in arithmetic]


def test_content_of_removes_only_the_stylesheet(view: ReportView) -> None:
    """The exclusion must not become a place to hide a figure.

    The stylesheet comes out because `font: 15px/1.55` is a decimal and nobody reads a
    line height as money. Everything else stays, including attributes -- so a figure
    smuggled into the body is still caught, which is what the second half asserts.
    """
    html = render_report(view)
    text = content_of(html)
    assert "<style>" not in text
    assert "1.55" not in text, "the stylesheet survived"
    assert "<table" in text and "class=" in text, "markup was stripped too"

    smuggled = html.replace("</main>", '<p title="99,999.99 USD">x</p></main>')
    assert "99,999.99" in monetary_strings(content_of(smuggled))


# ---------------------------------------------------------------------------
# Honesty, in the second format
# ---------------------------------------------------------------------------


def test_an_unknown_renders_as_unknown_and_never_as_zero(view: ReportView) -> None:
    """A blank or a zero in a savings column reads as "nothing to gain"."""
    assert view.unquantified, "the fixture must contain an unquantifiable finding"
    html = render_report(view)
    section = section_of(html, "Worth investigating")
    assert "unknown USD" in section
    assert "0.00 USD" not in section
    assert 'class="money unknown"' in section, "an unknown should not look like a figure"


def test_an_exempt_item_shows_a_cost_and_has_nowhere_to_put_a_saving(
    view: ReportView,
) -> None:
    """Asserted against the type, not only the output.

    `ExemptView` has no saving field, so the page cannot show one even by mistake --
    the same reasoning `ExemptFinding` carries in the domain.
    """
    assert not any("saving" in name for name in ExemptView.model_fields)

    assert view.exempt, "the fixture must contain an exempt finding"
    html = render_report(view)
    section = section_of(html, "Flagged but exempt")
    assert "Costs now" in section
    assert "Saving" not in section
    assert "what you are paying, not a saving available to you" in html


def test_an_anomaly_is_visibly_not_actionable(view: ReportView) -> None:
    """No action, no class, no saving -- and the section says so in words."""
    assert view.anomalies
    html = render_report(view)
    section = section_of(html, "Spend changes")
    assert "Difference" in section
    assert "CostSentinel proposes nothing here" in section
    assert "Saving if applied" not in section
    assert "Approve" not in section


def test_every_page_says_what_approval_does_not_do(view: ReportView) -> None:
    """The first thing an operator should know about a surface full of proposals."""
    for html in (
        render_report(view),
        render_estate(estate_view((view,))),
        render_queue((), banner=view.banner),
    ):
        assert "no execution path" in html.lower()
    assert "no execution path" in NO_EXECUTION_NOTE.lower()


def test_the_synthetic_banner_derives_from_the_report(view: ReportView) -> None:
    """So a screenshot cannot omit it by forgetting an argument.

    Same marker as the Markdown report and the commit guard: provenance plus the
    nil-prefixed subscription ids.
    """
    assert view.synthetic
    assert view.banner
    html = render_report(view)
    # The banner's Markdown emphasis is stripped for this format; the sentence is not.
    assert "synthetic demonstration estate" in html
    assert "must not be read as a statement about one" in html


def test_a_real_estate_carries_no_banner(report: ClientReport) -> None:
    """The banner must not become decoration, or it stops being read."""
    real = report.model_copy(
        update={"subscriptions_in_scope": ("7f2c1b40-9a3e-4f21-8c55-1d4e6b9a7c32",)}
    )
    built = report_view(real)
    assert not built.synthetic
    assert built.banner == ""
    assert SYNTHETIC_BANNER not in render_report(built)


def test_a_hostile_resource_name_is_escaped(tmp_path: Path) -> None:
    """Names and tags are provider-supplied, and the injection corpus puts markup there.

    Asserted against the shape of a real injection attempt rather than a made-up
    string: an attribute-breaking quote followed by a tag.
    """
    hostile = '"><script>alert(1)</script><span class="'
    assert "<script>" not in esc(hostile)
    assert "&lt;script&gt;" in esc(hostile)
    assert '"' not in esc(hostile)

    exempt = ExemptView(
        resource_name=hostile,
        subscription_id="sub-1",
        waste_kind="idle virtual machine",
        current_cost="10.00 USD",
        exemption_reason="disaster recovery",
        exemption_rule="builtin:dr",
        matched_tag=hostile,
        statement=hostile,
    )
    from costsentinel.dashboard.html import _exempt_item

    rendered = _exempt_item(exempt)
    assert "<script>" not in rendered
    assert rendered.count("&lt;script&gt;") == 3


# ---------------------------------------------------------------------------
# One decision path
# ---------------------------------------------------------------------------


def test_the_dashboard_builds_no_decision_record_of_its_own() -> None:
    """The single-decision-path guarantee, asserted structurally (D124).

    "We reused the shared function" is the sort of claim that stays true right up until
    somebody adds a shortcut, so the absence of a second implementation is checked
    against the source: the dashboard may not construct an `ApprovalRecord`, may not
    build an `ApprovalDecision`, and may not reach into the gate's internals.
    """
    forbidden = (
        "ApprovalRecord(",
        "ApprovalDecision(",
        "build_record",
        "read_payload",
        "interrupt(",
        "Command(",
    )
    for path in sorted(Path("src/costsentinel/dashboard").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{path.name} contains {name!r}"


def test_the_dashboard_calls_the_same_functions_the_cli_calls() -> None:
    """The positive half: it is not merely innocent, it uses the shared path.

    Pinned by name, so removing the shared call in favour of something local fails
    here rather than passing because nothing forbidden was added.
    """
    text = Path("src/costsentinel/dashboard/app.py").read_text(encoding="utf-8")
    for name in ("find_pending", "DecisionRequest(", "resume("):
        assert name in text, name

    cli = Path("src/costsentinel/cli.py").read_text(encoding="utf-8")
    for name in ("find_pending", "DecisionRequest(", "resume("):
        assert name in cli, f"the CLI no longer uses {name!r}; the paths have diverged"


def test_a_decision_from_the_dashboard_is_identical_to_one_from_the_cli(
    tmp_path: Path,
) -> None:
    """The same record, the same state, the same audit events.

    Built twice over two identical estates rather than compared in the abstract,
    because the claim is about what lands in the checkpoint.
    """
    from costsentinel.domain.governance import DecisionRequest
    from costsentinel.graph.approvals import find_pending, resume

    via_dashboard = _scanned(tmp_path / "a", CLIENT_MINISTRY)
    via_cli = _scanned(tmp_path / "b", CLIENT_MINISTRY)

    queue = pending_queue(via_dashboard)
    target = queue[0].recommendation_id

    record_decision(
        via_dashboard,
        recommendation_id=target,
        decision=Decision.APPROVED,
        approver="ops@example.com",
        reason="agreed in the Tuesday review",
    )

    with sqlite_checkpointer(via_cli.checkpoint_path) as saver:
        graph = approvals_graph(via_cli, saver)
        sweep, pending = find_pending(paused_sweeps(graph, saver), target)
        resume(
            graph,
            sweep.run_id,
            DecisionRequest(
                recommendation_id=pending.recommendation_id,
                decision=Decision.APPROVED,
                approver="ops@example.com",
                reason="agreed in the Tuesday review",
            ),
        )

    def _record(settings: Settings) -> dict[str, Any]:
        from costsentinel.domain.state import ScanState

        with sqlite_checkpointer(settings.checkpoint_path) as saver:
            graph = approvals_graph(settings, saver)
            snapshot = graph.get_state({"configurable": {"thread_id": f"run-{CLIENT_MINISTRY}"}})
            state = ScanState.model_validate(snapshot.values)
        one = next(r for r in state.approval_records if r.recommendation_id == target)
        dumped = one.model_dump(mode="json")
        # The timestamps legitimately differ; everything else must not.
        dumped.pop("decided_at")
        dumped["provenance"].pop("retrieved_at")
        return dumped

    assert _record(via_dashboard) == _record(via_cli)


def test_a_decision_from_the_dashboard_hands_off_and_touches_no_provider(
    tmp_path: Path,
) -> None:
    """The Phase 10 invariant, unchanged by the new surface."""
    settings = _scanned(tmp_path, CLIENT_MINISTRY)
    queue = pending_queue(settings)
    deletion = next(one for one in queue if "delete" in one.action)

    statement = record_decision(
        settings,
        recommendation_id=deletion.recommendation_id,
        decision=Decision.APPROVED,
        approver="ops@example.com",
        reason="confirmed orphaned",
    )
    assert "handed off for a person to carry out" in statement
    assert "no path to perform it" in statement

    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        graph = approvals_graph(settings, saver)
        remaining = paused_sweep(graph, f"run-{CLIENT_MINISTRY}")
    waiting = {one.recommendation_id for one in remaining.pending} if remaining else set()
    assert deletion.recommendation_id not in waiting


def test_the_dashboard_package_imports_nothing_that_could_act() -> None:
    """The structural no-mutation proof, applied to the new package."""
    imported: set[str] = set()
    for path in sorted(Path("src/costsentinel/dashboard").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert not any(name.startswith("azure") for name in imported), imported
    assert not any("mgmt" in name for name in imported), imported


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def test_the_surface_is_off_by_default(tmp_path: Path) -> None:
    """A page somebody can reach should be something an operator turned on."""
    settings = _settings(tmp_path)
    assert not settings.dashboard_enabled
    assert not settings.dashboard_can_decide

    app = FastAPI()
    assert not mount(app, settings)
    assert TestClient(app).get(BASE_PATH).status_code == 404


def test_the_pages_render_from_stored_reports(tmp_path: Path) -> None:
    """Opening a page reads a checkpoint; it does not run a sweep."""
    settings = _scanned(tmp_path)
    app = FastAPI()
    assert mount(app, settings)
    client = TestClient(app)

    overview = client.get(BASE_PATH)
    assert overview.status_code == 200
    assert CLIENT_NORTHWIND in overview.text

    page = client.get(f"{BASE_PATH}/client/{CLIENT_NORTHWIND}")
    assert page.status_code == 200
    assert "Executive summary" in page.text

    queue = client.get(f"{BASE_PATH}/approvals")
    assert queue.status_code == 200
    assert "Awaiting a decision" in queue.text

    assert client.get(f"{BASE_PATH}/client/nobody").status_code == 404


def test_a_display_only_deployment_refuses_the_post(tmp_path: Path) -> None:
    """A hidden form field is not a permission check.

    So the route refuses rather than relying on the page not having offered a button.
    """
    settings = _scanned(tmp_path).model_copy(update={"dashboard_can_decide": False})
    app = FastAPI()
    mount(app, settings)
    client = TestClient(app)

    assert "Approve" not in client.get(f"{BASE_PATH}/approvals").text

    refused = client.post(
        f"{BASE_PATH}/decide",
        content="recommendation_id=rec-1&decision=approved&reason=x&approver=y",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 403
    assert "display-only" in refused.json()["detail"]


def test_the_post_records_a_decision_and_redirects(tmp_path: Path) -> None:
    """End to end over HTTP, in-process."""
    settings = _scanned(tmp_path, CLIENT_MINISTRY)
    app = FastAPI()
    mount(app, settings)
    client = _local(app)

    target = pending_queue(settings)[0].recommendation_id
    response = client.post(
        f"{BASE_PATH}/decide",
        content=(
            f"recommendation_id={target}&decision=approved"
            f"&reason=agreed+in+review&approver=ops%40example.com"
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{BASE_PATH}/approvals"

    assert target not in {one.recommendation_id for one in pending_queue(settings)}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("recommendation_id=r&decision=approved&approver=o", 400),
        ("recommendation_id=r&decision=maybe&reason=x&approver=o", 400),
        ("recommendation_id=nope&decision=approved&reason=x&approver=o", 409),
    ],
    ids=["no-reason", "unknown-decision", "not-pending"],
)
def test_a_malformed_decision_is_refused(tmp_path: Path, body: str, expected: int) -> None:
    """Validated before it reaches the gate, and refused by the gate when it gets there."""
    settings = _scanned(tmp_path, CLIENT_MINISTRY)
    app = FastAPI()
    mount(app, settings)
    response = _local(app).post(
        f"{BASE_PATH}/decide",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == expected


def test_no_route_mutates_anything(tmp_path: Path) -> None:
    """One POST, which records a decision. No PUT, PATCH or DELETE."""
    router = build_router(_settings(tmp_path))
    verbs: set[str] = set()
    posts: list[str] = []
    for route in router.routes:
        methods = getattr(route, "methods", set()) or set()
        verbs.update(methods)
        if "POST" in methods:
            posts.append(getattr(route, "path", ""))

    assert verbs & {"PUT", "PATCH", "DELETE"} == set()
    assert posts == [f"{BASE_PATH}/decide"]


def test_the_page_fetches_no_external_asset(view: ReportView) -> None:
    """A page that fetched a stylesheet would render differently with no network.

    And an operator reviewing a cost estate on a locked-down machine is exactly the
    person who would find out.
    """
    html = render_report(view)
    assert "<style>" in html
    assert "<script" not in html
    for pattern in ("http://", "https://", "//cdn", "@import", "<link"):
        assert pattern not in html, pattern
    assert not re.search(r"src\s*=", html)


# ---------------------------------------------------------------------------
# The static render
# ---------------------------------------------------------------------------


def test_the_static_render_writes_the_same_pages(tmp_path: Path) -> None:
    """So the surface is reviewable with no server and no browser."""
    settings = _scanned(tmp_path / "state")
    out = tmp_path / "out"
    written = write_static(settings, out)

    assert (out / INDEX) in written
    assert (out / APPROVALS) in written
    assert (out / client_filename(CLIENT_NORTHWIND)) in written

    page = (out / client_filename(CLIENT_NORTHWIND)).read_text(encoding="utf-8")
    assert "Executive summary" in page
    # No form, because a static page has nowhere to post to.
    assert "<form" not in page
    assert "Approve" not in page


def test_a_client_filename_is_safe(tmp_path: Path) -> None:
    """A client name reaches a filename, and provider data reaches a client name."""
    assert client_filename("northwind-energy") == "client-northwind-energy.html"
    assert "/" not in client_filename("a/../../b")
    assert "\\" not in client_filename("a\\b")
    assert ".." not in client_filename("../etc/passwd").replace("-", "")


def test_the_dashboard_module_is_the_only_caller_of_its_renderer() -> None:
    """One chain: report -> view -> HTML.

    If something else started rendering a page, there would be two answers to "what
    does the operator see", which is the situation the shared `ClientReport` exists to
    prevent.
    """
    callers = {
        path.name
        for path in Path("src/costsentinel").rglob("*.py")
        if "render_report(" in path.read_text(encoding="utf-8")
    }
    assert callers <= {"html.py", "app.py", "static.py"}, callers


def test_stored_reports_reads_the_newest_sweep_per_client(tmp_path: Path) -> None:
    """Two sweeps of one client show the later one, not both and not the first."""
    settings = _settings(tmp_path, enabled=True)
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        for run_id in ("run-older", "run-newer"):
            run_client_scan(
                client=CLIENT_MINISTRY,
                provider=MockAzureProvider(),
                llm=FakeLLM(),
                settings=settings,
                checkpointer=saver,
                run_id=run_id,
                requested_by="test",
            )

    views = stored_reports(settings)
    assert len(views) == 1
    assert views[0].client == CLIENT_MINISTRY
    assert views[0].run_id == "run-newer"


def test_the_estate_view_keeps_the_order_it_was_given(view: ReportView) -> None:
    """A view that sorted by savings would be deciding which client matters most."""
    second = view.model_copy(update={"client": "another-client"})
    assert estate_view((view, second)).clients == (view.client, "another-client")
    assert estate_view((second, view)).clients == ("another-client", view.client)


def test_the_dashboard_app_module_has_no_second_settings_source() -> None:
    """Every function takes its settings explicitly.

    So a page cannot read a different configuration from the one that mounted it --
    which would make "is this dashboard allowed to decide?" two questions with two
    answers.
    """
    text = Path("src/costsentinel/dashboard/app.py").read_text(encoding="utf-8")
    assert "get_settings()" not in text
    assert dashboard_app.BASE_PATH == "/dashboard"
