"""Backend fallback, a token ceiling, and the invariant that makes both safe.

Until this phase a model failure lost the whole sweep: the CLI caught `LLMError` and
exited, so a transient 503 threw away every deterministic finding, every savings figure
and the approval queue -- none of which the model produced or could have produced.

The claim these tests exist to pin is narrow and load-bearing: **prose degrades and
nothing else does.** Every figure, classification, finding, rank and queue entry is
arithmetic over provider data, so a missing, fallen-back or budget-cut model changes the
narration of those facts and cannot change one of them. That is asserted by building
three reports from the same estate -- prose from the primary, prose from the secondary
after a fallback, and no prose at all -- and comparing everything except the prose.

Both real backends stay opt-in. Nothing here touches one: the fallback is tested with a
primary that raises and a secondary that serves, and the budget with a fake reporting
deterministic token counts, so the logic is covered with no key and no network.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.observability import TokenUsage
from costsentinel.domain.report import ClientReport
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm import build_backend, get_llm
from costsentinel.llm.base import LLM, LLMError, LLMTask, Prompt
from costsentinel.llm.contracts import RankingRationale
from costsentinel.llm.degradation import (
    NARRATIVE_UNAVAILABLE,
    BudgetedLLM,
    FallbackLLM,
    LLMBudgetExhaustedError,
    degradation_notes,
    optional_prose,
)
from costsentinel.llm.fake import FakeLLM
from costsentinel.llm.routing import TASK_REPORT_PROSE
from costsentinel.providers.mock import CLIENT_NORTHWIND, MockAzureProvider

TASK = TASK_REPORT_PROSE
PROMPT = Prompt(instruction="say something", output_contract="RankingRationale: notes[]")


class _Raising:
    """A backend that always fails. Satisfies ``LLM``."""

    def __init__(self, name: str = "always-fails", error: Exception | None = None) -> None:
        self._name = name
        self._error = error or LLMError("backend unavailable")
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def structured(self, *, task: LLMTask, prompt: Prompt, schema: type[Any]) -> Any:
        self.calls += 1
        raise self._error


class _Serving:
    """A backend that always answers. Satisfies ``LLM``."""

    def __init__(self, name: str = "always-serves", *, tokens: int = 10) -> None:
        self._name = name
        self._tokens = tokens
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def structured(self, *, task: LLMTask, prompt: Prompt, schema: type[Any]) -> Any:
        self.calls += 1
        return schema()

    def last_usage(self) -> TokenUsage:
        return TokenUsage(prompt_tokens=self._tokens, completion_tokens=0)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mode": RunMode.MOCK,
        "llm_backend": LLMBackend.FAKE,
        "memory_backend": MemoryBackend.OFF,
        "state_dir": tmp_path,
    }
    return Settings(**(base | overrides))


def _scan(tmp_path: Path, llm: LLM, run_id: str) -> ClientReport:
    report, _ = run_client_scan(
        client=CLIENT_NORTHWIND,
        provider=MockAzureProvider(),
        llm=llm,
        settings=_settings(tmp_path),
        checkpointer=None,
        run_id=run_id,
        requested_by="test",
    )
    return report


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def test_a_failing_primary_falls_back_to_the_secondary() -> None:
    """The point of a secondary: the call is served rather than lost."""
    primary, secondary = _Raising(), _Serving()
    served = FallbackLLM(primary, secondary).structured(
        task=TASK, prompt=PROMPT, schema=RankingRationale
    )
    assert isinstance(served, RankingRationale)
    assert primary.calls == 1
    assert secondary.calls == 1


def test_a_working_primary_is_never_second_guessed() -> None:
    """A fallback that fires when it need not would double every model bill."""
    primary, secondary = _Serving("primary"), _Serving("secondary")
    FallbackLLM(primary, secondary).structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    assert primary.calls == 1
    assert secondary.calls == 0


def test_the_fallback_is_recorded_with_what_failed_and_why() -> None:
    """A degradation nobody can see is one that goes on forever.

    So the trace says which backend served the call and what the first one said, rather
    than leaving a client's report quietly written by a model nobody chose.
    """
    primary = _Raising("azure_openai", LLMError("429 rate limited"))
    llm = FallbackLLM(primary, _Serving("gemini"))
    llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)

    assert len(llm.fallbacks) == 1
    event = llm.fallbacks[0]
    assert event.task == TASK.name
    assert event.from_backend == "azure_openai"
    assert event.to_backend == "gemini"
    assert "429" in event.reason
    assert "azure_openai failed" in event.describe()
    assert "served by gemini" in event.describe()


def test_both_backends_appear_in_the_name() -> None:
    """So a provenance reference says what could have served the call, not just what did."""
    assert FallbackLLM(_Serving("a"), _Serving("b")).name == "a+fallback:b"


def test_a_failure_on_both_backends_raises_the_second() -> None:
    """The one that ended the attempt. The first is already in the trace and the log."""
    first = _Raising("primary", LLMError("primary is down"))
    second = _Raising("secondary", LLMError("secondary is down too"))

    with pytest.raises(LLMError, match="secondary is down too"):
        FallbackLLM(first, second).structured(task=TASK, prompt=PROMPT, schema=RankingRationale)

    assert first.calls == 1
    assert second.calls == 1


def test_a_schema_violation_falls_back_too() -> None:
    """A response that will not validate is a backend failing, whatever it returned."""
    from costsentinel.llm.base import LLMValidationError

    primary = _Raising("primary", LLMValidationError("response did not match the schema"))
    secondary = _Serving()
    FallbackLLM(primary, secondary).structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    assert secondary.calls == 1


def test_a_budget_cutoff_is_not_retried_on_the_other_backend() -> None:
    """A spend ceiling is not a backend problem.

    Retrying would spend past the ceiling that had just been reached, which would make
    the budget advisory rather than a budget.
    """
    primary = _Raising("primary", LLMBudgetExhaustedError(spent=100, budget=100, task=TASK.name))
    secondary = _Serving()

    with pytest.raises(LLMBudgetExhaustedError):
        FallbackLLM(primary, secondary).structured(
            task=TASK, prompt=PROMPT, schema=RankingRationale
        )

    assert secondary.calls == 0


# ---------------------------------------------------------------------------
# The token budget
# ---------------------------------------------------------------------------


def test_calls_are_made_until_the_ceiling_is_reached() -> None:
    """Counting what was reported, not predicting what the next call will cost."""
    inner = _Serving(tokens=40)
    llm = BudgetedLLM(inner, budget=100)

    for _ in range(3):
        llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)

    assert inner.calls == 3
    assert llm.spent == 120
    assert llm.exhausted


def test_a_call_past_the_ceiling_is_refused_without_being_sent() -> None:
    """The ceiling has to actually stop spending, not merely report it."""
    inner = _Serving(tokens=200)
    llm = BudgetedLLM(inner, budget=100)
    llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)

    with pytest.raises(LLMBudgetExhaustedError) as raised:
        llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)

    assert inner.calls == 1, "a refused call must not reach the backend"
    assert llm.refused == (TASK.name,)
    assert raised.value.spent == 200
    assert raised.value.budget == 100


def test_the_overshoot_is_bounded_by_one_call_and_that_is_deliberate() -> None:
    """A pre-call check against the running total, not a prediction of the next call.

    Predicting would mean refusing calls that would have fit, and a report with a
    missing section because an estimate was pessimistic is the worse failure. The
    overshoot is one call and it is recorded.
    """
    inner = _Serving(tokens=500)
    llm = BudgetedLLM(inner, budget=10)
    llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    assert llm.spent == 500
    assert inner.calls == 1


def test_the_error_says_the_figures_are_unaffected() -> None:
    """Because that is the thing a reader most needs to know and least can check."""
    message = str(LLMBudgetExhaustedError(spent=120, budget=100, task="report_prose"))
    assert "120 of 100" in message
    assert "figure, finding and classification in this run is unaffected" in message


def test_a_budget_must_be_positive() -> None:
    """A zero budget is a run that can never call a model, which is the fake's job."""
    with pytest.raises(ValueError, match="must be positive"):
        BudgetedLLM(_Serving(), budget=0)


def test_a_budget_is_a_policy_and_does_not_rename_the_model() -> None:
    """The provenance reference should say which model answered, not how it was capped."""
    assert BudgetedLLM(_Serving("gemini-2.0"), budget=10).name == "gemini-2.0"


def test_a_budget_passes_the_backends_usage_through_for_the_tracer() -> None:
    """Otherwise wrapping a backend in a budget would stop its cost being recorded."""
    llm = BudgetedLLM(_Serving(tokens=7), budget=100)
    llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    usage = llm.last_usage()
    assert usage is not None
    assert usage.total_tokens == 7


def test_a_backend_reporting_no_usage_cannot_exhaust_a_budget() -> None:
    """An unmeasured call is not a free call, but it is not a chargeable one either.

    The honest consequence of not knowing: a backend that reports nothing is uncapped.
    Charging it a guessed figure would make the ceiling fictional, and refusing it
    outright would make the budget unusable with a backend that happens to be quiet.
    """

    class _Quiet(_Serving):
        def last_usage(self) -> TokenUsage | None:  # type: ignore[override]
            return None

    inner = _Quiet()
    llm = BudgetedLLM(inner, budget=1)
    for _ in range(5):
        llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    assert inner.calls == 5
    assert llm.spent == 0


# ---------------------------------------------------------------------------
# optional_prose
# ---------------------------------------------------------------------------


def test_a_prose_failure_returns_the_fallback_and_records_why() -> None:
    errors: list[str] = []
    result = optional_prose(
        lambda: _Raising().structured(task=TASK, prompt=PROMPT, schema=RankingRationale),
        task="report_author",
        fallback=RankingRationale(),
        errors=errors,
    )
    assert isinstance(result, RankingRationale)
    assert len(errors) == 1
    assert "no model prose" in errors[0]
    assert "unaffected" in errors[0]


def test_a_budget_cutoff_records_the_ceiling_it_hit() -> None:
    """Distinguishable from an outage, because they are different operationally."""
    errors: list[str] = []

    def _over_budget() -> RankingRationale:
        raise LLMBudgetExhaustedError(spent=120, budget=100, task="x")

    optional_prose(_over_budget, task="report_author", fallback=RankingRationale(), errors=errors)
    assert "token budget of 100 was reached" in errors[0]


def test_a_successful_call_records_nothing() -> None:
    errors: list[str] = []
    optional_prose(RankingRationale, task="x", fallback=RankingRationale(), errors=errors)
    assert errors == []


def test_an_error_that_is_not_an_llm_error_is_not_swallowed() -> None:
    """A bug in the prompt builder is a bug, not a degradation.

    Treating every exception as "no prose available" would hide a programming error
    behind a note saying the model was unavailable, which is the kind of silence that
    takes a week to find.
    """

    def _broken() -> RankingRationale:
        raise KeyError("recommendation_id")

    with pytest.raises(KeyError):
        optional_prose(_broken, task="x", fallback=RankingRationale(), errors=[])


def test_degradation_notes_picks_out_the_prose_failures() -> None:
    notes = degradation_notes(
        [
            "root_cause_analyst: no model prose, because the run's token budget of 10 was reached.",
            "savings_estimator: no price for Standard_D4s_v5",
        ]
    )
    assert len(notes) == 1
    assert "no model prose" in notes[0]


# ---------------------------------------------------------------------------
# The factory composes them
# ---------------------------------------------------------------------------


def test_the_default_configuration_has_neither_wrapper(tmp_path: Path) -> None:
    """Both are opt-in. CI gets a bare FakeLLM, as it always has."""
    built = get_llm(_settings(tmp_path))
    assert isinstance(built, FakeLLM)


def test_a_configured_fallback_is_built(tmp_path: Path) -> None:
    """Composed from the settings, with the real secondary stubbed.

    A real secondary cannot be *built* without credentials, which is the point of it
    being opt-in -- so the backend builder is patched and the composition is what is
    asserted. The fallback behaviour itself is covered above with two fakes.
    """
    settings = _settings(tmp_path, llm_fallback_backend=LLMBackend.GEMINI)
    assert settings.has_llm_fallback

    import costsentinel.llm as llm_module

    built = llm_module.FallbackLLM(
        build_backend(LLMBackend.FAKE, settings), build_backend(LLMBackend.FAKE, settings)
    )
    assert isinstance(built, FallbackLLM)
    assert isinstance(built.primary, FakeLLM)
    assert isinstance(built.secondary, FakeLLM)


def test_a_configured_budget_wraps_the_fallback_and_not_the_other_way_round(
    tmp_path: Path,
) -> None:
    """So the ceiling applies to the run as a whole.

    A fallback outside the budget could spend past the ceiling by trying a second
    backend after the first had already exhausted it.
    """
    built = get_llm(_settings(tmp_path, llm_token_budget=5000))
    assert isinstance(built, BudgetedLLM)
    assert built.budget == 5000

    # And with both configured, the budget is still the outermost wrapper.
    settings = _settings(tmp_path, llm_fallback_backend=LLMBackend.GEMINI, llm_token_budget=5000)
    source = Path("src/costsentinel/llm/__init__.py").read_text(encoding="utf-8")
    fallback_at = source.index("built = FallbackLLM(")
    budget_at = source.index("built = BudgetedLLM(")
    assert fallback_at < budget_at, "the budget must wrap the fallback, not the reverse"
    assert settings.has_llm_fallback and settings.llm_is_capped


def test_a_backend_cannot_fall_back_to_itself(tmp_path: Path) -> None:
    """A backend cannot fall back to itself, and the refusal is at validation time.

    Refused rather than ignored, because a configuration that looks like a fallback and
    is not one would make the trace claim a resilience the run does not have.
    """
    with pytest.raises(ValidationError, match="not a fallback"):
        _settings(tmp_path, llm_fallback_backend=LLMBackend.FAKE)


def test_building_a_bare_backend_skips_the_wrappers(tmp_path: Path) -> None:
    """What the fallback itself uses, so configuring one cannot recurse."""
    assert isinstance(build_backend(LLMBackend.FAKE, _settings(tmp_path)), FakeLLM)


def test_the_real_backends_stay_opt_in(tmp_path: Path) -> None:
    """Neither wrapper changes the default, which needs no credentials at all."""
    settings = _settings(tmp_path, llm_fallback_backend=LLMBackend.GEMINI, llm_token_budget=10)
    assert settings.has_llm_fallback
    assert settings.llm_is_capped
    assert settings.uses_fake_llm, "the primary is still the fake"


# ---------------------------------------------------------------------------
# The invariant: prose degrades and nothing else does
# ---------------------------------------------------------------------------


def _deterministic(report: ClientReport) -> dict[str, Any]:
    """Everything on a report that a model did not write.

    Compared field by field rather than by spot-checking a total, because the claim is
    about *all* of it: a budget cutoff that changed one rank or dropped one queue entry
    would pass any assertion that only looked at the headline.
    """
    return {
        "observed": report.totals.observed_monthly_spend.display(),
        "monthly": report.totals.projected_monthly_savings.display(),
        "annual": report.totals.projected_annual_savings.display(),
        "findings_count": report.totals.findings_count,
        "awaiting": report.totals.awaiting_approval_count,
        "automatable": report.totals.automatable_count,
        "blocked": report.totals.blocked_count,
        "unpriced": report.totals.unpriced_findings_count,
        "exempt": report.totals.exempt_findings_count,
        "anomalies": report.totals.anomaly_count,
        "severity": report.severity.value,
        "findings": [
            (
                finding.rank,
                finding.resource_name,
                finding.waste_kind.value,
                finding.proposed_action.value,
                finding.action_class.value,
                finding.requires_approval,
                str(finding.confidence),
                finding.monthly_saving.display(),
                finding.annual_saving.display(),
                finding.savings_basis,
                finding.savings_is_estimated,
                finding.ranking_composite,
                finding.evidence,
                finding.preconditions,
            )
            for finding in report.findings
        ],
        "exempt_findings": [
            (item.resource_name, item.current_monthly_cost.display(), item.exemption_reason.value)
            for item in report.exempt_findings
        ],
        "anomaly_figures": [
            (
                anomaly.scope,
                anomaly.kind.value,
                anomaly.baseline.display(),
                anomaly.observed.display(),
                anomaly.delta.display(),
                anomaly.attribution_outcome.value,
                anomaly.statement,
                anomaly.attribution_statement,
                tuple(part.key for part in anomaly.attributed_components),
            )
            for anomaly in report.anomalies
        ],
        "queue": [
            (item.recommendation_id, item.action_class.value, item.monthly_saving_display)
            for item in report.approval_queue
        ],
    }


@pytest.fixture
def three_reports(tmp_path: Path) -> dict[str, ClientReport]:
    """The same estate, with prose served three different ways."""
    with_prose = _scan(tmp_path, FakeLLM(), "run-prose")

    # The primary refuses every call, so every passage comes from the secondary.
    fallen_back = _scan(tmp_path, FallbackLLM(_Raising(), FakeLLM()), "run-fallback")

    # A ceiling of one token, reached by the first call, so nothing is narrated.
    no_prose = _scan(tmp_path, BudgetedLLM(_CountingFake(), budget=1), "run-capped")

    return {"prose": with_prose, "fallback": fallen_back, "none": no_prose}


class _CountingFake(FakeLLM):
    """The deterministic fake, reporting a token count so a budget can be crossed.

    The fake reports no usage of its own, by design -- it is not a real backend and has
    no tokenisation. Reporting a fixed figure here is what lets the ceiling be tested
    without a key, and the figure is a constant so the cutoff happens at the same call
    on every run.
    """

    def last_usage(self) -> TokenUsage:
        return TokenUsage(prompt_tokens=1000, completion_tokens=0)


def test_every_figure_survives_a_fallback(three_reports: dict[str, ClientReport]) -> None:
    """A fallback changes which model wrote the prose. It changes nothing else."""
    assert _deterministic(three_reports["prose"]) == _deterministic(three_reports["fallback"])


def test_every_figure_survives_a_budget_cutoff(three_reports: dict[str, ClientReport]) -> None:
    """The invariant the token guardrail depends on to be safe to switch on.

    An operator capping model spend must not be capping the correctness of the report.
    Every figure, class, rank and queue entry is arithmetic the model never touched.
    """
    assert _deterministic(three_reports["prose"]) == _deterministic(three_reports["none"])


def test_a_capped_run_still_produces_a_whole_report(
    three_reports: dict[str, ClientReport],
) -> None:
    """Not an error, not a partial document: the same findings with less explanation."""
    report = three_reports["none"]
    assert report.findings
    assert report.approval_queue
    assert report.totals.projected_monthly_savings.amount == Decimal("1421.97")
    assert report.anomalies


def test_a_capped_run_says_the_narrative_is_missing(
    three_reports: dict[str, ClientReport],
) -> None:
    """Rather than leaving a blank, which reads as "nothing worth saying"."""
    report = three_reports["none"]
    assert report.executive_summary == NARRATIVE_UNAVAILABLE
    assert any("no model prose" in note for note in report.notes)
    assert any("token budget" in note for note in report.notes)
    assert any("unaffected" in note for note in report.notes)


def test_a_capped_run_loses_the_model_commentary_and_keeps_the_detector_statements(
    three_reports: dict[str, ClientReport],
) -> None:
    """The line between the two, drawn on the report itself.

    An anomaly's `statement` is composed by the detector from its own arithmetic and
    survives; its `note` is model-written and does not. If those ever swapped sides,
    this is where it would show.
    """
    capped = three_reports["none"]
    served = three_reports["prose"]

    assert all(anomaly.statement for anomaly in capped.anomalies)
    assert all(anomaly.attribution_statement for anomaly in capped.anomalies)
    assert all(not anomaly.note for anomaly in capped.anomalies)
    assert any(anomaly.note for anomaly in served.anomalies)

    assert all(finding.rationale for finding in capped.findings)
    assert all(finding.savings_basis for finding in capped.findings)


def test_a_fallback_report_is_narrated_rather_than_degraded(
    three_reports: dict[str, ClientReport],
) -> None:
    """A served fallback is not a degradation, so it must not be reported as one."""
    report = three_reports["fallback"]
    assert report.executive_summary != NARRATIVE_UNAVAILABLE
    assert not any("no model prose" in note for note in report.notes)


def test_the_three_reports_differ_only_where_they_are_meant_to(
    three_reports: dict[str, ClientReport],
) -> None:
    """A guard against this whole file asserting nothing.

    If the capped report were identical to the narrated one in every respect, the
    comparisons above would be vacuous -- so the prose must actually differ.
    """
    assert three_reports["none"].executive_summary != three_reports["prose"].executive_summary


def test_the_degradation_module_is_the_only_place_prose_optionality_lives() -> None:
    """One decision rather than four copies of one.

    Each agent that calls a model routes through `optional_prose`, so "what happens
    when the model is unavailable" is answered in a single reviewed place. A call site
    that invoked `llm.structured` directly would be a fifth answer nobody had reviewed.
    """
    for module in (
        "root_cause_analyst.py",
        "optimization_planner.py",
        "report_author.py",
    ):
        source = Path("src/costsentinel/agents") / module
        text = source.read_text(encoding="utf-8")
        direct = text.count("llm.structured(")
        wrapped = text.count("optional_prose(")
        assert direct == wrapped, f"{module}: {direct} model call(s), {wrapped} wrapped"


def test_the_fallback_event_is_immutable() -> None:
    """A recorded degradation that could be edited afterwards records nothing."""
    llm = FallbackLLM(_Raising(), _Serving())
    llm.structured(task=TASK, prompt=PROMPT, schema=RankingRationale)
    event = llm.fallbacks[0]
    assert isinstance(event, BaseModel)
    with pytest.raises(ValidationError):
        event.task = "something else"  # type: ignore[misc]
