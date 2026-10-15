"""The memory stores, the embedder, and the factory that chooses between them.

Everything here runs against the SQLite store, which is the default and the only one
CI uses. The Postgres store gets one integration test at the bottom, skipped unless
a database URL is present, so CI never needs a database.

What is being tested is mostly the behaviour that makes memory safe to depend on:
empty reads return empty rather than raising, a write failure degrades rather than
propagating, a decision is never silently overwritten, and one client's history is
unreachable from another's query.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from costsentinel.config import EmbedderBackend, MemoryBackend, Settings
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.memory import (
    EpisodicEntry,
    EpisodicRecord,
    GuidanceKind,
    MemoryAccess,
    MemoryMetrics,
    MemoryOperation,
    Outcome,
    RecommendationOutcome,
    SemanticChunk,
)
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import WasteKind
from costsentinel.memory import (
    FakeEmbedder,
    MemoryNotConfiguredError,
    NullMemoryStore,
    OutcomeAlreadyRecordedError,
    SqliteMemoryStore,
    cosine_similarity,
    get_embedder,
    get_memory,
    tokenise,
)
from costsentinel.memory.base import MemoryStore

CLIENT = "northwind-energy"
OTHER_CLIENT = "ministry-of-transport"

PROV = Provenance(
    source=ProvenanceSource.OPERATOR,
    reference="test",
    verification=Verification.VERIFIED,
)


@pytest.fixture
def store() -> Any:
    """A fresh in-memory store per test."""
    built = SqliteMemoryStore()
    yield built
    built.close()


def _entry(
    recommendation_id: str = "rec-1",
    *,
    client: str = CLIENT,
    resource_id: str = "/subscriptions/00000000-0000-4000-8000-00000000a001/disks/d1",
    action: ActionType = ActionType.DELETE_ORPHANED_MANAGED_DISK,
    waste_kind: WasteKind = WasteKind.ORPHANED_MANAGED_DISK,
    rank: int = 1,
    run_id: str = "run-1",
) -> EpisodicEntry:
    return EpisodicEntry(
        recommendation_id=recommendation_id,
        signal_id=f"sig-{recommendation_id}",
        run_id=run_id,
        client=client,
        subscription_id="00000000-0000-4000-8000-00000000a001",
        target_resource_id=resource_id,
        target_resource_name=resource_id.rsplit("/", 1)[-1],
        waste_kind=waste_kind,
        action=action,
        action_class=ActionClass.BLOCK,
        requires_approval=True,
        policy_reference="policy:delete-requires-confirmation",
        monthly_saving_display="79.87 USD",
        rank=rank,
        provenance=PROV,
    )


def _record(
    *,
    run_id: str = "run-1",
    client: str = CLIENT,
    entries: tuple[EpisodicEntry, ...] | None = None,
    recorded_at: datetime | None = None,
) -> EpisodicRecord:
    return EpisodicRecord(
        run_id=run_id,
        client=client,
        subscriptions=("00000000-0000-4000-8000-00000000a001",),
        resources_examined=6,
        entries=entries if entries is not None else (_entry(run_id=run_id, client=client),),
        recorded_at=recorded_at or datetime.now(UTC),
        provenance=PROV,
    )


def _outcome(
    recommendation_id: str = "rec-1",
    *,
    client: str = CLIENT,
    outcome: Outcome = Outcome.REJECTED,
    reason: str = "migration planned for Q1; leaving the disk in place until then",
    decided_at: datetime | None = None,
    resource_id: str = "/subscriptions/00000000-0000-4000-8000-00000000a001/disks/d1",
    action: ActionType = ActionType.DELETE_ORPHANED_MANAGED_DISK,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        outcome_id=f"out-{recommendation_id}",
        recommendation_id=recommendation_id,
        run_id="run-1",
        client=client,
        subscription_id="00000000-0000-4000-8000-00000000a001",
        target_resource_id=resource_id,
        target_resource_name="d1",
        waste_kind=WasteKind.ORPHANED_MANAGED_DISK,
        action=action,
        outcome=outcome,
        reason=reason,
        decided_by="vanjana",
        decided_at=decided_at or datetime.now(UTC),
        monthly_saving_display="79.87 USD",
        provenance=PROV,
    )


def _chunk(
    chunk_id: str,
    text: str,
    *,
    client: str | None = None,
    kind: GuidanceKind = GuidanceKind.COST_PILLAR,
    embedder: FakeEmbedder | None = None,
) -> SemanticChunk:
    maker = embedder or FakeEmbedder(64)
    return SemanticChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        document_title="Test guidance",
        kind=kind,
        client=client,
        heading="A heading",
        text=text,
        embedding=maker.embed(text),
        embedder=maker.name,
        provenance=PROV,
    )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_both_stores_satisfy_the_protocol(store: SqliteMemoryStore) -> None:
    assert isinstance(store, MemoryStore)
    assert isinstance(NullMemoryStore(), MemoryStore)


def test_the_sqlite_store_is_the_default() -> None:
    """The demo and CI must work with no database and no configuration."""
    built = get_memory(Settings(memory_backend=MemoryBackend.SQLITE, memory_path=None))
    try:
        assert isinstance(built, SqliteMemoryStore)
        assert built.is_enabled
    finally:
        built.close()


def test_memory_can_be_switched_off_entirely() -> None:
    built = get_memory(Settings(memory_backend=MemoryBackend.OFF))
    assert isinstance(built, NullMemoryStore)
    assert not built.is_enabled


def test_postgres_without_a_url_refuses_before_importing_anything() -> None:
    """So the message is the same whether or not the optional extra is installed."""
    with pytest.raises(MemoryNotConfiguredError) as raised:
        get_memory(Settings(memory_backend=MemoryBackend.POSTGRES))
    message = str(raised.value)
    assert "DATABASE_URL" in message
    assert "MEMORY_BACKEND=sqlite" in message


def test_a_file_backed_store_survives_being_reopened(tmp_path: Path) -> None:
    """A learning loop you have to re-seed every run is not a learning loop."""
    path = tmp_path / "nested" / "memory.sqlite"
    first = SqliteMemoryStore(path)
    try:
        assert first.record_run(_record())
    finally:
        first.close()

    second = SqliteMemoryStore(path)
    try:
        assert [e.recommendation_id for e in second.entries_for(CLIENT)] == ["rec-1"]
    finally:
        second.close()


def test_an_incompatible_schema_version_is_refused_loudly(tmp_path: Path) -> None:
    """Rather than read with the wrong column meanings."""
    path = tmp_path / "memory.sqlite"
    SqliteMemoryStore(path).close()
    connection = sqlite3.connect(str(path))
    with connection:
        connection.execute("UPDATE schema_meta SET version = 99")
    connection.close()

    with pytest.raises(RuntimeError, match="schema 99"):
        SqliteMemoryStore(path)


# ---------------------------------------------------------------------------
# Empty is not a failure
# ---------------------------------------------------------------------------


def test_every_read_against_an_empty_store_is_empty_not_an_error(
    store: SqliteMemoryStore,
) -> None:
    """An estate nobody has scanned before misses on every lookup. That is correct."""
    assert store.outcomes_for(CLIENT) == ()
    assert store.outcome_for("rec-1") is None
    assert store.entries_for(CLIENT) == ()
    assert store.entry_for("rec-1") is None
    assert store.runs_for(CLIENT) == ()
    assert store.search(FakeEmbedder(64).embed("anything")) == ()
    assert store.chunk_count() == 0


def test_the_null_store_answers_everything_without_persisting(tmp_path: Path) -> None:
    null = NullMemoryStore()
    assert null.record_run(_record()) is False
    assert null.record_outcome(_outcome()) is False
    assert null.outcomes_for(CLIENT) == ()
    assert null.outcome_for("rec-1") is None
    assert null.entries_for(CLIENT) == ()
    assert null.entry_for("rec-1") is None
    assert null.runs_for(CLIENT) == ()
    assert null.upsert_chunks([_chunk("c1", "text")]) == 0
    assert null.search((0.1, 0.2)) == ()
    assert null.chunk_count() == 0
    assert null.accesses() == ()
    null.reset_accesses()
    null.close()
    _ = tmp_path


def test_the_null_store_does_not_raise_on_a_write() -> None:
    """A graph node writing history should degrade quietly.

    The ``decide`` CLI checks ``is_enabled`` and refuses up front instead, because a
    person recording a decision deserves to be told it went nowhere.
    """
    assert NullMemoryStore().record_outcome(_outcome()) is False


# ---------------------------------------------------------------------------
# Episodic round-trip
# ---------------------------------------------------------------------------


def test_a_run_and_its_entries_round_trip(store: SqliteMemoryStore) -> None:
    assert store.record_run(_record())
    (run,) = store.runs_for(CLIENT)
    assert run.run_id == "run-1"
    assert run.entry_count == 1
    entry = store.entry_for("rec-1")
    assert entry is not None
    assert entry.monthly_saving_display == "79.87 USD"
    assert entry.action is ActionType.DELETE_ORPHANED_MANAGED_DISK


def test_entries_come_back_newest_first(store: SqliteMemoryStore) -> None:
    now = datetime.now(UTC)
    store.record_run(
        _record(
            run_id="run-old",
            entries=(_entry("rec-old", run_id="run-old"),),
            recorded_at=now - timedelta(days=30),
        )
    )
    store.record_run(
        _record(run_id="run-new", entries=(_entry("rec-new", run_id="run-new"),), recorded_at=now)
    )
    # The entries carry their own recorded_at, defaulted at construction, so the
    # ordering asserted here is the one the store applies.
    ids = [e.recommendation_id for e in store.entries_for(CLIENT)]
    assert set(ids) == {"rec-old", "rec-new"}
    assert [r.run_id for r in store.runs_for(CLIENT)] == ["run-new", "run-old"]


def test_rewriting_the_same_run_replaces_rather_than_duplicates(
    store: SqliteMemoryStore,
) -> None:
    """A resumed sweep legitimately re-writes its own run after a crash."""
    store.record_run(_record(entries=(_entry("rec-1"),)))
    store.record_run(_record(entries=(_entry("rec-1"), _entry("rec-2", rank=2))))
    assert len(store.runs_for(CLIENT)) == 1
    assert len(store.entries_for(CLIENT)) == 2


def test_the_recall_limit_is_honoured(store: SqliteMemoryStore) -> None:
    store.record_run(_record(entries=tuple(_entry(f"rec-{n}", rank=n) for n in range(1, 11))))
    assert len(store.entries_for(CLIENT, limit=4)) == 4
    # A nonsensical limit floors at one rather than returning nothing.
    assert len(store.entries_for(CLIENT, limit=0)) == 1


# ---------------------------------------------------------------------------
# Client scoping
# ---------------------------------------------------------------------------


def test_one_clients_history_is_unreachable_from_anothers_query(
    store: SqliteMemoryStore,
) -> None:
    """ARCHITECTURE.md D12 reaching the storage layer.

    There is no method that returns another client's history, so this cannot be
    defeated by forgetting a filter at a call site.
    """
    store.record_run(_record(run_id="nw", entries=(_entry("rec-nw"),), client=CLIENT))
    store.record_run(
        _record(
            run_id="mot", entries=(_entry("rec-mot", client=OTHER_CLIENT),), client=OTHER_CLIENT
        )
    )
    store.record_outcome(_outcome("rec-nw", client=CLIENT))
    store.record_outcome(_outcome("rec-mot", client=OTHER_CLIENT))

    assert [e.recommendation_id for e in store.entries_for(CLIENT)] == ["rec-nw"]
    assert [o.recommendation_id for o in store.outcomes_for(CLIENT)] == ["rec-nw"]
    assert [e.recommendation_id for e in store.entries_for(OTHER_CLIENT)] == ["rec-mot"]
    assert [r.run_id for r in store.runs_for(CLIENT)] == ["nw"]


def test_a_clients_own_policy_is_never_searched_for_another_client(
    store: SqliteMemoryStore,
) -> None:
    embedder = FakeEmbedder(64)
    store.upsert_chunks(
        [
            _chunk("shared", "general guidance on orphaned disks", embedder=embedder),
            _chunk(
                "nw-only",
                "orphaned disks in northwind production must be snapshotted first",
                client=CLIENT,
                kind=GuidanceKind.ORG_POLICY,
                embedder=embedder,
            ),
        ]
    )
    query = embedder.embed("orphaned disks")

    for_other = [hit.chunk.chunk_id for hit in store.search(query, limit=10, client=OTHER_CLIENT)]
    assert for_other == ["shared"]

    for_owner = {hit.chunk.chunk_id for hit in store.search(query, limit=10, client=CLIENT)}
    assert for_owner == {"shared", "nw-only"}

    unscoped = [hit.chunk.chunk_id for hit in store.search(query, limit=10)]
    assert unscoped == ["shared"]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_an_outcome_round_trips_with_its_reason(store: SqliteMemoryStore) -> None:
    assert store.record_outcome(_outcome())
    recorded = store.outcome_for("rec-1")
    assert recorded is not None
    assert recorded.outcome is Outcome.REJECTED
    assert "migration planned" in recorded.reason
    assert recorded.decided_by == "vanjana"


def test_recording_a_second_outcome_is_refused_rather_than_overwriting(
    store: SqliteMemoryStore,
) -> None:
    """A decision records what somebody said.

    Overwriting one would lose the fact that they changed their mind, which is
    itself worth knowing -- so the caller has to decide what to do about it.
    """
    store.record_outcome(_outcome(outcome=Outcome.REJECTED))
    with pytest.raises(OutcomeAlreadyRecordedError) as raised:
        store.record_outcome(_outcome(outcome=Outcome.ACCEPTED))
    message = str(raised.value)
    assert "already rejected" in message
    assert "vanjana" in message
    # The original stands.
    existing = store.outcome_for("rec-1")
    assert existing is not None and existing.outcome is Outcome.REJECTED


@pytest.mark.parametrize(
    ("outcome", "discourages"),
    [
        (Outcome.REJECTED, True),
        (Outcome.DEFERRED, True),
        (Outcome.ACCEPTED, False),
    ],
)
def test_deferred_discourages_a_repitch_just_as_rejected_does(
    outcome: Outcome, discourages: bool
) -> None:
    """Deferral discourages a re-pitch exactly as rejection does.

    Treating them the same as acceptance would nag a client about something they
    have already considered.
    """
    assert outcome.discourages_repitch is discourages


def test_an_outcome_cannot_be_recorded_with_an_empty_reason() -> None:
    """An outcome with no reason can suppress a repeat but cannot explain it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _outcome(reason="")


def test_the_keys_an_outcome_is_matched_on() -> None:
    outcome = _outcome()
    assert outcome.exact_key == (
        CLIENT,
        "/subscriptions/00000000-0000-4000-8000-00000000a001/disks/d1",
        "delete_orphaned_managed_disk",
    )
    assert outcome.pattern_key == (
        CLIENT,
        "orphaned_managed_disk",
        "delete_orphaned_managed_disk",
    )


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_a_write_failure_degrades_rather_than_raising(
    store: SqliteMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing a memory write is an inconvenience; losing a report over it is not."""
    store.close()  # the connection is now closed, so every write fails
    with caplog.at_level("WARNING"):
        assert store.record_run(_record()) is False
    assert "could not write episodic history" in caplog.text
    (access,) = [a for a in store.accesses() if a.operation is MemoryOperation.RECORD_RUN]
    assert not access.succeeded
    assert not access.hit


def test_an_unreadable_historical_row_is_skipped_not_fatal(
    store: SqliteMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A row from an older build should cost a worse ranking, not a report."""
    store.record_run(_record(entries=(_entry("rec-good"),)))
    # Simulate a row written by a build whose schema has since changed.
    with store._db:
        store._db.execute(
            "INSERT INTO episodic_entries (recommendation_id, run_id, client, "
            "subscription_id, resource_id, waste_kind, action, recorded_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "rec-bad",
                "run-1",
                CLIENT,
                "s",
                "r",
                "orphaned_managed_disk",
                "x",
                "2026-01-01",
                "{}",
            ),
        )
    with caplog.at_level("WARNING"):
        entries = store.entries_for(CLIENT)
    assert [e.recommendation_id for e in entries] == ["rec-good"]
    assert "unreadable memory row" in caplog.text


# ---------------------------------------------------------------------------
# The embedder
# ---------------------------------------------------------------------------


def test_the_fake_embedder_is_deterministic_across_processes() -> None:
    """Hashed with blake2b, not ``hash()``, which is salted per process.

    A store that silently stopped matching after a restart would be worse than no
    memory at all.
    """
    first = FakeEmbedder(64).embed("orphaned managed disk")
    second = FakeEmbedder(64).embed("orphaned managed disk")
    assert first == second
    assert len(first) == 64
    assert pytest.approx(sum(value * value for value in first), abs=1e-9) == 1.0


def test_the_fake_embedder_ranks_by_shared_vocabulary() -> None:
    """Not a semantic model, but not a stub either.

    Retrieval quality is lexical, which is enough to exercise the plumbing and to
    make a retrieval assertion meaningful.
    """
    embedder = FakeEmbedder(256)
    query = embedder.embed("orphaned managed disk")
    close = embedder.embed("an orphaned managed disk left behind when a machine was deleted")
    far = embedder.embed("reserved instance commitment utilisation and exchange")
    assert cosine_similarity(query, close) > cosine_similarity(query, far)


def test_a_text_with_no_usable_tokens_embeds_to_zero() -> None:
    """Which scores 0.0 against everything -- the honest answer for an empty query."""
    vector = FakeEmbedder(32).embed("a of the and  !!  ")
    assert set(vector) == {0.0}
    assert cosine_similarity(vector, FakeEmbedder(32).embed("orphaned disk")) == 0.0


def test_a_repeated_word_does_not_swamp_the_vector() -> None:
    """Sub-linear term weighting, which is where a naive bag of words goes wrong."""
    embedder = FakeEmbedder(64)
    once = embedder.embed("disk snapshot")
    many = embedder.embed("disk disk disk disk disk disk disk disk snapshot")
    assert cosine_similarity(once, many) > 0.8


def test_the_embedder_name_records_the_width_it_used() -> None:
    """Mixing widths in one store would make every similarity meaningless."""
    assert FakeEmbedder(128).name == "fake-hashed-bow-128"
    assert FakeEmbedder(64).name != FakeEmbedder(128).name


def test_a_width_mismatch_scores_zero_rather_than_raising() -> None:
    narrow = FakeEmbedder(32).embed("orphaned disk")
    wide = FakeEmbedder(256).embed("orphaned disk")
    assert cosine_similarity(narrow, wide) == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Orphaned Managed Disk", ["orphaned", "managed", "disk"]),
        ("the and of a", []),
        ("VM-01 is idle", ["idle"]),
        ("", []),
    ],
)
def test_tokenisation(text: str, expected: list[str]) -> None:
    assert tokenise(text) == expected


def test_the_fake_embedder_is_the_default() -> None:
    assert isinstance(get_embedder(Settings()), FakeEmbedder)
    assert Settings().uses_fake_embedder


def test_a_real_embedder_without_configuration_refuses_at_construction() -> None:
    with pytest.raises(MemoryNotConfiguredError) as raised:
        get_embedder(Settings(embedder_backend=EmbedderBackend.AZURE_OPENAI))
    message = str(raised.value)
    assert "AZURE_OPENAI_EMBEDDING_DEPLOYMENT" in message
    assert "EMBEDDER_BACKEND=fake" in message


def test_a_real_embedder_batches_and_maps_the_response() -> None:
    """Driven by a double, so no credential and no network."""

    class FakeEmbeddings:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            items = [type("Item", (), {"embedding": [0.1, 0.2, 0.3]})() for _ in kwargs["input"]]
            return type("Response", (), {"data": items})()

    from costsentinel.memory.embedding import AzureOpenAIEmbedder

    embeddings = FakeEmbeddings()
    client = type("Client", (), {"embeddings": embeddings})()
    settings = Settings(
        embedder_backend=EmbedderBackend.AZURE_OPENAI,
        azure_openai_endpoint="https://example.invalid",
        azure_openai_api_version="2024-10-21",
        azure_openai_embedding_deployment="text-embed",
    )
    embedder = AzureOpenAIEmbedder(settings, client=client)
    assert embedder.name == "azure-openai-text-embed"
    vectors = embedder.embed_batch(["one", "two"])
    assert len(vectors) == 2
    # One round trip for both texts, not two.
    assert len(embeddings.calls) == 1
    assert embeddings.calls[0]["input"] == ["one", "two"]
    assert embedder.embed_batch([]) == ()


def test_a_real_embedder_failure_names_the_deployment() -> None:
    from costsentinel.memory.base import MemoryStoreError
    from costsentinel.memory.embedding import AzureOpenAIEmbedder

    class Exploding:
        def create(self, **kwargs: Any) -> Any:
            _ = kwargs
            msg = "429 Too Many Requests"
            raise RuntimeError(msg)

    client = type("Client", (), {"embeddings": Exploding()})()
    settings = Settings(
        embedder_backend=EmbedderBackend.AZURE_OPENAI,
        azure_openai_endpoint="https://example.invalid",
        azure_openai_api_version="2024-10-21",
        azure_openai_embedding_deployment="text-embed",
    )
    with pytest.raises(MemoryStoreError, match="text-embed"):
        AzureOpenAIEmbedder(settings, client=client).embed("anything")


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


def test_search_ranks_the_relevant_passage_first(store: SqliteMemoryStore) -> None:
    embedder = FakeEmbedder(256)
    store.upsert_chunks(
        [
            _chunk(
                "disks",
                "An orphaned managed disk is one no longer attached to any virtual machine.",
                embedder=embedder,
            ),
            _chunk(
                "reservations",
                "A reservation commitment is paid in advance and cannot be refunded.",
                embedder=embedder,
            ),
        ]
    )
    hits = store.search(embedder.embed("orphaned managed disk"), limit=2)
    assert [hit.chunk.chunk_id for hit in hits] == ["disks", "reservations"]
    assert hits[0].score > hits[1].score
    assert hits[0].citation == "Test guidance - A heading"


def test_search_ties_break_reproducibly(store: SqliteMemoryStore) -> None:
    """So an eval can assert on what came back."""
    embedder = FakeEmbedder(64)
    store.upsert_chunks(
        [
            _chunk("zebra", "identical text", embedder=embedder),
            _chunk("alpha", "identical text", embedder=embedder),
        ]
    )
    hits = store.search(embedder.embed("identical text"), limit=2)
    assert [hit.chunk.chunk_id for hit in hits] == ["alpha", "zebra"]


def test_upserting_the_same_chunk_replaces_it(store: SqliteMemoryStore) -> None:
    store.upsert_chunks([_chunk("c1", "first version")])
    store.upsert_chunks([_chunk("c1", "second version")])
    assert store.chunk_count() == 1
    (hit,) = store.search(FakeEmbedder(64).embed("second version"), limit=1)
    assert hit.chunk.text == "second version"


def test_a_zero_limit_returns_nothing_rather_than_everything(
    store: SqliteMemoryStore,
) -> None:
    store.upsert_chunks([_chunk("c1", "some guidance")])
    assert store.search(FakeEmbedder(64).embed("guidance"), limit=0) == ()


# ---------------------------------------------------------------------------
# Access tracing
# ---------------------------------------------------------------------------


def test_reads_and_writes_are_recorded_with_hit_and_count(
    store: SqliteMemoryStore,
) -> None:
    store.reset_accesses()
    store.record_run(_record())
    store.entries_for(CLIENT)
    store.entries_for(OTHER_CLIENT)

    by_operation = {access.operation: access for access in store.accesses()}
    assert by_operation[MemoryOperation.RECORD_RUN].hit
    assert by_operation[MemoryOperation.RECORD_RUN].record_count == 1

    reads = [a for a in store.accesses() if a.operation is MemoryOperation.READ_ENTRIES]
    assert [a.hit for a in reads] == [True, False]
    assert [a.client for a in reads] == [CLIENT, OTHER_CLIENT]
    assert all(a.store == store.name for a in store.accesses())


def test_resetting_the_access_log_lets_one_store_serve_several_runs(
    store: SqliteMemoryStore,
) -> None:
    store.record_run(_record())
    assert store.accesses()
    store.reset_accesses()
    assert store.accesses() == ()


def test_memory_metrics_summarise_a_runs_accesses() -> None:
    def access(operation: MemoryOperation, *, hit: bool, ok: bool = True) -> MemoryAccess:
        return MemoryAccess(operation=operation, store="test", hit=hit, latency_ms=3, succeeded=ok)

    metrics = MemoryMetrics(
        accesses=(
            access(MemoryOperation.READ_ENTRIES, hit=True),
            access(MemoryOperation.SEARCH, hit=False),
            access(MemoryOperation.RECORD_RUN, hit=True),
            access(MemoryOperation.READ_OUTCOMES, hit=False, ok=False),
        )
    )
    assert metrics.access_count == 4
    assert metrics.hit_count == 2
    assert metrics.miss_count == 1
    assert metrics.failure_count == 1
    assert metrics.total_latency_ms == 12
    # Two successful reads, one of which hit.
    assert metrics.hit_rate() == Decimal("0.5")


def test_an_empty_run_has_no_hit_rate_rather_than_a_zero_one() -> None:
    """Zero would claim memory found nothing; None says nothing was asked."""
    assert MemoryMetrics().hit_rate() is None
    assert MemoryMetrics().access_count == 0


def test_a_miss_is_not_counted_as_a_failure() -> None:
    """An estate nobody has scanned before misses on every read, which is correct."""
    metrics = MemoryMetrics(
        accesses=(MemoryAccess(operation=MemoryOperation.READ_ENTRIES, store="test", hit=False),)
    )
    assert metrics.miss_count == 1
    assert metrics.failure_count == 0


# ---------------------------------------------------------------------------
# Postgres: one integration test, skipped unless a database is present
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COSTSENTINEL_TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COSTSENTINEL_TEST_DATABASE_URL to run the pgvector integration test",
)
def test_the_postgres_store_round_trips_episodic_and_semantic_memory() -> None:
    """What cannot be covered offline: the SQL dialect and the vector operator.

    Everything else about this store -- the client scoping, the ordering, the refusal
    to overwrite an outcome -- is the same behaviour the SQLite tests above pin, by
    design. This test exists for the two things that genuinely differ.

    Run it by starting the compose stack, exporting
    ``COSTSENTINEL_TEST_DATABASE_URL`` to point at it, and running this module with
    ``-k postgres``. See docker-compose.yml for the DSN.
    """
    from costsentinel.memory.postgres import PostgresMemoryStore

    embedder = FakeEmbedder(256)
    built = PostgresMemoryStore(POSTGRES_URL, dimensions=256)
    try:
        assert built.is_enabled
        assert built.name == "postgres-pgvector"

        run_id = f"it-{datetime.now(UTC).timestamp()}"
        entry = _entry(f"rec-{run_id}", run_id=run_id)
        assert built.record_run(_record(run_id=run_id, entries=(entry,)))
        assert built.entry_for(entry.recommendation_id) is not None
        assert any(r.run_id == run_id for r in built.runs_for(CLIENT))

        assert built.record_outcome(_outcome(entry.recommendation_id))
        recorded = built.outcome_for(entry.recommendation_id)
        assert recorded is not None and recorded.outcome is Outcome.REJECTED
        with pytest.raises(OutcomeAlreadyRecordedError):
            built.record_outcome(_outcome(entry.recommendation_id, outcome=Outcome.ACCEPTED))

        # The vector operator: the near passage must outrank the far one.
        built.upsert_chunks(
            [
                _chunk(f"disks-{run_id}", "an orphaned managed disk", embedder=embedder),
                _chunk(f"reserve-{run_id}", "a reservation commitment", embedder=embedder),
            ]
        )
        hits = built.search(embedder.embed("orphaned managed disk"), limit=2)
        assert hits
        assert hits[0].chunk.chunk_id.startswith("disks-")
        assert -1.0 <= hits[0].score <= 1.0
        assert built.chunk_count() >= 2
    finally:
        built.close()


# ---------------------------------------------------------------------------
# The parts of the Postgres store that need no Postgres
# ---------------------------------------------------------------------------


def test_the_postgres_module_imports_without_psycopg() -> None:
    """Which is what makes the lazy import worth having.

    ``psycopg`` is only touched inside the connect path, so the module -- and the
    helpers below -- are importable and testable in CI, where the extra is absent.
    """
    from costsentinel.memory import postgres

    assert postgres.SCHEMA_VERSION == 1
    assert "vector" in postgres._SCHEMA


def test_a_vector_is_rendered_in_pgvectors_literal_syntax() -> None:
    """A string literal rather than a registered adapter, so one package suffices."""
    from costsentinel.memory.postgres import _vector

    assert _vector((0.5, -0.25, 0.0)) == "[0.5,-0.25,0]"
    assert _vector(()) == "[]"
    # Nine significant figures: enough that a normalised vector round-trips, short
    # enough that a 1536-dimension embedding is not an enormous statement.
    assert _vector((1 / 3,)) == "[0.333333333]"


def test_a_stored_row_parses_from_json_text_or_a_mapping() -> None:
    """Psycopg returns a JSONB column as a dict; SQLite returns it as text."""
    from costsentinel.memory.postgres import _parse, _parse_all

    outcome = _outcome()
    as_text = outcome.model_dump_json()
    as_mapping = outcome.model_dump(mode="json")

    assert _parse(RecommendationOutcome, as_text) == outcome
    assert _parse(RecommendationOutcome, as_mapping) == outcome
    assert _parse_all(RecommendationOutcome, [(as_text,), (as_mapping,)]) == (outcome, outcome)


def test_an_unparseable_row_is_skipped_rather_than_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from costsentinel.memory.postgres import _parse, _parse_all

    with caplog.at_level("WARNING"):
        assert _parse(RecommendationOutcome, "{}") is None
        assert _parse(RecommendationOutcome, None) is None
        assert _parse_all(RecommendationOutcome, [("{}",)]) == ()
    assert "unreadable memory row" in caplog.text


def test_connecting_without_psycopg_names_the_extra() -> None:
    """The condition CI itself is in."""
    import importlib.util

    try:
        installed = importlib.util.find_spec("psycopg") is not None
    except ModuleNotFoundError:  # pragma: no cover - psycopg has no namespace parent
        installed = False
    if installed:  # pragma: no cover - only when the extra is installed
        pytest.skip("psycopg is installed, so the missing-dependency path cannot fire")

    from costsentinel.memory.postgres import PostgresMemoryStore

    with pytest.raises(MemoryNotConfiguredError) as raised:
        PostgresMemoryStore("postgresql://localhost/nothing")
    message = str(raised.value)
    assert "psycopg" in message
    assert "uv sync --extra memory" in message
