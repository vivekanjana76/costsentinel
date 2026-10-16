"""Cosine similarity, shared by every store that does its own search.

The SQLite store ranks in Python and the null store does not search at all; only the
pgvector store pushes the comparison into the database. Keeping the arithmetic here
means the two that *do* compare in Python agree exactly, so a test written against
the default store is meaningful about the shape of the result.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors, or ``0.0`` when it is undefined.

    Zero rather than an exception for a mismatched length or a zero vector: a
    retrieval that cannot compare two things should rank them last, not take down
    the sweep that asked. A mismatched length means a chunk was embedded by a
    different embedder, which :func:`~costsentinel.memory.embedding.Embedder`
    identifiers are meant to catch earlier.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    # Clamped because floating-point error can push an identical pair just past 1.0,
    # and SemanticHit.score is validated to [-1, 1].
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
