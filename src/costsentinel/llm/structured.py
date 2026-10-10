"""What both real backends share: one validation gate and one retry.

Azure OpenAI and Gemini both have their own structured-output mechanism, and both
SDKs will hand back something shaped roughly like the requested schema. Neither is
trusted here. Whatever comes back goes through
:meth:`pydantic.BaseModel.model_validate` in this module, so there is exactly one
place where model output becomes a domain object -- which is what CLAUDE.md golden
rule 4 asks for, and what makes "the model cannot produce a number" checkable rather
than aspirational.

The retry exists because a real model occasionally returns something that does not
validate. Retrying is only worth doing if the second attempt knows what was wrong,
so the validation error is fed back as an additional instruction. It is not a
general-purpose retry: a transport failure is raised, not retried, because
CostSentinel has no way to tell a transient 503 from a misconfigured endpoint and
hammering the latter is worse than failing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from costsentinel.llm.base import LLMValidationError, Prompt, StructuredResponseT
from costsentinel.observability.logging import get_logger

_log = get_logger("llm.structured")

#: Appended to a retry, so the second attempt is told what was wrong with the first.
RETRY_PREFIX = (
    "Your previous response did not satisfy the required schema. Correct it. "
    "The validation error was:"
)


def validate(
    payload: object, schema: type[StructuredResponseT], *, backend: str
) -> StructuredResponseT:
    """Validate one backend response against the requested schema.

    Accepts whatever the SDK produced -- a parsed model, a mapping, or a JSON string
    -- and returns a validated instance of ``schema``. Nothing is salvaged from a
    partial parse: a response that does not validate is a failure, because a
    half-populated finding in a client report is worse than a missing one.

    Raises:
        LLMValidationError: If the payload does not validate.
    """
    if payload is None:
        msg = f"{backend} returned no content for schema {schema.__name__}"
        raise LLMValidationError(msg)
    try:
        if isinstance(payload, BaseModel):
            # Round-tripping through the schema rather than accepting the SDK's own
            # parse keeps one validation gate, not two trusted ones.
            return schema.model_validate(payload.model_dump())
        if isinstance(payload, str):
            return schema.model_validate_json(payload)
        return schema.model_validate(payload)
    except ValidationError as exc:
        msg = f"{backend} response did not satisfy {schema.__name__}: {exc}"
        raise LLMValidationError(msg) from exc


def retrying(
    *,
    send: Callable[[Prompt], Any],
    prompt: Prompt,
    schema: type[StructuredResponseT],
    backend: str,
    attempts: int,
) -> StructuredResponseT:
    """Call ``send``, validate the result, and retry a validation failure once more.

    Args:
        send: Performs one round trip and returns whatever the SDK gave back.
        prompt: The prompt to send. On a retry it is reissued with the validation
            error appended as an instruction.
        schema: What the response must satisfy.
        backend: Name used in log lines and error messages.
        attempts: Maximum round trips, at least one.

    Returns:
        A validated instance of ``schema``.

    Raises:
        LLMValidationError: If every attempt failed validation.
    """
    current = prompt
    last: LLMValidationError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return validate(send(current), schema, backend=backend)
        except LLMValidationError as exc:
            last = exc
            _log.warning(
                "model response failed schema validation",
                extra={
                    "backend": backend,
                    "schema": schema.__name__,
                    "attempt": attempt,
                    "attempts": attempts,
                },
            )
            current = _with_correction(prompt, exc)
    assert last is not None  # noqa: S101 - the loop runs at least once
    raise last


def _with_correction(prompt: Prompt, error: LLMValidationError) -> Prompt:
    """The same prompt, with the validation error appended to the instruction.

    The error text goes in the instruction rather than into an evidence block,
    because it is CostSentinel's own words rather than data from a provider -- the
    evidence fence is reserved for untrusted values and widening it would weaken it.
    """
    return prompt.model_copy(
        update={"instruction": f"{prompt.instruction}\n\n{RETRY_PREFIX} {error}"}
    )
