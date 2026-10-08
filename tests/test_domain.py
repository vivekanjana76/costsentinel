"""Domain-model tests.

The interesting ones here are not "does the model accept good data" but "does it
*refuse* the things CLAUDE.md's golden rules forbid". The no-invented-numbers rule
is enforced by :class:`MoneyAmount`, so these tests are where that guarantee is
actually pinned down.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from costsentinel.domain.common import (
    Currency,
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
    utc_now,
)
from costsentinel.domain.estate import (
    CostPoint,
    CostSeries,
    Environment,
    Estate,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    Subscription,
)
from costsentinel.domain.governance import (
    ApprovalDecision,
    AuditEvent,
    AuditEventType,
    Decision,
)
from costsentinel.domain.recommendations import (
    MONTHS_PER_YEAR,
    ActionClass,
    ActionType,
    SavingsEstimate,
)
from costsentinel.domain.report import ReportSeverity
from costsentinel.domain.signals import AnomalyDirection, Observation, WasteKind, WasteSignal
from costsentinel.domain.state import ScanState

PROVIDER_PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER,
    reference="test",
    verification=Verification.VERIFIED,
)


# ---------------------------------------------------------------------------
# MoneyAmount: where "never invent numbers" is enforced
# ---------------------------------------------------------------------------


def test_money_amount_refuses_llm_provenance() -> None:
    """A monetary amount may never carry LLM provenance (golden rule 5)."""
    with pytest.raises(ValidationError, match="may not have LLM provenance"):
        MoneyAmount(
            amount=Decimal("100.00"),
            provenance=Provenance(source=ProvenanceSource.LLM_INFERENCE),
        )


def test_money_amount_unknown_must_be_none_not_zero() -> None:
    """An unknown cost cannot be smuggled in as zero."""
    with pytest.raises(ValidationError, match="must be None exactly when"):
        MoneyAmount(amount=Decimal(0), provenance=Provenance.unknown("not measured"))


def test_money_amount_known_cannot_claim_unknown_verification() -> None:
    """A known amount cannot be labelled unknown."""
    with pytest.raises(ValidationError, match="must be None exactly when"):
        MoneyAmount(amount=None, provenance=PROVIDER_PROVENANCE)


def test_money_amount_rejects_negative() -> None:
    """Costs and savings are non-negative; a sign error is a validation error."""
    with pytest.raises(ValidationError, match="non-negative"):
        MoneyAmount(amount=Decimal("-1.00"), provenance=PROVIDER_PROVENANCE)


def test_money_amount_undetermined_is_explicitly_unknown() -> None:
    unknown = MoneyAmount.undetermined("metric not collected")
    assert not unknown.is_known
    assert unknown.amount is None
    assert unknown.provenance.verification is Verification.UNKNOWN
    assert "unknown" in unknown.display()


def test_money_amount_display_formats_known_value() -> None:
    known = MoneyAmount.of(Decimal("1234.5"), provenance=PROVIDER_PROVENANCE)
    assert known.display() == "1,234.50 USD"
    assert known.is_known


def test_money_amount_respects_currency() -> None:
    amount = MoneyAmount.of(Decimal("10.00"), provenance=PROVIDER_PROVENANCE, currency=Currency.QAR)
    assert amount.display().endswith("QAR")


def test_provenance_helpers() -> None:
    calculated = Provenance.calculated("a - b")
    assert calculated.source is ProvenanceSource.CALCULATION
    assert calculated.verification is Verification.VERIFIED
    assert not calculated.is_model_derived

    unknown = Provenance.unknown("no data")
    assert unknown.verification is Verification.UNKNOWN

    model = Provenance(source=ProvenanceSource.LLM_INFERENCE)
    assert model.is_model_derived


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is not None


def test_frozen_facts_are_immutable() -> None:
    """Facts do not change after observation."""
    amount = MoneyAmount.of(Decimal("1.00"), provenance=PROVIDER_PROVENANCE)
    with pytest.raises(ValidationError):
        amount.amount = Decimal("2.00")  # type: ignore[misc]


def test_frozen_facts_forbid_unexpected_fields() -> None:
    """An unexpected field is a validation error, not silently carried data."""
    with pytest.raises(ValidationError):
        MoneyAmount.model_validate(
            {
                "amount": "1.00",
                "provenance": PROVIDER_PROVENANCE.model_dump(),
                "injected": "surprise",
            }
        )


# ---------------------------------------------------------------------------
# SavingsEstimate
# ---------------------------------------------------------------------------


def test_savings_annualises_a_known_monthly_figure() -> None:
    monthly = MoneyAmount.of(Decimal("100.00"), provenance=Provenance.calculated("x"))
    estimate = SavingsEstimate.from_monthly(monthly, basis="test", is_estimated=False)
    assert estimate.annual.amount == Decimal("100.00") * MONTHS_PER_YEAR
    assert estimate.is_known


def test_savings_carries_unknown_through_to_annual() -> None:
    """An unknown monthly saving must not annualise into a confident zero."""
    estimate = SavingsEstimate.from_monthly(
        MoneyAmount.undetermined("cost not reported"), basis="test", is_estimated=False
    )
    assert not estimate.is_known
    assert estimate.annual.amount is None


def test_savings_undetermined_records_the_reason() -> None:
    estimate = SavingsEstimate.undetermined("no pricing available")
    assert estimate.basis == "no pricing available"
    assert not estimate.is_known


# ---------------------------------------------------------------------------
# ActionClass
# ---------------------------------------------------------------------------


def test_action_class_severity_ordering() -> None:
    assert ActionClass.ALLOW.severity < ActionClass.REVIEW.severity < ActionClass.BLOCK.severity


def test_action_class_at_least_takes_the_stricter() -> None:
    """The policy floor can only be tightened (ARCHITECTURE.md D8)."""
    assert ActionClass.ALLOW.at_least(ActionClass.REVIEW) is ActionClass.REVIEW
    assert ActionClass.BLOCK.at_least(ActionClass.ALLOW) is ActionClass.BLOCK
    assert ActionClass.REVIEW.at_least(ActionClass.REVIEW) is ActionClass.REVIEW


def test_action_class_approval_and_automation_flags() -> None:
    assert not ActionClass.ALLOW.requires_approval
    assert ActionClass.ALLOW.is_automatable

    assert ActionClass.REVIEW.requires_approval
    assert ActionClass.REVIEW.is_automatable

    assert ActionClass.BLOCK.requires_approval
    # A block has no execution path at all (ARCHITECTURE.md D9).
    assert not ActionClass.BLOCK.is_automatable


def test_action_vocabulary_is_closed() -> None:
    """An action outside the enum cannot be constructed."""
    with pytest.raises(ValueError, match="delete_everything"):
        ActionType("delete_everything")


# ---------------------------------------------------------------------------
# Estate
# ---------------------------------------------------------------------------


def _subscription(sub_id: str = "sub-1", client: str = "acme") -> Subscription:
    return Subscription(
        subscription_id=sub_id,
        display_name="Test",
        client=client,
        environment=Environment.DEVELOPMENT,
        provenance=PROVIDER_PROVENANCE,
    )


def _resource(name: str = "disk-1", sub_id: str = "sub-1") -> Resource:
    return Resource(
        resource_id=f"/subscriptions/{sub_id}/resources/{name}",
        name=name,
        kind=ResourceKind.MANAGED_DISK,
        subscription_id=sub_id,
        resource_group="rg-1",
        region="eastus",
        state=ResourceState.UNATTACHED,
        monthly_cost=MoneyAmount.of(Decimal("10.00"), provenance=PROVIDER_PROVENANCE),
        provenance=PROVIDER_PROVENANCE,
    )


def test_cost_series_observed_spend_sums_provider_dailies() -> None:
    series = CostSeries(
        subscription_id="sub-1",
        points=(
            CostPoint(day=datetime(2026, 9, 1, tzinfo=UTC).date(), amount=Decimal("10.00")),
            CostPoint(day=datetime(2026, 9, 2, tzinfo=UTC).date(), amount=Decimal("12.50")),
        ),
        provenance=PROVIDER_PROVENANCE,
    )
    spend = series.observed_spend()
    assert spend.amount == Decimal("22.50")
    assert spend.provenance.source is ProvenanceSource.CALCULATION
    assert series.window_days == 2


def test_estate_lookups() -> None:
    resource = _resource()
    metrics = ResourceMetrics(
        resource_id=resource.resource_id,
        observation_days=30,
        cpu_avg_pct=Decimal("5"),
        provenance=PROVIDER_PROVENANCE,
    )
    estate = Estate(
        client="acme",
        subscriptions=(_subscription(),),
        resources=(resource,),
        metrics=(metrics,),
    )
    assert estate.resource(resource.resource_id) == resource
    assert estate.resource("missing") is None
    assert estate.metrics_for(resource.resource_id) == metrics
    assert estate.metrics_for("missing") is None
    assert list(estate.resources_in("sub-1")) == [resource]
    assert list(estate.resources_in("sub-2")) == []


def test_estate_spend_is_unknown_without_a_cost_series() -> None:
    """No cost data reports unknown, never zero -- a zero would understate spend."""
    estate = Estate(client="acme", subscriptions=(_subscription(),), resources=())
    assert not estate.observed_spend().is_known


def test_estate_spend_sums_across_subscriptions() -> None:
    def series(sub_id: str, amount: str) -> CostSeries:
        return CostSeries(
            subscription_id=sub_id,
            points=(
                CostPoint(day=datetime(2026, 9, 1, tzinfo=UTC).date(), amount=Decimal(amount)),
            ),
            provenance=PROVIDER_PROVENANCE,
        )

    estate = Estate(
        client="acme",
        subscriptions=(_subscription("sub-1"), _subscription("sub-2")),
        resources=(),
        cost_series=(series("sub-1", "100.00"), series("sub-2", "50.00")),
    )
    assert estate.observed_spend().amount == Decimal("150.00")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def test_waste_signal_evidence_lookup() -> None:
    signal = WasteSignal(
        signal_id="ws-1",
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        client="acme",
        subscription_id="sub-1",
        resource_id="/r/1",
        resource_name="disk-1",
        resource_kind="managed_disk",
        evidence=(Observation(name="state", value="unattached", provenance=PROVIDER_PROVENANCE),),
        monthly_cost=MoneyAmount.of(Decimal("10.00"), provenance=PROVIDER_PROVENANCE),
        confidence=Decimal("0.95"),
        detector="test/v1",
        provenance=PROVIDER_PROVENANCE,
    )
    assert signal.evidence_value("state") == "unattached"
    assert signal.evidence_value("absent") is None


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        WasteSignal(
            signal_id="ws-1",
            kind=WasteKind.ORPHANED_MANAGED_DISK,
            client="acme",
            subscription_id="sub-1",
            resource_id="/r/1",
            resource_name="disk-1",
            resource_kind="managed_disk",
            evidence=(),
            monthly_cost=MoneyAmount.undetermined("n/a"),
            confidence=Decimal("1.5"),
            detector="test/v1",
            provenance=PROVIDER_PROVENANCE,
        )


def test_anomaly_direction_values() -> None:
    assert AnomalyDirection.INCREASE.value == "increase"
    assert AnomalyDirection.DECREASE.value == "decrease"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_approval_decision_authorises_only_when_approved() -> None:
    approved = ApprovalDecision(
        recommendation_id="rec-1", decision=Decision.APPROVED, approver="ops@example.com"
    )
    rejected = ApprovalDecision(
        recommendation_id="rec-1", decision=Decision.REJECTED, approver="ops@example.com"
    )
    assert approved.is_approved
    assert not rejected.is_approved


def test_decision_has_no_timeout_means_approved_member() -> None:
    """Absence of a decision is never authorisation (ARCHITECTURE.md section 8)."""
    assert {d.value for d in Decision} == {"approved", "rejected"}


def test_audit_event_detail_is_flat_strings() -> None:
    event = AuditEvent(
        event_id="run-1-0000",
        run_id="run-1",
        event_type=AuditEventType.SIGNAL_DETECTED,
        actor="anomaly_scout",
        subject="ws-1",
        detail={"kind": "orphaned_managed_disk"},
    )
    assert event.detail["kind"] == "orphaned_managed_disk"


# ---------------------------------------------------------------------------
# ScanState
# ---------------------------------------------------------------------------


def test_scan_state_defaults_are_empty_not_none() -> None:
    state = ScanState(run_id="run-1", client="acme")
    assert state.signals == ()
    assert state.recommendations == ()
    assert state.report is None
    assert state.audit == ()
    assert state.requested_by == "system"


def test_scan_state_lookups_return_none_when_absent() -> None:
    state = ScanState(run_id="run-1", client="acme")
    assert state.classification_for("rec-1") is None
    assert state.decision_for("rec-1") is None
    assert state.signal("ws-1") is None


def test_scan_state_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ScanState.model_validate({"run_id": "r", "client": "c", "unexpected": 1})


def test_report_severity_values() -> None:
    assert {s.value for s in ReportSeverity} == {"none", "low", "moderate", "high"}
