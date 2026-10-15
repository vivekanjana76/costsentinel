"""PostgreSQL + pgvector memory, for a real deployment.

Selected only when ``MEMORY_BACKEND=postgres`` and ``DATABASE_URL`` are both set.
The SQLite store remains the default and the only one CI and the demo use, so
nothing here is on the path of a test run and ``psycopg`` is an optional extra,
imported lazily.

Two differences from the SQLite store, both deliberate.

**The vector comparison happens in the database.** SQLite ranks candidate rows in
Python, which is fine for an estate's worth of policy documents and wrong at scale.
Here the ``<=>`` cosine-distance operator does it, so the database returns the
nearest rows rather than all of them.

**Client scoping is in the SQL.** As it is in the SQLite store, and for the same
reason: a query that could return another client's policy and then discard it in
Python is one refactor away from returning it.

Everything else -- the schema shape, the JSON payload column, the newest-first
ordering, the refusal to overwrite an outcome -- matches the SQLite store, so a test
written against the default store is meaningful about this one's behaviour too. What
this module adds that cannot be covered offline is the SQL dialect and the vector
operator, which is exactly what the skipped integration test exercises when a
database is present.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportAttributeAccessIssue=false, reportMissingTypeStubs=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# psycopg and pgvector are optional extras, genuinely absent at type-check time in
# CI. The pragma is scoped to this one file.

from __future__ import annotations

import json
import time
from collections.abc import Sequence
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
from costsentinel.memory.base import MemoryNotConfiguredError, OutcomeAlreadyRecordedError
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.postgres")

INSTALL_HINT = "install the optional memory extra with: uv sync --extra memory"

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS episodic_runs (
    run_id      TEXT PRIMARY KEY,
    client      TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL
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
    recorded_at       TIMESTAMPTZ NOT NULL,
    payload           JSONB NOT NULL
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
    decided_at        TIMESTAMPTZ NOT NULL,
    payload           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_client ON episodic_outcomes (client, decided_at DESC);
"""

#: Separate because the vector width is interpolated, so it cannot be a static
#: statement. The width comes from configuration, never from user input.
_CHUNKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    client      TEXT,
    embedder    TEXT NOT NULL,
    embedding   vector({dimensions}),
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_client ON semantic_chunks (client);
"""


class PostgresMemoryStore:
    """Episodic and semantic memory in PostgreSQL with pgvector."""

    def __init__(self, dsn: str, *, dimensions: int = 256, connection: Any = None) -> None:
        """Connect and create the schema idempotently.

        Args:
            dsn: PostgreSQL connection string.
            dimensions: Vector width for the ``semantic_chunks`` column.
            connection: An already-open connection, which is how the integration
                test and a pooled deployment supply one.

        Raises:
            MemoryNotConfiguredError: If ``psycopg`` is absent, or the connection
                cannot be made. Raised at construction, so a bad DSN is a startup
                error rather than a failure halfway through a sweep.
        """
        self._dsn = dsn
        self._dimensions = max(16, dimensions)
        self._accesses: list[MemoryAccess] = []
        self._closed = False
        self._owns_connection = connection is None
        self._db = connection if connection is not None else self._connect(dsn)
        self._setup()

    @staticmethod
    def _connect(dsn: str) -> Any:
        try:
            psycopg = __import__("psycopg")
        except ImportError as exc:
            msg = f"psycopg is required for MEMORY_BACKEND=postgres; {INSTALL_HINT}"
            raise MemoryNotConfiguredError(msg) from exc
        try:
            return psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            # The DSN may carry a password, so the exception text is reported and the
            # DSN is not. An error message that leaks a credential into a log is a
            # worse problem than the connection failure it describes.
            msg = f"could not connect to the memory database: {exc}"
            raise MemoryNotConfiguredError(msg) from exc

    def _setup(self) -> None:
        with self._db.cursor() as cursor:
            cursor.execute(_SCHEMA)
            cursor.execute(_CHUNKS_SCHEMA.format(dimensions=self._dimensions))

    # --- identity --------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "postgres-pgvector"

    @property
    def is_enabled(self) -> bool:
        """This store persists, so always true."""
        return True

    # --- episodic writes -------------------------------------------------

    def record_run(self, record: EpisodicRecord) -> bool:
        """Append one run's history, replacing any record for the same run id."""
        started = time.perf_counter()
        try:
            with self._db.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO episodic_runs (run_id, client, recorded_at, payload) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO UPDATE SET "
                    "client = EXCLUDED.client, recorded_at = EXCLUDED.recorded_at, "
                    "payload = EXCLUDED.payload",
                    (record.run_id, record.client, record.recorded_at, record.model_dump_json()),
                )
                for entry in record.entries:
                    cursor.execute(
                        "INSERT INTO episodic_entries (recommendation_id, run_id, client, "
                        " subscription_id, resource_id, waste_kind, action, recorded_at, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (recommendation_id) DO UPDATE SET "
                        "run_id = EXCLUDED.run_id, recorded_at = EXCLUDED.recorded_at, "
                        "payload = EXCLUDED.payload",
                        (
                            entry.recommendation_id,
                            entry.run_id,
                            entry.client,
                            entry.subscription_id,
                            entry.target_resource_id,
                            entry.waste_kind.value,
                            entry.action.value,
                            entry.recorded_at,
                            entry.model_dump_json(),
                        ),
                    )
        except Exception as exc:
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
                f"{outcome.recommendation_id} was already {existing.outcome.value} on "
                f"{existing.decided_at.date().isoformat()} by {existing.decided_by} "
                f"({existing.reason})."
            )
            raise OutcomeAlreadyRecordedError(msg)
        try:
            with self._db.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO episodic_outcomes (outcome_id, recommendation_id, client, "
                    " resource_id, waste_kind, action, outcome, decided_at, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        outcome.outcome_id,
                        outcome.recommendation_id,
                        outcome.client,
                        outcome.target_resource_id,
                        outcome.waste_kind.value,
                        outcome.action.value,
                        outcome.outcome.value,
                        outcome.decided_at,
                        outcome.model_dump_json(),
                    ),
                )
        except Exception as exc:
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
        rows = self._query(
            "SELECT payload FROM episodic_outcomes WHERE client = %s ORDER BY decided_at DESC",
            (client,),
        )
        outcomes = _parse_all(RecommendationOutcome, rows)
        self._record(
            MemoryOperation.READ_OUTCOMES,
            client=client,
            hit=bool(outcomes),
            count=len(outcomes),
            started=started,
        )
        return outcomes

    def outcome_for(self, recommendation_id: str) -> RecommendationOutcome | None:
        """The outcome recorded against one recommendation, or ``None``."""
        rows = self._query(
            "SELECT payload FROM episodic_outcomes WHERE recommendation_id = %s LIMIT 1",
            (recommendation_id,),
        )
        parsed = _parse_all(RecommendationOutcome, rows)
        return parsed[0] if parsed else None

    def entries_for(self, client: str, *, limit: int = 500) -> Sequence[EpisodicEntry]:
        """Recommendations this client has previously been shown, newest first."""
        started = time.perf_counter()
        rows = self._query(
            "SELECT payload FROM episodic_entries WHERE client = %s "
            "ORDER BY recorded_at DESC LIMIT %s",
            (client, max(1, limit)),
        )
        entries = _parse_all(EpisodicEntry, rows)
        self._record(
            MemoryOperation.READ_ENTRIES,
            client=client,
            hit=bool(entries),
            count=len(entries),
            started=started,
        )
        return entries

    def entry_for(self, recommendation_id: str) -> EpisodicEntry | None:
        """One previously recorded recommendation by id, or ``None``."""
        rows = self._query(
            "SELECT payload FROM episodic_entries WHERE recommendation_id = %s LIMIT 1",
            (recommendation_id,),
        )
        parsed = _parse_all(EpisodicEntry, rows)
        return parsed[0] if parsed else None

    def runs_for(self, client: str, *, limit: int = 50) -> Sequence[EpisodicRecord]:
        """Run histories for one client, newest first."""
        rows = self._query(
            "SELECT payload FROM episodic_runs WHERE client = %s "
            "ORDER BY recorded_at DESC LIMIT %s",
            (client, max(1, limit)),
        )
        return _parse_all(EpisodicRecord, rows)

    # --- semantic --------------------------------------------------------

    def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> int:
        """Store or replace retrievable passages."""
        started = time.perf_counter()
        written = 0
        try:
            with self._db.cursor() as cursor:
                for chunk in chunks:
                    cursor.execute(
                        "INSERT INTO semantic_chunks (chunk_id, document_id, kind, client, "
                        " embedder, embedding, payload) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (chunk_id) DO UPDATE SET "
                        "embedder = EXCLUDED.embedder, embedding = EXCLUDED.embedding, "
                        "payload = EXCLUDED.payload",
                        (
                            chunk.chunk_id,
                            chunk.document_id,
                            chunk.kind.value,
                            chunk.client,
                            chunk.embedder,
                            _vector(chunk.embedding),
                            chunk.model_dump_json(),
                        ),
                    )
                    written += 1
        except Exception as exc:
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
        """Nearest passages, ranked by the database's cosine-distance operator.

        ``1 - (embedding <=> query)`` converts pgvector's cosine *distance* into the
        cosine *similarity* the domain type carries, so a hit's score means the same
        thing whichever store produced it.
        """
        started = time.perf_counter()
        query = _vector(embedding)
        sql = (
            "SELECT payload, 1 - (embedding <=> %s) AS score FROM semantic_chunks "
            "WHERE embedding IS NOT NULL AND (client IS NULL{client_clause}) "
            "ORDER BY embedding <=> %s LIMIT %s"
        )
        if client is None:
            rows = self._query(
                sql.format(client_clause=""), (query, query, max(0, limit)), with_score=True
            )
        else:
            rows = self._query(
                sql.format(client_clause=" OR client = %s"),
                (query, client, query, max(0, limit)),
                with_score=True,
            )

        hits: list[SemanticHit] = []
        for row in rows:
            chunk = _parse(SemanticChunk, row[0])
            if chunk is None:
                continue
            score = max(-1.0, min(1.0, float(row[1])))
            hits.append(SemanticHit(chunk=chunk, score=score))
        self._record(
            MemoryOperation.SEARCH,
            client=client or "",
            hit=bool(hits),
            count=len(hits),
            started=started,
        )
        return tuple(hits)

    def chunk_count(self) -> int:
        """How many passages are stored."""
        with self._db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM semantic_chunks")
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    # --- lifecycle -------------------------------------------------------

    def accesses(self) -> Sequence[MemoryAccess]:
        """Reads and writes performed since the last reset."""
        return tuple(self._accesses)

    def reset_accesses(self) -> None:
        """Clear the access log."""
        self._accesses.clear()

    def close(self) -> None:
        """Close the connection, if this store opened it."""
        if not self._closed and self._owns_connection:
            self._db.close()
        self._closed = True

    # --- internals -------------------------------------------------------

    def _query(
        self, sql: str, params: tuple[Any, ...], *, with_score: bool = False
    ) -> list[tuple[Any, ...]]:
        with self._db.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        if with_score:
            return [(row[0], row[1]) for row in rows]
        return [(row[0],) for row in rows]

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


def _vector(embedding: Sequence[float]) -> str:
    """Render a vector in pgvector's literal syntax.

    A string rather than a registered adapter so this store works against a plain
    ``psycopg`` connection without ``pgvector.psycopg.register_vector``, which keeps
    the dependency surface to one package.
    """
    return "[" + ",".join(f"{value:.9g}" for value in embedding) + "]"


def _parse[ModelT: (EpisodicEntry, EpisodicRecord, RecommendationOutcome, SemanticChunk)](
    model: type[ModelT], payload: Any
) -> ModelT | None:
    """Parse one stored row, returning ``None`` if it no longer validates."""
    try:
        if isinstance(payload, str):
            return model.model_validate_json(payload)
        return model.model_validate(payload)
    except (ValidationError, json.JSONDecodeError, TypeError) as exc:
        _log.warning(
            "skipping an unreadable memory row",
            extra={"model": model.__name__, "error": str(exc)},
        )
        return None


def _parse_all[ModelT: (EpisodicEntry, EpisodicRecord, RecommendationOutcome, SemanticChunk)](
    model: type[ModelT], rows: Sequence[tuple[Any, ...]]
) -> tuple[ModelT, ...]:
    """Parse every stored row, skipping any that no longer validates."""
    parsed = (_parse(model, row[0]) for row in rows)
    return tuple(item for item in parsed if item is not None)
