"""Slack signature verification (ARCHITECTURE.md D134).

Synthetic requests and a labelled test secret only: nothing here has ever been sent by
Slack, and nothing here could authenticate to a real workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from costsentinel.slack.signature import (
    DEFAULT_WINDOW_SECONDS,
    SignatureError,
    SignatureFailure,
    sign,
    verify,
)

#: Labelled so nobody mistakes it for a credential. It signs synthetic requests only.
SECRET = "synthetic-test-signing-secret-not-a-real-slack-secret"  # noqa: S105 -- labelled fake
NOW = 1_730_600_000
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"


def _verify(
    *,
    body: bytes = BODY,
    timestamp: str | None = str(NOW),
    signature: str | None = None,
    now: float = NOW,
    secret: str = SECRET,
) -> None:
    if signature is None and timestamp is not None:
        signature = sign(SECRET, timestamp, body)
    verify(secret=secret, body=body, timestamp=timestamp, signature=signature, now=now)


def test_a_valid_signature_is_accepted() -> None:
    """The one way to pass: Slack's scheme, this body, this secret, a current time."""
    _verify()


def test_the_signature_matches_slacks_documented_scheme() -> None:
    """v0= plus hex HMAC-SHA256 of v0:<timestamp>:<body>, computed independently here."""
    import hashlib
    import hmac

    expected = hmac.new(SECRET.encode(), f"v0:{NOW}:".encode() + BODY, hashlib.sha256).hexdigest()
    assert sign(SECRET, str(NOW), BODY) == f"v0={expected}"


# ---------------------------------------------------------------------------
# Bad
# ---------------------------------------------------------------------------


def test_a_bad_signature_is_refused() -> None:
    """One wrong hex digit is a forgery."""
    good = sign(SECRET, str(NOW), BODY)
    forged = good[:-1] + ("0" if good[-1] != "0" else "1")
    with pytest.raises(SignatureError) as caught:
        _verify(signature=forged)
    assert caught.value.failure is SignatureFailure.MISMATCH


def test_a_tampered_body_is_refused() -> None:
    """A valid signature for one body does not cover another, e.g. a swapped decision."""
    signature = sign(SECRET, str(NOW), BODY)
    with pytest.raises(SignatureError) as caught:
        _verify(body=BODY.replace(b"block_actions", b"block_actionz"), signature=signature)
    assert caught.value.failure is SignatureFailure.MISMATCH


def test_a_signature_made_with_another_secret_is_refused() -> None:
    """Another Slack app's request is not this app's request."""
    other = sign("a-different-synthetic-secret", str(NOW), BODY)
    with pytest.raises(SignatureError) as caught:
        _verify(signature=other)
    assert caught.value.failure is SignatureFailure.MISMATCH


@pytest.mark.parametrize("signature", ["v1=abc", "abc", "sha256=abc"])
def test_an_unknown_signature_scheme_is_refused(signature: str) -> None:
    """Only v0 exists; anything else is malformed, not merely wrong."""
    with pytest.raises(SignatureError) as caught:
        _verify(signature=signature)
    assert caught.value.failure is SignatureFailure.MALFORMED


# ---------------------------------------------------------------------------
# Missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [(str(NOW), ""), ("", "v0=abc"), (None, "v0=abc"), (str(NOW), None), (None, None)],
)
def test_a_missing_signature_or_timestamp_is_refused(
    timestamp: str | None, signature: str | None
) -> None:
    """An unsigned request is refused before anything else looks at it."""
    with pytest.raises(SignatureError) as caught:
        verify(secret=SECRET, body=BODY, timestamp=timestamp, signature=signature, now=NOW)
    assert caught.value.failure is SignatureFailure.MISSING


def test_an_empty_secret_never_verifies() -> None:
    """An empty HMAC key would make every signature trivially forgeable."""
    signature = sign("", str(NOW), BODY)
    with pytest.raises(SignatureError) as caught:
        _verify(signature=signature, secret="")
    assert caught.value.failure is SignatureFailure.MISSING


# ---------------------------------------------------------------------------
# Stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skew", [DEFAULT_WINDOW_SECONDS + 1, -(DEFAULT_WINDOW_SECONDS + 1), 86_400]
)
def test_a_timestamp_outside_the_window_is_refused(skew: int) -> None:
    """Replay protection: a captured request stops working once the window passes.

    Correctly signed, so the only thing wrong is the time, in either direction.
    """
    timestamp = str(NOW - skew)
    with pytest.raises(SignatureError) as caught:
        _verify(timestamp=timestamp, signature=sign(SECRET, timestamp, BODY))
    assert caught.value.failure is SignatureFailure.STALE


def test_the_edge_of_the_window_is_still_inside_it() -> None:
    """Five minutes exactly is accepted; the refusal is for more than that."""
    timestamp = str(NOW - DEFAULT_WINDOW_SECONDS)
    _verify(timestamp=timestamp, signature=sign(SECRET, timestamp, BODY))


@pytest.mark.parametrize("timestamp", ["17306e5", "-1730600000", "²", " 1730600000", "1.5"])
def test_a_timestamp_that_is_not_a_time_is_malformed(timestamp: str) -> None:
    """Refused before the HMAC, and never by an int() that raises on strange digits."""
    with pytest.raises(SignatureError) as caught:
        _verify(timestamp=timestamp, signature="v0=abc")
    assert caught.value.failure is SignatureFailure.MALFORMED


# ---------------------------------------------------------------------------
# How it compares
# ---------------------------------------------------------------------------


def test_the_comparison_is_constant_time() -> None:
    """`==` on a signature leaks, through timing, how much of a forgery was right."""
    tree = ast.parse(Path("src/costsentinel/slack/signature.py").read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in calls
    compares = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)]
    for node in compares:
        names = {getattr(part, "id", "") for part in (node.left, *node.comparators)}
        assert "signature" not in names or not any(
            isinstance(op, ast.Eq | ast.NotEq) for op in node.ops
        ), "a signature compared with == or !="
