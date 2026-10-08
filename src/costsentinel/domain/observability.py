"""Run metrics and routing decisions.

CLAUDE.md section 10 requires token, cost and latency to be recorded per run. These
are the types that carry it, and they are domain types rather than logging details
because a per-run cost is itself a cost figure: it goes through
:class:`~costsentinel.domain.common.MoneyAmount` and therefore cannot be
model-derived either.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import (
    Currency,
    Frozen,
    MoneyAmount,
    Provenance,
    utc_now,
)

_CENTS = Decimal("0.01")

#: Token prices are quoted per million tokens, so costs are divided by this.
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)


class ModelTier(StrEnum):
    """How much model a task is worth.

    Light, high-volume work routes to ``SMALL``; synthesis and judging to ``LARGE``.
    Keeping this an enum rather than a free string means a routing table typo is a
    static error.
    """

    SMALL = "small"
    LARGE = "large"


class TokenUsage(Frozen):
    """Tokens consumed by one model call."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion."""
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Combine two usages, so a run total is a plain sum."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


class TokenPrice(Frozen):
    """What one model costs per million prompt and completion tokens."""

    model: str
    prompt_per_million: Decimal = Field(ge=Decimal(0))
    completion_per_million: Decimal = Field(ge=Decimal(0))
    currency: Currency = Currency.USD

    def cost_of(self, usage: TokenUsage) -> MoneyAmount:
        """The cost of one call, computed arithmetically.

        Zero is a legitimate answer here, unlike elsewhere in the domain: the fake
        backend genuinely costs nothing, which is a measured fact rather than an
        unknown standing in as zero.
        """
        amount = (
            Decimal(usage.prompt_tokens) * self.prompt_per_million
            + Decimal(usage.completion_tokens) * self.completion_per_million
        ) / TOKENS_PER_PRICE_UNIT
        return MoneyAmount.of(
            amount.quantize(_CENTS, rounding=ROUND_HALF_UP),
            currency=self.currency,
            provenance=Provenance.calculated(
                f"{usage.prompt_tokens} prompt + {usage.completion_tokens} completion "
                f"tokens at {self.model} rates"
            ),
        )


class RoutingDecision(Frozen):
    """Which model a task was routed to, and why.

    Recorded per call so the choice is auditable: "why did a report cost that much"
    is answerable by reading the trace rather than by reasoning about the code.
    """

    task: str
    tier: ModelTier
    backend: str
    model: str
    reason: str = ""


class LLMCallMetrics(Frozen):
    """What one model call cost, in tokens, money and time."""

    task: str
    routing: RoutingDecision
    usage: TokenUsage
    cost: MoneyAmount
    latency_ms: int = Field(ge=0)
    schema_name: str
    started_at: datetime = Field(default_factory=utc_now)
    succeeded: bool = True


class NodeMetrics(Frozen):
    """How long one graph node took."""

    node: str
    latency_ms: int = Field(ge=0)


class RunMetrics(Frozen):
    """Token, cost and latency for one graph run.

    Totals are summed from the per-call records, never reported independently, so
    the headline and the detail cannot disagree.
    """

    run_id: str
    client: str
    calls: tuple[LLMCallMetrics, ...] = ()
    nodes: tuple[NodeMetrics, ...] = ()
    started_at: datetime = Field(default_factory=utc_now)
    total_latency_ms: int = Field(default=0, ge=0)

    @property
    def call_count(self) -> int:
        """Number of model calls in the run."""
        return len(self.calls)

    @property
    def total_usage(self) -> TokenUsage:
        """Tokens across every call."""
        total = TokenUsage(prompt_tokens=0, completion_tokens=0)
        for call in self.calls:
            total = total + call.usage
        return total

    @property
    def total_cost(self) -> MoneyAmount:
        """Money across every call.

        Unknown when a call's cost could not be determined, rather than silently
        dropping that call from the total.
        """
        if not self.calls:
            return MoneyAmount.of(
                Decimal(0), provenance=Provenance.calculated("no model calls in this run")
            )
        if any(call.cost.amount is None for call in self.calls):
            return MoneyAmount.undetermined(
                "at least one model call has an unknown cost, so the run total is unknown"
            )
        currency = self.calls[0].cost.currency
        total = sum(
            (call.cost.amount for call in self.calls if call.cost.amount is not None),
            start=Decimal(0),
        )
        return MoneyAmount.of(
            total.quantize(_CENTS, rounding=ROUND_HALF_UP),
            currency=currency,
            provenance=Provenance.calculated(f"sum of {len(self.calls)} model call(s)"),
        )

    @property
    def model_latency_ms(self) -> int:
        """Time spent inside model calls."""
        return sum(call.latency_ms for call in self.calls)

    def routing_choices(self) -> tuple[RoutingDecision, ...]:
        """Every routing decision made in the run, in call order."""
        return tuple(call.routing for call in self.calls)

    def tier_counts(self) -> dict[str, int]:
        """How many calls went to each tier, for a quick read of routing behaviour."""
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.routing.tier.value] = counts.get(call.routing.tier.value, 0) + 1
        return counts
