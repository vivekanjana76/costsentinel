"""Durable checkpointing.

SQLite locally, Postgres in compose from Phase 4 (ARCHITECTURE.md D15). SQLite is
used in development deliberately rather than an in-memory saver: if the resume path
is not exercised day to day, it will not work on the day it is needed -- and from
Phase 3 the resume path is also how the approval gate works, since the graph halts,
the process exits, and the run continues days later on a recorded decision.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from costsentinel import domain
from costsentinel.config import Settings


def domain_allowlist() -> tuple[tuple[str, str], ...]:
    """Every domain type a checkpoint may legitimately contain.

    LangGraph's deserialiser requires an explicit allowlist of ``(module, qualname)``
    pairs -- prefix matching is deliberately unsupported, so that a checkpoint cannot
    be used to instantiate an arbitrary class. The list is derived from
    ``costsentinel.domain.__all__`` rather than hand-maintained, so adding a domain
    type cannot silently break a resume.
    """
    allowed: set[tuple[str, str]] = set()
    for name in domain.__all__:
        candidate = getattr(domain, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel | enum.Enum):
            allowed.add((candidate.__module__, candidate.__qualname__))
    return tuple(sorted(allowed))


def apply_allowlist(saver: BaseCheckpointSaver[str]) -> BaseCheckpointSaver[str]:
    """Permit CostSentinel's own domain types to be read back from a checkpoint."""
    return saver.with_allowlist(domain_allowlist())


@contextmanager
def sqlite_checkpointer(path: Path) -> Generator[BaseCheckpointSaver[str]]:
    """Open a SQLite checkpointer at ``path``, creating its directory if needed.

    ``check_same_thread=False`` because the FastAPI app serves a graph run on a
    worker thread while the connection is created on another. The connection is
    closed on exit, so a crashed run leaves a committed checkpoint behind rather
    than a lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(connection)
        saver.setup()
        yield apply_allowlist(saver)
    finally:
        connection.close()


@contextmanager
def checkpointer_for(settings: Settings) -> Generator[BaseCheckpointSaver[str]]:
    """Open the checkpointer this configuration selects.

    Phase 1 has one implementation. The context-manager seam is what lets Phase 4
    swap in Postgres without touching a call site.
    """
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        yield saver
