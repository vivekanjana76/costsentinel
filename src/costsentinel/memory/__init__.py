"""Memory: what CostSentinel remembers about an estate, and what it learned.

:func:`get_memory` is the only place a concrete store is named, mirroring
``providers.get_provider`` and ``llm.get_llm``. Everything else depends on the
:class:`~costsentinel.memory.base.MemoryStore` protocol, so swapping SQLite for
Postgres -- or switching memory off entirely -- is a configuration change.

The default is the SQLite store, which needs no service, no network and no
dependency beyond the standard library. That is deliberate: a learning loop nobody
can run without provisioning a database is a learning loop nobody demonstrates.
"""

from costsentinel.config import MemoryBackend, Settings
from costsentinel.memory.base import (
    MemoryNotConfiguredError,
    MemoryStore,
    MemoryStoreError,
    OutcomeAlreadyRecordedError,
)
from costsentinel.memory.embedding import (
    MIN_TOKEN_LENGTH,
    AzureOpenAIEmbedder,
    Embedder,
    FakeEmbedder,
    get_embedder,
    tokenise,
)
from costsentinel.memory.null import NullMemoryStore
from costsentinel.memory.similarity import cosine_similarity
from costsentinel.memory.sqlite import SCHEMA_VERSION, SqliteMemoryStore

__all__ = [
    "MIN_TOKEN_LENGTH",
    "SCHEMA_VERSION",
    "AzureOpenAIEmbedder",
    "Embedder",
    "FakeEmbedder",
    "MemoryNotConfiguredError",
    "MemoryStore",
    "MemoryStoreError",
    "NullMemoryStore",
    "OutcomeAlreadyRecordedError",
    "SqliteMemoryStore",
    "cosine_similarity",
    "get_embedder",
    "get_memory",
    "tokenise",
]


def get_memory(settings: Settings) -> MemoryStore:
    """Build the memory store this configuration selects.

    Args:
        settings: Resolved configuration. ``MEMORY_BACKEND=sqlite`` (the default)
            yields a local SQLite store; ``postgres`` yields the pgvector store and
            requires ``DATABASE_URL``; ``off`` yields the no-op store.

    Returns:
        An object satisfying :class:`MemoryStore`.

    Raises:
        MemoryNotConfiguredError: If a real store is selected but cannot be built
            with the configuration given.
    """
    match settings.memory_backend:
        case MemoryBackend.OFF:
            return NullMemoryStore()
        case MemoryBackend.SQLITE:
            return SqliteMemoryStore(settings.memory_db_path)
        case MemoryBackend.POSTGRES:
            if settings.database_url is None:
                msg = (
                    "MEMORY_BACKEND=postgres requires DATABASE_URL. Set "
                    "MEMORY_BACKEND=sqlite (the default) to keep a local history "
                    "with no database, or MEMORY_BACKEND=off for none at all."
                )
                raise MemoryNotConfiguredError(msg)
            # Imported here, not at module scope: psycopg and pgvector are optional
            # extras, and importing this package must not require them.
            from costsentinel.memory.postgres import PostgresMemoryStore  # noqa: PLC0415

            return PostgresMemoryStore(
                settings.database_url.get_secret_value(),
                dimensions=settings.embedding_dimensions,
            )
