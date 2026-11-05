"""FastAPI application.

The sweep and a health check, plus three surfaces that are each off until an operator
turns them on: the A2A endpoint, the operator dashboard and the Slack interaction
webhook. The scan runs synchronously; a mock sweep takes milliseconds, and a job queue
for long real-estate sweeps is still an open ROADMAP item. The durable checkpointer
that would make one safe is already wired.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from costsentinel import __version__
from costsentinel.a2a.server import mount
from costsentinel.config import Settings, get_settings
from costsentinel.dashboard.app import mount as mount_dashboard
from costsentinel.dashboard.auth import Authenticator
from costsentinel.domain.report import ScanResult
from costsentinel.graph.runner import ScanFailedError, run_scan
from costsentinel.llm.base import LLMError
from costsentinel.observability import tracing_status
from costsentinel.observability.logging import configure_logging
from costsentinel.providers.base import ProviderError
from costsentinel.slack.webhook import mount as mount_slack

API_TITLE = "CostSentinel"

#: Derived rather than restated. A hardcoded copy here had already drifted a
#: release behind the tag, which means the API was reporting a version that did
#: not exist -- not fatal, but exactly the kind of quiet untruth this codebase is
#: supposed to avoid.
API_VERSION = __version__


class ScanRequest(BaseModel):
    """A request to sweep the estate."""

    model_config = ConfigDict(extra="forbid")

    client: str | None = Field(
        default=None,
        description=(
            "Scan only this client. Omit to scan every client, each as its own run "
            "with its own report. Reports are never merged across clients."
        ),
    )
    requested_by: str = Field(
        default="api",
        max_length=200,
        description="Recorded on the run and in the audit trail.",
    )


class HealthResponse(BaseModel):
    """What the service is and what it is currently wired to."""

    status: str
    version: str
    mode: str
    llm_backend: str
    requires_no_credentials: bool
    memory: str
    tracing: str


def settings_dependency() -> Settings:
    """Provide the process settings, overridable in a test via ``dependency_overrides``."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dependency)]


def create_app(
    settings: Settings | None = None, authenticator: Authenticator | None = None
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: The configuration to mount the surfaces with. Defaults to the
            process settings; `costsentinel serve` passes its own so a ``--host``
            override is the bind the interlock judges.
        authenticator: Who stands behind a dashboard decision. Defaults to the
            null authenticator, which verifies nothing and is refused off loopback.

    Raises:
        UnsafeDecisionSurfaceError: When the dashboard would serve an unauthenticated
            decision surface on a non-loopback bind. The app is not built, so the
            server does not start (ARCHITECTURE.md D126).
        UnsafeSlackSurfaceError: When the Slack webhook is enabled with no signing
            secret to verify a request with (D134).
    """
    configured = settings or get_settings()
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        summary="Autonomous multi-agent FinOps governance for Azure estates.",
        description=(
            "Read-only: there is no execution path, so nothing this API returns, and "
            "no decision recorded through it, can have changed a resource."
        ),
    )

    # All off unless an operator turned them on.
    mount(app, configured)
    mount_dashboard(app, configured, authenticator)
    mount_slack(app, configured)

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health(settings: SettingsDep) -> HealthResponse:
        """Report liveness and the active backends."""
        configure_logging(settings)
        return HealthResponse(
            status="ok",
            version=API_VERSION,
            mode=settings.mode.value,
            llm_backend=settings.llm_backend.value,
            requires_no_credentials=settings.requires_no_credentials,
            memory="configured" if settings.memory_enabled else "disabled",
            tracing=tracing_status(settings),
        )

    @app.post(
        "/scan",
        response_model=ScanResult,
        status_code=status.HTTP_200_OK,
        tags=["scan"],
    )
    def scan(request: ScanRequest, settings: SettingsDep) -> ScanResult:
        """Run a sweep and return one report per client in scope.

        Raises:
            HTTPException: ``503`` when a provider or model backend is unavailable
                or unconfigured -- that is an operator-facing condition, not a bad
                request. ``500`` when a run completes without producing a report.
        """
        try:
            return run_scan(
                client=request.client,
                settings=settings,
                requested_by=request.requested_by,
            )
        except (ProviderError, LLMError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except ScanFailedError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc

    return app


def __getattr__(name: str) -> FastAPI:
    """The ASGI application, for ``uvicorn costsentinel.api.app:app``.

    Built on first access rather than at import. Building it is where the decision
    interlock runs, and an import-time build would make an unsafe environment break
    every importer, `costsentinel serve --host 127.0.0.1` included, before the override
    could be read. Uvicorn resolves ``:app`` through ``getattr``, so a server started
    that way still refuses to start.
    """
    if name == "app":
        return create_app()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
