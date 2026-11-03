"""``POST /slack/interactions``: a button click, verified, then decided the one way.

The order is the security argument, so it is the order of the code:

1. **Read the raw bytes and verify the signature** (:func:`.signature.verify`). A
   missing, malformed, stale or forged request is answered ``401`` having parsed
   nothing, decided nothing and recorded nothing. Nothing about it reaches the ledger,
   because an unsigned request has no verified author to record a refusal against
   (D134).
2. **Parse** the verified body into a :class:`~.interaction.SlackClick`. Anything that
   is not one of this app's three buttons is a ``400``, again recording nothing.
3. **Establish the approver** from the signed payload: ``slack:<team>/<user>``, marked
   verified by the signature, plus any allowlisted user group Slack says they are in.
4. **Decide** with :func:`~costsentinel.approvals.service.decide`, the function the CLI
   and the dashboard call. Authorisation, the refusal record, idempotency on a repeated
   click and the answer for an already-decided item all happen there, once, for every
   surface (D133).
5. **Answer in Slack**, if a bot client is configured. A resolving decision replaces
   the buttons for everyone. A refusal, a deferral or a missing reason is said to the
   clicker alone, so one person's refused click cannot clear the buttons for the
   approvers who may still use them.

Off unless ``SLACK_ENABLED=true``, and on without a signing secret refuses to start.

What it cannot do: act. An approval from Slack advances the item to handed-off and
records it, exactly as one from the CLI does (D135).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response, status
from starlette.concurrency import run_in_threadpool

from costsentinel.approvals.identity import AuthorisationPolicy, Surface
from costsentinel.approvals.service import MissingReasonError, decide
from costsentinel.config import Settings
from costsentinel.graph.approvals import NothingPendingError
from costsentinel.observability.logging import get_logger
from costsentinel.slack.blocks import outcome_message
from costsentinel.slack.client import SlackClient, SlackError, client_from_settings
from costsentinel.slack.interaction import (
    InteractionError,
    SlackClick,
    SlackInteraction,
    parse_interaction,
)
from costsentinel.slack.signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify,
)

#: Where Slack's Interactivity Request URL should point.
INTERACTIONS_PATH = "/slack/interactions"

_log = get_logger("slack.webhook")


class UnsafeSlackSurfaceError(RuntimeError):
    """The webhook was enabled without the secret it needs to verify a request."""


def check_slack_surface(settings: Settings) -> str | None:
    """The signing secret, if the webhook should be served; ``None`` if it is off.

    Raises:
        UnsafeSlackSurfaceError: When it is enabled with no signing secret. The app
            is not built, so the server does not start.
    """
    if not settings.slack_enabled:
        return None
    secret = settings.slack_signing_secret
    if secret is None or not secret.get_secret_value():
        msg = (
            "refusing to start: SLACK_ENABLED=true with no SLACK_SIGNING_SECRET. Without "
            "it no request can be verified, and an unverified webhook would accept a "
            "decision from anybody who can reach it. Set the secret from the Slack app's "
            "Basic Information page, or set SLACK_ENABLED=false."
        )
        raise UnsafeSlackSurfaceError(msg)
    return secret.get_secret_value()


def _groups(
    click: SlackClick, policy: AuthorisationPolicy, client: SlackClient | None
) -> tuple[str, ...]:
    """The allowlisted groups this user is in, as Slack reports them. Fails closed."""
    prefix = f"slack-group:{click.team_id}/"
    wanted = sorted(
        {entry.principal for entry in policy.entries if entry.principal.startswith(prefix)}
    )
    if not wanted or client is None:
        return ()
    found: list[str] = []
    for principal in wanted:
        try:
            members = client.usergroup_members(principal.removeprefix(prefix))
        except SlackError as exc:
            _log.warning(
                "group membership unavailable; the group admits nobody",
                extra={"group": principal, "error": str(exc)},
            )
            continue
        if click.user_id in members:
            found.append(principal)
    return tuple(found)


def handle(settings: Settings, interaction: SlackInteraction, client: SlackClient | None) -> str:
    """Decide one verified click, answer in Slack, and return what was said.

    Synchronous and separate from the route, so a test can drive it without HTTP.
    """
    click = interaction.click
    policy = AuthorisationPolicy.from_settings(settings)
    identity = click.identity(_groups(click, policy, client))
    resolved = False
    try:
        outcome = decide(
            settings,
            recommendation_id=click.recommendation_id,
            decision=click.decision,
            reason=click.reason,
            identity=identity,
            surface=Surface.SLACK,
            local=False,
            interaction_key=click.interaction_key,
            policy=policy,
        )
    except NothingPendingError:
        statement = f"Nothing is waiting on {click.recommendation_id}; nothing was recorded."
    except MissingReasonError:
        statement = (
            "A reason is required with every decision. Type one in the box above and "
            "click again; nothing was recorded."
        )
    else:
        statement = outcome.statement
        resolved = outcome.record is not None and outcome.record.decision.resolves

    if client is not None and interaction.reply is not None:
        # A mock-mode process holds only the synthetic estate, so its replies say so.
        message = outcome_message(statement, synthetic=settings.is_mock_mode)
        try:
            client.respond(interaction.reply, message, replace=resolved)
        except SlackError as exc:
            # The decision stands either way: it is in the checkpoint and the ledger.
            _log.warning("could not answer in Slack", extra={"error": str(exc)})
    return statement


def mount(
    app: FastAPI,
    settings: Settings,
    client: SlackClient | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Serve the webhook when enabled. Does nothing, and adds no route, when off.

    Args:
        app: The application to add the route to.
        settings: Whether to serve, the signing secret and the window.
        client: Who answers in Slack. Defaults to the configured bot client, or none.
        clock: The current Unix time, injectable so a test can stand at any moment.

    Raises:
        UnsafeSlackSurfaceError: When enabled without a signing secret.
    """
    secret = check_slack_surface(settings)
    if secret is None:
        return
    replies = client if client is not None else client_from_settings(settings)

    @app.post(INTERACTIONS_PATH, include_in_schema=False)
    async def interactions(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        body = await request.body()
        try:
            verify(
                secret=secret,
                body=body,
                timestamp=request.headers.get(TIMESTAMP_HEADER),
                signature=request.headers.get(SIGNATURE_HEADER),
                now=clock(),
                window_seconds=settings.slack_signature_window_seconds,
            )
        except SignatureError as exc:
            _log.warning("unsigned Slack request refused", extra={"failure": exc.failure.value})
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            interaction = parse_interaction(body)
        except InteractionError as exc:
            _log.warning("signed request is not a decision click", extra={"error": str(exc)})
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        await run_in_threadpool(handle, settings, interaction, replies)
        return Response(status_code=status.HTTP_200_OK)
