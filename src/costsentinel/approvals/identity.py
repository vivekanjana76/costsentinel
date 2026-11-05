"""Who is deciding, and whether they may: one answer for every surface.

Phase 12 gave the dashboard an :class:`Authenticator` seam and a refusal, and recorded
that "who approved" should have one meaning across every surface that can decide,
rather than one per surface. This module is that meaning. The dashboard and the Slack
adapter both establish an :class:`ApproverIdentity` their own way: a signed token, or a
signed Slack callback. Both then ask the same question of the same function,
:func:`authorise` (ARCHITECTURE.md D132).

Two kinds of identity
---------------------

- **Self-asserted**: a name somebody typed. Nothing checked it. It is accepted only on a
  *local* surface: the CLI, or a dashboard request that arrived on a loopback socket.
  Being able to type on this machine is the only thing standing behind it, and that
  was already true before this module existed.
- **Verified**: an authenticator established it and names itself as the verifier. It
  carries a **principal**, the stable id the authenticator vouched for: ``slack:T…/U…``
  for a Slack user, or ``token:<name>`` for a signed dashboard token. Verification says
  who someone is. It is not a permission. A verified identity may decide only if the
  allowlist names its principal, or a group it belongs to.

Failing closed
--------------

With no allowlist configured, every verified identity is refused. An empty allowlist
meaning "anybody who can authenticate" would turn a Slack workspace's whole membership
into approvers the moment somebody pasted a signing secret. Nobody would have decided
that; it would simply have followed from a default.

An entry may be scoped to one client (``slack:T1/U1@northwind-energy``). A client's own
FinOps lead can then approve that client's items and nobody else's.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from costsentinel.config import Settings
from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import ApproverAssurance, check_assurance

#: A principal is a kind, a colon, and an id the authenticator vouched for. Narrow on
#: purpose: a principal is matched against an allowlist, and a value that can contain a
#: comma, a space or an ``@`` would be one that can be confused with an entry separator
#: or a client scope.
PRINCIPAL_PATTERN = re.compile(r"^(slack|slack-group|token):[A-Za-z0-9._/-]+$")


class Surface(StrEnum):
    """Where a decision arrived from. Recorded on every outcome and refusal."""

    CLI = "cli"
    DASHBOARD = "dashboard"
    SLACK = "slack"


class ApproverIdentity(Frozen):
    """Who an authenticator says is deciding, and how sure it is.

    A verified identity names its verifier *and* its principal, the id it vouched for.
    A self-asserted one can name neither, because nothing vouched for anything.
    """

    name: str = Field(min_length=1, description="What the record shows as the approver.")
    assurance: ApproverAssurance
    verified_by: str | None = None
    principal: str | None = Field(
        default=None, description="The id the authenticator vouched for, when one did."
    )
    groups: tuple[str, ...] = Field(
        default=(), description="Group principals this identity was verified to belong to."
    )

    @model_validator(mode="after")
    def _assurance_is_consistent(self) -> ApproverIdentity:
        check_assurance(self.assurance, self.verified_by)
        if self.assurance is ApproverAssurance.VERIFIED and self.principal is None:
            msg = "a verified identity must carry the principal its authenticator vouched for"
            raise ValueError(msg)
        if self.assurance is ApproverAssurance.SELF_ASSERTED and (self.principal or self.groups):
            msg = "a self-asserted identity was vouched for by nothing; it has no principal"
            raise ValueError(msg)
        for one in (self.principal, *self.groups):
            if one is not None and not PRINCIPAL_PATTERN.match(one):
                msg = f"{one!r} is not a principal"
                raise ValueError(msg)
        return self

    @classmethod
    def self_asserted(cls, name: str) -> ApproverIdentity:
        """A typed name, labelled for what it is."""
        return cls(name=name, assurance=ApproverAssurance.SELF_ASSERTED)


class AllowEntry(Frozen):
    """One line of the allowlist: a principal, optionally limited to one client."""

    principal: str
    client: str | None = None

    @field_validator("principal")
    @classmethod
    def _is_a_principal(cls, value: str) -> str:
        if not PRINCIPAL_PATTERN.match(value):
            msg = (
                f"{value!r} is not a principal; expected slack:<team>/<user>, "
                f"slack-group:<team>/<group> or token:<name>"
            )
            raise ValueError(msg)
        return value

    def covers(self, principals: set[str], client: str) -> bool:
        """Whether this entry admits one of these principals for this client."""
        return self.principal in principals and self.client in {None, client}


class AuthorisationPolicy(Frozen):
    """Who may approve, read from configuration. Empty by default, which admits nobody."""

    entries: tuple[AllowEntry, ...] = ()

    @property
    def configured(self) -> bool:
        """Whether anybody has been named at all."""
        return bool(self.entries)

    @classmethod
    def parse(cls, raw: str) -> AuthorisationPolicy:
        """Read ``APPROVER_ALLOWLIST``: comma-separated principals, each optionally ``@client``.

        Raises:
            ValueError: On an entry that is not a principal. A typo in an allowlist
                should stop the process, not silently admit nobody or, worse, the wrong
                somebody.
        """
        entries: list[AllowEntry] = []
        for chunk in raw.split(","):
            item = chunk.strip()
            if not item:
                continue
            principal, _, client = item.partition("@")
            entries.append(AllowEntry(principal=principal.strip(), client=client.strip() or None))
        return cls(entries=tuple(entries))

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthorisationPolicy:
        """The policy this deployment configured."""
        return cls.parse(settings.approver_allowlist)


class Authorisation(Frozen):
    """The answer to "may this identity decide this client's item?", with the reason."""

    allowed: bool
    reason: str = Field(min_length=1)
    matched: str | None = Field(default=None, description="The allowlist entry that admitted it.")


def authorise(
    identity: ApproverIdentity,
    *,
    client: str,
    local: bool,
    policy: AuthorisationPolicy,
) -> Authorisation:
    """The one authorisation check every decision surface calls.

    Args:
        identity: Who is deciding, as an authenticator established it.
        client: Whose item is being decided. An allowlist entry may be scoped to it.
        local: Whether the decision arrived on this machine: the CLI, or a loopback
            dashboard request. Only a local surface may accept a self-asserted name.
        policy: The configured allowlist.
    """
    if identity.assurance is ApproverAssurance.SELF_ASSERTED:
        if local:
            return Authorisation(
                allowed=True,
                reason="a self-asserted name on a local surface, recorded as unverified",
            )
        return Authorisation(
            allowed=False,
            reason=(
                "a self-asserted name is accepted only on this machine; a decision from "
                "elsewhere needs a verifying authenticator"
            ),
        )

    if not policy.configured:
        return Authorisation(
            allowed=False,
            reason=(
                "no approver allowlist is configured (APPROVER_ALLOWLIST), and a verified "
                "identity is not on its own a permission to approve"
            ),
        )

    principals = {one for one in (identity.principal, *identity.groups) if one}
    for entry in policy.entries:
        if entry.covers(principals, client):
            scope = f"@{entry.client}" if entry.client else ""
            return Authorisation(
                allowed=True,
                reason="on the approver allowlist",
                matched=f"{entry.principal}{scope}",
            )
    return Authorisation(
        allowed=False,
        reason=(
            f"{identity.principal} is not on the approver allowlist for {client!r}, "
            f"directly or through a group"
        ),
    )
