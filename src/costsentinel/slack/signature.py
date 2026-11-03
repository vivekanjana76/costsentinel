"""Slack request signatures: verified before anything else reads the request.

Slack signs every request it sends with the app's signing secret. The signature is
``v0=`` plus the hex HMAC-SHA256, keyed by the secret, of ``v0:<timestamp>:<raw
body>``. The timestamp comes from the ``X-Slack-Request-Timestamp`` header and the
signature from ``X-Slack-Signature``. This module checks both, and the webhook calls it
on the **raw bytes** before it parses a single field. A check made after parsing would
be a check on what the parser understood, not on what Slack sent (D134).

Four ways to fail, one way to pass
----------------------------------

- **Missing**: either header is absent or empty.
- **Malformed**: the timestamp is not an integer, or the signature lacks the ``v0=``
  scheme. Refused before any HMAC is computed.
- **Stale**: the timestamp is more than the window (five minutes by default) from now,
  in either direction. This is the replay protection: a captured request stops being
  usable once the window passes. Inside the window, the decision service's interaction
  key makes a replay return the stored outcome rather than a second decision.
- **Mismatch**: the HMAC differs. Compared with :func:`hmac.compare_digest`, so the
  time taken does not reveal how much of a forged signature was right.

Every failure raises :class:`SignatureError`, and the webhook turns that into a 401
having recorded nothing. Only the standard library is used: ``hmac`` and ``hashlib``
are all a signature check needs.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum

#: Header names as Slack sends them.
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"

#: The only signature scheme Slack defines.
VERSION = "v0"

#: Slack's own recommendation: refuse a request more than five minutes old.
DEFAULT_WINDOW_SECONDS = 300


class SignatureFailure(StrEnum):
    """Why a request was refused. Logged; never echoed back with any detail."""

    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"


class SignatureError(Exception):
    """A request did not carry a valid, current Slack signature."""

    def __init__(self, failure: SignatureFailure, message: str) -> None:
        """Carry the failure kind for the log and the test."""
        super().__init__(message)
        self.failure = failure


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The signature Slack would send for this body at this timestamp.

    Used by the verifier, and by tests to build synthetic requests. Building a test
    request any other way would test a different scheme from the one verified.
    """
    base = f"{VERSION}:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{VERSION}={digest}"


def verify(
    *,
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    now: float,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> None:
    """Accept a request only if Slack signed exactly this body, recently.

    Args:
        secret: The app's signing secret. Never empty: the webhook refuses to start
            without one, and an empty key would make every signature trivially forgeable.
        body: The raw request body, byte for byte as received.
        timestamp: The ``X-Slack-Request-Timestamp`` header.
        signature: The ``X-Slack-Signature`` header.
        now: The current Unix time. Injected so a test can stand at any moment.
        window_seconds: How far from ``now`` a timestamp may be.

    Raises:
        SignatureError: On a missing, malformed, stale or mismatched signature.
    """
    if not secret:
        msg = "no signing secret is configured; refusing to verify against an empty key"
        raise SignatureError(SignatureFailure.MISSING, msg)
    if not timestamp or not signature:
        raise SignatureError(SignatureFailure.MISSING, "the request is not signed")
    # ASCII as well as digits: "²".isdigit() is true and int("²") raises.
    if not (timestamp.isascii() and timestamp.isdigit()):
        raise SignatureError(SignatureFailure.MALFORMED, "the request timestamp is not a time")
    if not signature.startswith(f"{VERSION}="):
        raise SignatureError(SignatureFailure.MALFORMED, "the signature scheme is not v0")
    if abs(now - int(timestamp)) > window_seconds:
        raise SignatureError(
            SignatureFailure.STALE,
            f"the request timestamp is outside the {window_seconds}-second window",
        )
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise SignatureError(SignatureFailure.MISMATCH, "the signature does not match the body")
