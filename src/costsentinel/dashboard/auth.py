"""Who is deciding: the authenticator seam, and the interlock that guards it.

The dashboard can record an approval decision. Phase 11 made that safe on loopback,
which covers one operator on their own machine, and left it to the README to say "do not
expose this". Documentation is not a control, so this module makes the unsafe
configuration impossible instead (ARCHITECTURE.md D126).

The seam
--------

An :class:`Authenticator` turns a request, plus the name the approver typed, into an
:class:`ApproverIdentity`. The only implementation is :class:`NullAuthenticator`. It
verifies nothing, so it returns the typed name marked self-asserted, and the decision
record carries that (D127). The real authenticator is a Phase 10b item: SSO, or a signed
adapter callback. It implements this protocol, and nothing in the dashboard changes when
it lands.

The interlock
-------------

A decision surface with no authenticator is safe only if nobody else can reach it, so:

- **At start-up**, :func:`check_decision_surface` refuses to build the app when decide
  is on, the declared bind is not loopback and the authenticator does not verify. That
  is the same posture as the gate refusing to execute: the configuration fails instead
  of being served with a warning.
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
from typing import Protocol

from fastapi import Request
from pydantic import Field, model_validator

from costsentinel.config import Settings
from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import ApproverAssurance, check_assurance


class ApproverIdentity(Frozen):
    """Who an authenticator says is deciding, and how sure it is."""

    name: str = Field(min_length=1)
    assurance: ApproverAssurance
    verified_by: str | None = None

    @model_validator(mode="after")
    def _assurance_is_consistent(self) -> ApproverIdentity:
        check_assurance(self.assurance, self.verified_by)
        return self


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

    def identify(self, request: Request, asserted: str) -> ApproverIdentity:
        """The identity behind one request.

        Args:
            request: The incoming request. A real authenticator reads its credential
                from here.
            asserted: The name the approver typed. A verifying authenticator may
                compare against it, and must not simply trust it.
        """
        ...


class NullAuthenticator:
    """The default, which verifies nothing and says so.

    Permitted only on loopback. It does not refuse to name the approver; it labels the
    name for what it is. A typed name is still worth recording, and is worth exactly
    as much as the record says it is.
    """

    name = "none"
    verifies = False

    def identify(self, request: Request, asserted: str) -> ApproverIdentity:
        """The typed name, marked self-asserted."""
        _ = request
        return ApproverIdentity(name=asserted, assurance=ApproverAssurance.SELF_ASSERTED)


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
        UnsafeDecisionSurfaceError: When the dashboard is enabled with decide on, the
            declared bind is not loopback, and the authenticator does not verify. The
            message names the three settings and both safe ways out, so the operator who
            hits it knows what to change without reading this module.
    """
    if not (settings.dashboard_enabled and settings.dashboard_can_decide):
        return
    if authenticator.verifies or is_loopback(settings.bind_host):
        return
    msg = (
        f"refusing to start: DASHBOARD_CAN_DECIDE=true with BIND_HOST={settings.bind_host!r} "
        f"would let anybody who can reach this address record an approval under any name, "
        f"and no authenticator is configured to verify who they are. Either bind to "
        f"loopback (BIND_HOST=127.0.0.1) or serve the dashboard display-only "
        f"(DASHBOARD_CAN_DECIDE=false). A verified approver identity is a Phase 10b item; "
        f"until it exists, decisions off this machine go through "
        f"`costsentinel approvals decide`."
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
