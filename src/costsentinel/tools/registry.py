"""The tool registry: the single source of truth for what the Azure surface exposes.

Registration is *data*. Every tool is a :class:`ToolSpec` in :data:`TOOLS`, so
"what can this system do to an Azure subscription" is answered by reading one tuple
rather than by auditing a server's decorators.

That matters for the read-only guarantee. The test in ``tests/test_readonly.py``
enumerates this registry and asserts every entry is a read operation and that no name
carries a mutating verb. Because :class:`~costsentinel.tools.schemas.ToolOperation`
has a single member, a write tool cannot be *declared*; because the registry is the
only path to the MCP server, a write tool cannot be *reached* either.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from costsentinel.domain.common import Frozen
from costsentinel.tools import azure as handlers
from costsentinel.tools.schemas import (
    GetCostSeriesRequest,
    GetCostSeriesResponse,
    GetResourceMetricsRequest,
    GetResourceMetricsResponse,
    ListAdvisorRecommendationsRequest,
    ListAdvisorRecommendationsResponse,
    ListReservationsRequest,
    ListReservationsResponse,
    ListSubscriptionsRequest,
    ListSubscriptionsResponse,
    QueryResourcesRequest,
    QueryResourcesResponse,
    ToolModel,
    ToolOperation,
    ToolResponse,
)

#: Verbs that would indicate a mutating operation. Checked against every tool name,
#: handler name and docstring by the read-only test, so a write path cannot be added
#: under a misleading name either.
MUTATING_VERBS: tuple[str, ...] = (
    "create",
    "update",
    "delete",
    "remove",
    "write",
    "put",
    "patch",
    "post",
    "set",
    "modify",
    "mutate",
    "deallocate",
    "resize",
    "restart",
    "start",
    "stop",
    "scale",
    "purge",
    "revoke",
    "attach",
    "detach",
    "apply",
    "execute",
    "invoke_action",
)


class ToolSpec(Frozen):
    """One tool on the Azure surface.

    ``operation`` can only be :attr:`ToolOperation.READ`, which is the point: the
    type system refuses to describe anything else.
    """

    name: str
    summary: str
    operation: ToolOperation = ToolOperation.READ
    request_model: type[ToolModel]
    response_model: type[ToolResponse]
    handler: Callable[..., Any]
    azure_service: str
    required_permission: str

    @property
    def is_read_only(self) -> bool:
        """Whether this tool only reads. Always true, by construction."""
        return self.operation is ToolOperation.READ


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="azure_list_subscriptions",
        summary="List the Azure subscriptions the caller's credential can see.",
        request_model=ListSubscriptionsRequest,
        response_model=ListSubscriptionsResponse,
        handler=handlers.list_subscriptions,
        azure_service="Azure Resource Graph",
        required_permission="Reader on the subscription or management group",
    ),
    ToolSpec(
        name="azure_query_resources",
        summary="Read the resource inventory of one subscription.",
        request_model=QueryResourcesRequest,
        response_model=QueryResourcesResponse,
        handler=handlers.query_resources,
        azure_service="Azure Resource Graph",
        required_permission="Reader on the subscription",
    ),
    ToolSpec(
        name="azure_list_reservations",
        summary="Read capacity commitments and their utilisation.",
        request_model=ListReservationsRequest,
        response_model=ListReservationsResponse,
        handler=handlers.list_reservations,
        azure_service="Azure Reservations",
        required_permission="Reader on the reservation order, or billing reader",
    ),
    ToolSpec(
        name="azure_get_cost_series",
        summary="Read daily actual spend for one subscription over a trailing window.",
        request_model=GetCostSeriesRequest,
        response_model=GetCostSeriesResponse,
        handler=handlers.get_cost_series,
        azure_service="Azure Cost Management",
        required_permission="Cost Management Reader on the subscription",
    ),
    ToolSpec(
        name="azure_get_resource_metrics",
        summary="Read observed utilisation for one resource.",
        request_model=GetResourceMetricsRequest,
        response_model=GetResourceMetricsResponse,
        handler=handlers.get_resource_metrics,
        azure_service="Azure Monitor",
        required_permission="Monitoring Reader on the subscription",
    ),
    ToolSpec(
        name="azure_list_advisor_cost_recommendations",
        summary="Read Azure Advisor's own cost recommendations, as corroboration.",
        request_model=ListAdvisorRecommendationsRequest,
        response_model=ListAdvisorRecommendationsResponse,
        handler=handlers.list_advisor_cost_recommendations,
        azure_service="Azure Advisor",
        required_permission="Reader on the subscription",
    ),
)

#: Tools by name, for dispatch.
BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def tool(name: str) -> ToolSpec:
    """One tool by name.

    Raises:
        KeyError: If no such tool is registered. There is deliberately no fallback:
            a caller asking for a tool that does not exist is a bug, not something
            to paper over.
    """
    if name not in BY_NAME:
        available = ", ".join(sorted(BY_NAME))
        msg = f"no tool named {name!r}; registered tools are: {available}"
        raise KeyError(msg)
    return BY_NAME[name]


def required_permissions() -> tuple[str, ...]:
    """Every Azure permission the surface needs, deduplicated and sorted.

    Used by the documentation and the real-run preflight, so what the tool asks for
    is stated rather than discovered when something returns a 403.
    """
    return tuple(sorted({spec.required_permission for spec in TOOLS}))
