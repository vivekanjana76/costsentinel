"""Who is deciding: the authenticator seam, and the interlock that guards it.

The dashboard can record an approval decision. Phase 11 made that safe on loopback,
which covers one operator on their own machine, and left it to the README to say "do not
expose this". Documentation is not a control, so this module makes the unsafe
configuration impossible instead (ARCHITECTURE.md D126).

The seam
--------

An :class:`Authenticator` turns a request, plus the name the approver typed and any
credential they presented, into an :class:`ApproverIdentity`. Two implementations:

- :class:`NullAuthenticator`, the default. It verifies nothing, so it returns the typed
  name marked self-asserted, and the decision record carries that (D127).
- :class:`TokenAuthenticator` (Phase 10b, D136), chosen when ``APPROVER_TOKEN_SECRET``
  is set. It verifies a signed, expiring approver token and returns ``token:<name>``,
  marked verified. Whether that name may decide is the allowlist's question, asked by
  the same :func:`~costsentinel.approvals.identity.authorise` the Slack webhook uses.

A credential that does not verify raises :class:`AuthenticationError`, and the request
is refused having recorded nothing: there is no verified author to record a refusal
against, the same rule as an unsigned Slack request (D134).

The interlock
-------------

A decision surface with no authenticator is safe only if nobody else can reach it, so:

- **At start-up**, :func:`check_decision_surface` refuses to build the app when decide
  is on and the declared bind is not loopback, unless the authenticator verifies *and*
  an allowlist is configured. Never lifted by default. That is the same posture as the
  gate refusing to execute: the configuration fails instead of being served with a
  warning.
- **Per request**, :func:`may_decide` covers the gap a declared bind cannot. Somebody
  can run ``uvicorn --host 0.0.0.0`` without setting ``BIND_HOST``, and the app never
  sees the flag. So a decision is accepted only when it arrived on a loopback socket, or
  when the authenticator verifies. The page renders no decision form otherwise.

One limit, recorded rather than hidden: a reverse proxy *on the same machine* forwards
over loopback, and the process cannot tell that from a local operator. Fronting a
decision surface with a proxy is an operator choice outside what this process can
observe, and SECURITY.md says so.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable
from typing import Protocol

from fastapi import Request

from costsentinel.approvals import token
from costsentinel.approvals.identity import ApproverIdentity, AuthorisationPolicy
from costsentinel.config import Settings
from costsentinel.domain.governance import ApproverAssurance

__all__ = [
    "ApproverIdentity",
    "AuthenticationError",
    "Authenticator",
    "NullAuthenticator",
    "TokenAuthenticator",
    "UnsafeDecisionSurfaceError",
    "arrived_on_loopback",
    "authenticator_from_settings",
    "check_decision_surface",
    "is_loopback",
    "may_decide",
]


class Authenticator(Protocol):
    """Establishes who is making a dashboard decision.

    ``verifies`` is what the interlock reads. An authenticator that cannot vouch for
    an identity must say so, because the loopback restriction is lifted on that word
    alone.
    """

    @property
    def name(self) -> str:
        """Recorded as ``verified_by`` on every identity this establishes."""
        ...

    @property
    def verifies(self) -> bool:
        """Whether an identity from here is anything more than the typed name."""
        ...

    def identify(self, request: Request, asserted: str, credential: str = "") -> ApproverIdentity:
        """The identity behind one request.

        Args:
            request: The incoming request. An authenticator may read a header from it.
            asserted: The name the approver typed. A verifying authenticator may
                compare against it, and must not simply trust it.
            credential: What the approver presented in the form, if anything.

        Raises:
            AuthenticationError: If a verifying authenticator cannot verify anyone.
        """
        ...


class AuthenticationError(ValueError):
    """A presented credential did not verify. Nothing is recorded for the request."""


class NullAuthenticator:
    """The default, which verifies nothing and says so.

    Permitted only on loopback. It does not refuse to name the approver; it labels the
    name for what it is. A typed name is still worth recording, and is worth exactly
    as much as the record says it is.
    """

    name = "none"
    verifies = False

    def identify(self, request: Request, asserted: str, credential: str = "") -> ApproverIdentity:
        """The typed name, marked self-asserted. Any credential is ignored."""
        _ = request, credential
        return ApproverIdentity.self_asserted(asserted)


#: Recorded as ``verified_by`` on a token-verified identity.
TOKEN_VERIFIER = "approver-token"  # noqa: S105 -- a label, not a credential


class TokenAuthenticator:
    """Verifies a signed, expiring approver token (:mod:`costsentinel.approvals.token`).

    The token comes from ``Authorization: Bearer <token>`` or, from the page's form,
    the password field. The typed name is not used: the token names the approver.
    """

    name = TOKEN_VERIFIER
    verifies = True

    def __init__(self, secret: str, clock: Callable[[], float] = time.time) -> None:
        """Hold the key, refusing a weak one so the server does not start with it."""
        token.check_secret(secret)
        self._secret = secret
        self._clock = clock

    def identify(self, request: Request, asserted: str, credential: str = "") -> ApproverIdentity:
        """The approver the token names, verified.

        Raises:
            AuthenticationError: If no token was presented, or it does not verify.
        """
        _ = asserted
        header = request.headers.get("authorization", "")
        presented = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        presented = presented or credential.strip()
        if not presented:
            msg = "an approver token is required to decide on this deployment"
            raise AuthenticationError(msg)
        try:
            approver = token.verify(self._secret, presented, now=self._clock())
        except token.TokenError as exc:
            raise AuthenticationError(str(exc)) from exc
        return ApproverIdentity(
            name=approver,
            assurance=ApproverAssurance.VERIFIED,
            verified_by=TOKEN_VERIFIER,
            principal=f"token:{approver}",
        )


def authenticator_from_settings(settings: Settings) -> Authenticator:
    """The token authenticator when a secret is configured; otherwise the null one.

    Raises:
        TokenError: If the configured secret is too short to sign with.
    """
    if settings.approver_token_secret is None:
        return NullAuthenticator()
    return TokenAuthenticator(settings.approver_token_secret.get_secret_value())


class UnsafeDecisionSurfaceError(RuntimeError):
    """The configuration would serve an unauthenticated decision surface off loopback."""


def is_loopback(host: str | None) -> bool:
    """Whether an address names this machine only.

    Fails closed. An empty host, an unparseable one, a hostname other than
    ``localhost`` and the wildcard ``0.0.0.0`` all count as reachable from elsewhere.
    A hostname could resolve anywhere, and the wildcard *is* elsewhere.
    """
    if not host:
        return False
    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def check_decision_surface(settings: Settings, authenticator: Authenticator) -> None:
    """Refuse to start an unauthenticated decision surface that others can reach.

    Raises:
        UnsafeDecisionSurfaceError: When the dashboard is enabled with decide on and the
            declared bind is not loopback, unless the authenticator verifies and an
            allowlist says who may decide. The message names the settings and the safe
            ways out, so the operator who hits it knows what to change without reading
            this module.
    """
    if not (settings.dashboard_enabled and settings.dashboard_can_decide):
        return
    if is_loopback(settings.bind_host):
        return
    configured = AuthorisationPolicy.from_settings(settings).configured
    if authenticator.verifies and configured:
        return
    if authenticator.verifies:
        msg = (
            f"refusing to start: DASHBOARD_CAN_DECIDE=true with BIND_HOST="
            f"{settings.bind_host!r} and a verifying authenticator, but APPROVER_ALLOWLIST "
            f"is empty, so nobody could decide. Name the approvers (e.g. token:alice), or "
            f"bind to loopback."
        )
        raise UnsafeDecisionSurfaceError(msg)
    msg = (
        f"refusing to start: DASHBOARD_CAN_DECIDE=true with BIND_HOST={settings.bind_host!r} "
        f"would let anybody who can reach this address record an approval under any name, "
        f"and no authenticator is configured to verify who they are. Either bind to "
        f"loopback (BIND_HOST=127.0.0.1) or serve the dashboard display-only "
        f"(DASHBOARD_CAN_DECIDE=false), or set APPROVER_TOKEN_SECRET and "
        f"APPROVER_ALLOWLIST so approvers present a signed token "
        f"(`costsentinel approvers token <name>`). Decisions can always be made on the "
        f"host with `costsentinel approvals decide`."
    )
    raise UnsafeDecisionSurfaceError(msg)


def arrived_on_loopback(request: Request) -> bool:
    """Whether a request reached a loopback socket.

    Reads ``scope["server"]``, the local address the connection was accepted on, and
    not the ``Host`` header, which the client writes. A process bound to every
    interface sees ``127.0.0.1`` here for a local operator and its LAN address for
    anybody else.
    """
    server = request.scope.get("server")
    if not server:
        return False
    return is_loopback(str(server[0]))


def may_decide(settings: Settings, authenticator: Authenticator, request: Request) -> bool:
    """Whether this request may record a decision, and see the form that does."""
    if not settings.dashboard_can_decide:
        return False
    return authenticator.verifies or arrived_on_loopback(request)
