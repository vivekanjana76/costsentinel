"""``TracedLLM``: the seam where routing becomes observable.

Routing by itself is just a lookup. What makes it useful operationally is being able
to answer "which model handled this, and what did it cost" after the fact -- so the
routing decision, the token usage, the latency and the computed cost are recorded
here, per call, rather than left implicit in the configuration.

The wrapper satisfies :class:`~costsentinel.llm.base.LLM` itself, so it is
transparent: nodes take an ``LLM`` and cannot tell whether they were handed a
backend or a wrapped one. The fake backend ignores which model it was routed to, as
it should -- but the decision is still recorded, which is what issue #10 asks for.
"""

from __future__ import annotations

import time
from decimal import Decimal

from pydantic import BaseModel

from costsentinel.config import LLMBackend, Settings
from costsentinel.domain.observability import (
    LLMCallMetrics,
    ModelTier,
    RoutingDecision,
    TokenPrice,
    TokenUsage,
)
from costsentinel.llm.base import LLM, LLMTask, Prompt, StructuredResponseT
from costsentinel.llm.routing import resolve_route, tier_for
from costsentinel.observability.logging import get_logger
from costsentinel.observability.tracing import Tracer

_log = get_logger("llm.traced")

#: Token prices per million tokens, by backend and tier.
#:
#: The fake backend genuinely costs nothing, so zero here is a measured fact rather
#: than an unknown standing in as zero. The real rates are placeholders until the
#: Phase 3 adapters land with their actual deployments; they are committed data so a
#: run's cost is reproducible, and they are the thing to update when a rate changes.
TOKEN_PRICES: dict[tuple[LLMBackend, ModelTier], TokenPrice] = {
    (LLMBackend.FAKE, ModelTier.SMALL): TokenPrice(
        model="fake-deterministic",
        prompt_per_million=Decimal(0),
        completion_per_million=Decimal(0),
    ),
    (LLMBackend.FAKE, ModelTier.LARGE): TokenPrice(
        model="fake-deterministic",
        prompt_per_million=Decimal(0),
        completion_per_million=Decimal(0),
    ),
    (LLMBackend.AZURE_OPENAI, ModelTier.SMALL): TokenPrice(
        model="configured-small",
        prompt_per_million=Decimal("0.15"),
        completion_per_million=Decimal("0.60"),
    ),
    (LLMBackend.AZURE_OPENAI, ModelTier.LARGE): TokenPrice(
        model="configured-large",
        prompt_per_million=Decimal("2.50"),
        completion_per_million=Decimal("10.00"),
    ),
    (LLMBackend.GEMINI, ModelTier.SMALL): TokenPrice(
        model="configured-small",
        prompt_per_million=Decimal("0.10"),
        completion_per_million=Decimal("0.40"),
    ),
    (LLMBackend.GEMINI, ModelTier.LARGE): TokenPrice(
        model="configured-large",
        prompt_per_million=Decimal("1.25"),
        completion_per_million=Decimal("5.00"),
    ),
}

#: Fallback when a prompt or response carries no usage information at all.
_NO_USAGE = TokenUsage(prompt_tokens=0, completion_tokens=0)

#: Characters per token, for estimating usage from a backend that does not report it.
CHARS_PER_TOKEN = 4


def price_for(backend: LLMBackend, tier: ModelTier) -> TokenPrice:
    """The token price for one backend and tier.

    An unlisted combination prices at zero rather than raising, and says so in the
    model name -- a missing rate should not take down a sweep, and a zero that is
    labelled ``unpriced`` cannot be mistaken for a real figure.
    """
    price = TOKEN_PRICES.get((backend, tier))
    if price is not None:
        return price
    return TokenPrice(
        model=f"unpriced-{backend.value}-{tier.value}",
        prompt_per_million=Decimal(0),
        completion_per_million=Decimal(0),
    )


class TracedLLM:
    """Wraps any :class:`LLM`, recording routing, usage, cost and latency per call."""

    def __init__(
        self,
        inner: LLM,
        *,
        tracer: Tracer,
        settings: Settings,
    ) -> None:
        """Wrap a backend.

        Args:
            inner: The backend that actually answers.
            tracer: Where call records go. A no-op tracer is fine.
            settings: Resolved configuration, for the routing table and prices.
        """
        self._inner = inner
        self._tracer = tracer
        self._settings = settings
        self.metrics: list[LLMCallMetrics] = []

    @property
    def name(self) -> str:
        """The wrapped backend's identifier, so wrapping stays invisible."""
        return self._inner.name

    @property
    def inner(self) -> LLM:
        """The wrapped backend."""
        return self._inner

    def routing_for(self, task: LLMTask) -> RoutingDecision:
        """The routing decision for one task, without making a call."""
        route = resolve_route(task, self._settings)
        tier = ModelTier(tier_for(task))
        return RoutingDecision(
            task=task.name,
            tier=tier,
            backend=route.backend.value,
            model=route.model,
            reason=(
                f"task {task.name!r} is classified {tier.value}, so it routes to the "
                f"{tier.value} model on the {route.backend.value} backend"
            ),
        )

    def _usage(self, prompt: Prompt, response: BaseModel) -> TokenUsage:
        """Usage reported by the backend, or estimated from sizes if it reports none.

        Asking the backend first matters: a real backend knows its own tokenisation,
        and an estimate would quietly misreport cost. The estimate is the fallback,
        and it is reproducible so that metrics stay assertable.
        """
        reporter = getattr(self._inner, "last_usage", None)
        if callable(reporter):
            reported = reporter()
            if isinstance(reported, TokenUsage):
                return reported
        try:
            rendered = prompt.render()
            dumped = response.model_dump_json()
        except Exception:
            return _NO_USAGE
        return TokenUsage(
            prompt_tokens=len(rendered) // CHARS_PER_TOKEN,
            completion_tokens=len(dumped) // CHARS_PER_TOKEN,
        )

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        """Delegate to the backend, recording what it cost.

        A failure is recorded with ``succeeded=False`` and then re-raised: the
        metrics show the attempt, and the caller still sees the error.
        """
        routing = self.routing_for(task)
        started = time.perf_counter()
        try:
            response = self._inner.structured(task=task, prompt=prompt, schema=schema)
        except Exception as exc:
            self._record(
                task=task,
                routing=routing,
                usage=_NO_USAGE,
                latency_ms=int((time.perf_counter() - started) * 1000),
                schema_name=schema.__name__,
                succeeded=False,
            )
            _log.warning(
                "llm call failed",
                extra={
                    "task": task.name,
                    "model": routing.model,
                    "tier": routing.tier.value,
                    "error": str(exc),
                },
            )
            raise

        self._record(
            task=task,
            routing=routing,
            usage=self._usage(prompt, response),
            latency_ms=int((time.perf_counter() - started) * 1000),
            schema_name=schema.__name__,
            succeeded=True,
        )
        return response

    def _record(
        self,
        *,
        task: LLMTask,
        routing: RoutingDecision,
        usage: TokenUsage,
        latency_ms: int,
        schema_name: str,
        succeeded: bool,
    ) -> None:
        price = price_for(LLMBackend(routing.backend), routing.tier)
        record = LLMCallMetrics(
            task=task.name,
            routing=routing,
            usage=usage,
            cost=price.cost_of(usage),
            latency_ms=latency_ms,
            schema_name=schema_name,
            succeeded=succeeded,
        )
        self.metrics.append(record)
        self._tracer.record_llm_call(record)

    def reset(self) -> None:
        """Clear recorded metrics, so one wrapper can serve several runs."""
        self.metrics.clear()

    def collected(self) -> tuple[LLMCallMetrics, ...]:
        """Every call recorded since the last reset."""
        return tuple(self.metrics)
