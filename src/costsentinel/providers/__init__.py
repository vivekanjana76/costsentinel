"""Provider access: the read-only seam between CostSentinel and a cloud.

:func:`get_provider` is the *only* place in the codebase that names a concrete
provider class. Everything else depends on the
:class:`~costsentinel.providers.base.AzureProvider` protocol, which is what makes
the mock/real swap a configuration change rather than a code change
(ARCHITECTURE.md section 4.1).
"""

from collections.abc import Sequence

from costsentinel.config import RunMode, Settings
from costsentinel.providers.azure import AzureLiveProvider
from costsentinel.providers.base import (
    AzureProvider,
    ProviderError,
    ProviderNotConfiguredError,
)
from costsentinel.providers.catalogue import (
    CATALOGUE_PATH,
    CatalogueError,
    PriceCatalogue,
    default_catalogue,
    load_catalogue,
)
from costsentinel.providers.mock import (
    CLIENT_MINISTRY,
    CLIENT_NORTHWIND,
    MOCK_AS_OF,
    MockAzureProvider,
)

__all__ = [
    "CATALOGUE_PATH",
    "CLIENT_MINISTRY",
    "CLIENT_NORTHWIND",
    "MOCK_AS_OF",
    "AzureLiveProvider",
    "AzureProvider",
    "CatalogueError",
    "MockAzureProvider",
    "PriceCatalogue",
    "ProviderError",
    "ProviderNotConfiguredError",
    "default_catalogue",
    "get_provider",
    "load_catalogue",
]


def get_provider(
    settings: Settings, *, subscription_ids: Sequence[str] | None = None
) -> AzureProvider:
    """Build the provider this configuration selects.

    ``MODE=mock`` (the default) yields the deterministic synthetic estate, which is
    the only provider CI and the demo ever use. ``MODE=real`` yields the read-only
    live provider, which reaches Azure through the MCP tool surface and authenticates
    with ``DefaultAzureCredential``.

    The real branch imports the Azure SDKs lazily, inside the function. That is
    deliberate rather than tidy: importing this package must not require the
    optional ``azure`` extra, because CI installs neither it nor any credential.

    Args:
        settings: Resolved configuration.
        subscription_ids: Subscriptions a real run may read. Overrides
            ``AZURE_SUBSCRIPTION_IDS`` when given, which is how ``--subscription``
            reaches the provider.

    Returns:
        An object satisfying :class:`AzureProvider`.

    Raises:
        ProviderNotConfiguredError: If a real provider is selected but cannot be
            built with the configuration given -- no subscription scope, a missing
            optional dependency, or no usable credential.
    """
    if settings.mode is RunMode.MOCK:
        return MockAzureProvider(seed=settings.mock_seed)

    scope = tuple(subscription_ids) if subscription_ids else settings.subscription_scope
    if not scope:
        msg = (
            "MODE=real needs an explicit subscription scope. Pass --subscription "
            "or set AZURE_SUBSCRIPTION_IDS."
        )
        raise ProviderNotConfiguredError(msg)

    # Imported here rather than at module scope on purpose: these modules reach
    # for the optional ``azure`` extra, and importing this package must not.
    from costsentinel.tools.clients import (  # noqa: PLC0415
        AzureCredentialError,
        AzureDependencyError,
        LiveAzureClients,
    )
    from costsentinel.tools.invoker import LocalToolInvoker  # noqa: PLC0415

    try:
        clients = LiveAzureClients()
    except (AzureDependencyError, AzureCredentialError) as exc:
        raise ProviderNotConfiguredError(str(exc)) from exc

    return AzureLiveProvider(
        invoker=LocalToolInvoker(clients),
        settings=settings,
        subscription_ids=scope,
    )
