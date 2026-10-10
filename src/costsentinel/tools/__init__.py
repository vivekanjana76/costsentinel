"""The Azure tool surface: typed, read-only, and enumerable.

CLAUDE.md section 10 puts every cloud read behind an MCP tool boundary. This package
is that boundary, and it is read-only by construction rather than by convention:

* :class:`~costsentinel.tools.schemas.ToolOperation` has one member, so a mutating
  tool cannot be *declared*;
* :data:`~costsentinel.tools.registry.TOOLS` is the only path to the MCP server, so a
  mutating tool cannot be *reached*;
* no Azure client capable of mutation is imported anywhere in the package, so the
  capability is *absent*.

``tests/test_readonly.py`` asserts all three mechanically, because a read-only
guarantee that depends on review is not a guarantee.

Layering: :mod:`schemas`, :mod:`registry`, :mod:`azure` and :mod:`invoker` import
without FastMCP or the Azure SDKs, so the whole surface is testable offline.
:mod:`clients` and :mod:`server` are the only modules that touch the optional extras,
and both import them lazily.
"""

from costsentinel.tools.azure import AzureClients
from costsentinel.tools.clients import (
    AzureCredentialError,
    AzureDependencyError,
    LiveAzureClients,
    build_credential,
)
from costsentinel.tools.invoker import (
    LocalToolInvoker,
    ToolInvoker,
    describe_surface,
    tool_specs,
)
from costsentinel.tools.registry import BY_NAME, MUTATING_VERBS, TOOLS, ToolSpec, tool
from costsentinel.tools.schemas import ToolOperation, ToolResponse, ToolStatus

__all__ = [
    "BY_NAME",
    "MUTATING_VERBS",
    "TOOLS",
    "AzureClients",
    "AzureCredentialError",
    "AzureDependencyError",
    "LiveAzureClients",
    "LocalToolInvoker",
    "ToolInvoker",
    "ToolOperation",
    "ToolResponse",
    "ToolSpec",
    "ToolStatus",
    "build_credential",
    "describe_surface",
    "tool",
    "tool_specs",
]
