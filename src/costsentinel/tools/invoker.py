"""Tool dispatch, shared by the in-process path and the MCP server.

Both the real provider and the MCP server reach Azure through a
:class:`ToolInvoker`, so there is exactly one dispatch path and the read-only
guarantee holds for both. The provider does not need an MCP server running to work
locally, and the MCP server does not get its own copy of the handler logic.

:class:`LocalToolInvoker` validates the request against the registered model, calls
the handler, and validates the response on the way back. That double validation is
the point: an Azure payload that does not fit the schema is a failure with a reason,
not a half-populated record flowing into a cost report.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from costsentinel.observability.logging import get_logger
from costsentinel.tools.azure import AzureClients
from costsentinel.tools.registry import TOOLS, ToolSpec, tool
from costsentinel.tools.schemas import ToolResponse, ToolStatus

_log = get_logger("tools.invoker")


@runtime_checkable
class ToolInvoker(Protocol):
    """Calls a named tool with a payload and returns its typed response."""

    def invoke(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        """Call one tool.

        Args:
            name: A registered tool name.
            payload: Fields for that tool's request model.

        Returns:
            The tool's response, always of its declared response type, and always
            carrying a status.
        """
        ...


class LocalToolInvoker:
    """Calls the handlers in this process.

    Used by the real provider for a local run, and by the MCP server to serve a
    request. Caches responses for the duration of one sweep, because the Scout asks
    for the same inventory once per subscription and Azure's read APIs are rate
    limited -- caching once at the boundary is cheaper than teaching every caller to
    be careful.
    """

    def __init__(self, clients: AzureClients, *, cache: bool = True) -> None:
        """Wrap a set of Azure read clients.

        Args:
            clients: The read clients the handlers call.
            cache: Whether to memoise responses within this invoker's lifetime.
                One sweep is one invoker, so the cache never outlives the data.
        """
        self._clients = clients
        self._cache_enabled = cache
        self._cache: dict[tuple[str, str], ToolResponse] = {}
        self.calls: list[tuple[str, int]] = []

    @property
    def call_count(self) -> int:
        """How many tool calls were served, cache hits excluded."""
        return len(self.calls)

    def invoke(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        """Validate, dispatch, and validate the result.

        A validation failure on either side becomes a ``failed`` response with the
        reason, so a schema mismatch degrades one signal rather than raising out of
        the middle of a sweep.
        """
        spec = tool(name)
        key = (name, repr(sorted(payload.items())))
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        try:
            request = spec.request_model.model_validate(payload)
        except ValidationError as exc:
            return spec.response_model(
                status=ToolStatus.FAILED,
                reason=f"{name} was called with an invalid request: {exc}",
            )

        started = time.perf_counter()
        response = spec.handler(request, self._clients)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.calls.append((name, elapsed_ms))

        if not isinstance(response, spec.response_model):
            response = spec.response_model(
                status=ToolStatus.FAILED,
                reason=(
                    f"{name} returned {type(response).__name__}, not {spec.response_model.__name__}"
                ),
            )

        _log.debug(
            "tool invoked",
            extra={
                "tool": name,
                "status": response.status.value,
                "latency_ms": elapsed_ms,
                "service": spec.azure_service,
            },
        )
        if self._cache_enabled:
            self._cache[key] = response
        return response

    def reset(self) -> None:
        """Drop the cache and the call log, so one invoker can serve another sweep."""
        self._cache.clear()
        self.calls.clear()


def describe_surface() -> tuple[str, ...]:
    """One line per registered tool, for a health endpoint or the docs.

    Enumerating the registry rather than a server's state means this cannot drift
    from what is actually exposed.
    """
    return tuple(
        f"{spec.name} [{spec.operation.value}] {spec.azure_service} -- {spec.summary}"
        for spec in TOOLS
    )


def tool_specs() -> tuple[ToolSpec, ...]:
    """Every registered tool."""
    return TOOLS
