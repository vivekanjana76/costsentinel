# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingTypeStubs=false
# The Azure SDKs and FastMCP are OPTIONAL extras, deliberately not installed in
# CI, so their imports cannot resolve here and everything derived from them is
# Unknown. Scoped to the only two modules that touch those packages -- everything
# CostSentinel owns, including the whole tool surface, stays strictly checked.
"""Real Azure read clients, constructed lazily from ``DefaultAzureCredential``.

Every SDK import happens inside the method that needs it, behind the ``azure``
optional extra. That keeps the default install small, lets CI skip the SDKs entirely,
and means :mod:`costsentinel.tools.azure` can be imported and fully tested without
them.

Authentication is ``DefaultAzureCredential`` and nothing else. It picks up an
existing ``az login``, a managed identity, or a workload identity, in that order.
There is deliberately no code path that reads a client secret from this repository:
the credential is the operator's, held by their machine or their workload, and
CostSentinel never sees or stores it.

Only read clients are constructed. No management client capable of mutating a
resource is imported anywhere in this module, so the capability is absent rather than
merely unused.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from costsentinel.observability.logging import get_logger

_log = get_logger("tools.clients")

#: The scope a read-only credential needs. Management-plane reads only.
ARM_SCOPE = "https://management.azure.com/.default"

#: The one-line install hint shown whenever the extra is missing.
INSTALL_HINT = "install the optional Azure extra with: uv sync --extra azure"


class AzureDependencyError(RuntimeError):
    """The Azure SDKs are not installed.

    Raised instead of an ``ImportError`` so the message can say what to do about it.
    """


class AzureCredentialError(RuntimeError):
    """No usable Azure credential was found.

    Raised at construction rather than on first use, so a missing ``az login`` fails
    before a sweep starts rather than halfway through one.
    """


def _require(module: str, package: str) -> Any:
    """Import an optional Azure module, or explain how to install it."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:
        msg = f"{package} is required for MODE=real but is not installed; {INSTALL_HINT}"
        raise AzureDependencyError(msg) from exc


def build_credential() -> Any:
    """Build a ``DefaultAzureCredential``.

    Raises:
        AzureDependencyError: If ``azure-identity`` is not installed.
        AzureCredentialError: If no credential could be obtained. The message names
            ``az login`` because that is what an operator running this locally will
            almost always be missing.
    """
    identity = _require("azure.identity", "azure-identity")
    try:
        credential = identity.DefaultAzureCredential(exclude_interactive_browser_credential=False)
        # Fail now rather than on the first API call: a sweep that dies three nodes
        # in is far harder to diagnose than one that refuses to start.
        credential.get_token(ARM_SCOPE)
    except Exception as exc:
        msg = (
            f"no usable Azure credential: {type(exc).__name__}: {exc}. Run 'az login' "
            f"and confirm the account has Reader and Cost Management Reader on the "
            f"target subscription. CostSentinel never reads a secret from the "
            f"repository, so there is no fallback."
        )
        raise AzureCredentialError(msg) from exc
    return credential


class LiveAzureClients:
    """Read-only Azure clients satisfying :class:`~costsentinel.tools.azure.AzureClients`.

    Each client is built on first use and cached, so a sweep that never reads metrics
    never constructs a Monitor client.
    """

    def __init__(self, *, credential: Any | None = None) -> None:
        """Build the client bundle.

        Args:
            credential: An already-built credential. Injected by the tests; left
                ``None`` in production, where ``DefaultAzureCredential`` is used.
        """
        self._credential = credential if credential is not None else build_credential()
        self._graph: Any | None = None
        self._cost: Any | None = None
        self._monitor: Any | None = None
        self._advisor: Any | None = None
        self._reservations: Any | None = None

    @property
    def credential(self) -> Any:
        """The credential these clients authenticate with."""
        return self._credential

    # --- Resource Graph --------------------------------------------------

    def resource_graph_query(
        self, query: str, subscriptions: Sequence[str]
    ) -> Iterable[Mapping[str, Any]]:
        """Run a read-only Resource Graph KQL query, following pagination."""
        graph = self._graph
        if graph is None:
            module = _require("azure.mgmt.resourcegraph", "azure-mgmt-resourcegraph")
            graph = module.ResourceGraphClient(self._credential)
            self._graph = graph
        models = _require("azure.mgmt.resourcegraph.models", "azure-mgmt-resourcegraph")

        rows: list[Mapping[str, Any]] = []
        skip_token: str | None = None
        while True:
            request = models.QueryRequest(
                subscriptions=list(subscriptions) or None,
                query=query,
                options=models.QueryRequestOptions(skip_token=skip_token, top=1000),
            )
            result = graph.resources(request)
            rows.extend(result.data or [])
            skip_token = getattr(result, "skip_token", None)
            if not skip_token:
                return rows

    # --- Cost Management -------------------------------------------------

    def cost_query(
        self, subscription_id: str, start: date, end: date
    ) -> Iterable[Mapping[str, Any]]:
        """Read daily actual cost rows for one subscription."""
        cost = self._cost
        if cost is None:
            module = _require("azure.mgmt.costmanagement", "azure-mgmt-costmanagement")
            cost = module.CostManagementClient(self._credential)
            self._cost = cost
        models = _require("azure.mgmt.costmanagement.models", "azure-mgmt-costmanagement")

        definition = models.QueryDefinition(
            type="ActualCost",
            timeframe="Custom",
            time_period=models.QueryTimePeriod(from_property=start, to=end),
            dataset=models.QueryDataset(
                granularity="Daily",
                aggregation={"cost": models.QueryAggregation(name="Cost", function="Sum")},
            ),
        )
        result = cost.query.usage(f"/subscriptions/{subscription_id}", definition)
        columns = [column.name for column in (result.columns or [])]
        return [dict(zip(columns, row, strict=False)) for row in (result.rows or [])]

    # --- Monitor ---------------------------------------------------------

    def metric_values(
        self,
        resource_id: str,
        metric_names: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Read aggregated metric values for one resource."""
        monitor = self._monitor
        if monitor is None:
            module = _require("azure.mgmt.monitor", "azure-mgmt-monitor")
            monitor = module.MonitorManagementClient(self._credential, subscription_id=None)
            self._monitor = monitor

        collected: dict[str, dict[str, float | None]] = {}
        result = monitor.metrics.list(
            resource_id,
            timespan=f"{start.isoformat()}/{end.isoformat()}",
            interval="P1D",
            metricnames=",".join(metric_names),
            aggregation="Average,Maximum,Total",
        )
        for metric in result.value or []:
            name = str(getattr(metric.name, "value", metric.name))
            averages = [
                point.average
                for series in (metric.timeseries or [])
                for point in (series.data or [])
                if point.average is not None
            ]
            maxima = [
                point.maximum
                for series in (metric.timeseries or [])
                for point in (series.data or [])
                if point.maximum is not None
            ]
            totals = [
                point.total
                for series in (metric.timeseries or [])
                for point in (series.data or [])
                if point.total is not None
            ]
            collected[name] = {
                "average": sum(averages) / len(averages) if averages else None,
                "maximum": max(maxima) if maxima else None,
                "total": sum(totals) if totals else None,
            }
        return collected

    # --- Advisor ---------------------------------------------------------

    def advisor_recommendations(self, subscription_id: str) -> Iterable[Mapping[str, Any]]:
        """Read Advisor recommendations for one subscription."""
        advisor = self._advisor
        if advisor is None:
            module = _require("azure.mgmt.advisor", "azure-mgmt-advisor")
            advisor = module.AdvisorManagementClient(self._credential, subscription_id)
            self._advisor = advisor
        return [item.as_dict() for item in advisor.recommendations.list()]

    # --- Reservations ----------------------------------------------------

    def reservation_orders(self) -> Iterable[Mapping[str, Any]]:
        """Read reservation orders and their utilisation."""
        reservations = self._reservations
        if reservations is None:
            module = _require("azure.mgmt.reservations", "azure-mgmt-reservations")
            reservations = module.AzureReservationAPI(self._credential)
            self._reservations = reservations
        orders: list[Mapping[str, Any]] = []
        for order in reservations.reservation_order.list():
            for reservation in order.reservations or []:
                orders.append(reservation.as_dict())
        return orders
