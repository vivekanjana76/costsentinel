"""Google Gemini: the secondary backend.

Opt-in via ``LLM_BACKEND=gemini``. Present as a second opinion and as insurance
against a single vendor: a report whose narration depends on one provider's
availability is a report that stops being producible when that provider has an
outage.

Gemini's structured-output mechanism differs from Azure OpenAI's -- a response
schema plus a JSON mime type, rather than a strict function schema -- but the
difference stops at this file. Both go through
:mod:`costsentinel.llm.structured`, so there is still exactly one place where model
output becomes a domain object.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportAttributeAccessIssue=false, reportMissingTypeStubs=false
# The google-genai SDK is an optional extra, genuinely absent at type-check time in
# CI. The pragma is scoped to this one file.

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from costsentinel.domain.observability import TokenUsage
from costsentinel.llm.azure_openai import SYSTEM_PROMPT
from costsentinel.llm.base import LLMError, LLMNotConfiguredError, Prompt
from costsentinel.llm.routing import resolve_route
from costsentinel.llm.structured import retrying
from costsentinel.observability.logging import get_logger

if TYPE_CHECKING:
    from costsentinel.config import Settings
    from costsentinel.llm.base import LLMTask, StructuredResponseT

_log = get_logger("llm.gemini")

INSTALL_HINT = "install the optional LLM extra with: uv sync --extra llm"


class _GenAIClient(Protocol):
    """The slice of the google-genai client this adapter uses."""

    @property
    def models(self) -> Any:
        """The models namespace."""
        ...


class GeminiLLM:
    """Structured reasoning against a Gemini model."""

    def __init__(self, settings: Settings, *, client: _GenAIClient | None = None) -> None:
        """Validate configuration and prepare a client.

        Args:
            settings: Resolved configuration.
            client: An already-built client, which is how tests drive this adapter.

        Raises:
            LLMNotConfiguredError: If required configuration is missing.
        """
        self._settings = settings
        self._client = client
        self._last_usage: TokenUsage | None = None

        missing = [
            name
            for name, value in (
                ("GEMINI_API_KEY", settings.gemini_api_key),
                ("GEMINI_MODEL", settings.gemini_model),
            )
            if not value
        ]
        if missing:
            msg = (
                f"LLM_BACKEND=gemini requires {', '.join(missing)}. Set "
                f"LLM_BACKEND=fake (the default) to run with no credentials."
            )
            raise LLMNotConfiguredError(msg)

    @property
    def name(self) -> str:
        """Short identifier for logs, traces and provenance references."""
        return "gemini"

    def last_usage(self) -> TokenUsage | None:
        """Tokens reported by the most recent call, or ``None`` if none were."""
        return self._last_usage

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        """Produce a validated instance of ``schema`` from the routed model."""
        route = resolve_route(task, self._settings)
        self._last_usage = None
        return retrying(
            send=lambda issued: self._send(issued, schema, route.model, route.temperature),
            prompt=prompt,
            schema=schema,
            backend=self.name,
            attempts=self._settings.llm_max_attempts,
        )

    def _send(
        self,
        prompt: Prompt,
        schema: type[StructuredResponseT],
        model: str,
        temperature: float,
    ) -> object:
        """One round trip. Returns whatever the SDK parsed, for validation upstream."""
        client = self._ensure_client()
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt.render(),
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "temperature": temperature,
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
        except Exception as exc:
            msg = f"the Gemini call to model {model!r} failed: {exc}"
            raise LLMError(msg) from exc

        self._last_usage = _usage_of(response)
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            return parsed
        # Falling back to the raw text is not a shortcut: it is validated upstream
        # exactly as a parsed object would be.
        return getattr(response, "text", None)

    def _ensure_client(self) -> _GenAIClient:
        client = self._client
        if client is not None:
            return client

        try:
            module = __import__("google.genai", fromlist=["Client"])
        except ImportError as exc:
            msg = f"the google-genai package is required for LLM_BACKEND=gemini; {INSTALL_HINT}"
            raise LLMNotConfiguredError(msg) from exc

        key = self._settings.gemini_api_key
        if key is None:  # pragma: no cover - construction already refused this
            msg = "LLM_BACKEND=gemini requires GEMINI_API_KEY"
            raise LLMNotConfiguredError(msg)

        _log.info("gemini client built from an api key")
        client = module.Client(api_key=key.get_secret_value())
        self._client = client
        return client


def _usage_of(response: Any) -> TokenUsage | None:
    """Token usage reported by a response, or ``None`` if it reported none.

    Gemini counts "prompt" and "candidates" where OpenAI counts "prompt" and
    "completion"; mapping them here keeps one shape in the trace rather than making
    every reader of a metric know which backend produced it.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_token_count", None)
    completion_tokens = getattr(usage, "candidates_token_count", None)
    if prompt_tokens is None or completion_tokens is None:
        return None
    return TokenUsage(prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens))
