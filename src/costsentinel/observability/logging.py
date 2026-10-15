"""Structured JSON logging, correlated by run id.

One ``run_id`` ties a checkpoint, a log line and (from Phase 3) a Langfuse span
together, so a sweep across a dozen subscriptions can be reconstructed after the
fact. JSON rather than formatted text because these lines are read by a log
platform far more often than by a person.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from costsentinel.config import LogLevel, Settings

LOGGER_NAME = "costsentinel"

_LEVELS: dict[LogLevel, int] = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}

#: Attributes the standard library puts on every record; anything else on a record
#: is a field we added and should appear in the JSON payload.
_RESERVED: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | frozenset({"message", "asctime", "taskName"})


class JsonFormatter(logging.Formatter):
    """Renders a record, and any extra fields attached to it, as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a single-line JSON object."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in _RESERVED}
        )
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


class _CurrentStderrHandler(logging.StreamHandler[Any]):
    """A stream handler that resolves ``sys.stderr`` at write time.

    ``logging.StreamHandler`` captures the stream object when it is constructed, so
    a handler installed before anything replaced ``sys.stderr`` keeps writing to the
    old object afterwards. That is not only a test problem: a wrapper process, a
    supervisor that reopens stderr on log rotation, or a context manager that
    redirects output would all silently lose CostSentinel's log lines, and a cost
    tool whose audit trail can go quietly missing is worse than one that is loud.
    """

    @property
    def stream(self) -> Any:
        """The current ``sys.stderr``."""
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        """Ignored: the stream is always the current ``sys.stderr``."""


def configure_logging(settings: Settings) -> logging.Logger:
    """Install the JSON handler on CostSentinel's logger and return it.

    Idempotent: calling it twice does not double up handlers, which matters because
    both the CLI and the API configure logging on start-up.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_LEVELS[settings.log_level])
    logger.propagate = False
    if not logger.handlers:
        handler = _CurrentStderrHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def get_logger(component: str) -> logging.Logger:
    """A child logger for one component, for example ``agents.anomaly_scout``."""
    return logging.getLogger(f"{LOGGER_NAME}.{component}")
