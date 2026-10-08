"""Provider access: the read-only seam between CostSentinel and a cloud.

:func:`get_provider` is the *only* place in the codebase that names a concrete
provider class. Everything else depends on the
:class:`~costsentinel.providers.base.AzureProvider` protocol, which is what makes
the mock/real swap a configuration change rather than a code change
(ARCHITECTURE.md section 4.1).
"""

from costsentinel.config import RunMode, Settings
from costsentinel.providers.azure import AzureLiveProvider
from costsentinel.providers.base import (
    AzureProvider,
    ProviderError,
    ProviderNotConfiguredError,
)
from costsentinel.providers.mock import (
    CLIENT_MINISTRY,
    CLIENT_NORTHWIND,
    MOCK_AS_OF,
    MockAzureProvider,
)

__all__ = [
    "CLIENT_MINISTRY",
    "CLIENT_NORTHWIND",
    "MOCK_AS_OF",
    "AzureLiveProvider",
    "AzureProvider",
    "MockAzureProvider",
    "ProviderError",
    "ProviderNotConfiguredError",
    "get_provider",
]


def get_provider(settings: Settings) -> AzureProvider:
    """Build the provider this configuration selects.

    Args:
        settings: Resolved configuration. ``MODE=mock`` (the default) yields the
            deterministic synthetic estate; ``MODE=real`` yields the live Azure
            provider, which is not implemented until Phase 2.

    Returns:
        An object satisfying :class:`AzureProvider`.

    Raises:
        ProviderNotConfiguredError: If a real provider is selected but cannot be
            built with the configuration given.
    """
    if settings.mode is RunMode.MOCK:
        return MockAzureProvider(seed=settings.mock_seed)
    return AzureLiveProvider(settings)
