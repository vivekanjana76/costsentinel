"""The read-only guarantee, asserted mechanically.

This is the most important property of the real-estate phase, so it is not left to
review. Three independent mechanisms have to hold, and each is checked here:

1. **A write tool cannot be declared.** :class:`ToolOperation` has one member, so
   there is no value a caller could pass to describe a mutating tool.
2. **A write tool cannot be reached.** The MCP server registers exactly what the
   registry holds, so enumerating the registry enumerates the surface.
3. **A write capability is absent.** No Azure client able to mutate a resource is
   imported anywhere in ``src``, so the dependency is not there to misuse.

Each test is written to fail loudly if someone adds a write path later. That is the
point of them: a guarantee that depends on nobody making a mistake is not a
guarantee.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from costsentinel.providers.azure import AzureLiveProvider
from costsentinel.providers.base import AzureProvider
from costsentinel.tools import azure as handlers
from costsentinel.tools import clients as live_clients
from costsentinel.tools.registry import BY_NAME, MUTATING_VERBS, TOOLS, required_permissions
from costsentinel.tools.schemas import ToolOperation

SRC = Path("src/costsentinel")

#: Azure SDK packages that can mutate a resource. If any of these is ever imported,
#: the read-only guarantee stops being structural and becomes a matter of discipline.
FORBIDDEN_AZURE_PACKAGES = (
    "azure.mgmt.compute",
    "azure.mgmt.storage",
    "azure.mgmt.network",
    "azure.mgmt.sql",
    "azure.mgmt.web",
    "azure.mgmt.resource",
    "azure.mgmt.authorization",
)

#: The read-only Azure packages the surface legitimately uses.
PERMITTED_AZURE_PACKAGES = (
    "azure.identity",
    "azure.mgmt.resourcegraph",
    "azure.mgmt.costmanagement",
    "azure.mgmt.monitor",
    "azure.mgmt.advisor",
    "azure.mgmt.reservations",
)


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _references(name: str, package: str) -> bool:
    """Whether ``name`` is ``package`` or a submodule of it.

    A plain ``startswith`` would be wrong: ``azure.mgmt.resource`` is a prefix of
    ``azure.mgmt.resourcegraph``, so the read-only Resource Graph client would be
    flagged as the mutating Resource Manager one.
    """
    return name == package or name.startswith(f"{package}.")


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by one file, including lazy in-function imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _string_literals(path: Path) -> set[str]:
    """Every string constant in one file.

    Needed because the Azure clients are imported through ``__import__`` with the
    module name as a string, so an AST import scan alone would miss them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


# ---------------------------------------------------------------------------
# 1. A write tool cannot be declared
# ---------------------------------------------------------------------------


def test_the_only_tool_operation_is_read() -> None:
    """A single-member enum is a constraint; a read_only flag would be a convention."""
    assert list(ToolOperation) == [ToolOperation.READ]
    assert len(ToolOperation) == 1


def test_every_registered_tool_is_read_only() -> None:
    assert TOOLS
    for spec in TOOLS:
        assert spec.operation is ToolOperation.READ
        assert spec.is_read_only


def test_declaring_a_non_read_operation_is_impossible() -> None:
    """There is no value to pass, so the attempt fails at validation."""
    with pytest.raises(ValueError, match="write"):
        ToolOperation("write")


# ---------------------------------------------------------------------------
# 2. A write tool cannot be reached
# ---------------------------------------------------------------------------


def test_no_tool_name_carries_a_mutating_verb() -> None:
    """A write path must not be able to hide behind an innocuous name either."""
    for spec in TOOLS:
        lowered = spec.name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"tool {spec.name} contains {verb!r}"


def test_no_handler_name_carries_a_mutating_verb() -> None:
    for spec in TOOLS:
        lowered = spec.handler.__name__.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"handler {spec.handler.__name__} contains {verb!r}"


def test_no_public_handler_in_the_tools_package_carries_a_mutating_verb() -> None:
    """Covers a handler that exists but was never registered."""
    for name, member in inspect.getmembers(handlers, inspect.isfunction):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"{name} in tools.azure contains {verb!r}"
        _ = member


def test_the_azure_client_protocol_exposes_only_reads() -> None:
    """A future client bundle cannot add a write method and still satisfy the protocol."""
    members = [name for name in dir(handlers.AzureClients) if not name.startswith("_")]
    assert members
    for name in members:
        lowered = name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"AzureClients.{name} contains {verb!r}"


def test_the_live_client_bundle_exposes_only_reads() -> None:
    for name, _member in inspect.getmembers(live_clients.LiveAzureClients):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"LiveAzureClients.{name} contains {verb!r}"


def test_the_registry_is_the_only_path_to_the_server() -> None:
    """The server loops over the registry; it has no decorator-based registration.

    If it did, the registry could drift from what is served, and every assertion in
    this file would be checking the wrong thing.
    """
    source = (SRC / "tools" / "server.py").read_text(encoding="utf-8")
    assert "for spec in TOOLS:" in source
    assert "@server.tool" not in source
    assert "@mcp.tool" not in source


def test_registry_lookup_refuses_an_unregistered_name() -> None:
    from costsentinel.tools.registry import tool

    with pytest.raises(KeyError, match="no tool named"):
        tool("azure_delete_everything")


def test_every_tool_declares_the_permission_it_needs() -> None:
    """So what the tool asks for is stated rather than discovered via a 403."""
    for spec in TOOLS:
        assert spec.required_permission
        assert "Reader" in spec.required_permission
    permissions = required_permissions()
    assert permissions == tuple(sorted(set(permissions)))
    # Nothing in the surface needs a Contributor or Owner assignment.
    for permission in permissions:
        assert "Contributor" not in permission
        assert "Owner" not in permission


# ---------------------------------------------------------------------------
# 3. A write capability is absent
# ---------------------------------------------------------------------------


def test_no_mutating_azure_client_is_imported_anywhere_in_src() -> None:
    """The dependency is not there to misuse, rather than present but unused."""
    for path in _python_files():
        referenced = _imported_modules(path) | _string_literals(path)
        for package in FORBIDDEN_AZURE_PACKAGES:
            offenders = [name for name in referenced if _references(name, package)]
            # This test file names the forbidden packages in order to look for them,
            # and the read-only test module is not part of src, so src is clean.
            assert not offenders, f"{path} references {offenders}"


def test_only_the_read_only_azure_packages_are_referenced() -> None:
    """A new Azure dependency has to be added to the permitted list deliberately."""
    for path in _python_files():
        referenced = _imported_modules(path) | _string_literals(path)
        for name in referenced:
            if not name.startswith("azure."):
                continue
            assert any(_references(name, ok) for ok in PERMITTED_AZURE_PACKAGES), (
                f"{path} references unexpected Azure package {name}"
            )


def test_the_azure_sdks_are_optional_extras_not_core_dependencies() -> None:
    """CI must stay able to run without them."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    core = pyproject.split("[dependency-groups]", 1)[0].split("dependencies = [", 1)[1]
    core = core.split("]", 1)[0]
    for package in ("azure-identity", "azure-mgmt", "fastmcp", "openai", "google-genai"):
        assert package not in core, f"{package} must be an optional extra, not core"
    assert "[project.optional-dependencies]" in pyproject


def test_the_provider_protocol_declares_no_write_method() -> None:
    """A future implementation cannot add a write path and still typecheck."""
    for name in dir(AzureProvider):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"AzureProvider.{name} contains {verb!r}"


def test_the_real_provider_exposes_no_write_method() -> None:
    for name, _member in inspect.getmembers(AzureLiveProvider):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        for verb in MUTATING_VERBS:
            assert verb not in lowered, f"AzureLiveProvider.{name} contains {verb!r}"


def test_the_tool_surface_is_enumerable_for_an_operator() -> None:
    """Somebody about to point this at their estate can read what it will do."""
    from costsentinel.tools import describe_surface

    lines = describe_surface()
    assert len(lines) == len(TOOLS)
    for line in lines:
        assert "[read]" in line
    assert len(BY_NAME) == len(TOOLS)
