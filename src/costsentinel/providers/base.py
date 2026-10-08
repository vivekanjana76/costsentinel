"""The provider seam: what CostSentinel needs from a cloud, and nothing more.

A :class:`typing.Protocol` rather than an abstract base class (ARCHITECTURE.md D2):
an implementation need not import anything from this package, which keeps the real
Azure adapter a thin wrapper and makes test doubles trivial, while pyright still
checks conformance statically.

The interface is read-only. Write operations do not appear here, and will not until
Phase 10 -- a provider literally cannot mutate anything through this seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from costsentinel.domain.estate import (
    CostSeries,
    Reservation,
    Resource,
    ResourceMetrics,
    SkuPrice,
    Subscription,
)


class ProviderError(RuntimeError):
    """A provider could not satisfy a request."""


class ProviderNotConfiguredError(ProviderError):
    """A real provider was selected without the configuration it needs.

    Raised at construction rather than at first use, so a misconfiguration fails
    fast and legibly instead of surfacing halfway through a sweep.
    """


@runtime_checkable
class AzureProvider(Protocol):
    """Read-only access to the cost, inventory, utilisation and pricing signals.

    In Phase 2 every method here is served by the MCP tool server, which is where
    caching, retry, rate limiting and read-only enforcement live. Agents never
    import an Azure SDK, and never call a provider other than through this
    protocol.
    """

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        ...

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        """Subscriptions in scope, optionally narrowed to one client.

        Args:
            client: When given, only that client's subscriptions are returned.
                When ``None``, every subscription the provider can see is returned
                -- callers that produce client-facing output must narrow it, since
                no report may span clients.
        """
        ...

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        """Every billable resource in one subscription."""
        ...

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        """Capacity commitments in scope, optionally narrowed to one client.

        Separate from :meth:`list_resources` because a reservation is a commitment
        rather than a resource, with a term, a quantity and a utilisation against
        that commitment -- and because the real Azure Reservations API is a
        different surface from Resource Graph.
        """
        ...

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        """Daily actual spend for one subscription over the trailing window."""
        ...

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Observed utilisation for one resource, or ``None`` if none was collected.

        ``None`` means "not measured", which the detectors treat as "cannot
        conclude" -- never as idle.
        """
        ...

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """The priced SKU catalogue for one region.

        Rightsizing needs the current SKU's price and a candidate's price, and both
        must come from here. Without this, a savings figure for a resize would be a
        guess, which CLAUDE.md golden rule 5 forbids.
        """
        ...
