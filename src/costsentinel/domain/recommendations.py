"""Proposals: the action vocabulary, action classes, savings and recommendations.

:class:`ActionType` is a *closed* enum (ARCHITECTURE.md D18). That is the deepest
layer of prompt-injection defence in the system: even a fully compromised model
response cannot name an operation CostSentinel does not already permit and
classify, because an unrecognised action fails schema validation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from costsentinel.domain.common import (
    Confidence,
    Frozen,
    MoneyAmount,
    Provenance,
    utc_now,
)

#: Months in a year, used to annualise a monthly saving.
MONTHS_PER_YEAR = 12


class ActionType(StrEnum):
    """Every remediation CostSentinel is able to propose.

    Nothing outside this enum can be proposed, classified or executed.
    """

    NO_ACTION = "no_action"
    APPLY_TAG = "apply_tag"
    NOTIFY_OWNER = "notify_owner"
    SNAPSHOT_MANAGED_DISK = "snapshot_managed_disk"
    DEALLOCATE_VIRTUAL_MACHINE = "deallocate_virtual_machine"
    RESIZE_VIRTUAL_MACHINE = "resize_virtual_machine"
    DELETE_ORPHANED_MANAGED_DISK = "delete_orphaned_managed_disk"
    DELETE_UNATTACHED_PUBLIC_IP = "delete_unattached_public_ip"
    DELETE_UNCONFIRMED_RESOURCE = "delete_unconfirmed_resource"


class ActionClass(StrEnum):
    """How much human involvement an action requires.

    ``BLOCK`` is not "approval by a more senior person" -- it means the action has no
    execution path in CostSentinel at all and is reported as advice for a human to
    perform by hand (ARCHITECTURE.md D9).
    """

    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"

    @property
    def severity(self) -> int:
        """Ordering for "at least as strict as" comparisons."""
        return _CLASS_SEVERITY[self]

    def at_least(self, other: ActionClass) -> ActionClass:
        """Return whichever of the two classes is stricter.

        Used to enforce the policy floor: a client may tighten a class, never loosen
        it below the built-in minimum (ARCHITECTURE.md D8).
        """
        return self if self.severity >= other.severity else other

    @property
    def requires_approval(self) -> bool:
        """Whether an explicit human decision is needed before proceeding."""
        return self is not ActionClass.ALLOW

    @property
    def is_automatable(self) -> bool:
        """Whether CostSentinel may ever execute this action itself."""
        return self is not ActionClass.BLOCK


_CLASS_SEVERITY: dict[ActionClass, int] = {
    ActionClass.ALLOW: 0,
    ActionClass.REVIEW: 1,
    ActionClass.BLOCK: 2,
}


class SavingsEstimate(Frozen):
    """A defensible monthly and annual saving for one recommendation.

    Both amounts are :class:`~costsentinel.domain.common.MoneyAmount`, which refuses
    LLM provenance -- so a saving is always either arithmetic over provider data or
    explicitly unknown. ``basis`` states the arithmetic in words so a client (or an
    auditor) can check it.
    """

    monthly: MoneyAmount
    annual: MoneyAmount
    basis: str = Field(description="Plain-language statement of how the figure was derived.")
    is_estimated: bool = Field(
        description=(
            "True when the figure depends on a modelling assumption (for example a "
            "rightsizing target); False when it is the observed cost of something "
            "being removed outright."
        )
    )

    @property
    def is_known(self) -> bool:
        """Whether a monthly saving was determined."""
        return self.monthly.is_known

    @classmethod
    def from_monthly(
        cls,
        monthly: MoneyAmount,
        *,
        basis: str,
        is_estimated: bool,
    ) -> SavingsEstimate:
        """Annualise a known monthly saving, or carry the unknown through."""
        if monthly.amount is None:
            return cls(
                monthly=monthly,
                annual=MoneyAmount.undetermined(
                    "monthly saving unknown, so annual saving is unknown",
                    currency=monthly.currency,
                ),
                basis=basis,
                is_estimated=is_estimated,
            )
        annual = MoneyAmount.of(
            monthly.amount * Decimal(MONTHS_PER_YEAR),
            currency=monthly.currency,
            provenance=Provenance.calculated(
                f"monthly saving {monthly.amount} x {MONTHS_PER_YEAR} months"
            ),
        )
        return cls(monthly=monthly, annual=annual, basis=basis, is_estimated=is_estimated)

    @classmethod
    def undetermined(cls, reason: str) -> SavingsEstimate:
        """An explicitly unknown saving, carrying why."""
        return cls(
            monthly=MoneyAmount.undetermined(reason),
            annual=MoneyAmount.undetermined(reason),
            basis=reason,
            is_estimated=False,
        )


class Recommendation(Frozen):
    """A ranked, costed, classified proposal to remediate one waste signal.

    ``rationale`` is prose and may be model-derived; ``rationale_provenance`` records
    that. ``savings`` can never be model-derived. Keeping the two provenances
    separate is what makes "the model explained it, the arithmetic costed it"
    checkable rather than merely asserted.
    """

    recommendation_id: str
    signal_id: str
    client: str
    subscription_id: str
    target_resource_id: str
    target_resource_name: str
    action: ActionType
    risk_class: ActionClass
    rationale: str
    rationale_provenance: Provenance
    preconditions: tuple[str, ...] = Field(
        description="Conditions re-checked immediately before any execution.",
    )
    savings: SavingsEstimate
    confidence: Confidence
    rank: int = Field(ge=1, description="1 is the highest-value recommendation.")
    proposed_at: datetime = Field(default_factory=utc_now)
