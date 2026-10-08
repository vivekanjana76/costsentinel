"""Diagnosis and ranking: why a signal exists, and why it ranks where it does.

Both types here keep model-authored prose and computed numbers in separate fields
with separate provenance. A :class:`RootCause` narrative and a
:class:`RankingScore` rationale may be model-derived; the ranking components and the
composite score never are.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import Confidence, Frozen, Provenance, utc_now


class FactorKind(StrEnum):
    """What sort of thing contributed to a root cause.

    A closed vocabulary, so the Root-Cause Analyst classifies contributing factors
    rather than inventing categories, and so a report can group them.
    """

    LIFECYCLE = "lifecycle"
    CONFIGURATION = "configuration"
    UTILISATION = "utilisation"
    OWNERSHIP = "ownership"
    PROCUREMENT = "procurement"
    UNKNOWN = "unknown"


class ContributingFactor(Frozen):
    """One cited reason a signal exists.

    ``evidence_name`` must name an :class:`~costsentinel.domain.signals.Observation`
    that is actually attached to the signal. That is what makes groundedness
    checkable: a factor citing evidence the signal does not carry is rejected rather
    than reported.
    """

    kind: FactorKind
    statement: str = Field(min_length=1, max_length=500)
    evidence_name: str = Field(
        min_length=1,
        description="Name of the observation on the signal that supports this factor.",
    )


class RootCause(Frozen):
    """Why one waste signal exists.

    The narrative is prose and may come from a model, which
    ``narrative_provenance`` records. The factors are constrained to cite evidence
    already on the signal, so the explanation cannot introduce a new fact.
    """

    signal_id: str
    narrative: str = Field(min_length=1, max_length=2000)
    narrative_provenance: Provenance
    factors: tuple[ContributingFactor, ...] = ()
    confidence: Confidence
    analysed_at: datetime = Field(default_factory=utc_now)

    @property
    def cited_evidence(self) -> tuple[str, ...]:
        """Names of the observations this explanation rests on."""
        return tuple(factor.evidence_name for factor in self.factors)


class RankingScore(Frozen):
    """Why a recommendation ranks where it does.

    Every component and the composite are computed arithmetically from the savings
    estimate, the action class and the detector confidence. The ``rationale`` is the
    only model-derived field, and it explains the ordering rather than deciding it --
    a hostile model can change the wording and cannot change the rank.
    """

    recommendation_id: str
    savings_component: Decimal = Field(
        ge=Decimal(0),
        le=Decimal(1),
        description="Monthly saving as a share of the largest in this run; 0 if unpriced.",
    )
    risk_component: Decimal = Field(
        ge=Decimal(0),
        le=Decimal(1),
        description="Higher is safer. Derived from the action class, not from savings.",
    )
    confidence_component: Decimal = Field(
        ge=Decimal(0), le=Decimal(1), description="The detector's own confidence."
    )
    composite: Decimal = Field(ge=Decimal(0), le=Decimal(1))
    rationale: str = Field(default="", max_length=800)
    rationale_provenance: Provenance | None = None
    provenance: Provenance

    def explain(self) -> str:
        """A compact, human-readable breakdown of the computed components."""
        return (
            f"savings {self.savings_component:.3f}, "
            f"safety {self.risk_component:.3f}, "
            f"confidence {self.confidence_component:.3f} "
            f"-> {self.composite:.3f}"
        )
