"""Defensive paths and declared-but-unimplemented seams.

These are the branches that only fire when something is wrong or absent. They are
tested because that is exactly when they matter: a stub that silently returns
``None`` instead of refusing, or a severity that quietly reads as "none" when spend
is unknown, is the kind of defect nobody notices until a client sees it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from costsentinel.agents.report_author import (
    SEVERITY_HIGH_RATIO,
    SEVERITY_MODERATE_RATIO,
    _period,
    _resource_name,
    _severity,
)
from costsentinel.config import Settings
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import Environment, Estate
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.report import ReportSeverity
from costsentinel.domain.signals import AnomalyDirection, CostAnomaly
from costsentinel.domain.state import ScanState

PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER, reference="test", verification=Verification.VERIFIED
)


def _money(amount: str) -> MoneyAmount:
    return MoneyAmount.of(Decimal(amount), provenance=PROVENANCE)


# ---------------------------------------------------------------------------
# Report severity
# ---------------------------------------------------------------------------


def test_severity_is_none_without_findings() -> None:
    assert _severity(_money("0.01"), _money("1000"), 0) is ReportSeverity.NONE


def test_severity_scales_with_savings_as_a_share_of_spend() -> None:
    spend = _money("1000.00")
    assert _severity(_money("300.00"), spend, 1) is ReportSeverity.HIGH
    assert _severity(_money("100.00"), spend, 1) is ReportSeverity.MODERATE
    assert _severity(_money("10.00"), spend, 1) is ReportSeverity.LOW


def test_severity_thresholds_are_inclusive_at_the_boundary() -> None:
    spend = _money("1000.00")
    at_high = MoneyAmount.of(Decimal("1000.00") * SEVERITY_HIGH_RATIO, provenance=PROVENANCE)
    at_moderate = MoneyAmount.of(
        Decimal("1000.00") * SEVERITY_MODERATE_RATIO, provenance=PROVENANCE
    )
    assert _severity(at_high, spend, 1) is ReportSeverity.HIGH
    assert _severity(at_moderate, spend, 1) is ReportSeverity.MODERATE


def test_severity_is_low_when_the_share_cannot_be_computed() -> None:
    """Findings exist, so "none" would be wrong; the share is simply unknown."""
    unknown = MoneyAmount.undetermined("not measured")
    assert _severity(unknown, _money("1000"), 1) is ReportSeverity.LOW
    assert _severity(_money("100"), unknown, 1) is ReportSeverity.LOW
    assert _severity(_money("100"), _money("0"), 1) is ReportSeverity.LOW


# ---------------------------------------------------------------------------
# Report period
# ---------------------------------------------------------------------------


def test_period_falls_back_to_the_estate_timestamp_without_a_cost_series() -> None:
    state = ScanState(
        run_id="run-1", client="acme", estate=Estate(client="acme", subscriptions=(), resources=())
    )
    period = _period(state)
    assert period.days == 30
    assert period.start < period.end


def test_period_falls_back_to_the_request_time_without_an_estate() -> None:
    period = _period(ScanState(run_id="run-1", client="acme"))
    assert period.days == 30


def test_resource_name_falls_back_to_unknown() -> None:
    state = ScanState(run_id="run-1", client="acme")
    assert _resource_name(state, "rec-missing") == "unknown"


# ---------------------------------------------------------------------------
# Policy Guard: the planner/guard divergence check
# ---------------------------------------------------------------------------


def test_policy_guard_records_a_divergence_from_the_planner(settings: Settings) -> None:
    """If the two ever disagree, the classification stands and the divergence is logged."""
    from costsentinel.agents.policy_guard import make_policy_guard
    from costsentinel.domain.recommendations import Recommendation, SavingsEstimate

    # A recommendation that claims to be ALLOW for an action whose floor is REVIEW.
    recommendation = Recommendation(
        recommendation_id="rec-1",
        signal_id="ws-1",
        client="acme",
        subscription_id="sub-1",
        target_resource_id="/r/1",
        target_resource_name="disk-1",
        action=ActionType.DELETE_ORPHANED_MANAGED_DISK,
        risk_class=ActionClass.ALLOW,
        rationale="mislabelled on purpose",
        rationale_provenance=Provenance(source=ProvenanceSource.LLM_INFERENCE),
        preconditions=("x.",),
        savings=SavingsEstimate.undetermined("n/a"),
        confidence=Decimal("0.9"),
        rank=1,
    )
    state = ScanState(run_id="run-1", client="acme", recommendations=(recommendation,))

    update = make_policy_guard(settings=settings)(state)

    classification = update["classifications"][0]
    assert classification.action_class is ActionClass.REVIEW
    assert any("but classifies as review" in error for error in update["errors"])


def test_policy_guard_handles_an_unknown_subscription_environment(settings: Settings) -> None:
    """No estate in state means UNKNOWN environment, not a crash."""
    from costsentinel.agents.policy_guard import make_policy_guard

    update = make_policy_guard(settings=settings)(ScanState(run_id="run-1", client="acme"))
    assert update["classifications"] == ()
    assert update["approval_requests"] == ()


def test_environment_enum_covers_the_lifecycle() -> None:
    assert {e.value for e in Environment} >= {"production", "development", "unknown"}


# ---------------------------------------------------------------------------
# CostAnomaly (modelled in Phase 1, emitted in Phase 3)
# ---------------------------------------------------------------------------


def _anomaly(baseline: str | None, delta: str | None) -> CostAnomaly:
    return CostAnomaly(
        anomaly_id="ca-1",
        client="acme",
        subscription_id="sub-1",
        service="Microsoft.Storage",
        direction=AnomalyDirection.INCREASE,
        baseline=_money(baseline) if baseline else MoneyAmount.undetermined("no baseline"),
        observed=_money("1200.00"),
        delta=_money(delta) if delta else MoneyAmount.undetermined("no delta"),
        window_days=7,
        confidence=Decimal("0.8"),
        detector="egress/v1",
        provenance=PROVENANCE,
    )


def test_anomaly_delta_fraction_is_computed_from_known_values() -> None:
    anomaly = _anomaly("1000.00", "200.00")
    assert anomaly.delta_is_known
    assert anomaly.delta_fraction == Decimal("0.2")


def test_anomaly_delta_fraction_is_none_when_either_side_is_unknown() -> None:
    assert _anomaly(None, "200.00").delta_fraction is None
    assert _anomaly("1000.00", None).delta_fraction is None
    assert not _anomaly("1000.00", None).delta_is_known


def test_anomaly_delta_fraction_is_none_against_a_zero_baseline() -> None:
    """Division by a zero baseline is undefined, not infinite."""
    assert _anomaly("0.00", "200.00").delta_fraction is None


# ---------------------------------------------------------------------------
# Declared-but-unimplemented seams must refuse, not return None
# ---------------------------------------------------------------------------


def test_real_azure_provider_methods_all_refuse() -> None:
    """A stub that returned ``None`` would look like an empty estate."""
    from costsentinel.providers.azure import AzureLiveProvider
    from costsentinel.providers.base import ProviderNotConfiguredError

    stub = AzureLiveProvider.__new__(AzureLiveProvider)
    assert stub.name == "azure-live"

    with pytest.raises(ProviderNotConfiguredError, match="Phase 3"):
        stub.list_subscriptions()
    with pytest.raises(ProviderNotConfiguredError):
        stub.list_resources("sub-1")
    with pytest.raises(ProviderNotConfiguredError):
        stub.list_reservations()
    with pytest.raises(ProviderNotConfiguredError):
        stub.get_cost_series("sub-1")
    with pytest.raises(ProviderNotConfiguredError):
        stub.get_resource_metrics("/r/1")
    with pytest.raises(ProviderNotConfiguredError):
        stub.list_sku_prices("eastus")


def test_real_llm_adapters_refuse_to_answer() -> None:
    from costsentinel.llm.azure_openai import AzureOpenAILLM
    from costsentinel.llm.base import LLMNotConfiguredError, Prompt
    from costsentinel.llm.contracts import RemediationPlan
    from costsentinel.llm.gemini import GeminiLLM
    from costsentinel.llm.routing import TASK_PLANNING

    azure = AzureOpenAILLM.__new__(AzureOpenAILLM)
    assert azure.name == "azure-openai"
    with pytest.raises(LLMNotConfiguredError, match="Phase 3"):
        azure.structured(task=TASK_PLANNING, prompt=Prompt(instruction="x"), schema=RemediationPlan)

    gemini = GeminiLLM.__new__(GeminiLLM)
    assert gemini.name == "gemini"
    with pytest.raises(LLMNotConfiguredError, match="Phase 3"):
        gemini.structured(
            task=TASK_PLANNING, prompt=Prompt(instruction="x"), schema=RemediationPlan
        )


def test_mock_provider_prices_an_uncatalogued_vm_as_unknown() -> None:
    """The honest answer when a SKU has no catalogue price."""
    from costsentinel.domain.estate import ResourceKind, ResourceState
    from costsentinel.providers.mock import MockAzureProvider, _ResSpec

    provider = MockAzureProvider(seed=1337)
    spec = _ResSpec(
        name="vm-exotic",
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_index=0,
        resource_group="rg-core-prod",
        state=ResourceState.RUNNING,
        sku="Standard_Unlisted_v9",
    )
    priced = provider._resource_monthly(spec, "eastus")
    assert not priced.is_known
    assert "no catalogue price" in (priced.provenance.reference or "")
