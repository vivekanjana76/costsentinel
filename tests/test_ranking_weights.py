"""Per-client ranking weights (ARCHITECTURE.md D137).

The weights are data: validated, defaulting to the built-in constants, and able to
change the *order* of a client's findings and nothing else. No figure, class or finding
moves, and the unknown-ranks-low tier holds whatever the weights say.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from costsentinel.agents.ranking import (
    DEFAULT_WEIGHTS,
    WEIGHT_CONFIDENCE,
    WEIGHT_RISK,
    WEIGHT_SAVINGS,
    RankingWeights,
    RankingWeightsFile,
    weights_for,
)
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.common import MoneyAmount
from costsentinel.domain.report import ClientReport
from costsentinel.graph.runner import run_client_scan
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import CLIENT_MINISTRY, CLIENT_NORTHWIND, MockAzureProvider
from costsentinel.reports.markdown import render_markdown, unrendered_figures

SAFETY_FIRST = {"savings": "0", "safety": "1", "confidence": "0"}


def _settings(tmp_path: Path, weights: dict[str, object] | None = None) -> Settings:
    path = None
    tmp_path.mkdir(parents=True, exist_ok=True)
    if weights is not None:
        path = tmp_path / "weights.json"
        path.write_text(json.dumps(weights), encoding="utf-8")
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=tmp_path,
        ranking_weights_path=path,
    )


def _report(settings: Settings, client: str = CLIENT_NORTHWIND) -> ClientReport:
    report, _ = run_client_scan(
        client=client,
        provider=MockAzureProvider(),
        llm=FakeLLM(),
        settings=settings,
        checkpointer=None,
        run_id="run-weights",
        requested_by="test",
    )
    return report


# ---------------------------------------------------------------------------
# The type
# ---------------------------------------------------------------------------


def test_the_default_is_the_built_in_policy() -> None:
    """Nothing changes for a client nobody configured."""
    assert (DEFAULT_WEIGHTS.savings, DEFAULT_WEIGHTS.safety, DEFAULT_WEIGHTS.confidence) == (
        WEIGHT_SAVINGS,
        WEIGHT_RISK,
        WEIGHT_CONFIDENCE,
    )


@pytest.mark.parametrize(
    "weights",
    [
        {"savings": "0.5", "safety": "0.5", "confidence": "0.5"},
        {"savings": "0.5", "safety": "0.4", "confidence": "0"},
        {"savings": "-0.2", "safety": "1", "confidence": "0.2"},
        {"savings": "1.5", "safety": "-0.5", "confidence": "0"},
        {"savings": "0.33333", "safety": "0.33333", "confidence": "0.33334"},
        {"savings": "0.6", "safety": "0.4"},
        {"savings": "0.6", "safety": "0.25", "confidence": "0.15", "memory": "0"},
    ],
    ids=["over", "under", "negative", "over-one", "too-precise", "missing", "unknown-key"],
)
def test_weights_that_are_not_a_valid_policy_are_refused(weights: dict[str, str]) -> None:
    """A weight file with a typo fails loudly; it never silently falls back."""
    with pytest.raises(ValidationError):
        RankingWeights.model_validate(weights)


def test_a_file_names_clients_and_nothing_else() -> None:
    """An unknown top-level key is an error, not an ignored setting."""
    with pytest.raises(ValidationError):
        RankingWeightsFile.model_validate({"clients": {}, "default": SAFETY_FIRST})


def test_an_unconfigured_or_unnamed_client_gets_the_default(tmp_path: Path) -> None:
    """Only the clients a file names are affected."""
    assert weights_for(_settings(tmp_path), CLIENT_NORTHWIND) == DEFAULT_WEIGHTS
    configured = _settings(tmp_path, {"clients": {CLIENT_NORTHWIND: SAFETY_FIRST}})
    assert weights_for(configured, CLIENT_MINISTRY) == DEFAULT_WEIGHTS
    assert weights_for(configured, CLIENT_NORTHWIND).safety == Decimal(1)


# ---------------------------------------------------------------------------
# What weights can and cannot change
# ---------------------------------------------------------------------------


def _money(amount: MoneyAmount) -> tuple[object, ...]:
    # What a figure is: its value and its derivation. Not when this run computed it.
    return (amount.amount, amount.currency, amount.display(), amount.provenance.reference)


def _facts(report: ClientReport) -> dict[str, tuple[object, ...]]:
    """Everything about a finding except where it sits and why."""
    return {
        f.recommendation_id: (
            _money(f.monthly_saving),
            _money(f.annual_saving),
            _money(f.current_monthly_cost),
            f.action_class,
            f.proposed_action,
            f.savings_basis,
        )
        for f in report.findings
    }


def test_weights_change_the_order_and_nothing_else(tmp_path: Path) -> None:
    """Safety-first reorders a client's findings; every figure and class is unchanged."""
    default = _report(_settings(tmp_path / "a"))
    safety_first = _report(_settings(tmp_path / "b", {"clients": {CLIENT_NORTHWIND: SAFETY_FIRST}}))

    order = [f.recommendation_id for f in default.findings]
    reordered = [f.recommendation_id for f in safety_first.findings]
    assert reordered != order, "safety-first weights did not move anything"
    assert _facts(safety_first) == _facts(default)
    for name in ("observed_monthly_spend", "projected_monthly_savings", "projected_annual_savings"):
        assert _money(getattr(safety_first.totals, name)) == _money(getattr(default.totals, name))


def test_no_weighting_lifts_an_unpriced_finding_above_a_priced_one(tmp_path: Path) -> None:
    """The tier is outside the sum: even with savings weighted to zero, unknown ranks low."""
    report = _report(_settings(tmp_path, {"clients": {CLIENT_NORTHWIND: SAFETY_FIRST}}))
    known = [f.monthly_saving.is_known for f in report.findings]
    assert False in known and True in known, "the estate needs both kinds for this to bite"
    assert known == sorted(known, reverse=True), "an unpriced finding outranked a priced one"


def test_the_effective_weights_are_written_into_every_finding(tmp_path: Path) -> None:
    """A client can see what was applied to them, not just the order it produced."""
    report = _report(_settings(tmp_path, {"clients": {CLIENT_NORTHWIND: SAFETY_FIRST}}))
    for finding in report.findings:
        assert "(weights: savings 0, safety 1, confidence 0)" in finding.ranking_breakdown
    default = _report(_settings(tmp_path / "default"))
    for finding in default.findings:
        assert "(weights: savings 0.60, safety 0.25, confidence 0.15)" in (
            finding.ranking_breakdown
        )


def test_a_weight_nobody_applied_is_caught_in_the_rendered_document(tmp_path: Path) -> None:
    """The weights are admitted as the ones carried on the finding, and no others."""
    report = _report(_settings(tmp_path, {"clients": {CLIENT_NORTHWIND: {
        "savings": "0.40", "safety": "0.45", "confidence": "0.15"}}}))  # fmt: skip
    text = render_markdown(report)
    assert "(weights: savings 0.40, safety 0.45, confidence 0.15)" in text
    assert unrendered_figures(report, text) == []

    forged = text.replace("safety 0.45,", "safety 0.47,", 1)
    assert unrendered_figures(report, forged) == ["0.47"]
