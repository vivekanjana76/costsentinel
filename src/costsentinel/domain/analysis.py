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

#: Findings whose monthly saving was quantified. They rank above everything else.
TIER_QUANTIFIED = 0

#: Findings with no quantified saving: an unpriceable rightsizing target, a resource
#: missing from the estate snapshot, a spend anomaly. Reported in full, ranked last.
TIER_UNQUANTIFIED = 1


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
    guidance_citations: tuple[str, ...] = Field(
        default=(),
        description=(
            "Semantic-memory passages this explanation was shown, by citation. "
            "Recorded so a client can see which policy or guidance the reasoning "
            "rested on -- and so an eval can check that a cited rule exists. "
            "Retrieved guidance is evidence like any other: it may ground a "
            "narrative and cannot introduce a quantity."
        ),
    )

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

    ``savings_known`` is not a fourth component. It is a *tier*: every finding with a
    quantified saving ranks above every finding without one, whatever their composites
    say (ARCHITECTURE.md D104). Keeping it out of the weighted sum is deliberate --
    expressed as a weight, the rule would hold only for the current weights and would
    break silently the first time somebody retuned them.
    """

    recommendation_id: str
    savings_known: bool = Field(
        description=(
            "Whether a monthly saving was quantified. Decides the ranking tier: an "
            "unquantified finding ranks below every quantified one, because a FinOps "
            "tool must not assert that the thing it cannot cost is the bigger "
            "opportunity."
        )
    )
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

    @property
    def tier(self) -> int:
        """Which ranking tier this finding belongs to.

        The boundary is whether a number exists, not how big it is. A one-dollar
        saving is in the upper tier and an unquantifiable one is not, because the
        claim being made is "we can tell you what this is worth" -- and that claim is
        either true or it is not.
        """
        return TIER_QUANTIFIED if self.savings_known else TIER_UNQUANTIFIED

    @property
    def tier_label(self) -> str:
        """The ranking tier in words, for a report a client reads."""
        if self.savings_known:
            return "quantified"
        return "unquantified (ranks below every priced finding)"

    def order_key(self, *, composite: Decimal | None = None) -> tuple[int, Decimal, Decimal, str]:
        """This finding's position in the total order, highest-ranked first.

        Four keys, in this order and for these reasons:

        1. **the tier** -- a quantified saving outranks an unquantified one, always;
        2. **the composite** -- the weighted trade-off of savings, safety and
           confidence, which orders findings within a tier;
        3. **safety** -- so a genuine tie between two comparable known magnitudes
           breaks toward the more reversible action. Safety is already inside the
           composite; this is the fallback for when the composites come out equal,
           and it is what "reversibility breaks ties" means literally;
        4. **the recommendation id** -- so the order is total and reproducible rather
           than dependent on input ordering.

        Args:
            composite: Overrides the stored composite. Used by the memory consult
                step, which re-orders on a penalised composite. The tier and the
                safety component are *not* overridable, which is what makes "a
                penalty moves a finding within its tier and never between tiers"
                (ARCHITECTURE.md D68, D104) a property of this method rather than a
                convention its callers are trusted to honour.

        The order lives here, on the type, rather than in
        :mod:`costsentinel.agents.ranking` where the weights are. Two callers need it
        -- the planner and the memory consult step -- and if memory had to import the
        ranking module to get it, ``memory`` would depend on ``agents`` while
        ``agents`` already depends on ``memory``. One definition, in the package both
        can depend on.
        """
        return (
            self.tier,
            -(self.composite if composite is None else composite),
            -self.risk_component,
            self.recommendation_id,
        )

    def explain(self) -> str:
        """A compact, human-readable breakdown of the computed components.

        States the tier first, because it dominates the composite. A breakdown that
        showed only the weighted score would leave a client unable to explain why a
        finding with a higher-looking composite sits lower.
        """
        return (
            f"{self.tier_label}; savings {self.savings_component:.3f}, "
            f"safety {self.risk_component:.3f}, "
            f"confidence {self.confidence_component:.3f} "
            f"-> {self.composite:.3f}"
        )
