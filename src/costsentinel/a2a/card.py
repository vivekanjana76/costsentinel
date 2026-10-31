"""The Agent Card: what CostSentinel advertises to another agent, and nothing more.

One card, one capability set, all of it **read-only**. An external agent can ask for a
scan, fetch the findings, or fetch the pending-approval queue. It cannot approve
anything and it cannot execute anything. That is not because those endpoints are
protected. It is because they do not exist (ARCHITECTURE.md D118).

The wire shape is A2A v1's
--------------------------

Serialised as ``a2a.proto`` defines ``AgentCard`` in its JSON form: camelCase,
``supportedInterfaces`` instead of a top-level URL and protocol version, and
``signatures`` as a list of ``AgentCardSignature`` (D128). That is not cosmetic. A
signature only verifies if the verifier canonicalises the same bytes the signer did,
and a verifier written against the spec canonicalises the spec's shape.

Two places where the card says less than it might, both deliberate:

- **Read-only is declared as a capability extension**, ``urn:costsentinel:a2a:read-only``,
  not as a top-level field. A strict proto parser rejects unknown fields, and an
  extension is the spec's own place for a claim the core schema has no word for.
- **The interface does not claim an official binding.** The routes are a task API
  shaped like A2A's, not the HTTP+JSON binding (``SendMessage`` with ``Message`` and
  ``Part``). Declaring ``HTTP+JSON`` would invite a conformant client to send requests
  this server does not understand. So the binding is named for what it is, and a
  strict xfail encodes the target (Phase 12b, #113).

Field presence
--------------

Spec section 8.4.1 says a field with a default value is omitted unless it is
``REQUIRED`` or proto ``optional``. :func:`present` implements that for this card by
dropping ``None`` and empty strings, lists and objects. That is correct here because of
two facts, and both are enforced rather than assumed. Every ``REQUIRED`` field on the
card is a non-empty string or list, enforced with ``min_length``. And every boolean on
the card is a proto ``optional`` one (``streaming``, ``pushNotifications``), so an
explicit ``false`` is kept.

Signing
-------

See :mod:`costsentinel.a2a.signing`. A JWS per spec section 8.4, ES256 over the RFC 8785
canonical form, verified in CI by an independent JOSE implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

import rfc8785
from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from costsentinel.domain.common import Frozen

#: The A2A protocol version this card's interface declares.
PROTOCOL_VERSION = "1.0"

#: Named for what the routes are. Not `HTTP+JSON`: see the module docstring (D128).
PROTOCOL_BINDING = "COSTSENTINEL-TASKS"

#: The bindings A2A v1 names as core. The card must not claim one until it speaks it.
OFFICIAL_BINDINGS: frozenset[str] = frozenset({"JSONRPC", "GRPC", "HTTP+JSON"})

#: Named so a reader can tell at a glance that nothing here acts.
AGENT_NAME = "costsentinel-reporting"

#: The capability extension that says this agent only reads.
READ_ONLY_EXTENSION_URI = "urn:costsentinel:a2a:read-only"

#: A JSON value, as the card is served and canonicalised.
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


class _Wire(Frozen):
    """Base for the card's parts. camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )


class SkillId(StrEnum):
    """Every skill this agent advertises. Closed, and read-only by construction.

    No member changes anything, and that is the design, not an omission waiting to be
    filled in. An external agent asks what spend looks like and what is waiting on a
    human. A human decides, through a terminal, and carries the action out by hand.
    """

    SCAN = "report.scan"
    FINDINGS = "report.findings"
    APPROVAL_QUEUE = "approvals.queue"


class AgentSkill(_Wire):
    """One advertised capability, as ``a2a.proto`` defines ``AgentSkill``."""

    id: SkillId
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(min_length=1)
    examples: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("application/json",)
    output_modes: tuple[str, ...] = ("application/json",)


class AgentExtension(_Wire):
    """A capability extension. ``required`` is left at its default and so omitted."""

    uri: str = Field(min_length=1)
    description: str = ""


class AgentCapabilities(_Wire):
    """What the agent supports at the protocol level.

    Streaming and push notifications are set to ``False`` explicitly rather than
    omitted. Both are proto ``optional``, so the explicit value survives
    canonicalisation and a consumer is told instead of left to discover it.
    """

    streaming: bool = False
    push_notifications: bool = False
    extensions: tuple[AgentExtension, ...] = ()


class AgentInterface(_Wire):
    """Where the agent can be reached, and how."""

    url: str = Field(min_length=1)
    protocol_binding: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)


class AgentCardSignature(_Wire):
    """One JWS over the card, as spec section 8.4.2 lays it out.

    ``protected`` is the base64url protected header and ``signature`` the base64url
    signature. The payload is detached: it is the card's own canonical form, which a
    verifier recomputes from the card it received.
    """

    protected: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class AgentCard(_Wire):
    """The published description of this agent.

    Carries no credential, no endpoint secret and no capability that mutates.
    """

    name: str = Field(default=AGENT_NAME, min_length=1)
    description: str = Field(min_length=1)
    supported_interfaces: tuple[AgentInterface, ...] = Field(min_length=1)
    version: str = Field(min_length=1)
    capabilities: AgentCapabilities = AgentCapabilities()
    default_input_modes: tuple[str, ...] = Field(default=("application/json",), min_length=1)
    default_output_modes: tuple[str, ...] = Field(default=("application/json",), min_length=1)
    skills: tuple[AgentSkill, ...] = Field(min_length=1)
    signatures: tuple[AgentCardSignature, ...] = ()

    @property
    def read_only(self) -> bool:
        """Whether the card declares the read-only extension."""
        return any(ext.uri == READ_ONLY_EXTENSION_URI for ext in self.capabilities.extensions)

    def wire(self) -> dict[str, Json]:
        """The card exactly as it is served: camelCase, with absent fields omitted."""
        return present_object(self.model_dump(mode="json", by_alias=True))

    def canonical(self) -> bytes:
        """The bytes a JWS covers: the served card without ``signatures``, per RFC 8785.

        One method, with one caller on each side. Canonicalisation is the part of
        signing that is easy to get subtly wrong, so it is not left to a convention.
        """
        return canonical_payload(self.wire())


def present(value: Json) -> Json:
    """Drop absent values, recursively. Field presence per spec 8.4.1, for this card.

    ``None``, ``""``, ``[]`` and ``{}`` are dropped. ``False`` and ``0`` are kept: on
    this card every boolean is proto ``optional``, so an explicit one is meaningful.
    """
    if isinstance(value, dict):
        return present_object(value)
    if isinstance(value, list):
        items = [present(item) for item in value]
        return [item for item in items if not _absent(item)]
    return value


def present_object(value: Mapping[str, Json]) -> dict[str, Json]:
    """:func:`present` for an object, typed as one."""
    kept = {key: present(item) for key, item in value.items()}
    return {key: item for key, item in kept.items() if not _absent(item)}


def _absent(value: Json) -> bool:
    return value is None or (isinstance(value, str | list | dict) and len(value) == 0)


def canonical_payload(card: Mapping[str, Json]) -> bytes:
    """RFC 8785 over a served card, with ``signatures`` excluded.

    Takes the *served* mapping rather than a model, so a verifier can apply it to
    exactly the JSON it received. It does not re-serialise a model it parsed, which
    would quietly canonicalise our idea of the card instead of the one on the wire.
    """
    unsigned = present_object({key: item for key, item in card.items() if key != "signatures"})
    return rfc8785.dumps(unsigned)


#: The advertised skills. Read-only, and the descriptions say so in the text a
#: consuming agent actually reads, not only in our docstrings.
READ_ONLY_SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        id=SkillId.SCAN,
        name="Request a cost review",
        description=(
            "Run a read-only sweep of a client's estate and return the report: "
            "findings, ranked recommendations, projected savings, spend changes and "
            "what is awaiting a human decision. Reads only. Proposes; never applies."
        ),
        tags=("finops", "read-only", "report", "analysis"),
        examples=(
            "Produce a cost review for northwind-energy.",
            "What waste is there in the ministry-of-transport estate?",
        ),
    ),
    AgentSkill(
        id=SkillId.FINDINGS,
        name="Fetch the current findings",
        description=(
            "Return the findings from the most recent sweep for a client, without "
            "running a new one. Reads a stored result; touches no cloud."
        ),
        tags=("finops", "read-only", "report"),
        examples=("What did the last review of northwind-energy find?",),
    ),
    AgentSkill(
        id=SkillId.APPROVAL_QUEUE,
        name="Fetch the pending-approval queue",
        description=(
            "Return the gated actions awaiting a human decision, with the figures the "
            "report showed. Read-only: this surface can report what is waiting and "
            "cannot decide it. A decision is recorded by a person, and an approval "
            "hands the action to a person -- CostSentinel has no execution path."
        ),
        tags=("finops", "read-only", "governance", "approvals"),
        examples=("What is waiting on an approval for the ministry estate?",),
    ),
)

#: The declaration a consumer reads, worded as a denial so it passes the guard honestly.
READ_ONLY_EXTENSION = AgentExtension(
    uri=READ_ONLY_EXTENSION_URI,
    description=(
        "Every skill on this card only reads. A task can be cancelled before it runs; "
        "nothing on this surface can change a resource or record a decision."
    ),
)


def build_card(*, url: str, version: str) -> AgentCard:
    """Build the unsigned card. :mod:`costsentinel.a2a.signing` adds signatures.

    Args:
        url: Where the A2A endpoint lives. The card is published so a consumer can
            discover what is *available* without having to probe for it.
        version: The CostSentinel version, so a consumer can tell cards apart.
    """
    return AgentCard(
        description=(
            "Read-only FinOps reporting for multi-subscription Azure estates. "
            "Detects waste and spend anomalies, costs remediations from provider data, "
            "and reports what is awaiting a human decision. Every skill is read-only: "
            "this agent proposes and reports, and has no path to change a resource."
        ),
        supported_interfaces=(
            AgentInterface(
                url=url, protocol_binding=PROTOCOL_BINDING, protocol_version=PROTOCOL_VERSION
            ),
        ),
        version=version,
        capabilities=AgentCapabilities(extensions=(READ_ONLY_EXTENSION,)),
        skills=READ_ONLY_SKILLS,
    )
