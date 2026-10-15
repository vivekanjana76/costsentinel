"""The default memory store: SQLite, or in-process for a test.

This is the implementation the demo and the whole of CI use. It needs no service, no
network and no dependency beyond the standard library's ``sqlite3``, which is the
only way "memory works offline" can be a property rather than an aspiration.

It is a real store, not a stub. The same schema, the same client scoping, the same
ordering and the same cosine ranking as the Postgres implementation -- the difference
is where the vector comparison happens: here in Python over the candidate rows,
there in the database. For an estate's worth of policy documents that is a few
hundred comparisons, which is nothing; at a scale where it matters, the pgvector
store exists.

``path=None`` gives an in-memory database that lives as long as the store, which is
what tests use. A path gives a file that survives a restart, which is what the demo
uses -- a learning loop you have to re-seed every time you run it is not a learning
loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from costsentinel.domain.memory import (
    EpisodicEntry,
    EpisodicRecord,
    MemoryAccess,
    MemoryOperation,
    RecommendationOutcome,
    SemanticChunk,
    SemanticHit,
)
from costsentinel.memory.base import OutcomeAlreadyRecordedError
from costsentinel.memory.similarity import cosine_similarity
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.sqlite")

#: Bumped when the schema changes incompatibly. A store at an older version is
#: rejected loudly rather than read with the wrong column meanings -- the same
#: posture as the price catalogue's schema check.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS episodic_runs (
    run_id       TEXT PRIMARY KEY,
    client       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_client ON episodic_runs (client, recorded_at DESC);

CREATE TABLE IF NOT EXISTS episodic_entries (
    recommendation_id TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    client            TEXT NOT NULL,
    subscription_id   TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    waste_kind        TEXT NOT NULL,
    action            TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    payload           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_client ON episodic_entries (client, recorded_at DESC);

CREATE TABLE IF NOT EXISTS episodic_outcomes (
    outcome_id        TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL UNIQUE,
    client            TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    waste_kind        TEXT NOT NULL,
    action            TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    decided_at        TEXT NOT NULL,
    payload           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_client ON episodic_outcomes (client, decided_at DESC);

CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    client      TEXT,
    embedder    TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_client ON semantic_chunks (client);
"""


class SqliteMemoryStore:
    """Episodic and semantic memory in a local SQLite database."""

    def __init__(self, path: Path | None = None) -> None:
        """Open or create the store.

        Args:
            path: Database file. ``None`` uses an in-memory database that lives only
                as long as this object, which is what tests want.
        """
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False for the same reason as the checkpointer: the API
        # serves a graph run on a worker thread while the store is built on another.
        self._db = sqlite3.connect(
            str(path) if path is not None else ":memory:", check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._accesses: list[MemoryAccess] = []
        self._closed = False
        self._read_failed = False
        self._setup()

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run one query, returning no rows if the database cannot be read.

        Reads degrade for the same reason writes do, and it took a test to notice:
        a closed or corrupt store was raising out of the middle of a sweep, which
        would cost a client their report over a memory lookup. A read that cannot
        happen is indistinguishable, to every caller here, from a read that found
        nothing -- and finding nothing is already a supported outcome.

        The failure is still recorded as a failure rather than as a clean miss. The
        two look the same to a caller and must not look the same in the metrics: one
        is an estate nobody has scanned, the other is a broken store.
        """
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            _log.warning(
                "memory read failed; continuing as though it found nothing",
                extra={"store": self.name, "error": str(exc)},
            )
            self._read_failed = True
            return []

    def _take_read_failure(self) -> bool:
        """Whether the most recent read failed, clearing the flag."""
        failed = self._read_failed
        self._read_failed = False
        return failed

    def _setup(self) -> None:
        with self._db:
            self._db.executescript(_SCHEMA)
            # Direct, not through ``_read``: a schema check that degraded to "found
            # nothing" would silently re-initialise a store it could not read.
            row = self._db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                self._db.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                msg = (
                    f"memory store at {self._path or ':memory:'} is schema "
                    f"{row['version']}, but this build expects {SCHEMA_VERSION}. "
                    f"Delete the file to start a fresh history, or migrate it."
                )
                raise RuntimeError(msg)

    # --- identity --------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "sqlite-memory" if self._path is not None else "in-memory"

    @property
    def is_enabled(self) -> bool:
        """This store persists, so always true."""
        return True

    @property
    def path(self) -> Path | None:
        """Where the database lives, or ``None`` for in-memory."""
        return self._path

    # --- episodic writes -------------------------------------------------

    def record_run(self, record: EpisodicRecord) -> bool:
        """Append one run's history, replacing any record for the same run id.

        Replacing rather than erroring because a resumed sweep legitimately re-writes
        its own run: the checkpointer may replay the report node after a crash, and
        the second write is the same run's final state, not a different run.
        """
        started = time.perf_counter()
        try:
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO episodic_runs "
                    "(run_id, client, recorded_at, payload) VALUES (?, ?, ?, ?)",
                    (
                        record.run_id,
                        record.client,
                        record.recorded_at.isoformat(),
                        record.model_dump_json(),
                    ),
                )
                for entry in record.entries:
                    self._db.execute(
                        "INSERT OR REPLACE INTO episodic_entries "
                        "(recommendation_id, run_id, client, subscription_id, resource_id, "
                        " waste_kind, action, recorded_at, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry.recommendation_id,
                            entry.run_id,
                            entry.client,
                            entry.subscription_id,
                            entry.target_resource_id,
                            entry.waste_kind.value,
                            entry.action.value,
                            entry.recorded_at.isoformat(),
                            entry.model_dump_json(),
                        ),
                    )
        except sqlite3.Error as exc:
            self._record(
                MemoryOperation.RECORD_RUN,
                client=record.client,
                hit=False,
                started=started,
                succeeded=False,
                detail=str(exc),
            )
            _log.warning(
                "could not write episodic history; the sweep continues",
                extra={"run_id": record.run_id, "client": record.client, "error": str(exc)},
            )
            return False

        self._record(
            MemoryOperation.RECORD_RUN,
            client=record.client,
            hit=True,
            count=record.entry_count,
            started=started,
        )
        return True

    def record_outcome(self, outcome: RecommendationOutcome) -> bool:
        """Record a human decision, refusing to silently replace an existing one."""
        started = time.perf_counter()
        existing = self.outcome_for(outcome.recommendation_id)
        if existing is not None:
            msg = (
                f"{outcome.recommendation_id} was already "
                f"{existing.outcome.value} on "
                f"{existing.decided_at.date().isoformat()} by {existing.decided_by} "
                f"({existing.reason}). A decision is a record of what somebody said; "
                f"overwriting it would lose the fact that they changed their mind."
            )
            raise OutcomeAlreadyRecordedError(msg)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO episodic_outcomes "
                    "(outcome_id, recommendation_id, client, resource_id, waste_kind, "
                    " action, outcome, decided_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        outcome.outcome_id,
                        outcome.recommendation_id,
                        outcome.client,
                        outcome.target_resource_id,
                        outcome.waste_kind.value,
                        outcome.action.value,
                        outcome.outcome.value,
                        outcome.decided_at.isoformat(),
                        outcome.model_dump_json(),
                    ),
                )
        except sqlite3.Error as exc:
            self._record(
                MemoryOperation.RECORD_OUTCOME,
                client=outcome.client,
                hit=False,
                started=started,
                succeeded=False,
                detail=str(exc),
            )
            return False
        self._record(
            MemoryOperation.RECORD_OUTCOME,
            client=outcome.client,
            hit=True,
            count=1,
            started=started,
        )
        return True

    # --- episodic reads --------------------------------------------------

    def outcomes_for(self, client: str) -> Sequence[RecommendationOutcome]:
        """Every outcome recorded for one client, newest first."""
        started = time.perf_counter()
        rows = self._read(
            "SELECT payload FROM episodic_outcomes WHERE client = ? ORDER BY decided_at DESC",
            (client,),
        )
        outcomes = tuple(
            parsed
            for parsed in (_load(RecommendationOutcome, row["payload"]) for row in rows)
            if parsed is not None
        )
        self._record(
            MemoryOperation.READ_OUTCOMES,
            client=client,
            hit=bool(outcomes),
            count=len(outcomes),
            started=started,
            succeeded=not self._take_read_failure(),
        )
        return outcomes

    def outcome_for(self, recommendation_id: str) -> RecommendationOutcome | None:
        """The outcome recorded against one recommendation, or ``None``."""
        row = _first(
            self._read(
                "SELECT payload FROM episodic_outcomes WHERE recommendation_id = ?",
                (recommendation_id,),
            )
        )
        return _load(RecommendationOutcome, row["payload"]) if row is not None else None

    def entries_for(self, client: str, *, limit: int = 500) -> Sequence[EpisodicEntry]:
        """Recommendations this client has previously been shown, newest first."""
        started = time.perf_counter()
        rows = self._read(
            "SELECT payload FROM episodic_entries WHERE client = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (client, max(1, limit)),
        )
        entries = tuple(
            parsed
            for parsed in (_load(EpisodicEntry, row["payload"]) for row in rows)
            if parsed is not None
        )
        self._record(
            MemoryOperation.READ_ENTRIES,
            client=client,
            hit=bool(entries),
            count=len(entries),
            started=started,
            succeeded=not self._take_read_failure(),
        )
        return entries

    def entry_for(self, recommendation_id: str) -> EpisodicEntry | None:
        """One previously recorded recommendation by id, or ``None``."""
        row = _first(
            self._read(
                "SELECT payload FROM episodic_entries WHERE recommendation_id = ?",
                (recommendation_id,),
            )
        )
        return _load(EpisodicEntry, row["payload"]) if row is not None else None

    def runs_for(self, client: str, *, limit: int = 50) -> Sequence[EpisodicRecord]:
        """Run histories for one client, newest first."""
        rows = self._read(
            "SELECT payload FROM episodic_runs WHERE client = ? ORDER BY recorded_at DESC LIMIT ?",
            (client, max(1, limit)),
        )
        return tuple(
            parsed
            for parsed in (_load(EpisodicRecord, row["payload"]) for row in rows)
            if parsed is not None
        )

    # --- semantic --------------------------------------------------------

    def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> int:
        """Store or replace retrievable passages."""
        started = time.perf_counter()
        written = 0
        try:
            with self._db:
                for chunk in chunks:
                    self._db.execute(
                        "INSERT OR REPLACE INTO semantic_chunks "
                        "(chunk_id, document_id, kind, client, embedder, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            chunk.chunk_id,
                            chunk.document_id,
                            chunk.kind.value,
                            chunk.client,
                            chunk.embedder,
                            chunk.model_dump_json(),
                        ),
                    )
                    written += 1
        except sqlite3.Error as exc:
            self._record(
                MemoryOperation.UPSERT_CHUNKS,
                hit=False,
                started=started,
                succeeded=False,
                detail=str(exc),
            )
            return 0
        self._record(
            MemoryOperation.UPSERT_CHUNKS, hit=bool(written), count=written, started=started
        )
        return written

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 5,
        client: str | None = None,
    ) -> Sequence[SemanticHit]:
        """Nearest passages by cosine similarity, ranked in Python.

        The client filter is in the SQL, not applied to the result afterwards: a
        query that could return another client's policy and then discard it is one
        refactor away from returning it.
        """
        started = time.perf_counter()
        if client is None:
            rows = self._read("SELECT payload FROM semantic_chunks WHERE client IS NULL")
        else:
            rows = self._read(
                "SELECT payload FROM semantic_chunks WHERE client IS NULL OR client = ?",
                (client,),
            )

        scored: list[SemanticHit] = []
        for row in rows:
            chunk = _load(SemanticChunk, row["payload"])
            if chunk is None:
                continue
            scored.append(
                SemanticHit(chunk=chunk, score=cosine_similarity(embedding, chunk.embedding))
            )
        # Ties break on chunk id so retrieval is reproducible rather than dependent
        # on row order, which matters because an eval asserts on what came back.
        scored.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        hits = tuple(scored[: max(0, limit)])
        self._record(
            MemoryOperation.SEARCH,
            client=client or "",
            hit=bool(hits),
            count=len(hits),
            started=started,
            succeeded=not self._take_read_failure(),
        )
        return hits

    def chunk_count(self) -> int:
        """How many passages are stored."""
        row = _first(self._read("SELECT COUNT(*) AS n FROM semantic_chunks"))
        return int(row["n"]) if row is not None else 0

    # --- lifecycle -------------------------------------------------------

    def accesses(self) -> Sequence[MemoryAccess]:
        """Reads and writes performed since the last reset."""
        return tuple(self._accesses)

    def reset_accesses(self) -> None:
        """Clear the access log."""
        self._accesses.clear()

    def close(self) -> None:
        """Close the database. Safe to call more than once."""
        if not self._closed:
            self._db.close()
            self._closed = True

    # --- internals -------------------------------------------------------

    def _record(
        self,
        operation: MemoryOperation,
        *,
        hit: bool,
        started: float,
        client: str = "",
        count: int = 0,
        succeeded: bool = True,
        detail: str = "",
    ) -> None:
        self._accesses.append(
            MemoryAccess(
                operation=operation,
                store=self.name,
                client=client,
                hit=hit,
                record_count=count,
                latency_ms=int((time.perf_counter() - started) * 1000),
                succeeded=succeeded,
                detail=detail,
            )
        )


def _first(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The first row, or ``None``."""
    return rows[0] if rows else None


def _load[ModelT: (EpisodicEntry, EpisodicRecord, RecommendationOutcome, SemanticChunk)](
    model: type[ModelT], payload: Any
) -> ModelT | None:
    """Parse a stored row, returning ``None`` if it no longer validates.

    A row written by an older build whose schema has since changed is skipped with a
    warning rather than raising. Memory is an optimisation: one unreadable historical
    record should cost a client a slightly worse ranking, not their report.
    """
    try:
        return model.model_validate_json(str(payload))
    except (ValidationError, json.JSONDecodeError) as exc:
        _log.warning(
            "skipping an unreadable memory row",
            extra={"model": model.__name__, "error": str(exc)},
        )
        return None
