"""The no-op store, used when memory is switched off.

This exists so that nothing else in the codebase needs an ``if memory is not None``
branch. Every caller holds a :class:`~costsentinel.memory.base.MemoryStore`
unconditionally; when memory is off it holds this one, every read returns empty and
every write returns ``False``, and the graph behaves exactly as it did before memory
existed.

The alternative -- an optional store and a conditional at each call site -- puts the
"memory is off" path in six places instead of one, and five of them are the ones
nobody tests.
"""

from __future__ import annotations

from collections.abc import Sequence

from costsentinel.domain.memory import (
    EpisodicEntry,
    EpisodicRecord,
    MemoryAccess,
    RecommendationOutcome,
    SemanticChunk,
    SemanticHit,
)


class NullMemoryStore:
    """A store that remembers nothing and says so."""

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "no-memory"

    @property
    def is_enabled(self) -> bool:
        """False, which is how a caller reports "memory is off" honestly."""
        return False

    # --- episodic --------------------------------------------------------

    def record_run(self, record: EpisodicRecord) -> bool:
        """Discard the record and report that nothing was stored."""
        _ = record
        return False

    def record_outcome(self, outcome: RecommendationOutcome) -> bool:
        """Discard the outcome and report that nothing was stored.

        Note this does *not* raise. The ``decide`` CLI checks
        :attr:`is_enabled` and refuses up front with an actionable message, because
        a person recording a decision deserves to be told it went nowhere -- but a
        graph node writing history should degrade quietly.
        """
        _ = outcome
        return False

    def outcomes_for(self, client: str) -> Sequence[RecommendationOutcome]:
        """Nothing."""
        _ = client
        return ()

    def outcome_for(self, recommendation_id: str) -> RecommendationOutcome | None:
        """Nothing."""
        _ = recommendation_id
        return None

    def entries_for(self, client: str, *, limit: int = 500) -> Sequence[EpisodicEntry]:
        """Nothing."""
        _ = (client, limit)
        return ()

    def entry_for(self, recommendation_id: str) -> EpisodicEntry | None:
        """Nothing."""
        _ = recommendation_id
        return None

    def runs_for(self, client: str, *, limit: int = 50) -> Sequence[EpisodicRecord]:
        """Nothing."""
        _ = (client, limit)
        return ()

    # --- semantic --------------------------------------------------------

    def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> int:
        """Discard the chunks and report that none were written."""
        _ = chunks
        return 0

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 5,
        client: str | None = None,
    ) -> Sequence[SemanticHit]:
        """Nothing."""
        _ = (embedding, limit, client)
        return ()

    def chunk_count(self) -> int:
        """Zero."""
        return 0

    # --- lifecycle -------------------------------------------------------

    def accesses(self) -> Sequence[MemoryAccess]:
        """Nothing: a store that does no work has nothing to trace."""
        return ()

    def reset_accesses(self) -> None:
        """Nothing to clear."""

    def close(self) -> None:
        """Nothing to release."""
