"""Memory inside the graph: the consult step, the corpus, and the invariant.

The invariant this whole phase rests on has its own section at the bottom: memory
influences ordering and annotation only, and every money figure is byte-identical
with and without a seeded rejection. It is asserted mechanically because it is the
kind of property that is easy to claim and easy to break -- a single
``savings.monthly * penalty`` somewhere would satisfy every other test here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from costsentinel.agents.memory_consult import (
    accepted_count,
    declined_count,
    make_memory_consult,
    summarise_recalls,
)
from costsentinel.agents.ranking import score_recommendations
from costsentinel.config import Settings
from costsentinel.domain.analysis import RankingScore
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import Estate
from costsentinel.domain.governance import ActionClassification
from costsentinel.domain.memory import (
    GuidanceKind,
    MemoryOperation,
    Outcome,
    RecallKind,
    RecommendationOutcome,
)
from costsentinel.domain.recommendations import (
    ActionClass,
    ActionType,
    Recommendation,
    SavingsEstimate,
)
from costsentinel.domain.report import ClientReport
from costsentinel.domain.signals import Observation, WasteKind, WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.memory import (
    CORPUS,
    POLICY_CLIENT,
    FakeEmbedder,
    NullMemoryStore,
    SqliteMemoryStore,
    build_chunks,
    citations,
    get_embedder,
    load_documents,
    record_decision,
    retrieve,
    seed,
)
from costsentinel.memory.corpus import MIN_CHUNK_CHARS, MIN_RELEVANCE, chunk
from costsentinel.memory.recall import (
    PENALTY_ACCEPTED,
    PENALTY_EXACT,
    PENALTY_PATTERN,
    adjusted_composite,
    consult,
    penalty_for,
)
from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider

PROV = Provenance(
    source=ProvenanceSource.OPERATOR, reference="test", verification=Verification.VERIFIED
)
CLIENT = "acme"
SUB = "00000000-0000-4000-8000-00000000a001"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings with an isolated state directory."""
    return Settings(state_dir=tmp_path)


@pytest.fixture
def store() -> Any:
    """A fresh in-memory store per test."""
    built = SqliteMemoryStore()
    yield built
    built.close()


# ---------------------------------------------------------------------------
# Fixtures for the consult step
# ---------------------------------------------------------------------------


def _signal(signal_id: str, kind: WasteKind, resource_id: str) -> WasteSignal:
    return WasteSignal(
        signal_id=signal_id,
        kind=kind,
        resource_id=resource_id,
        resource_name=resource_id.rsplit("/", 1)[-1],
        resource_kind="virtual_machine",
        subscription_id=SUB,
        client=CLIENT,
        detector="test",
        evidence=(Observation(name="cpu_avg_pct", value="2.0", provenance=PROV),),
        confidence=Decimal("0.9"),
        monthly_cost=MoneyAmount.of(Decimal(100), provenance=PROV),
        provenance=PROV,
    )


def _recommendation(
    recommendation_id: str,
    *,
    signal_id: str,
    resource_id: str,
    action: ActionType,
    monthly: str | None,
    risk: ActionClass = ActionClass.REVIEW,
    rank: int = 1,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        signal_id=signal_id,
        client=CLIENT,
        subscription_id=SUB,
        target_resource_id=resource_id,
        target_resource_name=resource_id.rsplit("/", 1)[-1],
        action=action,
        risk_class=risk,
        rationale="because the detector said so",
        rationale_provenance=PROV,
        preconditions=("re-check before acting",),
        savings=(
            SavingsEstimate.from_monthly(
                MoneyAmount.of(Decimal(monthly), provenance=PROV),
                basis="test",
                is_estimated=False,
            )
            if monthly is not None
            else SavingsEstimate.undetermined("no cheaper tier exists")
        ),
        confidence=Decimal("0.9"),
        rank=rank,
    )


def _classification(recommendation_id: str, action: ActionType) -> ActionClassification:
    return ActionClassification(
        recommendation_id=recommendation_id,
        action=action,
        action_class=ActionClass.REVIEW,
        floor_class=ActionClass.REVIEW,
        requires_approval=True,
        is_automatable=True,
        policy_reference="policy:test",
        reason="test",
        provenance=PROV,
    )


def _outcome(
    recommendation_id: str,
    *,
    resource_id: str,
    action: ActionType,
    kind: WasteKind,
    outcome: Outcome = Outcome.REJECTED,
    reason: str = "migration planned for Q1",
    client: str = CLIENT,
    decided_at: datetime | None = None,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        outcome_id=f"out-{recommendation_id}",
        recommendation_id=recommendation_id,
        run_id="run-old",
        client=client,
        subscription_id=SUB,
        target_resource_id=resource_id,
        target_resource_name=resource_id.rsplit("/", 1)[-1],
        waste_kind=kind,
        action=action,
        outcome=outcome,
        reason=reason,
        decided_by="operator",
        decided_at=decided_at or datetime(2026, 9, 12, tzinfo=UTC),
        monthly_saving_display="100.00 USD",
        provenance=PROV,
    )


def _three_recommendations() -> tuple[
    tuple[WasteSignal, ...], tuple[Recommendation, ...], tuple[RankingScore, ...]
]:
    """Three findings whose base order is big, medium, small."""
    signals = (
        _signal("sig-big", WasteKind.IDLE_VIRTUAL_MACHINE, "/vm/big"),
        _signal("sig-mid", WasteKind.OVERSIZED_VIRTUAL_MACHINE, "/vm/mid"),
        _signal("sig-small", WasteKind.ORPHANED_MANAGED_DISK, "/disk/small"),
    )
    recommendations = (
        _recommendation(
            "rec-big",
            signal_id="sig-big",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            monthly="500",
            rank=1,
        ),
        _recommendation(
            "rec-mid",
            signal_id="sig-mid",
            resource_id="/vm/mid",
            action=ActionType.RESIZE_VIRTUAL_MACHINE,
            monthly="200",
            rank=2,
        ),
        _recommendation(
            "rec-small",
            signal_id="sig-small",
            resource_id="/disk/small",
            action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
            monthly="40",
            rank=3,
        ),
    )
    return signals, recommendations, score_recommendations(recommendations)


# ---------------------------------------------------------------------------
# The penalty table
# ---------------------------------------------------------------------------


def test_an_exact_match_is_penalised_far_harder_than_a_pattern_match() -> None:
    """Declining this disk is strong evidence about this disk, weak about another.

    Treating them the same would let one "no" bury a whole category of genuine
    waste, which is how a cost tool starts under-reporting.
    """
    assert PENALTY_EXACT > PENALTY_PATTERN > PENALTY_ACCEPTED
    assert penalty_for(RecallKind.EXACT, Outcome.REJECTED) == PENALTY_EXACT
    assert penalty_for(RecallKind.PATTERN, Outcome.REJECTED) == PENALTY_PATTERN
    assert penalty_for(RecallKind.EXACT, Outcome.DEFERRED) == PENALTY_EXACT


def test_acceptance_is_noted_and_does_not_move_the_order() -> None:
    """Promoting on acceptance would make the order reflect agreeableness, not value.

    A client having agreed to something last month is not evidence that a different
    resource is more wasteful this month.
    """
    assert penalty_for(RecallKind.EXACT, Outcome.ACCEPTED) == Decimal(0)
    assert penalty_for(RecallKind.PATTERN, Outcome.ACCEPTED) == Decimal(0)


def test_the_penalty_never_takes_a_score_outside_zero_to_one() -> None:
    """Multiplicative, so it means the same thing for a high score as a low one."""
    for base in (Decimal(0), Decimal("0.5"), Decimal(1)):
        for penalty in (Decimal(0), PENALTY_PATTERN, PENALTY_EXACT, Decimal(1)):
            result = adjusted_composite(base, penalty)
            assert Decimal(0) <= result <= Decimal(1)
            assert result <= base


def test_an_exact_penalty_does_not_collapse_a_score_to_zero() -> None:
    """So previously-declined findings keep a stable order among themselves."""
    assert adjusted_composite(Decimal("0.8"), PENALTY_EXACT) > 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_a_previously_declined_finding_is_demoted_and_annotated() -> None:
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "rec-big-last-month",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        ),
    )
    reranked, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )

    assert [r.recommendation_id for r in reranked] == ["rec-mid", "rec-small", "rec-big"]
    (recall,) = recalls
    assert recall.recommendation_id == "rec-big"
    assert recall.kind is RecallKind.EXACT
    assert recall.base_rank == 1
    assert recall.adjusted_rank == 3
    assert recall.was_demoted
    assert "Previously declined on 2026-09-12" in recall.annotation
    assert "migration planned for Q1" in recall.annotation


def test_nothing_is_ever_removed_only_reordered() -> None:
    """A client declining advice does not make the waste disappear."""
    signals, recommendations, ranking = _three_recommendations()
    outcomes = tuple(
        _outcome(
            f"old-{rec.recommendation_id}",
            resource_id=rec.target_resource_id,
            action=rec.action,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        )
        for rec in recommendations
    )
    reranked, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    assert len(reranked) == len(recommendations)
    assert {r.recommendation_id for r in reranked} == {r.recommendation_id for r in recommendations}
    assert len(recalls) == 3


def test_a_similar_recommendation_elsewhere_annotates_without_a_hard_demotion() -> None:
    """The reason may have been specific to the other resource."""
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "rec-other-vm",
            resource_id="/vm/somewhere-else",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        ),
    )
    reranked, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    (recall,) = recalls
    assert recall.kind is RecallKind.PATTERN
    assert "A similar recommendation for somewhere-else" in recall.annotation
    # A 20% penalty is not enough to lose a 500-vs-200 lead, which is the point.
    assert reranked[0].recommendation_id == "rec-big"
    assert not recall.was_demoted


def test_an_exact_match_beats_a_pattern_match() -> None:
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "pattern",
            resource_id="/vm/elsewhere",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
            reason="pattern reason",
        ),
        _outcome(
            "exact",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
            reason="exact reason",
        ),
    )
    _, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    exact = next(r for r in recalls if r.recommendation_id == "rec-big")
    assert exact.kind is RecallKind.EXACT
    assert "exact reason" in exact.annotation


def test_the_most_recent_decision_wins() -> None:
    """What the client said last is what they think now."""
    signals, recommendations, ranking = _three_recommendations()
    older = _outcome(
        "old",
        resource_id="/vm/big",
        action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        reason="first answer",
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _outcome(
        "new",
        resource_id="/vm/big",
        action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        reason="changed our minds",
        decided_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    _, recalls = consult(
        recommendations=recommendations,
        signals=signals,
        ranking=ranking,
        outcomes=(older, newer),
    )
    (recall,) = recalls
    assert "changed our minds" in recall.annotation


def test_a_different_action_on_the_same_resource_is_not_a_match() -> None:
    """Declining a deletion says nothing about agreeing to a resize."""
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "rec-other-action",
            resource_id="/vm/big",
            action=ActionType.APPLY_TAG,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        ),
    )
    _, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    assert recalls == ()


def test_another_clients_outcome_can_never_annotate_this_one() -> None:
    """Cannot happen through the store, whose reads are client-scoped.

    Checked anyway, because a cross-client annotation would be a confidentiality
    failure rather than a ranking bug.
    """
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "someone-else",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
            client="a-different-client",
        ),
    )
    reranked, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    assert recalls == ()
    assert [r.rank for r in reranked] == [1, 2, 3]


def test_an_empty_memory_leaves_the_order_exactly_as_it_was() -> None:
    """Which is what makes the memory-less path and the empty-memory path one path."""
    signals, recommendations, ranking = _three_recommendations()
    reranked, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=()
    )
    assert recalls == ()
    assert [r.recommendation_id for r in reranked] == [r.recommendation_id for r in recommendations]
    assert [r.rank for r in reranked] == [1, 2, 3]


def test_consulting_about_nothing_returns_nothing() -> None:
    """The empty case, so the node can run unconditionally rather than behind a guard."""
    assert consult(recommendations=(), signals=(), ranking=(), outcomes=()) == ((), ())


def test_a_penalty_cannot_lift_an_unquantified_finding_above_a_quantified_one() -> None:
    """The ranking tier survives a memory penalty (ARCHITECTURE.md D104).

    The order comes from ``RankingScore.order_key``, which accepts an overridden
    composite and keeps the tier its own -- so this is a property of the method rather
    than a convention ``consult`` is trusted to honour. The case is constructed to be
    as unfavourable as possible: the quantified finding is declined outright, taking a
    70% penalty, and the unquantified one is untouched and perfectly safe.
    """
    signals = (
        _signal("sig-known", WasteKind.ORPHANED_MANAGED_DISK, "/disk/known"),
        _signal("sig-unknown", WasteKind.IDLE_SQL_DATABASE, "/sql/unknown"),
    )
    known = _recommendation(
        "rec-known",
        signal_id="sig-known",
        resource_id="/disk/known",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        monthly="40",
        risk=ActionClass.BLOCK,
        rank=1,
    )
    unknown = _recommendation(
        "rec-unknown",
        signal_id="sig-unknown",
        resource_id="/sql/unknown",
        action=ActionType.SCALE_DOWN_SQL_DATABASE,
        monthly=None,
        rank=2,
    )
    recommendations = (known, unknown)
    ranking = score_recommendations(recommendations)

    declined = _outcome(
        "rec-known",
        resource_id="/disk/known",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        reason="that disk is referenced by an image",
    )
    reranked, recalls = consult(
        recommendations=recommendations,
        signals=signals,
        ranking=ranking,
        outcomes=(declined,),
    )

    # The penalty landed...
    assert [r.recommendation_id for r in recalls] == ["rec-known"]
    assert recalls[0].penalty > 0
    # ...and the quantified finding still leads, because the tier is not penalisable.
    assert [r.recommendation_id for r in reranked] == ["rec-known", "rec-unknown"]


# ---------------------------------------------------------------------------
# The recall summary
# ---------------------------------------------------------------------------


def test_the_summary_counts_outcomes_separately() -> None:
    """Outcomes are counted separately rather than lumped together.

    "Two declined, one deferred" is a different conversation from "three declined".
    """
    signals, recommendations, ranking = _three_recommendations()
    outcomes = (
        _outcome(
            "a",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
            outcome=Outcome.REJECTED,
        ),
        _outcome(
            "b",
            resource_id="/vm/mid",
            action=ActionType.RESIZE_VIRTUAL_MACHINE,
            kind=WasteKind.OVERSIZED_VIRTUAL_MACHINE,
            outcome=Outcome.DEFERRED,
        ),
    )
    _, recalls = consult(
        recommendations=recommendations, signals=signals, ranking=ranking, outcomes=outcomes
    )
    state = ScanState(run_id="r", client=CLIENT, recalls=recalls)
    summary = summarise_recalls(state)
    assert "1 previously declined" in summary
    assert "1 previously deferred" in summary
    assert "re-pitched as new" in summary
    assert declined_count(state) == 2
    assert accepted_count(state) == 0


def test_a_single_recall_reads_as_singular() -> None:
    signals, recommendations, ranking = _three_recommendations()
    _, recalls = consult(
        recommendations=recommendations,
        signals=signals,
        ranking=ranking,
        outcomes=(
            _outcome(
                "a",
                resource_id="/vm/big",
                action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
                kind=WasteKind.IDLE_VIRTUAL_MACHINE,
            ),
        ),
    )
    assert "1 of these findings has been seen before" in summarise_recalls(
        ScanState(run_id="r", client=CLIENT, recalls=recalls)
    )


def test_no_recalls_means_no_summary_line() -> None:
    """Which is also what a first sweep of a new estate correctly produces."""
    assert summarise_recalls(ScanState(run_id="r", client=CLIENT)) == ""


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


def _consult_state() -> ScanState:
    signals, recommendations, ranking = _three_recommendations()
    return ScanState(
        run_id="run-1",
        client=CLIENT,
        estate=Estate(client=CLIENT, subscriptions=(), resources=()),
        signals=signals,
        recommendations=recommendations,
        ranking=ranking,
        classifications=tuple(
            _classification(rec.recommendation_id, rec.action) for rec in recommendations
        ),
    )


def test_the_node_marks_itself_done_even_with_nothing_to_do(settings: Settings) -> None:
    """Otherwise the supervisor would route to it forever."""
    node = make_memory_consult(memory=NullMemoryStore(), settings=settings)
    update = node(ScanState(run_id="run-1", client=CLIENT))
    assert update["memory_consulted"] is True
    assert "recommendations" not in update


def test_the_node_is_a_recorded_no_op_when_memory_is_off(settings: Settings) -> None:
    node = make_memory_consult(memory=NullMemoryStore(), settings=settings)
    update = node(_consult_state())
    assert update["memory_consulted"] is True
    assert update["recalls"] == ()
    assert [r.rank for r in update["recommendations"]] == [1, 2, 3]
    (event,) = update["audit"][-1:]
    assert event.detail["reason"] == "memory is switched off"


def test_the_node_records_every_recall_in_the_audit_trail(
    settings: Settings, store: SqliteMemoryStore
) -> None:
    """Including the base and adjusted rank, so a reordering is explicable."""
    store.record_outcome(
        _outcome(
            "rec-big-last-month",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        )
    )
    node = make_memory_consult(memory=store, settings=settings)
    update = node(_consult_state())
    events = [e for e in update["audit"] if e.actor == "memory_consult"]
    assert events
    detail = events[-1].detail
    assert detail["recall"] == "exact"
    assert detail["prior_outcome"] == "rejected"
    assert detail["base_rank"] == "1"
    assert detail["adjusted_rank"] == "3"
    assert detail["penalty"] == str(PENALTY_EXACT)


def test_the_node_does_not_add_to_errors(settings: Settings, store: SqliteMemoryStore) -> None:
    """``errors`` feeds the report's "may be incomplete" wording.

    A client having declined something last month has not made this report
    incomplete, so a recall must not land there.
    """
    store.record_outcome(
        _outcome(
            "rec-big-last-month",
            resource_id="/vm/big",
            action=ActionType.DEALLOCATE_VIRTUAL_MACHINE,
            kind=WasteKind.IDLE_VIRTUAL_MACHINE,
        )
    )
    node = make_memory_consult(memory=store, settings=settings)
    update = node(_consult_state())
    assert "errors" not in update


def test_the_node_records_memory_metrics(settings: Settings, store: SqliteMemoryStore) -> None:
    node = make_memory_consult(memory=store, settings=settings)
    update = node(_consult_state())
    metrics = update["memory_metrics"]
    assert metrics.access_count >= 1
    assert any(a.operation is MemoryOperation.READ_OUTCOMES for a in metrics.accesses)
    # An empty store misses, which is correct and not a failure.
    assert metrics.failure_count == 0


def test_the_node_imports_no_language_model() -> None:
    """Memory must not be able to move a number, and a model cannot help it try."""
    import ast

    source = Path("src/costsentinel/agents/memory_consult.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(name.startswith("costsentinel.llm") for name in imported)


def test_the_recall_module_touches_no_money_type() -> None:
    """The structural half of the determinism invariant.

    The behavioural half is asserted end to end below. This one catches the change
    that would break it before it can produce a wrong number.
    """
    import ast

    tree = ast.parse(Path("src/costsentinel/memory/recall.py").read_text(encoding="utf-8"))

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "MoneyAmount" not in imported
    assert "SavingsEstimate" not in imported

    # No attribute access reaches a money field. Checked on the AST rather than the
    # text, because the docstrings legitimately discuss savings: explaining why a
    # thing is absent requires naming it.
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not {"savings", "monthly", "annual", "monthly_cost"} & attributes


# ---------------------------------------------------------------------------
# The semantic corpus
# ---------------------------------------------------------------------------


def test_every_corpus_document_loads() -> None:
    documents = load_documents()
    assert len(documents) == len(CORPUS)
    assert {d.kind for d in documents} == {
        GuidanceKind.ORG_POLICY,
        GuidanceKind.COST_PILLAR,
        GuidanceKind.PRICING_NOTE,
    }


def test_the_corpus_says_it_is_synthetic_in_every_document() -> None:
    """It ships in a public repository. A reader must not mistake it for real rules."""
    for document in load_documents():
        assert "synthetic" in document.body.lower()[:600]


def test_the_corpus_contains_no_prices() -> None:
    """Guidance explains billing shape; figures come from the price catalogue.

    A number in a retrieved passage is one a model could copy into a narrative, so
    the corpus deliberately has none.
    """
    import re

    money = re.compile(r"[$€£]\s?\d|\d+\.\d{2}\s?(USD|EUR|GBP|QAR)")
    for document in load_documents():
        assert not money.search(document.body), f"{document.document_id} contains a price"


def test_only_the_org_policy_is_client_scoped() -> None:
    """One client's own rules must never be citable to another."""
    by_kind = {d.kind: d for d in load_documents()}
    assert by_kind[GuidanceKind.ORG_POLICY].client == POLICY_CLIENT
    assert by_kind[GuidanceKind.COST_PILLAR].client is None
    assert by_kind[GuidanceKind.PRICING_NOTE].client is None


def test_chunking_splits_on_headings_not_character_counts() -> None:
    """Half a rule retrieved and cited is worse than no citation."""
    embedder = FakeEmbedder()
    policy = next(d for d in load_documents() if d.kind is GuidanceKind.ORG_POLICY)
    chunks = chunk(policy, embedder)
    assert len(chunks) >= 6
    for passage in chunks:
        assert passage.heading
        assert len(passage.text) >= MIN_CHUNK_CHARS
        assert passage.embedder == embedder.name
        assert passage.citation.startswith(policy.title)


def test_a_section_titles_vocabulary_is_retrievable() -> None:
    """The heading is embedded with the body.

    A section called "Snapshots and backups" whose body never repeats "snapshot"
    would otherwise be unreachable, which is the most obvious failure mode of a
    lexical embedder.
    """
    embedder = FakeEmbedder()
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        found = retrieve(store, embedder, "snapshots", client=POLICY_CLIENT, limit=3)
        assert any("Snapshots" in passage.heading for passage in found)
    finally:
        store.close()


def test_seeding_is_idempotent_and_skipped_when_already_populated() -> None:
    embedder = FakeEmbedder()
    store = SqliteMemoryStore()
    try:
        first = seed(store, embedder)
        assert first > 0
        assert seed(store, embedder) == 0
        assert seed(store, embedder, force=True) == first
        assert store.chunk_count() == first
    finally:
        store.close()


def test_seeding_a_disabled_store_writes_nothing() -> None:
    assert seed(NullMemoryStore(), FakeEmbedder(64)) == 0


def test_retrieval_prefers_the_clients_own_policy_over_general_guidance() -> None:
    embedder = FakeEmbedder()
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        found = retrieve(
            store,
            embedder,
            "orphaned managed disk deletion",
            client=POLICY_CLIENT,
            limit=3,
        )
        assert found
        assert found[0].kind is GuidanceKind.ORG_POLICY
    finally:
        store.close()


def test_another_client_never_retrieves_the_policy() -> None:
    embedder = FakeEmbedder()
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        found = retrieve(
            store, embedder, "orphaned managed disk deletion", client="someone-else", limit=5
        )
        assert found
        assert all(passage.kind is not GuidanceKind.ORG_POLICY for passage in found)
    finally:
        store.close()


def test_an_irrelevant_query_retrieves_nothing_rather_than_the_nearest_thing() -> None:
    """An explanation citing an unrelated policy section looks grounded and is not.

    A store always returns its nearest passages however unrelated they are, so the
    relevance floor is what makes retrieval honest rather than merely nearest.
    """
    embedder = FakeEmbedder()
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        assert retrieve(store, embedder, "quantum teleportation of llamas") == ()
        assert retrieve(store, embedder, "   ") == ()
    finally:
        store.close()


def test_a_narrow_fake_embedder_collides_and_cannot_be_thresholded() -> None:
    """The reason DEFAULT_FAKE_DIMENSIONS is as large as it is.

    A reference section holds 37-86 distinct tokens. At 256 buckets those fill most
    of the vector, every query overlaps every document, and the scores compress into
    a band too narrow to separate signal from collision. This pins the measurement
    the default rests on, so shrinking the width fails here rather than quietly
    degrading retrieval.
    """
    narrow = FakeEmbedder(256)
    wide = FakeEmbedder()
    nonsense = "quantum teleportation of llamas"

    for embedder, collides in ((narrow, True), (wide, False)):
        store = SqliteMemoryStore()
        try:
            seed(store, embedder)
            top = store.search(embedder.embed(nonsense), limit=1, client=POLICY_CLIENT)
            assert top
            assert (top[0].score > MIN_RELEVANCE) is collides
        finally:
            store.close()


def test_citations_name_the_document_and_the_section() -> None:
    chunks = build_chunks(FakeEmbedder())
    named = citations(chunks[:2])
    assert all(" - " in citation for citation in named)


# ---------------------------------------------------------------------------
# Grounded explanations
# ---------------------------------------------------------------------------


def test_the_analyst_records_which_guidance_it_was_shown(settings: Settings) -> None:
    """So a client can see which rule the reasoning rested on."""
    from costsentinel.agents.root_cause_analyst import make_root_cause_analyst

    embedder = get_embedder(settings)
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        node = make_root_cause_analyst(
            llm=FakeLLM(), settings=settings, memory=store, embedder=embedder
        )
        state = ScanState(
            run_id="run-1",
            client=POLICY_CLIENT,
            signals=(_signal("sig-1", WasteKind.IDLE_VIRTUAL_MACHINE, "/vm/idle"),),
        )
        update = node(state)
        (cause,) = update["root_causes"]
        assert cause.guidance_citations
        assert any("Cloud Cost Policy" in citation for citation in cause.guidance_citations)
    finally:
        store.close()


def test_the_analyst_works_identically_with_no_memory(settings: Settings) -> None:
    """A graph built without memory produces the explanations it did before memory."""
    from costsentinel.agents.root_cause_analyst import make_root_cause_analyst

    node = make_root_cause_analyst(llm=FakeLLM(), settings=settings)
    state = ScanState(
        run_id="run-1",
        client=CLIENT,
        signals=(_signal("sig-1", WasteKind.IDLE_VIRTUAL_MACHINE, "/vm/idle"),),
    )
    (cause,) = node(state)["root_causes"]
    assert cause.guidance_citations == ()
    assert cause.narrative


def test_a_guidance_passage_cannot_become_a_cited_factor(settings: Settings) -> None:
    """Retrieved text may ground a narrative; it cannot become a fact about the estate.

    A factor must still cite a detector observation, so the grounding widens what an
    explanation rests on and not what it may assert.
    """
    from costsentinel.agents.root_cause_analyst import make_root_cause_analyst

    embedder = get_embedder(settings)
    store = SqliteMemoryStore()
    try:
        seed(store, embedder)
        node = make_root_cause_analyst(
            llm=FakeLLM(), settings=settings, memory=store, embedder=embedder
        )
        signal = _signal("sig-1", WasteKind.IDLE_VIRTUAL_MACHINE, "/vm/idle")
        update = node(ScanState(run_id="run-1", client=POLICY_CLIENT, signals=(signal,)))
        (cause,) = update["root_causes"]
        observations = {obs.name for obs in signal.evidence}
        for factor in cause.factors:
            assert factor.evidence_name in observations
    finally:
        store.close()


# ---------------------------------------------------------------------------
# THE INVARIANT: memory reorders and annotates, it never recomputes
# ---------------------------------------------------------------------------


def _money_fingerprint(report: ClientReport) -> dict[str, str]:
    """Every monetary figure in a report, keyed so a diff names what moved.

    Keyed by recommendation id rather than by rank on purpose: the whole point is
    that the ranks differ between the two runs, so a rank-keyed comparison would
    compare different findings and pass for the wrong reason.
    """
    figures: dict[str, str] = {
        "totals.observed_monthly_spend": report.totals.observed_monthly_spend.display(),
        "totals.projected_monthly_savings": report.totals.projected_monthly_savings.display(),
        "totals.projected_annual_savings": report.totals.projected_annual_savings.display(),
        "totals.findings_count": str(report.totals.findings_count),
        "totals.unpriced": str(report.totals.unpriced_findings_count),
    }
    for finding in report.findings:
        key = finding.recommendation_id
        figures[f"{key}.current_monthly_cost"] = finding.current_monthly_cost.display()
        figures[f"{key}.monthly_saving"] = finding.monthly_saving.display()
        figures[f"{key}.annual_saving"] = finding.annual_saving.display()
        figures[f"{key}.savings_basis"] = finding.savings_basis
        figures[f"{key}.savings_is_estimated"] = str(finding.savings_is_estimated)
    for summary in report.waste_breakdown:
        figures[f"breakdown.{summary.waste_kind.value}.monthly"] = summary.monthly_saving.display()
        figures[f"breakdown.{summary.waste_kind.value}.annual"] = summary.annual_saving.display()
    return figures


def _scan(settings: Settings, store: Any, run_id: str) -> ClientReport:
    report, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=MockAzureProvider(seed=1337),
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
        memory=store,
        run_id=run_id,
    )
    return report


def test_a_seeded_rejection_changes_the_order_and_not_one_number(
    settings: Settings,
) -> None:
    """The invariant the whole phase rests on, asserted end to end.

    Two runs of the same estate: one with an empty store, one with a rejection
    recorded against the top finding. Every money figure must be byte-identical, the
    set of findings must be identical, and only the order and the annotations may
    differ.
    """
    clean = SqliteMemoryStore()
    seeded = SqliteMemoryStore()
    try:
        baseline = _scan(settings, clean, "run-baseline")
        # Record a rejection against whatever ranked first on merit.
        top = baseline.findings[0]
        _scan(settings, seeded, "run-first")
        record_decision(
            seeded,
            recommendation_id=top.recommendation_id,
            outcome=Outcome.REJECTED,
            reason="owner confirmed this is a quarter-end batch host",
            decided_by="operator",
        )
        after = _scan(settings, seeded, "run-second")

        # 1. Not one monetary figure moved.
        assert _money_fingerprint(after) == _money_fingerprint(baseline)

        # 2. The same findings are present. Memory does not suppress waste.
        assert {f.recommendation_id for f in after.findings} == {
            f.recommendation_id for f in baseline.findings
        }

        # 3. The order did change, and the declined finding is no longer first.
        before_order = [f.recommendation_id for f in baseline.findings]
        after_order = [f.recommendation_id for f in after.findings]
        assert after_order != before_order
        assert after_order[0] != top.recommendation_id

        # 4. It is annotated with the date and the reason, and says where it ranked.
        declined = next(f for f in after.findings if f.recommendation_id == top.recommendation_id)
        assert "Previously declined" in declined.memory_note
        assert "quarter-end batch host" in declined.memory_note
        assert declined.base_rank == 1
        assert declined.rank > 1

        # 5. The report says so in one line.
        assert "1 previously declined" in after.memory_recall
        assert baseline.memory_recall == ""
    finally:
        clean.close()
        seeded.close()


def test_the_ranking_components_themselves_are_unchanged_by_memory(
    settings: Settings,
) -> None:
    """Memory adjusts the order *after* the composite, not the composite itself.

    Keeping the computed components intact is what lets a report show both the merit
    score and the memory adjustment, instead of a single number nobody can
    decompose.
    """
    clean = SqliteMemoryStore()
    seeded = SqliteMemoryStore()
    try:
        baseline = _scan(settings, clean, "run-a")
        _scan(settings, seeded, "run-b")
        record_decision(
            seeded,
            recommendation_id=baseline.findings[0].recommendation_id,
            outcome=Outcome.REJECTED,
            reason="not now",
            decided_by="operator",
        )
        after = _scan(settings, seeded, "run-c")

        before_scores = {
            f.recommendation_id: (f.ranking_composite, f.ranking_breakdown)
            for f in baseline.findings
        }
        after_scores = {
            f.recommendation_id: (f.ranking_composite, f.ranking_breakdown) for f in after.findings
        }
        assert after_scores == before_scores
    finally:
        clean.close()
        seeded.close()


def test_a_deferral_demotes_exactly_as_a_rejection_does(settings: Settings) -> None:
    """A deferral is considered advice too, and should not be re-pitched as new."""
    clean = SqliteMemoryStore()
    seeded = SqliteMemoryStore()
    try:
        baseline = _scan(settings, clean, "run-a")
        top = baseline.findings[0]
        _scan(settings, seeded, "run-b")
        record_decision(
            seeded,
            recommendation_id=top.recommendation_id,
            outcome=Outcome.DEFERRED,
            reason="scheduled for the January maintenance window",
            decided_by="operator",
        )
        after = _scan(settings, seeded, "run-c")
        assert _money_fingerprint(after) == _money_fingerprint(baseline)
        declined = next(f for f in after.findings if f.recommendation_id == top.recommendation_id)
        assert "Previously deferred" in declined.memory_note
        assert declined.rank > 1
    finally:
        clean.close()
        seeded.close()


def test_an_accepted_outcome_is_noted_without_reordering(settings: Settings) -> None:
    clean = SqliteMemoryStore()
    seeded = SqliteMemoryStore()
    try:
        baseline = _scan(settings, clean, "run-a")
        top = baseline.findings[0]
        _scan(settings, seeded, "run-b")
        record_decision(
            seeded,
            recommendation_id=top.recommendation_id,
            outcome=Outcome.ACCEPTED,
            reason="agreed, scheduling it",
            decided_by="operator",
        )
        after = _scan(settings, seeded, "run-c")

        assert _money_fingerprint(after) == _money_fingerprint(baseline)
        assert [f.recommendation_id for f in after.findings] == [
            f.recommendation_id for f in baseline.findings
        ]
        accepted = next(f for f in after.findings if f.recommendation_id == top.recommendation_id)
        assert "Previously accepted" in accepted.memory_note
        assert accepted.rank == 1
    finally:
        clean.close()
        seeded.close()


def test_a_run_with_memory_off_matches_a_run_with_an_empty_store(
    settings: Settings,
) -> None:
    """Empty-safe and off-safe have to be the same behaviour, not merely similar."""
    empty = SqliteMemoryStore()
    try:
        with_store = _scan(settings, empty, "run-empty")
        without = _scan(settings, NullMemoryStore(), "run-off")
        assert _money_fingerprint(with_store) == _money_fingerprint(without)
        assert [f.recommendation_id for f in with_store.findings] == [
            f.recommendation_id for f in without.findings
        ]
        assert with_store.memory_recall == "" == without.memory_recall
    finally:
        empty.close()


def test_the_history_is_written_once_per_run(settings: Settings) -> None:
    store = SqliteMemoryStore()
    try:
        _scan(settings, store, "run-one")
        _scan(settings, store, "run-two")
        runs = store.runs_for(CLIENT_NORTHWIND)
        assert [r.run_id for r in runs] == ["run-two", "run-one"]
        assert all(r.entry_count > 0 for r in runs)
        assert all(r.resources_examined > 0 for r in runs)
    finally:
        store.close()


def test_a_history_entry_carries_the_saving_as_a_string_not_a_figure(
    settings: Settings,
) -> None:
    """So a stale figure cannot be summed into a later total."""
    store = SqliteMemoryStore()
    try:
        _scan(settings, store, "run-one")
        entries = store.entries_for(CLIENT_NORTHWIND)
        assert entries
        for entry in entries:
            assert isinstance(entry.monthly_saving_display, str)
            assert "USD" in entry.monthly_saving_display or entry.monthly_saving_display == ""
        dumped = entries[0].model_dump()
        assert not any(isinstance(value, Decimal) for value in dumped.values())
    finally:
        store.close()


def test_two_clients_histories_do_not_cross_in_a_full_sweep(settings: Settings) -> None:
    from costsentinel.graph.runner import run_scan

    store = SqliteMemoryStore()
    try:
        result = run_scan(settings=settings, memory=store, requested_by="test")
        assert len(result.reports) >= 2
        for report in result.reports:
            entries = store.entries_for(report.client)
            assert entries
            assert {e.client for e in entries} == {report.client}
    finally:
        store.close()


def test_a_write_failure_does_not_cost_the_client_their_report(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing a memory write is an inconvenience. Losing the report is not acceptable."""
    store = SqliteMemoryStore()
    store.close()  # every write now fails
    with caplog.at_level("WARNING"):
        report = _scan(settings, store, "run-broken")
    assert report.findings
    assert report.totals.projected_monthly_savings.is_known
    assert "could not write episodic history" in caplog.text


def test_time_moves_forward_between_the_decision_and_the_rescan(
    settings: Settings,
) -> None:
    """The annotation quotes a date, so a decision recorded today must read as today."""
    store = SqliteMemoryStore()
    try:
        baseline = _scan(settings, store, "run-a")
        yesterday = datetime.now(UTC) - timedelta(days=1)
        record_decision(
            store,
            recommendation_id=baseline.findings[0].recommendation_id,
            outcome=Outcome.REJECTED,
            reason="declined yesterday",
            decided_by="operator",
            decided_at=yesterday,
        )
        after = _scan(settings, store, "run-b")
        declined = next(
            f
            for f in after.findings
            if f.recommendation_id == baseline.findings[0].recommendation_id
        )
        assert yesterday.date().isoformat() in declined.memory_note
    finally:
        store.close()
