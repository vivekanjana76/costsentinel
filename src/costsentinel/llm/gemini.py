"""Google Gemini adapter -- the secondary fallback backend, implemented in Phase 3.

Present now for the same reason as the Azure OpenAI adapter: the routing seam must
be real, and selecting an unconfigured backend must fail loudly at construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from costsentinel.llm.base import LLMNotConfiguredError

if TYPE_CHECKING:
    from costsentinel.config import Settings
    from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT


class GeminiLLM:
    """Placeholder for the Phase 3 Gemini backend."""

    def __init__(self, settings: Settings) -> None:
        """Validate configuration and refuse until Phase 3 implements the call.

        Raises:
            LLMNotConfiguredError: Always, in Phase 1.
        """
        self._settings = settings
        if not settings.gemini_api_key:
            msg = (
                "LLM_BACKEND=gemini requires GEMINI_API_KEY. Set LLM_BACKEND=fake "
                "(the default) to run with no credentials."
            )
            raise LLMNotConfiguredError(msg)
        msg = (
            "The Gemini backend arrives in Phase 3. Set LLM_BACKEND=fake (the "
            "default) to run against the deterministic fake model. See ROADMAP.md."
        )
        raise LLMNotConfiguredError(msg)

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "gemini"

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> NoReturn:
        """Not implemented in Phase 1."""
        _ = (task, prompt, schema)
        msg = "The Gemini backend arrives in Phase 3."
        raise LLMNotConfiguredError(msg)
