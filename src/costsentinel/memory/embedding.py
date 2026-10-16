"""The embedding seam, and a fake that is actually useful.

``FakeEmbedder`` is not a stub returning zeros. It hashes tokens into a fixed-width
vector and normalises the result, so cosine similarity between two texts genuinely
tracks how much vocabulary they share. That matters for two reasons: the demo
retrieves something sensible without a credential, and a test can assert that
searching for "orphaned disk" ranks the orphaned-disk passage first -- which a
constant or random embedding could not support.

It is not a semantic model. It has no notion that "idle" and "unused" are related,
so retrieval quality is lexical. The real embedder exists for that, opt-in via
``EMBEDDER_BACKEND``. The point of the fake is that the *plumbing* -- chunking,
storage, ranking, citation, client scoping -- is fully exercised offline, so when a
real embedder is switched on the only new variable is the vector.

Every chunk records which embedder produced it. Mixing vectors from two embedders in
one store would produce silently meaningless similarities, and the identifier is what
makes that detectable rather than mysterious.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from costsentinel.config import EmbedderBackend, Settings
from costsentinel.memory.base import MemoryNotConfiguredError, MemoryStoreError
from costsentinel.observability.logging import get_logger

_log = get_logger("memory.embedding")

#: Tokens are lower-cased alphanumeric runs. Deliberately crude: the fake embedder's
#: job is to be reproducible and lexically sensible, not to tokenise well.
_TOKEN = re.compile(r"[a-z0-9]+")

#: Words carrying no retrieval signal. A short list on purpose -- an aggressive stop
#: list would start making editorial decisions about the corpus.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "which",
        "with",
    }
)

#: Shortest token worth keeping. Two-character tokens are overwhelmingly noise
#: ("vm" being the notable exception, which the corpus spells out anyway).
MIN_TOKEN_LENGTH = 3

INSTALL_HINT = "install the optional LLM extra with: uv sync --extra llm"


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a vector."""

    @property
    def name(self) -> str:
        """Identifier recorded on every chunk this embedder produced."""
        ...

    @property
    def dimensions(self) -> int:
        """Vector width."""
        ...

    def embed(self, text: str) -> tuple[float, ...]:
        """Embed one string."""
        ...

    def embed_batch(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed several strings, which a real backend does in one round trip."""
        ...


def tokenise(text: str) -> list[str]:
    """Lower-cased alphanumeric tokens, with the shortest and commonest dropped."""
    return [
        token
        for token in _TOKEN.findall(text.lower())
        if len(token) >= MIN_TOKEN_LENGTH and token not in _STOPWORDS
    ]


#: Default width for the fake embedder, and the reason it is this wide.
#:
#: Hashed bag-of-words lives or dies on collisions. A reference section in the
#: committed corpus holds 37-86 distinct tokens; at 256 buckets those fill most of
#: the vector, so *every* query overlaps *every* document and the similarity scores
#: compress into a band too narrow to threshold. Measured on the real corpus: at 256
#: dimensions a relevant query scores 0.27 and nonsense scores 0.15, which cannot be
#: separated. At 4096, relevant stays at 0.24 and nonsense drops to 0.00.
#:
#: This was found by a test asserting that an irrelevant query retrieves nothing,
#: which failed at the original width. The threshold was guessed; the width is now
#: measured.
DEFAULT_FAKE_DIMENSIONS = 4096


class FakeEmbedder:
    """Deterministic hashed bag-of-words. The default, and the only one CI uses."""

    def __init__(self, dimensions: int = DEFAULT_FAKE_DIMENSIONS) -> None:
        """Fix the vector width.

        Args:
            dimensions: Vector width. Recorded in :attr:`name`, because a stored
                chunk embedded at a different width cannot be compared to a query
                and should be visibly, not silently, incompatible. See
                :data:`DEFAULT_FAKE_DIMENSIONS` for why the default is as large as
                it is.
        """
        self._dimensions = max(16, dimensions)

    @property
    def name(self) -> str:
        """Identifier recorded on every chunk this embedder produced."""
        return f"fake-hashed-bow-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        """Vector width."""
        return self._dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        """Hash tokens into a normalised vector.

        Sub-linear term weighting (``1 + log(count)``) so a word repeated twenty
        times does not swamp the vector, which is the one place a naive bag of words
        gives obviously wrong retrieval.

        Built sparsely: the weights and the norm are computed over the buckets that
        actually have tokens, and the dense vector is materialised once at the end.
        The obvious implementation loops over all 4096 buckets three times, which
        makes embedding cost scale with the vector width rather than with the length
        of the text -- fine in isolation, and a seven-fold slowdown of the whole test
        suite under coverage instrumentation, which is how it was noticed.
        """
        counts: dict[int, float] = {}
        for token in tokenise(text):
            bucket = self._bucket(token)
            counts[bucket] = counts.get(bucket, 0.0) + 1.0

        weights = {bucket: 1.0 + math.log(count) for bucket, count in counts.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        if norm == 0.0:
            # No usable tokens. A zero vector scores 0.0 against everything, which
            # is the honest answer for a query with no content.
            return (0.0,) * self._dimensions

        vector = [0.0] * self._dimensions
        for bucket, weight in weights.items():
            vector[bucket] = weight / norm
        return tuple(vector)

    def embed_batch(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed several strings."""
        return tuple(self.embed(text) for text in texts)

    def _bucket(self, token: str) -> int:
        """Stable bucket for a token.

        ``blake2b`` rather than ``hash()``: Python's string hash is salted per
        process, so a vector built in one run would not match one built in the next.
        A memory store that silently stops matching after a restart would be worse
        than no memory at all.
        """
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self._dimensions


class AzureOpenAIEmbedder:
    """Real embeddings from an Azure OpenAI deployment. Opt-in.

    Authenticates the same way as the chat backend -- ``DefaultAzureCredential``
    unless an API key is set -- so one ``az login`` covers the estate read, the model
    calls and the embeddings.
    """

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        """Validate configuration and prepare a client.

        Args:
            settings: Resolved configuration.
            client: An already-built client, which is how tests drive this.

        Raises:
            MemoryNotConfiguredError: If required configuration is missing.
        """
        self._settings = settings
        self._client = client
        self._deployment = settings.azure_openai_embedding_deployment
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint),
                ("AZURE_OPENAI_API_VERSION", settings.azure_openai_api_version),
                ("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", self._deployment),
            )
            if not value
        ]
        if missing:
            msg = (
                f"EMBEDDER_BACKEND=azure_openai requires {', '.join(missing)}. Set "
                f"EMBEDDER_BACKEND=fake (the default) to run with no credentials."
            )
            raise MemoryNotConfiguredError(msg)

    @property
    def name(self) -> str:
        """Identifier recorded on every chunk this embedder produced."""
        return f"azure-openai-{self._deployment}"

    @property
    def dimensions(self) -> int:
        """Expected vector width, from configuration.

        The deployment decides the real width; this is what the store was told to
        expect, and a mismatch shows up as a length check in
        :func:`~costsentinel.memory.similarity.cosine_similarity`.
        """
        return self._settings.embedding_dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        """Embed one string."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed several strings in one round trip."""
        if not texts:
            return ()
        client = self._ensure_client()
        try:
            response = client.embeddings.create(model=self._deployment, input=list(texts))
        except Exception as exc:
            msg = f"the Azure OpenAI embedding call to {self._deployment!r} failed: {exc}"
            raise MemoryStoreError(msg) from exc
        return tuple(tuple(float(value) for value in item.embedding) for item in response.data)

    def _ensure_client(self) -> Any:
        client = self._client
        if client is not None:
            return client
        # Imported here, not at module scope: the openai SDK is an optional extra and
        # importing this module must not require it.
        from costsentinel.llm.azure_openai import client_kwargs  # noqa: PLC0415

        try:
            module = __import__("openai", fromlist=["AzureOpenAI"])
        except ImportError as exc:
            msg = (
                f"the openai package is required for EMBEDDER_BACKEND=azure_openai; {INSTALL_HINT}"
            )
            raise MemoryNotConfiguredError(msg) from exc
        client = module.AzureOpenAI(**client_kwargs(self._settings))
        self._client = client
        return client


def get_embedder(settings: Settings) -> Embedder:
    """Build the embedder this configuration selects.

    The only place a concrete embedder is named, mirroring ``get_provider`` and
    ``get_llm``.
    """
    if settings.embedder_backend is EmbedderBackend.FAKE:
        return FakeEmbedder(settings.embedding_dimensions)
    _log.info("using a real embedder", extra={"backend": settings.embedder_backend.value})
    return AzureOpenAIEmbedder(settings)
