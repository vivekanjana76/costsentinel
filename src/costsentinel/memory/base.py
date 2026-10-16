"""The memory seam: what CostSentinel needs from a store, and nothing more.

A :class:`typing.Protocol` rather than an abstract base class, consistent with the
provider and LLM seams (ARCHITECTURE.md D2): an implementation need not import
anything from this package, and a test double is a class with the right methods.

Three properties are designed in rather than left to callers.

**Memory is optional, and absence is not an error.** A sweep with no store
configured must behave exactly as it does today. That is why
:class:`NullMemoryStore` exists: every caller holds a store unconditionally and no
node needs an ``if memory is not None`` branch. A tool that stops working when its
database is down is worse than one that never had a database.

**Empty is not a failure.** Every read against an empty store returns an empty
result. An estate nobody has scanned before misses on every lookup, which is correct
and expected, and the first run on a new client must not be different code.

**A write failure degrades the sweep; it never fails it.** Losing a memory write is
an inconvenience. Losing a client's report because a history could not be appended
is not acceptable, so the write path reports failure rather than raising.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from costsentinel.domain.memory import (
    EpisodicEntry,
    EpisodicRecord,
    MemoryAccess,
    RecommendationOutcome,
    SemanticChunk,
    SemanticHit,
)


class MemoryStoreError(RuntimeError):
    """A memory store could not satisfy a request.

    Not named ``MemoryError``: that is a Python builtin, and shadowing it means a
    bare ``raise MemoryError(...)`` elsewhere silently raises the builtin instead.
    """


class MemoryNotConfiguredError(MemoryStoreError):
    """A real store was selected without the configuration or dependency it needs.

    Raised at construction, so a misconfiguration is a startup error rather than a
    failure halfway through a sweep.
    """


class OutcomeAlreadyRecordedError(MemoryStoreError):
    """An outcome already exists for this recommendation.

    Raised rather than overwritten. A decision is a record of what a person said, and
    silently replacing one would lose the fact that they changed their mind -- which
    is itself worth knowing. The caller decides whether to supersede it.
    """


@runtime_checkable
class MemoryStore(Protocol):
    """Durable episodic and semantic memory for one deployment.

    Episodic methods are scoped by client in their signatures rather than filtered
    afterwards, which is how ARCHITECTURE.md D12 reaches the storage layer: there is
    no method that returns another client's history.
    """

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        ...

    @property
    def is_enabled(self) -> bool:
        """Whether this store actually persists anything.

        ``False`` for the null store, which lets a caller report "memory is off"
        without knowing which implementation it holds.
        """
        ...

    # --- episodic --------------------------------------------------------

    def record_run(self, record: EpisodicRecord) -> bool:
        """Append one run's history. Returns whether it was stored."""
        ...

    def record_outcome(self, outcome: RecommendationOutcome) -> bool:
        """Record a human decision about one recommendation.

        Raises:
            OutcomeAlreadyRecordedError: If one already exists and this is not an
                explicit supersession.
        """
        ...

    def outcomes_for(self, client: str) -> Sequence[RecommendationOutcome]:
        """Every outcome recorded for one client, newest first."""
        ...

    def outcome_for(self, recommendation_id: str) -> RecommendationOutcome | None:
        """The outcome recorded against one recommendation, or ``None``."""
        ...

    def entries_for(self, client: str, *, limit: int = 500) -> Sequence[EpisodicEntry]:
        """Recommendations this client has previously been shown, newest first."""
        ...

    def entry_for(self, recommendation_id: str) -> EpisodicEntry | None:
        """One previously recorded recommendation by id, or ``None``.

        What ``costsentinel decide`` resolves an id against, so an outcome cannot be
        recorded against a recommendation that was never proposed.
        """
        ...

    def runs_for(self, client: str, *, limit: int = 50) -> Sequence[EpisodicRecord]:
        """Run histories for one client, newest first, without their entries."""
        ...

    # --- semantic --------------------------------------------------------

    def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> int:
        """Store or replace retrievable passages. Returns how many were written."""
        ...

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 5,
        client: str | None = None,
    ) -> Sequence[SemanticHit]:
        """Nearest passages by cosine similarity.

        Args:
            embedding: The query vector.
            limit: Maximum hits to return.
            client: When given, general guidance *and* that client's own documents
                are searched. Another client's documents are never searched, with or
                without this argument.
        """
        ...

    def chunk_count(self) -> int:
        """How many passages are stored."""
        ...

    # --- lifecycle -------------------------------------------------------

    def accesses(self) -> Sequence[MemoryAccess]:
        """Reads and writes performed since the last reset, for the trace."""
        ...

    def reset_accesses(self) -> None:
        """Clear the access log, so one store can serve several runs."""
        ...

    def close(self) -> None:
        """Release any resources. Safe to call more than once."""
        ...
