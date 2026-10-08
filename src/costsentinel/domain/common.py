"""Provenance, verification and money primitives shared by every domain type.

This module is where CLAUDE.md golden rule 5 ("never invent numbers") stops being a
prompt instruction and becomes a type-system guarantee. Every externally derived
fact carries a :class:`Provenance`, and :class:`MoneyAmount` structurally refuses to
hold a value whose source is a language model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used everywhere instead of ``datetime.now()`` so that timestamps are
    comparable across a sweep and serialise unambiguously in an audit trail.
    """
    return datetime.now(UTC)


class ProvenanceSource(StrEnum):
    """Where a fact came from.

    The distinction that matters most is ``LLM_INFERENCE`` versus everything else:
    a model may explain and rank, but no quantity may originate with it.
    """

    MOCK_PROVIDER = "mock_provider"
    AZURE_COST_MANAGEMENT = "azure_cost_management"
    AZURE_RESOURCE_GRAPH = "azure_resource_graph"
    AZURE_MONITOR = "azure_monitor"
    AZURE_ADVISOR = "azure_advisor"
    AZURE_RETAIL_PRICES = "azure_retail_prices"
    POLICY_STORE = "policy_store"
    CALCULATION = "calculation"
    LLM_INFERENCE = "llm_inference"
    OPERATOR = "operator"


class Verification(StrEnum):
    """How much trust a fact has earned.

    ``UNKNOWN`` is a first-class value, not an error state. A missing cost is
    ``None`` with ``UNKNOWN`` verification, never ``0.0`` -- a zero would silently
    understate waste, which in a cost tool is worse than admitting ignorance.
    """

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class Frozen(BaseModel):
    """Base for immutable domain facts.

    Facts do not change after observation; only :class:`~costsentinel.domain.state.ScanState`
    is mutable. ``extra="forbid"`` means an unexpected field from an LLM response or
    a provider payload is a validation error rather than silently carried data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Provenance(Frozen):
    """Where a fact came from, when, and how far it is trusted."""

    source: ProvenanceSource
    retrieved_at: datetime = Field(default_factory=utc_now)
    reference: str | None = Field(
        default=None,
        description="API path, query, policy rule id, or calculation expression.",
    )
    verification: Verification = Verification.UNVERIFIED

    @property
    def is_model_derived(self) -> bool:
        """Whether this fact originated with a language model."""
        return self.source is ProvenanceSource.LLM_INFERENCE

    @classmethod
    def calculated(cls, expression: str) -> Provenance:
        """Provenance for a value derived arithmetically from other known values."""
        return cls(
            source=ProvenanceSource.CALCULATION,
            reference=expression,
            verification=Verification.VERIFIED,
        )

    @classmethod
    def unknown(cls, reason: str) -> Provenance:
        """Provenance for a value we could not determine."""
        return cls(
            source=ProvenanceSource.CALCULATION,
            reference=reason,
            verification=Verification.UNKNOWN,
        )


class Currency(StrEnum):
    """Billing currencies CostSentinel reports in."""

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    QAR = "QAR"
    AED = "AED"


#: A probability-like confidence score.
Confidence = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]

#: A non-negative percentage, as reported by a metrics provider.
Percentage = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(100))]


class MoneyAmount(Frozen):
    """A monetary quantity with a known origin, or an explicit unknown.

    Three invariants are enforced here rather than trusted to callers:

    1. The source may never be :attr:`ProvenanceSource.LLM_INFERENCE`. A model
       cannot produce a number in this system, because this type will not hold one.
    2. ``amount is None`` if and only if verification is
       :attr:`Verification.UNKNOWN`. There is no way to express "unknown" as zero.
    3. Amounts are non-negative. A saving is expressed as a positive amount; a cost
       increase is a separate, explicitly signed concept.
    """

    amount: Decimal | None
    currency: Currency = Currency.USD
    provenance: Provenance

    @model_validator(mode="after")
    def _enforce_invariants(self) -> Self:
        if self.provenance.source is ProvenanceSource.LLM_INFERENCE:
            msg = (
                "A monetary amount may not have LLM provenance. Cost and savings "
                "figures must come from provider data or explicit calculation "
                "(CLAUDE.md golden rule 5)."
            )
            raise ValueError(msg)

        is_unknown = self.provenance.verification is Verification.UNKNOWN
        if (self.amount is None) is not is_unknown:
            msg = (
                "amount must be None exactly when verification is 'unknown'; got "
                f"amount={self.amount!r}, verification={self.provenance.verification}."
            )
            raise ValueError(msg)

        if self.amount is not None and self.amount < 0:
            msg = f"A monetary amount must be non-negative; got {self.amount}."
            raise ValueError(msg)

        return self

    @property
    def is_known(self) -> bool:
        """Whether this amount has a determined value."""
        return self.amount is not None

    def display(self) -> str:
        """Render for a human, making an unknown value visibly unknown."""
        if self.amount is None:
            return f"unknown {self.currency.value}"
        return f"{self.amount:,.2f} {self.currency.value}"

    @classmethod
    def of(
        cls,
        amount: Decimal,
        *,
        provenance: Provenance,
        currency: Currency = Currency.USD,
    ) -> MoneyAmount:
        """Build a known amount."""
        return cls(amount=amount, currency=currency, provenance=provenance)

    @classmethod
    def undetermined(cls, reason: str, *, currency: Currency = Currency.USD) -> MoneyAmount:
        """Build an explicitly unknown amount, carrying why it is unknown."""
        return cls(amount=None, currency=currency, provenance=Provenance.unknown(reason))
