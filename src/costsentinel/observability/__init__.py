"""Observability: structured logging, and Langfuse tracing since Phase 3.

Tracing is designed to be a no-op when unconfigured and never a hard dependency
(CLAUDE.md section 10), which is why :func:`tracing_status` reports rather than
raises when Langfuse is absent.
"""

from costsentinel.config import Settings
from costsentinel.observability.logging import (
    LOGGER_NAME,
    JsonFormatter,
    configure_logging,
    get_logger,
)

__all__ = [
    "LOGGER_NAME",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "tracing_status",
]


def tracing_status(settings: Settings) -> str:
    """Describe the tracing backend in one line, for a health endpoint or a log.

    Reporting "disabled" rather than failing is the behaviour the Phase 3 tracer
    kept: an unconfigured Langfuse never breaks a run.
    """
    if settings.tracing_enabled:
        return "langfuse configured"
    return "disabled (no Langfuse configuration; tracing is a no-op)"
