"""Observability: structured logging now, Langfuse tracing from Phase 3.

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

    Phase 1 has no tracer. Reporting "disabled" rather than failing is the
    behaviour Phase 3 keeps: an unconfigured Langfuse never breaks a run.
    """
    if settings.tracing_enabled:
        return "langfuse configured (tracer arrives in Phase 3)"
    return "disabled (no Langfuse configuration; tracing is a no-op)"
