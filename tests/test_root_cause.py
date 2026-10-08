"""Root-Cause Analyst tests.

The grounding check is the point of this node, so most of these tests are about
what it *refuses* to carry: a factor citing evidence nobody measured is dropped,
and the drop is recorded rather than swallowed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from costsentinel.agents.root_cause_analyst import make_root_cause_analyst
from costsentinel.config import Settings
from costsentinel.domain.analysis import FactorKind
from costsentinel.domain.common import (
    MoneyAmount,
    Provenance,
    ProvenanceSource,
    Verification,
)
from costsentinel.domain.estate import Estate
from costsentinel.domain.signals import Observation, WasteKind, WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.llm.base import LLMTask, Prompt, StructuredResponseT
from costsentinel.llm.contracts import (
    RootCauseAnalysis,
    RootCauseExplanation,
    RootCauseFactor,
)
from costsentinel.llm.fake import FakeLLM

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PROVENANCE = Provenance(
    source=ProvenanceSource.MOCK_PROVIDER,
    retrieved_at=AS_OF,
    reference="test",
    verification=Verification.VERIFIED,
)


def _signal(
    signal_id: str = "ws-1", *, observations: tuple[tuple[str, str], ...] = ()
) -> WasteSignal:
    return WasteSignal(
        signal_id=signal_id,
        kind=WasteKind.ORPHANED_MANAGED_DISK,
        client="acme",
        subscription_id="sub-1",
        resource_id="/subscriptions/sub-1/disks/disk-1",
        resource_name="disk-1",
        resource_kind="managed_disk",
        evidence=tuple(
            Observation(name=name, value=value, provenance=PROVENANCE)
            for name, value in observations
        ),
        monthly_cost=MoneyAmount.of(Decimal("38.42"), provenance=PROVENANCE),
        confidence=Decimal("0.95"),
        detector="orphaned_managed_disk/v1",
        detected_at=AS_OF,
        provenance=PROVENANCE,
    )


def _state(*signals: WasteSignal) -> ScanState:
    return ScanState(
        run_id="run-1",
        client="acme",
        estate=Estate(client="acme", retrieved_at=AS_OF, subscriptions=(), resources=()),
        signals=signals,
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_every_signal_gets_a_diagnosis(settings: Settings, llm: FakeLLM) -> None:
    state = _state(
        _signal("ws-a", observations=(("state", "unattached"), ("age_days", "241"))),
        _signal("ws-b", observations=(("state", "unattached"),)),
    )
    update = make_root_cause_analyst(llm=llm, settings=settings)(state)
    causes = update["root_causes"]
    assert {c.signal_id for c in causes} == {"ws-a", "ws-b"}
    assert all(c.narrative for c in causes)


def test_the_narrative_is_labelled_as_model_derived(settings: Settings, llm: FakeLLM) -> None:
    state = _state(_signal(observations=(("state", "unattached"),)))
    cause = make_root_cause_analyst(llm=llm, settings=settings)(state)["root_causes"][0]
    assert cause.narrative_provenance.source is ProvenanceSource.LLM_INFERENCE
    assert cause.narrative_provenance.is_model_derived


def test_factors_cite_the_evidence_and_are_classified(settings: Settings, llm: FakeLLM) -> None:
    state = _state(
        _signal(observations=(("state", "unattached"), ("age_days", "241"), ("sku", "P15")))
    )
    cause = make_root_cause_analyst(llm=llm, settings=settings)(state)["root_causes"][0]
    assert set(cause.cited_evidence) == {"state", "age_days", "sku"}
    kinds = {f.evidence_name: f.kind for f in cause.factors}
    assert kinds["age_days"] is FactorKind.LIFECYCLE
    assert kinds["state"] is FactorKind.CONFIGURATION


def test_the_diagnosis_quotes_the_observed_values(settings: Settings, llm: FakeLLM) -> None:
    """Derived from the evidence, not canned: a different value changes the output."""
    state = _state(_signal(observations=(("age_days", "999"),)))
    cause = make_root_cause_analyst(llm=llm, settings=settings)(state)["root_causes"][0]
    assert "999" in cause.narrative
    assert "disk-1" in cause.narrative


def test_the_diagnosis_inherits_the_detector_confidence(settings: Settings, llm: FakeLLM) -> None:
    """A model narrating a finding does not make the finding more or less certain."""
    state = _state(_signal(observations=(("state", "unattached"),)))
    cause = make_root_cause_analyst(llm=llm, settings=settings)(state)["root_causes"][0]
    assert cause.confidence == Decimal("0.95")


def test_no_signals_means_no_diagnosis_and_no_model_call(settings: Settings, llm: FakeLLM) -> None:
    update = make_root_cause_analyst(llm=llm, settings=settings)(_state())
    assert "root_causes" not in update
    assert llm.call_count == 0
    assert update["audit"]


# ---------------------------------------------------------------------------
# Grounding: what the node refuses to carry
# ---------------------------------------------------------------------------


class _UngroundedLLM(FakeLLM):
    """Cites an observation the detector never recorded."""

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        if schema is RootCauseAnalysis:
            fabricated = RootCauseAnalysis(
                explanations=(
                    RootCauseExplanation(
                        signal_id="ws-1",
                        narrative="The owner left the company and nobody took it over.",
                        factors=(
                            RootCauseFactor(
                                kind=FactorKind.OWNERSHIP,
                                statement="The owner left the company",
                                evidence_name="former_employee_record",
                            ),
                            RootCauseFactor(
                                kind=FactorKind.CONFIGURATION,
                                statement="The disk is unattached",
                                evidence_name="state",
                            ),
                        ),
                    ),
                )
            )
            return schema.model_validate(fabricated.model_dump())
        return super().structured(task=task, prompt=prompt, schema=schema)


def test_a_factor_citing_unrecorded_evidence_is_dropped(settings: Settings) -> None:
    """A model cannot get an invented fact into a report by asserting it."""
    state = _state(_signal("ws-1", observations=(("state", "unattached"),)))
    update = make_root_cause_analyst(llm=_UngroundedLLM(), settings=settings)(state)

    cause = update["root_causes"][0]
    assert cause.cited_evidence == ("state",)
    assert "former_employee_record" not in str(cause.factors)


def test_the_drop_is_recorded_rather_than_swallowed(settings: Settings) -> None:
    state = _state(_signal("ws-1", observations=(("state", "unattached"),)))
    update = make_root_cause_analyst(llm=_UngroundedLLM(), settings=settings)(state)
    errors = update["errors"]
    assert any("former_employee_record" in error for error in errors)
    assert any("the detector did not" in error for error in errors)


def test_a_signal_with_no_evidence_keeps_no_factors(settings: Settings) -> None:
    """Nothing to cite means nothing can be grounded."""
    state = _state(_signal("ws-1", observations=()))
    update = make_root_cause_analyst(llm=_UngroundedLLM(), settings=settings)(state)
    assert update["root_causes"][0].factors == ()


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


class _SilentLLM(FakeLLM):
    """Returns no explanations at all."""

    def structured(
        self,
        *,
        task: LLMTask,
        prompt: Prompt,
        schema: type[StructuredResponseT],
    ) -> StructuredResponseT:
        if schema is RootCauseAnalysis:
            return schema.model_validate(RootCauseAnalysis(explanations=()).model_dump())
        return super().structured(task=task, prompt=prompt, schema=schema)


def test_a_missing_explanation_falls_back_to_the_detector_evidence(
    settings: Settings,
) -> None:
    """The finding survives; only the narrative is the detector's own restatement."""
    state = _state(_signal("ws-1", observations=(("state", "unattached"),)))
    update = make_root_cause_analyst(llm=_SilentLLM(), settings=settings)(state)

    cause = update["root_causes"][0]
    assert cause.signal_id == "ws-1"
    assert "unattached" in cause.narrative
    # The fallback is arithmetic over the evidence, so it is not model-derived.
    assert not cause.narrative_provenance.is_model_derived
    assert cause.narrative_provenance.source is ProvenanceSource.CALCULATION
    assert any("no explanation" in error for error in update["errors"])


def test_the_prompt_sends_only_the_observations_the_model_may_cite(
    settings: Settings, llm: FakeLLM
) -> None:
    """The names a model is allowed to cite are exactly the names it was shown."""
    state = _state(_signal(observations=(("state", "unattached"), ("age_days", "241"))))
    make_root_cause_analyst(llm=llm, settings=settings)(state)

    _, prompt = llm.calls[0]
    block = prompt.blocks("waste_signal")[0]
    shown = {
        field.name.removeprefix("observation.")
        for field in block.fields
        if field.name.startswith("observation.")
    }
    assert shown == {"state", "age_days"}


def test_the_contract_has_no_numeric_fields() -> None:
    """A diagnosis explains; it does not quantify."""
    rendered = str(RootCauseAnalysis.model_json_schema())
    assert "MoneyAmount" not in rendered
    for forbidden in ("monthly", "saving", "cost"):
        assert f'"{forbidden}"' not in rendered
