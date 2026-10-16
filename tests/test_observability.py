"""Tracing, run metrics and the observable routing seam.

``langfuse`` is an optional extra and is deliberately not installed in CI, so the
Langfuse adapter is driven here with an injected test double. That keeps the adapter
path covered offline and keeps the "never a hard dependency" promise honest rather
than aspirational.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from costsentinel.config import LLMBackend, Settings
from costsentinel.domain.common import MoneyAmount
from costsentinel.domain.memory import MemoryAccess, MemoryOperation
from costsentinel.domain.observability import (
    LLMCallMetrics,
    ModelTier,
    NodeMetrics,
    RoutingDecision,
    RunMetrics,
    TokenPrice,
    TokenUsage,
)
from costsentinel.llm.base import LLMError, LLMTask, Prompt, StructuredResponseT
from costsentinel.llm.contracts import RootCauseAnalysis
from costsentinel.llm.fake import FakeLLM
from costsentinel.llm.routing import (
    TASK_PLANNING,
    TASK_REPORT_PROSE,
    TASK_ROOT_CAUSE,
)
from costsentinel.llm.traced import FAKE_PRICE, TracedLLM, price_for
from costsentinel.observability import tracing_status
from costsentinel.observability.tracing import (
    LangfuseSpan,
    LangfuseTracer,
    NoOpTracer,
    NullSpan,
    Tracer,
    get_tracer,
    timed,
)


def _configured(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "langfuse_host": "https://example.invalid",
            "langfuse_public_key": "pk",
            "langfuse_secret_key": SecretStr("sk"),
        }
    )


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


def test_token_usage_adds() -> None:
    left = TokenUsage(prompt_tokens=10, completion_tokens=5)
    right = TokenUsage(prompt_tokens=3, completion_tokens=2)
    combined = left + right
    assert combined.prompt_tokens == 13
    assert combined.total_tokens == 20


def test_cost_is_computed_per_million_tokens() -> None:
    price = TokenPrice(
        model="test", prompt_per_million=Decimal("2.00"), completion_per_million=Decimal("10.00")
    )
    cost = price.cost_of(TokenUsage(prompt_tokens=1_000_000, completion_tokens=100_000))
    assert cost.amount == Decimal("3.00")
    assert not cost.provenance.is_model_derived


def test_a_zero_cost_is_a_measured_fact_not_an_unknown() -> None:
    """The fake backend genuinely costs nothing, unlike a missing figure."""
    price = price_for(LLMBackend.FAKE, ModelTier.LARGE, Settings())
    assert price is not None
    cost = price.cost_of(TokenUsage(prompt_tokens=5000, completion_tokens=5000))
    assert cost.amount == Decimal(0)
    assert cost.is_known


def test_the_fake_backend_is_free_whatever_rates_are_configured() -> None:
    """A configured rate is about a real deployment; the fake makes no call at all."""
    priced = Settings(
        llm_price_small_prompt=Decimal("1.00"),
        llm_price_small_completion=Decimal("2.00"),
    )
    assert price_for(LLMBackend.FAKE, ModelTier.SMALL, priced) == FAKE_PRICE


@pytest.mark.parametrize("backend", [LLMBackend.AZURE_OPENAI, LLMBackend.GEMINI])
def test_a_real_backend_has_no_rate_until_one_is_configured(backend: LLMBackend) -> None:
    """There is deliberately no default published rate.

    A plausible list price applied to whatever deployment an operator happened to
    name would be a fabricated number dressed as a calculated one, which golden rule
    5 forbids. Unknown is the honest answer.
    """
    for tier in ModelTier:
        assert price_for(backend, tier, Settings()) is None


def test_a_configured_rate_is_used_and_names_the_deployment_it_prices() -> None:
    settings = Settings(
        llm_price_large_prompt=Decimal("2.50"),
        llm_price_large_completion=Decimal("10.00"),
    )
    price = price_for(LLMBackend.AZURE_OPENAI, ModelTier.LARGE, settings, model="my-gpt-deployment")
    assert price is not None
    assert price.model == "my-gpt-deployment"
    assert price.prompt_per_million == Decimal("2.50")


def test_half_a_rate_prices_nothing() -> None:
    """A missing half silently read as zero would understate a run's cost."""
    half = Settings(llm_price_small_prompt=Decimal("0.15"))
    assert price_for(LLMBackend.GEMINI, ModelTier.SMALL, half) is None


def test_an_unpriced_real_call_reports_its_cost_as_unknown_not_zero() -> None:
    routing = RoutingDecision(
        task="planning",
        tier=ModelTier.LARGE,
        backend="azure_openai",
        model="my-deployment",
        reason="test",
    )
    settings = Settings(llm_backend=LLMBackend.AZURE_OPENAI)
    price = price_for(LLMBackend.AZURE_OPENAI, ModelTier.LARGE, settings)
    assert price is None
    unknown = MoneyAmount.undetermined("no token rate is configured")
    run = RunMetrics(
        run_id="r1",
        client="Northwind",
        calls=(
            LLMCallMetrics(
                task="planning",
                routing=routing,
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
                cost=unknown,
                latency_ms=5,
                schema_name="Test",
            ),
        ),
    )
    assert run.total_cost.amount is None


# ---------------------------------------------------------------------------
# Run metrics
# ---------------------------------------------------------------------------


def _call(task: str, *, tier: ModelTier = ModelTier.SMALL, tokens: int = 100) -> LLMCallMetrics:
    usage = TokenUsage(prompt_tokens=tokens, completion_tokens=tokens // 2)
    routing = RoutingDecision(
        task=task, tier=tier, backend="fake", model="fake-deterministic", reason="test"
    )
    return LLMCallMetrics(
        task=task,
        routing=routing,
        usage=usage,
        cost=FAKE_PRICE.cost_of(usage),
        latency_ms=12,
        schema_name="Test",
    )


def test_run_totals_are_summed_from_the_calls() -> None:
    """The headline and the detail cannot disagree if one is derived from the other."""
    metrics = RunMetrics(
        run_id="run-1",
        client="acme",
        calls=(_call("root_cause"), _call("planning", tier=ModelTier.LARGE, tokens=200)),
        nodes=(NodeMetrics(node="scout", latency_ms=5),),
        total_latency_ms=99,
    )
    assert metrics.call_count == 2
    assert metrics.total_usage.prompt_tokens == 300
    assert metrics.total_usage.completion_tokens == 150
    assert metrics.total_usage.total_tokens == 450
    assert metrics.model_latency_ms == 24
    assert metrics.total_latency_ms == 99


def test_a_run_with_no_calls_costs_a_known_zero() -> None:
    metrics = RunMetrics(run_id="run-1", client="acme")
    assert metrics.total_cost.amount == Decimal(0)
    assert metrics.total_cost.is_known


def test_one_unknown_call_cost_makes_the_run_total_unknown() -> None:
    """Dropping the unknown call from the total would understate the run."""
    from costsentinel.domain.common import MoneyAmount

    unknown = _call("planning").model_copy(
        update={"cost": MoneyAmount.undetermined("rate not published")}
    )
    metrics = RunMetrics(run_id="run-1", client="acme", calls=(_call("root_cause"), unknown))
    assert not metrics.total_cost.is_known


def test_tier_counts_summarise_routing() -> None:
    metrics = RunMetrics(
        run_id="run-1",
        client="acme",
        calls=(
            _call("root_cause"),
            _call("planning", tier=ModelTier.LARGE),
            _call("report_prose", tier=ModelTier.LARGE),
        ),
    )
    assert metrics.tier_counts() == {"small": 1, "large": 2}
    assert [c.task for c in metrics.routing_choices()] == [
        "root_cause",
        "planning",
        "report_prose",
    ]


# ---------------------------------------------------------------------------
# The no-op tracer
# ---------------------------------------------------------------------------


def test_the_noop_tracer_accepts_everything_and_records_nothing() -> None:
    """The graph calls these unconditionally, so none may raise."""
    tracer = NoOpTracer()
    assert isinstance(tracer, Tracer)
    assert tracer.name == "noop"
    assert not tracer.enabled

    tracer.start_run(run_id="run-1", client="acme", metadata={"a": "b"})
    span = tracer.span("node", kind="node")
    span.set_attribute("k", "v")
    span.finish()
    span.finish(error="boom")
    tracer.record_llm_call(_call("root_cause"))
    tracer.record_memory_access(_access())
    tracer.end_run(run_id="run-1", metadata={})


def test_an_unconfigured_tracer_is_the_noop(settings: Settings) -> None:
    assert isinstance(get_tracer(settings), NoOpTracer)
    assert "disabled" in tracing_status(settings)


def test_a_configured_but_uninstalled_langfuse_degrades_to_the_noop(
    settings: Settings,
) -> None:
    """Never a hard dependency: an absent package must not break a sweep."""
    tracer = get_tracer(_configured(settings))
    # langfuse is not installed in CI, so the real client cannot be built.
    assert isinstance(tracer, NoOpTracer)


def test_tracing_status_reports_a_configured_langfuse(settings: Settings) -> None:
    assert "langfuse configured" in tracing_status(_configured(settings))


# ---------------------------------------------------------------------------
# The Langfuse adapter, driven by a double
# ---------------------------------------------------------------------------


class _FakeHandle:
    """Stands in for a Langfuse trace or span handle."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class _FakeLangfuse:
    """Records what the adapter sends, without a network or a package."""

    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []
        self.spans: list[dict[str, Any]] = []
        self.generations: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.flushes = 0
        self.handles: list[_FakeHandle] = []

    def trace(self, **kwargs: Any) -> _FakeHandle:
        self.traces.append(kwargs)
        handle = _FakeHandle()
        self.handles.append(handle)
        return handle

    def span(self, **kwargs: Any) -> _FakeHandle:
        self.spans.append(kwargs)
        handle = _FakeHandle()
        self.handles.append(handle)
        return handle

    def generation(self, **kwargs: Any) -> None:
        self.generations.append(kwargs)

    def event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def flush(self) -> None:
        self.flushes += 1


class _AngryLangfuse(_FakeLangfuse):
    """Fails on every call, the way a misconfigured backend would."""

    def trace(self, **kwargs: Any) -> _FakeHandle:
        msg = "trace failed"
        raise RuntimeError(msg)

    def span(self, **kwargs: Any) -> _FakeHandle:
        msg = "span failed"
        raise RuntimeError(msg)

    def generation(self, **kwargs: Any) -> None:
        msg = "generation failed"
        raise RuntimeError(msg)

    def event(self, **kwargs: Any) -> None:
        msg = "event failed"
        raise RuntimeError(msg)

    def flush(self) -> None:
        msg = "flush failed"
        raise RuntimeError(msg)


def test_the_adapter_opens_a_trace_per_run(settings: Settings) -> None:
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    assert tracer.enabled
    assert tracer.name == "langfuse"

    tracer.start_run(run_id="run-1", client="acme", metadata={"mode": "mock"})
    assert client.traces[0]["id"] == "run-1"
    assert client.traces[0]["user_id"] == "acme"
    assert client.traces[0]["metadata"]["mode"] == "mock"

    tracer.end_run(run_id="run-1", metadata={"llm_calls": "4"})
    assert client.flushes == 1


def test_the_adapter_records_routing_usage_and_cost(settings: Settings) -> None:
    """Which model handled a call, and what it cost, must be in the trace."""
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    tracer.record_llm_call(_call("planning", tier=ModelTier.LARGE, tokens=400))

    recorded = client.generations[0]
    assert recorded["name"] == "llm.planning"
    assert recorded["model"] == "fake-deterministic"
    assert recorded["usage"]["input"] == 400
    assert recorded["usage"]["total"] == 600
    assert recorded["metadata"]["tier"] == "large"
    assert recorded["metadata"]["routing_reason"]
    assert "cost" in recorded["metadata"]


def test_the_adapter_opens_and_closes_spans(settings: Settings) -> None:
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    span = tracer.span("anomaly_scout", kind="node")
    span.set_attribute("resources", "21")
    span.finish()

    assert client.spans[0]["name"] == "anomaly_scout"
    handle = client.handles[-1]
    assert handle.ended
    assert any("resources" in str(update) for update in handle.updates)


def test_a_failing_span_is_closed_with_its_error(settings: Settings) -> None:
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    span = tracer.span("broken")
    span.finish(error="it exploded")
    handle = client.handles[-1]
    assert any(update.get("level") == "ERROR" for update in handle.updates)
    assert handle.ended


def test_a_tracer_whose_backend_fails_never_breaks_a_run(settings: Settings) -> None:
    """Observability is an aid, not a liability."""
    tracer = LangfuseTracer(_configured(settings), client=_AngryLangfuse())
    tracer.start_run(run_id="run-1", client="acme", metadata={})
    span = tracer.span("node")
    span.set_attribute("k", "v")
    span.finish()
    tracer.record_llm_call(_call("root_cause"))
    tracer.end_run(run_id="run-1", metadata={})


def test_a_failing_span_handle_never_breaks_a_run() -> None:
    class _Broken:
        def update(self, **kwargs: Any) -> None:
            msg = "update failed"
            raise RuntimeError(msg)

        def end(self) -> None:
            msg = "end failed"
            raise RuntimeError(msg)

    span = LangfuseSpan(_Broken())
    span.set_attribute("k", "v")
    span.finish(error="boom")


def test_a_tracer_without_a_client_records_nothing(settings: Settings) -> None:
    """The disabled path must be exercised, not assumed."""
    tracer = LangfuseTracer(settings)  # tracing not configured, so no client
    assert not tracer.enabled
    tracer.start_run(run_id="run-1", client="acme", metadata={})
    assert isinstance(tracer.span("node"), NullSpan)
    tracer.record_llm_call(_call("root_cause"))
    tracer.end_run(run_id="run-1", metadata={})


# ---------------------------------------------------------------------------
# timed()
# ---------------------------------------------------------------------------


def test_timed_records_a_span_and_its_duration() -> None:
    tracer = NoOpTracer()
    with timed(tracer, "node") as timer:
        pass
    assert timer.elapsed_ms >= 0


def test_timed_closes_the_span_and_re_raises() -> None:
    """Tracing observes failures; it does not swallow them."""
    client = _FakeLangfuse()
    tracer = LangfuseTracer(
        Settings(langfuse_host="h", langfuse_public_key="p", langfuse_secret_key=SecretStr("s")),
        client=client,
    )
    with pytest.raises(RuntimeError, match="inner failure"), timed(tracer, "node"):
        msg = "inner failure"
        raise RuntimeError(msg)

    handle = client.handles[-1]
    assert handle.ended
    assert any(update.get("level") == "ERROR" for update in handle.updates)


# ---------------------------------------------------------------------------
# TracedLLM: the observable routing seam
# ---------------------------------------------------------------------------


def test_wrapping_is_transparent(settings: Settings) -> None:
    """A node takes an LLM and cannot tell it was handed a wrapper."""
    from costsentinel.llm.base import LLM

    inner = FakeLLM()
    traced = TracedLLM(inner, tracer=NoOpTracer(), settings=settings)
    assert isinstance(traced, LLM)
    assert traced.name == inner.name
    assert traced.inner is inner


def test_light_tasks_route_small_and_synthesis_routes_large(settings: Settings) -> None:
    traced = TracedLLM(FakeLLM(), tracer=NoOpTracer(), settings=settings)
    assert traced.routing_for(TASK_ROOT_CAUSE).tier is ModelTier.SMALL
    assert traced.routing_for(TASK_PLANNING).tier is ModelTier.LARGE
    assert traced.routing_for(TASK_REPORT_PROSE).tier is ModelTier.LARGE


def test_the_routing_decision_explains_itself(settings: Settings) -> None:
    traced = TracedLLM(FakeLLM(), tracer=NoOpTracer(), settings=settings)
    routing = traced.routing_for(TASK_ROOT_CAUSE)
    assert "small" in routing.reason
    assert routing.backend == "fake"
    assert routing.model == "fake-deterministic"


def _root_cause_prompt() -> Prompt:
    from costsentinel.llm.base import EvidenceBlock, EvidenceField
    from costsentinel.llm.fake import LABEL_WASTE_SIGNAL

    return Prompt(
        instruction="why",
        evidence=(
            EvidenceBlock(
                label=LABEL_WASTE_SIGNAL,
                fields=(
                    EvidenceField(name="signal_id", value="ws-1"),
                    EvidenceField(name="resource_name", value="disk-1"),
                    EvidenceField(name="observation.state", value="unattached"),
                ),
            ),
        ),
    )


def test_a_call_is_recorded_with_routing_usage_cost_and_latency(settings: Settings) -> None:
    traced = TracedLLM(FakeLLM(), tracer=NoOpTracer(), settings=settings)
    traced.structured(task=TASK_ROOT_CAUSE, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)

    assert len(traced.collected()) == 1
    record = traced.collected()[0]
    assert record.task == "root_cause"
    assert record.routing.tier is ModelTier.SMALL
    assert record.schema_name == "RootCauseAnalysis"
    assert record.usage.total_tokens > 0
    assert record.cost.is_known
    assert record.latency_ms >= 0
    assert record.succeeded


def test_the_backend_reported_usage_is_preferred_over_an_estimate(
    settings: Settings,
) -> None:
    """A real backend knows its own tokenisation; an estimate would misreport cost."""
    inner = FakeLLM()
    traced = TracedLLM(inner, tracer=NoOpTracer(), settings=settings)
    traced.structured(task=TASK_ROOT_CAUSE, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)
    assert traced.collected()[0].usage == inner.usages[0]


def test_usage_is_estimated_when_the_backend_reports_none(settings: Settings) -> None:
    class _Quiet(FakeLLM):
        def last_usage(self) -> TokenUsage | None:
            return None

    traced = TracedLLM(_Quiet(), tracer=NoOpTracer(), settings=settings)
    traced.structured(task=TASK_ROOT_CAUSE, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)
    assert traced.collected()[0].usage.total_tokens > 0


def test_a_failed_call_is_recorded_and_re_raised(settings: Settings) -> None:
    class _Broken(FakeLLM):
        def structured(
            self,
            *,
            task: LLMTask,
            prompt: Prompt,
            schema: type[StructuredResponseT],
        ) -> StructuredResponseT:
            _ = (task, prompt, schema)
            msg = "backend down"
            raise LLMError(msg)

    traced = TracedLLM(_Broken(), tracer=NoOpTracer(), settings=settings)
    with pytest.raises(LLMError, match="backend down"):
        traced.structured(task=TASK_PLANNING, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)

    record = traced.collected()[0]
    assert not record.succeeded
    assert record.usage.total_tokens == 0


def test_calls_are_forwarded_to_the_tracer(settings: Settings) -> None:
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    traced = TracedLLM(FakeLLM(), tracer=tracer, settings=settings)
    traced.structured(task=TASK_ROOT_CAUSE, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)
    assert len(client.generations) == 1
    assert client.generations[0]["metadata"]["task"] == "root_cause"


def test_reset_clears_the_record_so_one_wrapper_serves_several_runs(
    settings: Settings,
) -> None:
    traced = TracedLLM(FakeLLM(), tracer=NoOpTracer(), settings=settings)
    traced.structured(task=TASK_ROOT_CAUSE, prompt=_root_cause_prompt(), schema=RootCauseAnalysis)
    assert traced.collected()
    traced.reset()
    assert traced.collected() == ()


def test_fake_usage_is_deterministic() -> None:
    """Per-run metrics must be assertable without being fabricated."""
    prompt = _root_cause_prompt()
    first = FakeLLM()
    second = FakeLLM()
    first.structured(task=TASK_ROOT_CAUSE, prompt=prompt, schema=RootCauseAnalysis)
    second.structured(task=TASK_ROOT_CAUSE, prompt=prompt, schema=RootCauseAnalysis)
    assert first.usages == second.usages
    assert first.last_usage() is not None


def test_fake_usage_is_none_before_any_call() -> None:
    assert FakeLLM().last_usage() is None


def _access(
    *,
    operation: MemoryOperation = MemoryOperation.READ_OUTCOMES,
    hit: bool = True,
    records: int = 3,
    succeeded: bool = True,
) -> MemoryAccess:
    return MemoryAccess(
        operation=operation,
        store="in-memory",
        client="acme",
        hit=hit,
        record_count=records,
        latency_ms=7,
        succeeded=succeeded,
        detail="",
    )


def test_the_adapter_records_a_memory_access_as_an_event(settings: Settings) -> None:
    """An event rather than a span.

    These are short, frequent and already timed by the store; a span per lookup
    would bury the model calls that actually cost something.
    """
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    tracer.record_memory_access(_access())

    recorded = client.events[0]
    assert recorded["name"] == "memory.read_outcomes"
    assert recorded["metadata"]["store"] == "in-memory"
    assert recorded["metadata"]["client"] == "acme"
    assert recorded["metadata"]["hit"] == "true"
    assert recorded["metadata"]["records"] == "3"
    assert recorded["metadata"]["latency_ms"] == "7"
    assert recorded["metadata"]["succeeded"] == "true"
    assert client.spans == []


def test_a_miss_and_a_failure_are_distinguishable_in_the_trace(
    settings: Settings,
) -> None:
    """One is an estate nobody has scanned; the other is a broken store."""
    client = _FakeLangfuse()
    tracer = LangfuseTracer(_configured(settings), client=client)
    tracer.record_memory_access(_access(hit=False, records=0))
    tracer.record_memory_access(_access(hit=False, records=0, succeeded=False))

    miss, failure = client.events
    assert miss["metadata"]["hit"] == "false"
    assert miss["metadata"]["succeeded"] == "true"
    assert failure["metadata"]["succeeded"] == "false"


def test_the_no_op_tracer_accepts_a_memory_access() -> None:
    """The graph calls it unconditionally, so it must never raise."""
    tracer = NoOpTracer()
    tracer.record_memory_access(_access())
    assert isinstance(tracer, Tracer)


def test_a_runs_memory_accesses_reach_its_metrics_and_the_tracer(
    settings: Settings, tmp_path: Path
) -> None:
    """The reason this is traced at all: a hit rate nobody can see cannot be tuned."""
    from costsentinel.graph.runner import run_client_scan
    from costsentinel.memory import SqliteMemoryStore
    from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider

    class _Recording(NoOpTracer):
        def __init__(self) -> None:
            self.accesses: list[MemoryAccess] = []

        def record_memory_access(self, access: MemoryAccess) -> None:
            self.accesses.append(access)

    tracer = _Recording()
    store = SqliteMemoryStore()
    try:
        _, metrics = run_client_scan(
            client=CLIENT_NORTHWIND,
            provider=MockAzureProvider(seed=1337),
            llm=FakeLLM(),
            settings=settings.model_copy(update={"state_dir": tmp_path}),
            checkpointer=None,
            memory=store,
            tracer=tracer,
            run_id="run-traced",
        )
    finally:
        store.close()

    assert metrics.memory.access_count > 0
    # The consult step's reads and the post-report write both reach the tracer.
    operations = {access.operation for access in tracer.accesses}
    assert MemoryOperation.READ_OUTCOMES in operations
    assert MemoryOperation.RECORD_RUN in operations
    assert all(access.succeeded for access in tracer.accesses)
    # A first sweep of a new estate misses on every read, which is correct.
    assert metrics.memory.failure_count == 0


def test_a_runs_memory_metrics_are_empty_when_memory_is_off(
    settings: Settings, tmp_path: Path
) -> None:
    from costsentinel.graph.runner import run_client_scan
    from costsentinel.memory import NullMemoryStore
    from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider

    _, metrics = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=MockAzureProvider(seed=1337),
        llm=FakeLLM(),
        settings=settings.model_copy(update={"state_dir": tmp_path}),
        checkpointer=None,
        memory=NullMemoryStore(),
        run_id="run-nomemory",
    )
    assert metrics.memory.access_count == 0
    assert metrics.memory.hit_rate() is None
