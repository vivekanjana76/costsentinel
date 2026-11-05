"""Approver tokens: a signed, expiring statement of who an approver is.

The dashboard's real authenticator (D136). An operator holding ``APPROVER_TOKEN_SECRET``
mints a token for one named approver with ``costsentinel approvers token <name>``, and
the approver presents it with a decision. The token says *who*; whether that person may
decide is still the allowlist's question, asked by the same
:func:`~costsentinel.approvals.identity.authorise` the Slack webhook uses. A token is
``token:<name>`` on the allowlist, and nothing more.

Format
------

``cst1.<name>.<expires>.<signature>``. The signature is the hex HMAC-SHA256, keyed by
the secret, of ``cst1.<name>.<expires>``. The expiry is a Unix time. Names are
``[A-Za-z0-9_-]``, so the dots are unambiguous. Comparison is constant-time.

What it is not
--------------

Not single sign-on, and not revocable before it expires except by rotating the secret,
which revokes every token at once. Both limits are recorded in ARCHITECTURE.md. It is
the smallest real authenticator that can be tested offline with no dependency, and the
:class:`~costsentinel.dashboard.auth.Authenticator` seam is unchanged, so SSO can
replace it.
"""

from __future__ import annotations

import hashlib
import hmac
import re

#: The format version. A token without it is not a token from this module.
PREFIX = "cst1"

#: A shorter key is refused at start-up. HMAC is only as strong as its key.
MIN_SECRET_LENGTH = 32

#: A token is a credential; it should not outlive the reason it was issued.
MAX_LIFETIME_SECONDS = 90 * 24 * 3600

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TokenError(ValueError):
    """A token that does not verify. The message never says which part was wrong."""


def _signature(secret: str, name: str, expires: int) -> str:
    message = f"{PREFIX}.{name}.{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def check_secret(secret: str) -> None:
    """Refuse a key too short to sign with.

    Raises:
        TokenError: If it is shorter than :data:`MIN_SECRET_LENGTH` characters.
    """
    if len(secret) < MIN_SECRET_LENGTH:
        msg = f"APPROVER_TOKEN_SECRET must be at least {MIN_SECRET_LENGTH} characters"
        raise TokenError(msg)


def mint(secret: str, name: str, *, now: int, lifetime_seconds: int) -> str:
    """A token for one approver, valid from now for the given lifetime.

    Raises:
        TokenError: On a weak secret, a name outside the pattern, or a lifetime that
            is not positive or exceeds :data:`MAX_LIFETIME_SECONDS`.
    """
    check_secret(secret)
    if not NAME_PATTERN.match(name):
        msg = f"{name!r} is not an approver name; use letters, digits, '_' and '-'"
        raise TokenError(msg)
    if not 0 < lifetime_seconds <= MAX_LIFETIME_SECONDS:
        msg = f"a token lives between 1 second and {MAX_LIFETIME_SECONDS // 86400} days"
        raise TokenError(msg)
    expires = now + lifetime_seconds
    return f"{PREFIX}.{name}.{expires}.{_signature(secret, name, expires)}"


def verify(secret: str, token: str, *, now: float) -> str:
    """The approver a token names, if it is genuine and current.

    Raises:
        TokenError: If it is malformed, forged, signed with another secret, or expired.
            One message for all four, so a failure teaches a forger nothing.
    """
    refused = TokenError("the approver token is not valid")
    parts = token.strip().split(".")
    if len(parts) != 4 or parts[0] != PREFIX:  # noqa: PLR2004 -- the format's four fields
        raise refused
    _, name, expires_text, signature = parts
    if not NAME_PATTERN.match(name) or not (expires_text.isascii() and expires_text.isdigit()):
        raise refused
    expected = _signature(secret, name, int(expires_text))
    if not hmac.compare_digest(expected, signature):
        raise refused
    if int(expires_text) < now:
        raise refused
    return name
