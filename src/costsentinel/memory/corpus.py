"""The committed reference corpus, and how it becomes retrievable passages.

Three documents ship with the package: a synthetic org FinOps policy, a synthetic
restatement of cost-optimisation practice, and notes on how Azure's billing shape
works. All three are written for the demo estate and say so in their first lines.
Nothing real is committed here -- no client's actual policy, no vendor's copied text
-- and a test asserts the corpus passes the real-data guard.

Chunking is by Markdown heading rather than by character count. A fixed-size window
cuts a rule in half, and half a rule retrieved and cited is worse than no citation:
"may be deleted only once two conditions hold" is actively misleading without the
conditions. A section is the unit a policy is actually written in, so it is the unit
retrieved.

The org policy is scoped to a client; the other two are shared. That distinction
matters at retrieval: one client's own rules must never be cited to another, and the
store enforces it in the query rather than afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import NamedTuple

from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.memory import GuidanceKind, SemanticChunk, SemanticDocument
from costsentinel.memory.base import MemoryStore
from costsentinel.memory.embedding import Embedder
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.corpus")

#: The client the synthetic policy belongs to. Matches the mock estate, so the demo
#: shows a client-scoped document being retrieved for its owner and withheld from
#: everybody else.
POLICY_CLIENT = "northwind-energy"

#: Headings shallower than this are the document title and its front matter; the
#: retrievable unit is a numbered section.
_SECTION = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)

#: Shortest passage worth storing. A heading with one line under it is usually a
#: stub, and retrieving it adds noise without adding a citation worth making.
MIN_CHUNK_CHARS = 120

#: Below this similarity a passage is not cited at all.
#:
#: Measured against this corpus with the default fake embedder rather than guessed:
#: a relevant query scores around 0.24, a loosely-related one around 0.06, and an
#: unrelated one 0.00. 0.10 sits in the gap. It is a property of the embedder as much
#: as of the corpus, so a real embedder may want a different value -- which is why it
#: is a named constant and a keyword argument rather than a literal.
MIN_RELEVANCE = 0.10


class CorpusDocument(NamedTuple):
    """One committed document and how it should be scoped."""

    document_id: str
    filename: str
    title: str
    kind: GuidanceKind
    client: str | None


#: The committed corpus. Adding a document means adding a line here, so what is
#: seeded is a reviewable list rather than whatever happens to be in the directory.
CORPUS: tuple[CorpusDocument, ...] = (
    CorpusDocument(
        document_id="northwind-finops-policy",
        filename="finops-policy.md",
        title="Northwind Energy Cloud Cost Policy",
        kind=GuidanceKind.ORG_POLICY,
        client=POLICY_CLIENT,
    ),
    CorpusDocument(
        document_id="cost-pillar-guidance",
        filename="cost-pillar.md",
        title="Cost-optimisation guidance",
        kind=GuidanceKind.COST_PILLAR,
        client=None,
    ),
    CorpusDocument(
        document_id="pricing-meter-notes",
        filename="pricing-notes.md",
        title="Pricing and meter notes",
        kind=GuidanceKind.PRICING_NOTE,
        client=None,
    ),
)


def corpus_dir() -> Path:
    """Where the committed documents live."""
    return Path(str(resources.files("costsentinel.memory") / "data"))


def load_documents() -> tuple[SemanticDocument, ...]:
    """Read every committed document.

    Raises:
        FileNotFoundError: If a document named in :data:`CORPUS` is missing. Loudly,
            because a silently-absent policy document would mean a client's own rules
            quietly stopped being cited.
    """
    directory = corpus_dir()
    documents: list[SemanticDocument] = []
    for entry in CORPUS:
        path = directory / entry.filename
        documents.append(
            SemanticDocument(
                document_id=entry.document_id,
                title=entry.title,
                kind=entry.kind,
                client=entry.client,
                body=path.read_text(encoding="utf-8"),
                source=f"costsentinel/memory/data/{entry.filename}",
            )
        )
    return tuple(documents)


def _sections(body: str) -> list[tuple[str, str]]:
    """Split a document into (heading, text) pairs on its level-two headings."""
    matches = list(_SECTION.finditer(body))
    if not matches:
        return [("", body.strip())]
    sections: list[tuple[str, str]] = []
    for index, found in enumerate(matches):
        start = found.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((found.group("heading"), body[start:end].strip()))
    return sections


def chunk(document: SemanticDocument, embedder: Embedder) -> tuple[SemanticChunk, ...]:
    """Split one document into embedded, retrievable passages.

    The heading is prepended to the embedded text. A section titled "Snapshots and
    backups" whose body never repeats the word "snapshot" would otherwise be
    unreachable by a query for snapshots, which is the most obvious failure mode of
    a lexical embedder.
    """
    chunks: list[SemanticChunk] = []
    for index, (heading, text) in enumerate(_sections(document.body)):
        if len(text) < MIN_CHUNK_CHARS:
            continue
        embedded = f"{heading}\n{text}" if heading else text
        chunks.append(
            SemanticChunk(
                chunk_id=f"{document.document_id}:{index:02d}",
                document_id=document.document_id,
                document_title=document.title,
                kind=document.kind,
                client=document.client,
                heading=heading,
                text=text,
                embedding=embedder.embed(embedded),
                embedder=embedder.name,
                provenance=Provenance(
                    source=ProvenanceSource.OPERATOR,
                    reference=(
                        f"{document.source}"
                        f"{f' - {heading}' if heading else ''} "
                        f"(synthetic reference corpus)"
                    ),
                    verification=Verification.VERIFIED,
                ),
            )
        )
    return tuple(chunks)


def build_chunks(embedder: Embedder) -> tuple[SemanticChunk, ...]:
    """Every passage in the committed corpus, embedded."""
    return tuple(passage for document in load_documents() for passage in chunk(document, embedder))


def seed(store: MemoryStore, embedder: Embedder, *, force: bool = False) -> int:
    """Load the corpus into a store, returning how many passages were written.

    Idempotent: chunk ids are derived from the document and section, so re-seeding
    replaces rather than duplicates. Skipped when the store already holds passages
    unless ``force``, so starting the demo twice does not re-embed the corpus.
    """
    if not store.is_enabled:
        return 0
    if not force and store.chunk_count() > 0:
        _log.debug("semantic corpus already seeded", extra={"store": store.name})
        return 0
    chunks = build_chunks(embedder)
    written = store.upsert_chunks(chunks)
    _log.info(
        "semantic corpus seeded",
        extra={
            "store": store.name,
            "embedder": embedder.name,
            "documents": len(CORPUS),
            "chunks": written,
        },
    )
    return written


def retrieve(
    store: MemoryStore,
    embedder: Embedder,
    query: str,
    *,
    client: str | None = None,
    limit: int = 3,
    min_score: float = MIN_RELEVANCE,
) -> tuple[SemanticChunk, ...]:
    """Passages relevant to a query, best first.

    ``min_score`` discards near-irrelevant hits. Without it, a store always returns
    its ``limit`` nearest passages however unrelated they are, and an explanation
    citing an unrelated policy section is worse than one citing nothing: it looks
    grounded and is not.
    """
    if not query.strip():
        return ()
    hits = store.search(embedder.embed(query), limit=limit, client=client)
    return tuple(hit.chunk for hit in hits if hit.score >= min_score)


def citations(chunks: Sequence[SemanticChunk]) -> tuple[str, ...]:
    """How a set of retrieved passages is referred to in a report."""
    return tuple(passage.citation for passage in chunks)
