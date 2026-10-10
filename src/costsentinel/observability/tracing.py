# pyright: reportMissingImports=false, reportUnknownVariableType=false
# langfuse is an OPTIONAL extra and is deliberately not installed in CI, so its
# import cannot resolve here. Scoped to this file, which is the only place that
# touches the package, so everything else stays strictly checked.
"""Tracing: Langfuse when configured, a no-op otherwise, never a hard dependency.

CLAUDE.md section 10 requires tracing around every graph run and every LLM call, and
requires it to be a no-op when unconfigured. "Never a hard dependency" is taken
literally here: ``langfuse`` is an *optional* extra, the import happens lazily inside
:class:`LangfuseTracer`, and an absent package degrades to
:class:`NoOpTracer` with a log line rather than an exception. CI does not install it,
and the suite passes without it.

That raises an obvious problem -- untested adapter code -- which is why
:class:`LangfuseTracer` accepts an injected client. The tests drive it with a double,
so the adapter path is covered offline and the only untested part is the one import
line that reaches for the real package.
"""

from __future__ import annotations

import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

from costsentinel.config import Settings
from costsentinel.domain.observability import LLMCallMetrics
from costsentinel.observability.logging import get_logger

_log = get_logger("observability.tracing")


@runtime_checkable
class Span(Protocol):
    """One timed, named unit of work inside a trace."""

    def set_attribute(self, key: str, value: str) -> None:
        """Attach a key/value detail to this span."""
        ...

    def finish(self, *, error: str | None = None) -> None:
        """Close the span, optionally recording that it failed."""
        ...


@runtime_checkable
class Tracer(Protocol):
    """Wraps a run and its model calls.

    Every method must be safe to call when tracing is off, because the graph calls
    them unconditionally. A tracer that can raise would make observability a
    liability rather than an aid.
    """

    @property
    def name(self) -> str:
        """Short identifier for logs and the health endpoint."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether anything is actually being recorded."""
        ...

    def start_run(self, *, run_id: str, client: str, metadata: Mapping[str, str]) -> None:
        """Begin a trace for one graph run."""
        ...

    def end_run(self, *, run_id: str, metadata: Mapping[str, str]) -> None:
        """Close the run's trace."""
        ...

    def span(self, name: str, *, kind: str = "node") -> Span:
        """Open a child span."""
        ...

    def record_llm_call(self, metrics: LLMCallMetrics) -> None:
        """Record one model call, including its routing choice and cost."""
        ...


class NullSpan:
    """A span that records nothing."""

    def set_attribute(self, key: str, value: str) -> None:
        """Discard the attribute."""

    def finish(self, *, error: str | None = None) -> None:
        """Do nothing."""


class NoOpTracer:
    """The default. Accepts every call and records nothing."""

    @property
    def name(self) -> str:
        """Short identifier for logs and the health endpoint."""
        return "noop"

    @property
    def enabled(self) -> bool:
        """Always false: this tracer never records."""
        return False

    def start_run(self, *, run_id: str, client: str, metadata: Mapping[str, str]) -> None:
        """Discard the run start."""

    def end_run(self, *, run_id: str, metadata: Mapping[str, str]) -> None:
        """Discard the run end."""

    def span(self, name: str, *, kind: str = "node") -> Span:
        """Return a span that records nothing."""
        _ = (name, kind)
        return NullSpan()

    def record_llm_call(self, metrics: LLMCallMetrics) -> None:
        """Discard the call record."""


class LangfuseSpan:
    """Adapts one Langfuse span object to the :class:`Span` protocol."""

    def __init__(self, handle: Any) -> None:
        """Wrap a Langfuse span handle."""
        self._handle = handle

    def set_attribute(self, key: str, value: str) -> None:
        """Attach a detail, tolerating an SDK that names the method differently."""
        try:
            self._handle.update(metadata={key: value})
        except Exception as exc:
            _log.debug("span attribute dropped", extra={"key": key, "error": str(exc)})

    def finish(self, *, error: str | None = None) -> None:
        """Close the span, recording an error if one occurred."""
        try:
            if error is not None:
                self._handle.update(level="ERROR", status_message=error)
            self._handle.end()
        except Exception as exc:
            _log.debug("span close dropped", extra={"error": str(exc)})


class LangfuseTracer:
    """Records runs and model calls to Langfuse.

    Args:
        settings: Resolved configuration, used for the host and keys.
        client: An already-built Langfuse client. Injected by the tests so the
            adapter is covered without the package or the network; left ``None`` in
            production, where it is constructed lazily.
    """

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        """Build the tracer, falling back to recording nothing if Langfuse is absent."""
        self._settings = settings
        self._client = client if client is not None else self._build_client(settings)
        self._run_handles: dict[str, Any] = {}

    @staticmethod
    def _build_client(settings: Settings) -> Any | None:
        """Construct a Langfuse client, or return ``None`` if that is not possible.

        Returning ``None`` rather than raising is the whole point: an unconfigured or
        uninstalled Langfuse must degrade to silence, not break a sweep.
        """
        if not settings.tracing_enabled:
            return None
        try:
            from langfuse import Langfuse  # noqa: PLC0415 -- optional extra, imported lazily
        except ImportError:
            _log.warning(
                "langfuse is configured but not installed; tracing is disabled. "
                "Install the optional extra with: uv sync --extra tracing",
                extra={"tracer": "langfuse"},
            )
            return None
        secret = settings.langfuse_secret_key
        try:
            return Langfuse(
                host=settings.langfuse_host,
                public_key=settings.langfuse_public_key,
                secret_key=secret.get_secret_value() if secret else None,
            )
        except Exception as exc:
            _log.warning("langfuse client could not be built", extra={"error": str(exc)})
            return None

    @property
    def name(self) -> str:
        """Short identifier for logs and the health endpoint."""
        return "langfuse"

    @property
    def enabled(self) -> bool:
        """Whether a client was actually obtained."""
        return self._client is not None

    def start_run(self, *, run_id: str, client: str, metadata: Mapping[str, str]) -> None:
        """Open a trace for one graph run."""
        if self._client is None:
            return
        try:
            self._run_handles[run_id] = self._client.trace(
                id=run_id,
                name="costsentinel.scan",
                user_id=client,
                metadata=dict(metadata),
            )
        except Exception as exc:
            _log.debug("trace start dropped", extra={"run_id": run_id, "error": str(exc)})

    def end_run(self, *, run_id: str, metadata: Mapping[str, str]) -> None:
        """Close the run's trace and flush, so a short-lived process still reports."""
        handle = self._run_handles.pop(run_id, None)
        if self._client is None:
            return
        try:
            if handle is not None:
                handle.update(metadata=dict(metadata))
            self._client.flush()
        except Exception as exc:
            _log.debug("trace end dropped", extra={"run_id": run_id, "error": str(exc)})

    def span(self, name: str, *, kind: str = "node") -> Span:
        """Open a child span, or a null span if the client is unavailable."""
        if self._client is None:
            return NullSpan()
        try:
            return LangfuseSpan(self._client.span(name=name, metadata={"kind": kind}))
        except Exception as exc:
            _log.debug("span start dropped", extra={"span": name, "error": str(exc)})
            return NullSpan()

    def record_llm_call(self, metrics: LLMCallMetrics) -> None:
        """Record one model call, with its routing choice, usage and cost."""
        if self._client is None:
            return
        try:
            self._client.generation(
                name=f"llm.{metrics.task}",
                model=metrics.routing.model,
                usage={
                    "input": metrics.usage.prompt_tokens,
                    "output": metrics.usage.completion_tokens,
                    "total": metrics.usage.total_tokens,
                },
                metadata={
                    "task": metrics.task,
                    "tier": metrics.routing.tier.value,
                    "backend": metrics.routing.backend,
                    "routing_reason": metrics.routing.reason,
                    "schema": metrics.schema_name,
                    "latency_ms": str(metrics.latency_ms),
                    "cost": metrics.cost.display(),
                    "succeeded": str(metrics.succeeded).lower(),
                },
            )
        except Exception as exc:
            _log.debug("generation record dropped", extra={"error": str(exc)})


def get_tracer(settings: Settings) -> Tracer:
    """Build the tracer this configuration selects.

    Returns a :class:`NoOpTracer` unless Langfuse is configured *and* a client could
    be built, so there is exactly one code path in which tracing does anything and it
    is the one where someone asked for it.
    """
    if not settings.tracing_enabled:
        return NoOpTracer()
    tracer = LangfuseTracer(settings)
    if not tracer.enabled:
        return NoOpTracer()
    return tracer


class _Timer:
    """Measures wall-clock duration in whole milliseconds."""

    def __init__(self) -> None:
        self.elapsed_ms = 0
        self._started = 0.0

    def __enter__(self) -> _Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._started) * 1000)


@contextmanager
def timed(tracer: Tracer, name: str, *, kind: str = "node") -> Generator[_Timer]:
    """Time a block and record it as a span.

    The span is closed on the way out even when the block raises, and the exception
    propagates: tracing observes failures, it does not swallow them.
    """
    span = tracer.span(name, kind=kind)
    timer = _Timer()
    try:
        with timer:
            yield timer
    except Exception as exc:
        span.set_attribute("error", str(exc))
        span.finish(error=str(exc))
        raise
    span.set_attribute("latency_ms", str(timer.elapsed_ms))
    span.finish()
