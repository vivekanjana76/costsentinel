# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingTypeStubs=false
# The Azure SDKs and FastMCP are OPTIONAL extras, deliberately not installed in
# CI, so their imports cannot resolve here and everything derived from them is
# Unknown. Scoped to the only two modules that touch those packages -- everything
# CostSentinel owns, including the whole tool surface, stays strictly checked.
"""The FastMCP server binding.

Thin by design. Everything that matters -- the schemas, the registry, the handlers
and the dispatch -- lives in modules that import without FastMCP, so the whole
surface is testable offline and only this file needs the optional extra.

The server registers exactly what :data:`costsentinel.tools.registry.TOOLS` holds.
There is no decorator-based registration anywhere, which means the registry cannot
drift from what is actually served, and the read-only assertion over the registry is
an assertion over the server too.
"""

from __future__ import annotations

from typing import Any

from costsentinel.observability.logging import get_logger
from costsentinel.tools.azure import AzureClients
from costsentinel.tools.invoker import LocalToolInvoker
from costsentinel.tools.registry import TOOLS, required_permissions

_log = get_logger("tools.server")

SERVER_NAME = "costsentinel-azure"

#: Shown to any MCP client that connects, so an operator knows what they attached to.
SERVER_INSTRUCTIONS = """
Read-only Azure cost and inventory tools for CostSentinel.

Every tool on this server reads. There is no create, update, delete or any other
mutating operation, and none can be added without changing the tool registry in
source: the operation type has a single member, so a write tool cannot be declared.

Each response carries a status of verified, unknown or failed. A failed call returns
no data and says why. An unknown call means Azure answered and does not know -- which
is different from an empty result, and must not be read as "nothing is there".

Required Azure permissions: {permissions}
""".strip()

INSTALL_HINT = "install the optional MCP extra with: uv sync --extra mcp"


class McpDependencyError(RuntimeError):
    """FastMCP is not installed."""


def build_server(clients: AzureClients, *, invoker: LocalToolInvoker | None = None) -> Any:
    """Build a FastMCP server exposing the registered read-only tools.

    Args:
        clients: Azure read clients the handlers call.
        invoker: Dispatch to use. Defaults to a fresh in-process invoker, so the
            server and a local run share one code path.

    Returns:
        A configured ``FastMCP`` instance, not yet running.

    Raises:
        McpDependencyError: If ``fastmcp`` is not installed.
    """
    try:
        from fastmcp import FastMCP  # noqa: PLC0415 -- optional extra, imported lazily
    except ImportError as exc:
        msg = f"fastmcp is required to run the MCP server but is not installed; {INSTALL_HINT}"
        raise McpDependencyError(msg) from exc

    dispatch = invoker or LocalToolInvoker(clients)
    server = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS.format(permissions="; ".join(required_permissions())),
    )

    for spec in TOOLS:
        server.add_tool(_bind(spec.name, dispatch), name=spec.name, description=spec.summary)

    _log.info(
        "mcp server built",
        extra={"tools": len(TOOLS), "name": SERVER_NAME, "operations": "read-only"},
    )
    return server


def _bind(name: str, dispatch: LocalToolInvoker) -> Any:
    """Bind one registered tool to a callable the MCP runtime can serve."""

    def call(**payload: Any) -> dict[str, Any]:
        return dispatch.invoke(name, payload).model_dump(mode="json")

    call.__name__ = name
    return call
