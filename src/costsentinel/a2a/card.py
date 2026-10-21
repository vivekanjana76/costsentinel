"""The Agent Card: what CostSentinel advertises to another agent, and nothing more.

One card, one capability set, all of it **read-only**. An external agent can ask for a
scan, fetch the findings, or fetch the pending-approval queue. It cannot approve
anything, it cannot execute anything, and the reason is not that those endpoints are
protected -- it is that they do not exist (ARCHITECTURE.md D118).

Why one card now rather than six later
--------------------------------------

The roadmap had per-specialist cards in Phase 11. The approval queue is the first thing
in this system another agent would have a reason to ask about, so the card came forward
with it (D117). Building it against this one capability means "is this surface
read-only?" is established once, while the answer is the same as the phase's own
invariant, rather than re-argued for each of six specialists later.

Why the skill vocabulary is closed
----------------------------------

:data:`READ_ONLY_SKILLS` is the whole advertised set and a test asserts that no skill
id, name, description or tag contains a mutating verb. That is a weaker-looking check
than enumerating the allowed list and a stronger one in practice: a skill added later
has to pass it, and the thing that would go wrong is somebody adding
``remediation.apply`` and nobody noticing the card now advertises a write.

Signing
-------

The card carries a **detached HMAC-SHA256** over its canonical JSON, not a JWS. A2A v1
describes JWS, and a real deployment should use one -- that needs an asymmetric key and
a JSON-canonicalisation library, neither of which belongs in a phase whose rule is that
CI installs no optional extra. What this provides is integrity against modification in
transit by anybody without the key, verifiable offline with the standard library, and it
is labelled as what it is rather than as what it resembles. JWS is a Phase 11 item.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import StrEnum

from pydantic import ConfigDict, Field

from costsentinel.domain.common import Frozen

#: The A2A protocol version this card declares.
PROTOCOL_VERSION = "1.0"

#: Named so a reader can tell at a glance that nothing here acts.
AGENT_NAME = "costsentinel-reporting"

#: The synthetic signing key used in tests and in mock mode. Committed deliberately
#: and useless deliberately: it signs nothing anybody should trust, and a real
#: deployment sets its own via configuration. A key that only exists in an operator's
#: environment cannot be asserted about in CI, and a card nobody verifies in CI is a
#: card whose signature nobody has ever checked.
SYNTHETIC_SIGNING_KEY = "costsentinel-synthetic-card-key-not-a-secret"


class SkillId(StrEnum):
    """Every skill this agent advertises. Closed, and read-only by construction.

    There is no member that changes anything, and that is the whole design rather than
    an omission to be filled in later. An external agent asks what spend looks like and
    what is waiting on a human; a human decides, through a terminal, and carries the
    action out by hand.
    """

    SCAN = "report.scan"
    FINDINGS = "report.findings"
    APPROVAL_QUEUE = "approvals.queue"


class AgentSkill(Frozen):
    """One advertised capability, in the shape A2A v1 expects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: SkillId
    name: str
    description: str
    tags: tuple[str, ...]
    examples: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("application/json",)
    output_modes: tuple[str, ...] = ("application/json",)


class AgentCapabilities(Frozen):
    """What the agent supports at the protocol level.

    Streaming and push notifications are both declared ``False`` rather than omitted:
    a consumer should be told what is unsupported instead of discovering it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = True


class AgentCard(Frozen):
    """The published description of this agent.

    Carries no credential, no endpoint secret and no capability that mutates. The
    ``signature`` is detached: it covers :meth:`canonical`, which excludes the
    signature itself, so a card can be signed, transmitted and verified without the
    verifier having to reconstruct what was signed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: str = PROTOCOL_VERSION
    name: str = AGENT_NAME
    description: str
    version: str
    url: str = Field(description="Where the A2A endpoint lives when the server is enabled.")
    capabilities: AgentCapabilities = AgentCapabilities()
    default_input_modes: tuple[str, ...] = ("application/json",)
    default_output_modes: tuple[str, ...] = ("application/json",)
    skills: tuple[AgentSkill, ...] = Field(min_length=1)
    read_only: bool = Field(
        default=True,
        description=(
            "Declared on the card so a consumer does not have to infer it from the "
            "skill list. Asserted by a test that walks every skill's id, name, "
            "description and tags for mutating verbs."
        ),
    )
    signature: str = Field(
        default="",
        description=(
            "Detached HMAC-SHA256 over `canonical()`, hex-encoded. Empty on an "
            "unsigned card. Not a JWS -- see the module docstring."
        ),
    )
    signature_algorithm: str = Field(
        default="HMAC-SHA256",
        description="Stated so a verifier never has to guess, and so a change is visible.",
    )

    def canonical(self) -> bytes:
        """The exact bytes a signature covers.

        Sorted keys, no insignificant whitespace, and the signature fields excluded.
        Canonicalisation is the part of signing that is easy to get subtly wrong, so it
        is one method with one caller on each side rather than a convention.
        """
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, key: str) -> AgentCard:
        """Return a copy carrying a signature over :meth:`canonical`."""
        digest = hmac.new(key.encode(), self.canonical(), hashlib.sha256).hexdigest()
        return self.model_copy(update={"signature": digest})

    def verify(self, key: str) -> bool:
        """Whether the signature matches, compared in constant time.

        ``hmac.compare_digest`` rather than ``==``: a card signature is a secret-keyed
        value, and comparing one with a short-circuiting equality is the textbook way
        to leak it a byte at a time.
        """
        if not self.signature:
            return False
        expected = hmac.new(key.encode(), self.canonical(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


#: The advertised skills. Read-only, and the docstrings say so in the text a consuming
#: agent actually reads rather than only in ours.
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


def build_card(*, url: str, version: str, key: str | None = None) -> AgentCard:
    """Build the card, signed when a key is given.

    Args:
        url: Where the A2A endpoint lives. The card is published whether or not the
            server is enabled, so a consumer can discover what is *available* rather
            than having to probe for it.
        version: The CostSentinel version, so a consumer can tell cards apart.
        key: The signing key. ``None`` produces an unsigned card, which
            :meth:`AgentCard.verify` then refuses -- an unsigned card is not a card
            that passes verification trivially.
    """
    card = AgentCard(
        description=(
            "Read-only FinOps reporting for multi-subscription Azure estates. "
            "Detects waste and spend anomalies, costs remediations from provider data, "
            "and reports what is awaiting a human decision. Every skill is read-only: "
            "this agent proposes and reports, and has no path to change a resource."
        ),
        version=version,
        url=url,
        skills=READ_ONLY_SKILLS,
    )
    return card.sign(key) if key else card
