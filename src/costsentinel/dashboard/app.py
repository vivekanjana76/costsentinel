"""The operator surface: routes, and the one write it is allowed.

Off by default and mounted on the FastAPI app that already exists, so enabling it costs
no dependency and starts no second process. The same reasoning as the A2A surface: a
page somebody can reach should be something an operator turned on.

Reading
-------

Three pages -- the estate, one client, and the pending-approval queue. Each builds a
view model from a :class:`~costsentinel.domain.report.ClientReport` and renders it. The
client and queue pages read their reports back out of the **checkpoint**, not by
running a sweep: opening a page must not cost a provider call, and "what did the last
review find" is a different question from "what would a review find now".

Writing, through the one path
-----------------------------

`POST /dashboard/decide` records a decision, and it does so by calling the same three
things ``costsentinel approvals decide`` calls:
:func:`~costsentinel.graph.approvals.find_pending`,
:class:`~costsentinel.domain.governance.DecisionRequest` and
:func:`~costsentinel.graph.approvals.resume`. It constructs no
:class:`~costsentinel.domain.governance.ApprovalRecord`, validates nothing itself, and
has no path that bypasses the gate -- the gate validates the request against the queue
that actually exists, writes the record, and emits the audit events. A test asserts the
absence of a second implementation rather than trusting this paragraph (D124).

The four-way no-mutation invariant of Phase 10 is untouched: this module imports no
provider for the purpose of acting, no cloud client and nothing executable, and a
decision made here reaches exactly the terminal hand-off state a decision made on the
CLI reaches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from costsentinel.config import Settings
from costsentinel.dashboard.auth import (
    Authenticator,
    NullAuthenticator,
    check_decision_surface,
    may_decide,
)
from costsentinel.dashboard.html import render_estate, render_queue, render_report
from costsentinel.dashboard.view import (
    EstateView,
    PendingView,
    ReportView,
    estate_view,
    pending_view,
    report_view,
)
from costsentinel.domain.governance import ApproverAssurance, Decision, DecisionRequest
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import (
    ApprovalsError,
    approvals_graph,
    find_pending,
    paused_sweeps,
    resume,
    thread_ids,
)
from costsentinel.graph.checkpointer import checkpointer_for
from costsentinel.observability.logging import get_logger

_log = get_logger("dashboard.app")

#: Where the operator surface lives.
BASE_PATH = "/dashboard"


def stored_reports(settings: Settings) -> tuple[ReportView, ...]:
    """Every client's most recent report, read out of the checkpoint.

    One view per client, newest sweep wins. Reading rather than re-running is the whole
    point: opening a page costs a file read, not a provider call, and the figures shown
    are the ones somebody was shown -- which is what an operator comparing a page with
    a report needs.
    """
    with checkpointer_for(settings) as saver:
        graph = approvals_graph(settings, saver)
        pending_by_run = {sweep.run_id: sweep.pending for sweep in paused_sweeps(graph, saver)}

        latest: dict[str, ReportView] = {}
        for run_id in thread_ids(saver):
            snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
            if not snapshot.values:
                continue
            state = ScanState.model_validate(snapshot.values)
            if state.report is None:
                continue
            # `thread_ids` returns newest first, so the first report seen for a client
            # is its most recent and later ones are history.
            if state.client in latest:
                continue
            latest[state.client] = report_view(state.report, pending=pending_by_run.get(run_id, ()))

    return tuple(latest[client] for client in sorted(latest))


def pending_queue(settings: Settings) -> tuple[PendingView, ...]:
    """Everything awaiting a decision, across every paused sweep."""
    with checkpointer_for(settings) as saver:
        graph = approvals_graph(settings, saver)
        return tuple(
            pending_view(item) for sweep in paused_sweeps(graph, saver) for item in sweep.pending
        )


def estate(settings: Settings, *, generated_at: datetime | None = None) -> EstateView:
    """The overview across every client with a stored report."""
    return estate_view(stored_reports(settings), generated_at=generated_at)


def record_decision(
    settings: Settings,
    *,
    recommendation_id: str,
    decision: Decision,
    approver: str,
    reason: str,
    approver_assurance: ApproverAssurance = ApproverAssurance.SELF_ASSERTED,
    verified_by: str | None = None,
) -> str:
    """Record one decision through the shared path, and say what happened.

    Deliberately a thin call sequence rather than logic: locate, build the request,
    resume. Every rule about what a valid decision is lives in the gate, where the
    state is -- a check here as well would be a second place for those rules to drift,
    and the dashboard is not the place that should win such a disagreement.

    The assurance defaults to self-asserted, so a caller that says nothing about the
    approver's identity gets the weaker claim on the record, not the stronger one.

    Returns:
        The statement the gate composed, for the page to show back.

    Raises:
        ApprovalsError: If nothing is awaiting a decision on that recommendation.
    """
    with checkpointer_for(settings) as saver:
        graph = approvals_graph(settings, saver)
        sweep, pending = find_pending(paused_sweeps(graph, saver), recommendation_id)
        state = resume(
            graph,
            sweep.run_id,
            DecisionRequest(
                recommendation_id=pending.recommendation_id,
                decision=decision,
                approver=approver,
                reason=reason,
                approver_assurance=approver_assurance,
                verified_by=verified_by,
            ),
        )

    record = next(
        (
            one
            for one in reversed(state.approval_records)
            if one.recommendation_id == recommendation_id
        ),
        None,
    )
    if record is None:  # pragma: no cover -- the gate always records
        msg = f"the decision on {recommendation_id!r} was not recorded"
        raise ApprovalsError(msg)

    _log.info(
        "decision recorded from the dashboard",
        extra={
            "run_id": sweep.run_id,
            "recommendation_id": recommendation_id,
            "decision": decision.value,
            "approver": approver,
            "approver_assurance": approver_assurance.value,
            # Named so the audit trail distinguishes a decision made on a page from one
            # made in a terminal, even though both take the same path.
            "surface": "dashboard",
        },
    )
    return record.statement()


def _one(fields: Mapping[str, Sequence[str]], name: str) -> str:
    """One form field, stripped, or an empty string.

    Takes the *first* value for a repeated field rather than joining them. A browser
    sends one value per input; two would mean a crafted body, and picking the first is
    the behaviour least likely to smuggle something past the validation below.
    """
    values = fields.get(name, ())
    return values[0].strip() if values else ""


def build_router(settings: Settings, authenticator: Authenticator | None = None) -> APIRouter:
    """The dashboard routes.

    Whether a page offers the decision form is decided *per request*, by
    :func:`~costsentinel.dashboard.auth.may_decide`. A process can be bound more widely
    than its settings say, and the socket a request arrived on is the one fact about
    the bind the app can always see.
    """
    router = APIRouter(tags=["dashboard"])
    auth: Authenticator = authenticator or NullAuthenticator()
    verifies = auth.verifies

    @router.get(BASE_PATH, response_class=HTMLResponse)
    def overview() -> HTMLResponse:
        """Who is in scope, and what is waiting."""
        return HTMLResponse(render_estate(estate(settings)))

    @router.get(f"{BASE_PATH}/client/{{client}}", response_class=HTMLResponse)
    def client_report(client: str, request: Request) -> HTMLResponse:
        """One client's most recent report, with every section the document has."""
        view = next((one for one in stored_reports(settings) if one.client == client), None)
        if view is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no stored report for {client!r}. Run `costsentinel scan` first.",
            )
        return HTMLResponse(
            render_report(
                view, decidable=may_decide(settings, auth, request), verifies_identity=verifies
            )
        )

    @router.get(f"{BASE_PATH}/approvals", response_class=HTMLResponse)
    def approvals(request: Request) -> HTMLResponse:
        """Everything awaiting a decision."""
        items = pending_queue(settings)
        banner = next((one.banner for one in stored_reports(settings) if one.banner), "")
        return HTMLResponse(
            render_queue(
                items,
                decidable=may_decide(settings, auth, request),
                verifies_identity=verifies,
                banner=banner,
            )
        )

    @router.post(f"{BASE_PATH}/decide")
    async def decide(request: Request) -> RedirectResponse:
        """Record one decision and return to the queue.

        The body is parsed with :func:`urllib.parse.parse_qs` rather than FastAPI's
        ``Form``, which is not a style preference: ``Form`` requires
        ``python-multipart``, and this phase promised no new default dependency.
        Multipart exists for file uploads and this form has none -- an
        ``application/x-www-form-urlencoded`` body is a standard-library parse.

        Refused outright when `DASHBOARD_CAN_DECIDE` is off, so a deployment can serve
        the surface as a viewer and nothing else. A hidden form field is not a
        permission check.
        """
        fields = parse_qs((await request.body()).decode(), keep_blank_values=True)
        recommendation_id = _one(fields, "recommendation_id")
        decision = _one(fields, "decision")
        reason = _one(fields, "reason")
        approver = _one(fields, "approver")

        missing = [
            name
            for name, value in (
                ("recommendation_id", recommendation_id),
                ("decision", decision),
                ("reason", reason),
                ("approver", approver),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"a decision must state {', '.join(missing)}"
            )

        if not settings.dashboard_can_decide:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this dashboard is display-only; set DASHBOARD_CAN_DECIDE=true or use "
                "`costsentinel approvals decide`",
            )
        if not may_decide(settings, auth, request):
            # The start-up interlock judged the declared bind. This catches a server
            # bound more widely than that: the decision arrived from off this machine
            # and nothing can say who sent it (D126).
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "decisions are accepted only on a loopback address unless an "
                "authenticator is configured, and this one arrived on another address; "
                "use `costsentinel approvals decide` on the host",
            )
        try:
            chosen = Decision(decision)
        except ValueError as exc:
            allowed = ", ".join(sorted(member.value for member in Decision))
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{decision!r} is not a decision; expected {allowed}"
            ) from exc
        identity = auth.identify(request, approver)
        try:
            record_decision(
                settings,
                recommendation_id=recommendation_id,
                decision=chosen,
                approver=identity.name,
                reason=reason,
                approver_assurance=identity.assurance,
                verified_by=identity.verified_by,
            )
        except ApprovalsError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

        return RedirectResponse(f"{BASE_PATH}/approvals", status_code=status.HTTP_303_SEE_OTHER)

    return router


def mount(app: FastAPI, settings: Settings, authenticator: Authenticator | None = None) -> bool:
    """Attach the dashboard if this deployment enables it.

    Returns:
        Whether it was attached, so the caller can log it rather than infer it.

    Raises:
        UnsafeDecisionSurfaceError: If the configuration would serve an unauthenticated
            decision surface off loopback. Raised here, while the app is being built, so
            the process fails to start instead of serving with a warning.
    """
    if not settings.dashboard_enabled:
        _log.info("dashboard disabled")
        return False
    auth: Authenticator = authenticator or NullAuthenticator()
    check_decision_surface(settings, auth)
    app.include_router(build_router(settings, auth))
    _log.info(
        "dashboard enabled",
        extra={
            "path": BASE_PATH,
            "can_decide": settings.dashboard_can_decide,
            "bind_host": settings.bind_host,
            "authenticator": auth.name,
        },
    )
    return True
