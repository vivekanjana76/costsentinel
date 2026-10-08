"""FastAPI application.

Phase 1 exposes the sweep and a health check. The scan runs synchronously: a mock
sweep takes milliseconds, and introducing a job queue before there is a real
provider to make it slow would be scope the current phase has not earned. Phase 2
makes the scan a background job once real Azure calls make it long-running -- the
durable checkpointer that makes that safe is already wired.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from costsentinel.config import Settings, get_settings
from costsentinel.domain.report import ScanResult
from costsentinel.graph.runner import ScanFailedError, run_scan
from costsentinel.llm.base import LLMError
from costsentinel.observability import tracing_status
from costsentinel.observability.logging import configure_logging
from costsentinel.providers.base import ProviderError

API_TITLE = "CostSentinel"
API_VERSION = "0.1.0"


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
    phase: int
    mode: str
    llm_backend: str
    requires_no_credentials: bool
    memory: str
    tracing: str


def settings_dependency() -> Settings:
    """Provide the process settings, overridable in a test via ``dependency_overrides``."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dependency)]


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        summary="Autonomous multi-agent FinOps governance for Azure estates.",
        description=(
            "Phase 1 is read-only and mock-only: there is no execution path, so "
            "nothing this API returns can have changed a resource."
        ),
    )

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health(settings: SettingsDep) -> HealthResponse:
        """Report liveness and the active backends."""
        configure_logging(settings)
        return HealthResponse(
            status="ok",
            version=API_VERSION,
            phase=1,
            mode=settings.mode.value,
            llm_backend=settings.llm_backend.value,
            requires_no_credentials=settings.requires_no_credentials,
            memory="configured" if settings.memory_enabled else "disabled (Phase 4)",
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


#: The ASGI application, for ``uvicorn costsentinel.api.app:app``.
app = create_app()
