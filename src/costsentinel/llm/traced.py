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
from costsentinel.domain.common import MoneyAmount
from costsentinel.domain.observability import (
    LLMCallMetrics,
    ModelTier,
    RoutingDecision,
    TokenPrice,
    TokenUsage,
)
from costsentinel.llm.base import LLM, LLMTask, Prompt, StructuredResponseT
from costsentinel.llm.routing import FAKE_MODEL, resolve_route, tier_for
from costsentinel.observability.logging import get_logger
from costsentinel.observability.tracing import Tracer

_log = get_logger("llm.traced")

#: What the fake backend costs per million tokens.
#:
#: Zero here is a measured fact rather than an unknown standing in as zero: the fake
#: makes no network call, so it genuinely costs nothing. The real backends are
#: deliberately absent from this table -- see :func:`price_for`.
FAKE_PRICE = TokenPrice(
    model=FAKE_MODEL,
    prompt_per_million=Decimal(0),
    completion_per_million=Decimal(0),
)

#: Fallback when a prompt or response carries no usage information at all.
_NO_USAGE = TokenUsage(prompt_tokens=0, completion_tokens=0)

#: Characters per token, for estimating usage from a backend that does not report it.
CHARS_PER_TOKEN = 4


def price_for(
    backend: LLMBackend, tier: ModelTier, settings: Settings, *, model: str = ""
) -> TokenPrice | None:
    """The token price for one backend and tier, or ``None`` if there is no rate.

    The fake backend is free, which is a fact. Every real backend's rate must come
    from configuration, and ``None`` means the run reports its model cost as
    unknown.

    There is deliberately no default rate for a real model. A per-run cost is a cost
    figure, and a plausible-looking published list price applied to whatever
    deployment an operator happened to name would be a fabricated number dressed as
    a calculated one -- exactly what CLAUDE.md golden rule 5 forbids. Unknown is the
    honest answer, and :attr:`RunMetrics.total_cost` already propagates it.
    """
    if backend is LLMBackend.FAKE:
        return FAKE_PRICE
    rates = settings.token_rates(tier.value)
    if rates is None:
        return None
    prompt_rate, completion_rate = rates
    return TokenPrice(
        model=model or f"{backend.value}-{tier.value}",
        prompt_per_million=prompt_rate,
        completion_per_million=completion_rate,
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
        price = price_for(
            LLMBackend(routing.backend),
            routing.tier,
            self._settings,
            model=routing.model,
        )
        cost = (
            price.cost_of(usage)
            if price is not None
            else MoneyAmount.undetermined(
                f"no token rate is configured for the {routing.tier.value} tier on "
                f"the {routing.backend} backend, so this call's cost is unknown. Set "
                f"LLM_PRICE_{routing.tier.value.upper()}_PROMPT and "
                f"LLM_PRICE_{routing.tier.value.upper()}_COMPLETION to price it."
            )
        )
        record = LLMCallMetrics(
            task=task.name,
            routing=routing,
            usage=usage,
            cost=cost,
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
