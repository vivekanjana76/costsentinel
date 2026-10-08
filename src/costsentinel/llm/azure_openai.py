"""Azure OpenAI adapter -- the primary real backend, implemented in Phase 3.

It exists now so the routing seam is real and so selecting it without configuration
fails at construction with one clear message, rather than deep inside a sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from costsentinel.llm.base import LLMNotConfiguredError

if TYPE_CHECKING:
    from costsentinel.config import Settings
    from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT


class AzureOpenAILLM:
    """Placeholder for the Phase 3 Azure OpenAI backend."""

    def __init__(self, settings: Settings) -> None:
        """Validate configuration and refuse until Phase 3 implements the call.

        Missing configuration is reported before the unimplemented-phase message,
        because that is the error an operator is far more likely to hit first.

        Raises:
            LLMNotConfiguredError: Always, in Phase 1.
        """
        self._settings = settings
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint),
                ("AZURE_OPENAI_API_KEY", settings.azure_openai_api_key),
                ("AZURE_OPENAI_API_VERSION", settings.azure_openai_api_version),
                ("AZURE_OPENAI_DEPLOYMENT", settings.azure_openai_deployment),
            )
            if not value
        ]
        if missing:
            msg = (
                f"LLM_BACKEND=azure_openai requires {', '.join(missing)}. "
                f"Set LLM_BACKEND=fake (the default) to run with no credentials."
            )
            raise LLMNotConfiguredError(msg)
        msg = (
            "The Azure OpenAI backend arrives in Phase 3. Set LLM_BACKEND=fake (the "
            "default) to run against the deterministic fake model. See ROADMAP.md."
        )
        raise LLMNotConfiguredError(msg)

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "azure-openai"

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> NoReturn:
        """Not implemented in Phase 1."""
        _ = (task, prompt, schema)
        msg = "The Azure OpenAI backend arrives in Phase 3."
        raise LLMNotConfiguredError(msg)
