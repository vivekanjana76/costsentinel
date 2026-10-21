"""Exemption through the whole graph: surfaced, annotated, and never actioned.

The claim this file exists to pin down is *not* "an exempt resource is skipped". It
is the stricter and more useful one: **an exempt resource is still detected, still
reported, still priced at its current cost, and simply never turned into a
recommendation.** Silently dropping the finding would be the easy implementation and
the dishonest one -- the client loses the one fact they might act on later, which is
that the standby costs them money every month whether or not they keep it.

The estate is built here rather than borrowed from the eval datasets, so these tests
keep holding when a dataset's labels change, and so the "every signal is exempt" case
can be constructed at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import (
    CostBreakdown,
    CostPoint,
    CostSeries,
    Environment,
    Reservation,
    Resource,
    ResourceKind,
    ResourceMetrics,
    ResourceState,
    SkuPrice,
    Subscription,
)
from costsentinel.domain.exemption import ExemptionReason
from costsentinel.domain.governance import AuditEventType, RouteAction
from costsentinel.domain.signals import WasteKind
from costsentinel.domain.state import ScanState
from costsentinel.graph.build import build_graph
from costsentinel.graph.runner import state_from
from costsentinel.llm.fake import FakeLLM

SUBSCRIPTION = "00000000-0000-4000-8000-0000000f0001"
CLIENT = "meridian-health"
REGION = "eastus"
AS_OF = datetime(2026, 6, 1, tzinfo=UTC)

#: D8s_v5 in eastus, from the committed catalogue. Used so the arithmetic in these
#: tests is the same arithmetic the rest of the system does.
D8S_V5_MONTHLY = Decimal("280.32")
D4S_V5_MONTHLY = Decimal("140.16")


def _provenance() -> Provenance:
    return Provenance(
        source=ProvenanceSource.MOCK_PROVIDER,
        retrieved_at=AS_OF,
        reference="tests/test_exemption_pipeline",
        verification=Verification.VERIFIED,
    )


def _idle_vm(name: str, *, tags: Mapping[str, str], monthly: Decimal = D8S_V5_MONTHLY) -> Resource:
    """A running virtual machine whose peak CPU never left the floor."""
    return Resource(
        resource_id=f"/subscriptions/{SUBSCRIPTION}/vm/{name}",
        name=name,
        kind=ResourceKind.VIRTUAL_MACHINE,
        subscription_id=SUBSCRIPTION,
        resource_group="rg-continuity",
        region=REGION,
        sku="Standard_D8s_v5" if monthly == D8S_V5_MONTHLY else "Standard_D4s_v5",
        state=ResourceState.RUNNING,
        tags=dict(tags),
        created_at=AS_OF - timedelta(days=540),
        monthly_cost=MoneyAmount.of(monthly, provenance=_provenance()),
        provenance=_provenance(),
    )


class _Estate:
    """A tiny provider over a handful of resources. Satisfies ``AzureProvider``."""

    def __init__(self, resources: Sequence[Resource]) -> None:
        self._resources = tuple(resources)

    @property
    def name(self) -> str:
        return "test-estate"

    def list_subscriptions(self, *, client: str | None = None) -> Sequence[Subscription]:
        if client is not None and client != CLIENT:
            return ()
        return (
            Subscription(
                subscription_id=SUBSCRIPTION,
                display_name="MH-PROD-Clinical",
                client=CLIENT,
                environment=Environment.PRODUCTION,
                provenance=_provenance(),
            ),
        )

    def list_resources(self, subscription_id: str) -> Sequence[Resource]:
        return tuple(r for r in self._resources if r.subscription_id == subscription_id)

    def list_reservations(self, *, client: str | None = None) -> Sequence[Reservation]:
        _ = client
        return ()

    def get_cost_series(self, subscription_id: str, *, window_days: int = 30) -> CostSeries:
        start = AS_OF.date() - timedelta(days=window_days - 1)
        return CostSeries(
            subscription_id=subscription_id,
            points=tuple(
                CostPoint(day=start + timedelta(days=offset), amount=Decimal("40.00"))
                for offset in range(window_days)
            ),
            provenance=_provenance(),
        )

    def get_cost_breakdown(
        self, subscription_id: str, *, window_days: int = 30
    ) -> CostBreakdown | None:
        """No breakdown. This estate's spend is flat, so there is nothing to attribute."""
        _ = (subscription_id, window_days)
        return None

    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None:
        if "/vm/" not in resource_id:
            return None
        return ResourceMetrics(
            resource_id=resource_id,
            observation_days=30,
            cpu_avg_pct=Decimal("0.8"),
            cpu_max_pct=Decimal("2.1"),
            network_in_gb=Decimal("0.0"),
            provenance=_provenance(),
        )

    def list_sku_prices(self, region: str) -> Sequence[SkuPrice]:
        return (
            SkuPrice(
                sku="Standard_D8s_v5",
                applies_to=ResourceKind.VIRTUAL_MACHINE,
                family="Dsv5",
                region=region,
                vcpu=8,
                monthly_cost=MoneyAmount.of(D8S_V5_MONTHLY, provenance=_provenance()),
            ),
            SkuPrice(
                sku="Standard_D4s_v5",
                applies_to=ResourceKind.VIRTUAL_MACHINE,
                family="Dsv5",
                region=region,
                vcpu=4,
                monthly_cost=MoneyAmount.of(D4S_V5_MONTHLY, provenance=_provenance()),
            ),
        )


def _settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path_factory.mktemp("state"),
    )


def _run(resources: Sequence[Resource], settings: Settings) -> ScanState:
    graph = build_graph(
        provider=_Estate(resources),
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
    )
    final = graph.invoke(
        ScanState(run_id="run-exempt-test", client=CLIENT),
        config={"configurable": {"thread_id": "run-exempt-test"}},
    )
    return state_from(final)


# ---------------------------------------------------------------------------


@pytest.fixture
def mixed(tmp_path_factory: pytest.TempPathFactory) -> ScanState:
    """One exempt standby and one ordinary idle machine, both genuinely idle."""
    return _run(
        [
            _idle_vm("vm-dr-standby", tags={"role": "disaster-recovery-standby"}),
            _idle_vm("vm-reporting-03", tags={"owner": "bi-team"}, monthly=D4S_V5_MONTHLY),
        ],
        _settings(tmp_path_factory),
    )


def test_an_exempt_resource_is_still_detected(mixed: ScanState) -> None:
    """The detector is not told about tags, so it keeps answering its own question."""
    detected = {signal.resource_name for signal in mixed.signals}
    assert detected == {"vm-dr-standby", "vm-reporting-03"}
    standby = next(s for s in mixed.signals if s.resource_name == "vm-dr-standby")
    assert standby.kind is WasteKind.IDLE_VIRTUAL_MACHINE
    assert standby.monthly_cost.amount == D8S_V5_MONTHLY


def test_an_exempt_resource_carries_a_verdict_with_a_machine_readable_reason(
    mixed: ScanState,
) -> None:
    """Prose would drift. A report, an audit event and an eval label share one enum."""
    assert len(mixed.exemptions) == 1
    verdict = mixed.exemptions[0]
    assert verdict.resource_name == "vm-dr-standby"
    assert verdict.reason is ExemptionReason.DISASTER_RECOVERY
    assert verdict.matched_tag == "role=disaster-recovery-standby"
    assert "standby" in verdict.statement.lower()


def test_no_remediation_is_proposed_for_an_exempt_resource(mixed: ScanState) -> None:
    """The finding survives; the recommendation does not."""
    targets = {rec.target_resource_name for rec in mixed.recommendations}
    assert targets == {"vm-reporting-03"}
    assert not any(c for c in mixed.classifications if "dr-standby" in c.recommendation_id)


def test_an_exempt_resource_is_never_priced(mixed: ScanState) -> None:
    """A saving nobody may take is one edit away from a headline total, so it is never computed."""
    standby = next(s for s in mixed.signals if s.resource_name == "vm-dr-standby")
    assert mixed.options_for(standby.signal_id) == ()


def test_the_actionable_and_exempt_signals_partition_the_signals(mixed: ScanState) -> None:
    """One definition of "actionable", so four nodes cannot drift apart."""
    assert set(mixed.actionable_signals) | set(mixed.exempt_signals) == set(mixed.signals)
    assert not set(mixed.actionable_signals) & set(mixed.exempt_signals)
    assert len(mixed.exempt_signals) == 1


def test_an_audit_event_records_every_exemption(mixed: ScanState) -> None:
    """Why something was not recommended must be answerable from the trail alone."""
    events = [e for e in mixed.audit if e.event_type is AuditEventType.EXEMPTION_APPLIED]
    assert len(events) == 1
    detail = events[0].detail
    assert detail["reason"] == ExemptionReason.DISASTER_RECOVERY.value
    assert detail["rule"] == "builtin-exemptions-v1:disaster_recovery"
    assert detail["matched_tag"] == "role=disaster-recovery-standby"
    assert "no remediation proposed" in detail["effect"]


def test_the_estate_read_event_records_that_the_question_was_asked(mixed: ScanState) -> None:
    """Carrying a verdict per resource would bloat every checkpoint; the count will do."""
    read = next(e for e in mixed.audit if e.event_type is AuditEventType.ESTATE_READ)
    assert read.detail["exemption_ruleset"] == "builtin-exemptions-v1"
    assert read.detail["exemptions_checked"] == "2"
    assert read.detail["exemptions_applied"] == "1"


def test_exemption_is_deterministic(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A scorecard baseline is worthless if the same estate exempts differently."""
    resources = [
        _idle_vm("vm-dr-standby", tags={"role": "standby", "retention": "REF-1"}),
        _idle_vm("vm-reporting-03", tags={}, monthly=D4S_V5_MONTHLY),
    ]
    first = _run(resources, _settings(tmp_path_factory))
    second = _run(resources, _settings(tmp_path_factory))
    assert [(e.resource_id, e.reason, e.matched_tag) for e in first.exemptions] == [
        (e.resource_id, e.reason, e.matched_tag) for e in second.exemptions
    ]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"role": "dr-standby"}, ExemptionReason.DISASTER_RECOVERY),
        ({"retention": "HIPAA-6YR-REF-2291"}, ExemptionReason.RETENTION_REQUIRED),
        ({"do-not-deallocate": "true"}, ExemptionReason.DO_NOT_DEALLOCATE),
        ({"cost-exempt": "yes"}, ExemptionReason.COST_EXEMPTION),
    ],
)
def test_each_convention_reaches_the_pipeline(
    tags: Mapping[str, str],
    expected: ExemptionReason,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The rules are not merely unit-tested; they change what a sweep proposes."""
    state = _run([_idle_vm("vm-protected", tags=tags)], _settings(tmp_path_factory))
    assert [e.reason for e in state.exemptions] == [expected]
    assert state.recommendations == ()


def test_an_estate_of_nothing_but_exempt_waste_short_circuits_honestly(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """And does not loop.

    The supervisor used to short-circuit on "no signals". An estate whose every
    signal is exempt has signals, so it would have been routed onward to a diagnosis
    with nothing to diagnose, then retried, then escalated, then sent round the
    pricing edge again. Keying on *actionable* signals is what closes that, and the
    reason line distinguishes the two cases -- "nothing found" and "everything found
    is protected by your own tags" are very different things to tell a client.
    """
    state = _run(
        [_idle_vm("vm-dr-standby", tags={"role": "standby"})],
        _settings(tmp_path_factory),
    )
    assert state.report is not None
    assert state.recommendations == ()
    assert len(state.signals) == 1

    decision = next(d for d in state.supervisor_decisions if d.action is RouteAction.SHORT_CIRCUIT)
    assert decision.to_node == "report_author"
    assert "exempt" in decision.reason
    assert "1 signal(s) found" in decision.reason
    # Not the "nothing detected" wording, which would have been a lie.
    assert "no waste detected" not in decision.reason


def test_a_resource_with_no_exemption_tag_behaves_exactly_as_before(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The regression guard for every estate that uses none of these conventions."""
    settings = _settings(tmp_path_factory)
    plain = _run([_idle_vm("vm-reporting-03", tags={"owner": "bi-team"})], settings)
    assert plain.exemptions == ()
    assert len(plain.recommendations) == 1
    assert plain.recommendations[0].savings.monthly.amount == D8S_V5_MONTHLY


def test_exemption_can_only_reduce_what_is_proposed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The invariant, measured end to end rather than argued.

    The same estate twice, once with the standby's tag and once without. Adding the
    tag may remove a recommendation and must not add one, must not raise the
    automatable count, and must not loosen a single class.
    """
    settings = _settings(tmp_path_factory)
    without = _run(
        [
            _idle_vm("vm-dr-standby", tags={"owner": "platform-team"}),
            _idle_vm("vm-reporting-03", tags={}, monthly=D4S_V5_MONTHLY),
        ],
        settings,
    )
    with_tag = _run(
        [
            _idle_vm("vm-dr-standby", tags={"role": "disaster-recovery-standby"}),
            _idle_vm("vm-reporting-03", tags={}, monthly=D4S_V5_MONTHLY),
        ],
        settings,
    )

    assert len(with_tag.recommendations) < len(without.recommendations)
    assert len(with_tag.signals) == len(without.signals)

    automatable = sum(1 for c in with_tag.classifications if c.is_automatable)
    assert automatable <= sum(1 for c in without.classifications if c.is_automatable)

    # Every class that survives is identical, never looser.
    before = {c.recommendation_id: c for c in without.classifications}
    for after in with_tag.classifications:
        original = before[after.recommendation_id]
        assert after.action_class.severity >= original.action_class.severity
        assert after.requires_approval == original.requires_approval
