"""Memory: what CostSentinel remembers about an estate, and what it learned.

Two kinds, modelled separately because they answer different questions.

**Episodic memory** is "what happened here before": every run's findings and
classifications per subscription and client, and -- crucially -- what a human
decided about each recommendation. This is the per-estate history, and it is the
only thing that lets a sweep stop re-pitching advice a client already declined.

**Semantic memory** is "what is true in general": the org FinOps policy, cost-pillar
guidance, pricing and meter notes. Retrieved to ground an explanation, never to
produce a figure.

One rule governs both, and it is the invariant the whole memory phase rests on:
**memory may reorder and annotate; it may never recompute.** No type in this module
holds a :class:`~costsentinel.domain.common.MoneyAmount`. A saving is arithmetic over
provider data, and nothing a client said last month changes what a disk costs this
month. Monetary figures appear here only as pre-rendered display strings, carried so
a history can be read back, and they are never summed or re-derived.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import Frozen, Provenance, utc_now
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import WasteKind


class Outcome(StrEnum):
    """What a human decided about a recommendation.

    Wider than :class:`~costsentinel.domain.governance.Decision`, which is the
    approval gate's binary authorisation. An outcome is a *learning* signal, so
    ``DEFERRED`` is a first-class member: "not now" is different information from
    "no", and treating them the same would either nag a client about something they
    intend to do, or forget something they only postponed.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"

    @property
    def discourages_repitch(self) -> bool:
        """Whether this outcome means the advice should not be offered as new again."""
        return self is not Outcome.ACCEPTED


class RecommendationOutcome(Frozen):
    """A recorded human decision about one recommendation, kept for next time.

    ``reason`` is mandatory and non-empty. An outcome with no reason teaches the next
    sweep nothing -- it can suppress a repeat but cannot explain the suppression to
    the client, which is the whole point of recording it.

    ``monthly_saving_display`` is a pre-rendered string, deliberately not a
    :class:`MoneyAmount`. It exists so a history can be read back in the terms the
    client saw at the time; it is never re-summed, and a stale figure here cannot
    reach a report total.
    """

    outcome_id: str
    recommendation_id: str
    run_id: str = Field(description="The run that produced the recommendation.")
    client: str
    subscription_id: str
    target_resource_id: str
    target_resource_name: str = ""
    waste_kind: WasteKind
    action: ActionType
    outcome: Outcome
    reason: str = Field(min_length=1, max_length=2000)
    decided_by: str = Field(min_length=1)
    decided_at: datetime = Field(default_factory=utc_now)
    monthly_saving_display: str = ""
    provenance: Provenance

    @property
    def exact_key(self) -> tuple[str, str, str]:
        """Identity of the *specific* advice: this client, this resource, this action."""
        return (self.client, self.target_resource_id, self.action.value)

    @property
    def pattern_key(self) -> tuple[str, str, str]:
        """Identity of the *kind* of advice: this client, this waste kind, this action.

        A looser key than :attr:`exact_key`, used to notice that a client has already
        said no to this sort of thing somewhere else in their estate.
        """
        return (self.client, self.waste_kind.value, self.action.value)


class EpisodicEntry(Frozen):
    """One recommendation as it stood when a run produced it.

    A snapshot, not a reference: the resource may be gone next month and the price
    may have changed, and a history that silently re-resolved either would stop being
    a history. Hence the display string rather than a live figure.
    """

    recommendation_id: str
    signal_id: str
    run_id: str
    client: str
    subscription_id: str
    target_resource_id: str
    target_resource_name: str
    waste_kind: WasteKind
    action: ActionType
    action_class: ActionClass
    requires_approval: bool
    policy_reference: str = ""
    monthly_saving_display: str = ""
    rank: int = Field(ge=1)
    recorded_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    @property
    def exact_key(self) -> tuple[str, str, str]:
        """Identity of the specific advice this entry records."""
        return (self.client, self.target_resource_id, self.action.value)


class EpisodicRecord(Frozen):
    """One run's episodic history for one client.

    Written as a unit after the report is composed, so a sweep that failed halfway
    does not leave a partial history that a later run would read as fact.
    """

    run_id: str
    client: str
    subscriptions: tuple[str, ...] = ()
    resources_examined: int = Field(default=0, ge=0)
    entries: tuple[EpisodicEntry, ...] = ()
    recorded_at: datetime = Field(default_factory=utc_now)
    provenance: Provenance

    @property
    def entry_count(self) -> int:
        """How many recommendations this run recorded."""
        return len(self.entries)


class RecallKind(StrEnum):
    """How strongly a prior outcome applies to a recommendation.

    The distinction is load-bearing. An ``EXACT`` match is the same advice about the
    same resource, so repeating it as new would be straightforwardly wrong. A
    ``PATTERN`` match is the same kind of advice about a *different* resource, which
    is weaker evidence: a client who declined to resize one VM may well accept
    resizing another. Both are worth telling the client about; only one justifies a
    full demotion.
    """

    EXACT = "exact"
    PATTERN = "pattern"


class MemoryRecall(Frozen):
    """What episodic memory had to say about one recommendation.

    Carries the base rank *and* the adjusted rank rather than silently replacing the
    order. A client should be able to read "this ranked second on merit and was
    demoted to fifth because you declined it in September" -- an ordering that
    changed for reasons nobody can see is worse than no reordering at all.
    """

    recommendation_id: str
    kind: RecallKind
    prior_outcome: Outcome
    prior_recommendation_id: str
    prior_reason: str
    decided_at: datetime
    decided_by: str = ""
    annotation: str = Field(
        min_length=1,
        description="The line the client reads, naming the date and the reason.",
    )
    penalty: Decimal = Field(
        ge=Decimal(0),
        le=Decimal(1),
        description=(
            "Share of the base ranking composite withheld. Affects ordering only; "
            "no monetary figure is touched."
        ),
    )
    base_rank: int = Field(ge=1)
    adjusted_rank: int = Field(ge=1)
    provenance: Provenance

    @property
    def was_demoted(self) -> bool:
        """Whether the adjustment actually moved this recommendation down."""
        return self.adjusted_rank > self.base_rank

    @property
    def discourages_repitch(self) -> bool:
        """Whether the prior outcome was a decline or a deferral."""
        return self.prior_outcome.discourages_repitch


# ---------------------------------------------------------------------------
# Semantic memory
# ---------------------------------------------------------------------------


class GuidanceKind(StrEnum):
    """What sort of reference material a chunk came from.

    A closed vocabulary so a retrieved passage can be attributed in a report: "your
    own FinOps policy says" carries different weight from "general cost guidance
    suggests", and conflating them would misrepresent the client's own rules.
    """

    ORG_POLICY = "org_policy"
    COST_PILLAR = "cost_pillar"
    PRICING_NOTE = "pricing_note"


class SemanticDocument(Frozen):
    """A reference document before it is chunked."""

    document_id: str
    title: str
    kind: GuidanceKind
    client: str | None = Field(
        default=None,
        description=(
            "Set when the document is one client's own policy. None means general "
            "guidance shared across clients. Retrieval filters on this, so a "
            "client's own rules can never be cited to another."
        ),
    )
    body: str = Field(min_length=1)
    source: str = Field(description="Where the text came from, for attribution.")


class SemanticChunk(Frozen):
    """One retrievable passage, with its embedding and its provenance.

    The embedding is a plain tuple of floats rather than a vendor type, so the store
    and the embedder stay independent and a chunk can be round-tripped through JSON
    or SQLite without a dependency.
    """

    chunk_id: str
    document_id: str
    document_title: str
    kind: GuidanceKind
    client: str | None = None
    heading: str = ""
    text: str = Field(min_length=1)
    embedding: tuple[float, ...] = Field(default=())
    embedder: str = Field(default="", description="Which embedder produced the vector.")
    provenance: Provenance

    @property
    def citation(self) -> str:
        """How this passage is referred to in a report or an explanation."""
        return f"{self.document_title}{f' - {self.heading}' if self.heading else ''}"


class SemanticHit(Frozen):
    """One retrieved passage and how well it matched."""

    chunk: SemanticChunk
    score: float = Field(ge=-1.0, le=1.0, description="Cosine similarity.")

    @property
    def citation(self) -> str:
        """How this passage is referred to in a report or an explanation."""
        return self.chunk.citation


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class MemoryOperation(StrEnum):
    """What a memory access was doing, for the trace."""

    RECORD_RUN = "record_run"
    RECORD_OUTCOME = "record_outcome"
    READ_OUTCOMES = "read_outcomes"
    READ_ENTRIES = "read_entries"
    UPSERT_CHUNKS = "upsert_chunks"
    SEARCH = "search"


class MemoryAccess(Frozen):
    """One memory read or write, recorded so a hit rate is observable.

    A memory layer whose hit rate nobody can see is a memory layer nobody can tune,
    and "memory is on" is not the same claim as "memory is finding anything".
    """

    operation: MemoryOperation
    store: str
    client: str = ""
    hit: bool = Field(description="Whether the access found or wrote anything at all.")
    record_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    succeeded: bool = True
    detail: str = ""
    occurred_at: datetime = Field(default_factory=utc_now)


class MemoryMetrics(Frozen):
    """Memory accesses across one run.

    Alongside the report rather than inside it (ARCHITECTURE.md D45): how long a
    lookup took is an operational fact, not something a client should read.
    """

    accesses: tuple[MemoryAccess, ...] = ()

    @property
    def access_count(self) -> int:
        """How many memory operations the run performed."""
        return len(self.accesses)

    @property
    def hit_count(self) -> int:
        """How many found or wrote something."""
        return sum(1 for access in self.accesses if access.hit)

    @property
    def miss_count(self) -> int:
        """How many found nothing.

        A miss is not a failure: an estate nobody has scanned before misses on every
        read, which is correct and expected.
        """
        return sum(1 for access in self.accesses if not access.hit and access.succeeded)

    @property
    def failure_count(self) -> int:
        """How many could not be completed."""
        return sum(1 for access in self.accesses if not access.succeeded)

    @property
    def total_latency_ms(self) -> int:
        """Time spent in memory operations."""
        return sum(access.latency_ms for access in self.accesses)

    def hit_rate(self) -> Decimal | None:
        """Share of successful reads that found something, or ``None`` if none ran."""
        reads = [
            access
            for access in self.accesses
            if access.succeeded
            and access.operation
            in (
                MemoryOperation.READ_OUTCOMES,
                MemoryOperation.READ_ENTRIES,
                MemoryOperation.SEARCH,
            )
        ]
        if not reads:
            return None
        hits = sum(1 for access in reads if access.hit)
        return Decimal(hits) / Decimal(len(reads))
