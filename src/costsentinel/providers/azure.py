"""Real Azure provider -- a declared seam, implemented in Phase 2.

It exists now so that ``MODE=real`` fails with one clear message at construction
instead of an import error or a confusing failure halfway through a sweep.

When Phase 2 implements this, it will sit behind the MCP tool server rather than
calling the Azure SDKs directly (ARCHITECTURE.md section 10), and it will stay
read-only until Phase 10.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NoReturn

from costsentinel.providers.base import ProviderNotConfiguredError

if TYPE_CHECKING:
    from costsentinel.config import Settings
    from costsentinel.domain.estate import (
        CostSeries,
        Resource,
        ResourceMetrics,
        SkuPrice,
        Subscription,
    )

_NOT_IMPLEMENTED = (
    "The real Azure provider arrives in Phase 2. Set MODE=mock (the default) to run "
    "against the deterministic synthetic estate. See ROADMAP.md."
)


class AzureLiveProvider:
    """Placeholder for the Phase 2 read-only Azure provider."""

    def __init__(self, settings: Settings) -> None:
        """Refuse construction until Phase 2 implements this provider.

        Raises:
            ProviderNotConfiguredError: Always, in Phase 1.
        """
        self._settings = settings
        raise ProviderNotConfiguredError(_NOT_IMPLEMENTED)

    def _unavailable(self) -> NoReturn:
        raise ProviderNotConfiguredError(_NOT_IMPLEMENTED)

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "azure-live"

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        """Not implemented in Phase 1."""
        _ = client
        self._unavailable()

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        """Not implemented in Phase 1."""
        _ = subscription_id
        self._unavailable()

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        """Not implemented in Phase 1."""
        _ = (subscription_id, window_days)
        self._unavailable()

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        """Not implemented in Phase 1."""
        _ = resource_id
        self._unavailable()

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        """Not implemented in Phase 1."""
        _ = region
        self._unavailable()
