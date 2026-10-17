"""Report groundedness, judged offline.

CLAUDE.md section 8 asks for groundedness via LLM-as-judge. A metric that only works
with a credential is a metric CI never computes, so the judge is a seam with a
deterministic implementation behind it — and the deterministic one is *stricter* than
a model would be, which is the right trade for a build check.

``FakeJudge`` does not score prose plausibility, because that is not checkable. It
checks the one thing that is: **every number and resource name a report states must
appear in the state the report was built from.** A model-authored summary claiming
"we identified a 2,400 dollar saving" when state holds no such figure fails, and
fails for a stated reason. That is a narrower claim than a real judge makes and a
far more reliable one, and it is exactly the failure mode the system is built to
prevent.

The real judge is opt-in and uses the existing structured-output seam, so its
response is schema-validated like any other and cannot introduce a number.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import Field

from costsentinel.domain.common import Frozen
from costsentinel.domain.report import ClientReport
from costsentinel.domain.state import ScanState
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import Untrusted
from costsentinel.llm.routing import TASK_JUDGE

#: Decimal numbers, which is how figures and scores are both rendered. Bare integers
#: are deliberately not matched: "30 days" and "8 findings" are not claims worth
#: tracing, and a judge that cries wolf about them is one nobody reads.
_DECIMAL = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")


class Claim(Frozen):
    """One checkable assertion extracted from a report's prose."""

    text: str
    supported: bool
    reason: str = ""


class Groundedness(Frozen):
    """How much of a report's prose is traceable to the state behind it."""

    judge: str
    claims: tuple[Claim, ...] = ()

    @property
    def supported(self) -> int:
        """How many claims traced to state."""
        return sum(1 for claim in self.claims if claim.supported)

    @property
    def unsupported(self) -> tuple[Claim, ...]:
        """The claims that did not."""
        return tuple(claim for claim in self.claims if not claim.supported)

    @property
    def score(self) -> Decimal | None:
        """Share of claims supported, or ``None`` when there was nothing to check.

        ``None`` rather than 1.0: a report with no checkable claim has not
        demonstrated groundedness, it has merely avoided the question.
        """
        if not self.claims:
            return None
        return Decimal(self.supported) / Decimal(len(self.claims))


@runtime_checkable
class Judge(Protocol):
    """Scores whether a report's prose is supported by the state behind it."""

    @property
    def name(self) -> str:
        """Short identifier, recorded on the scorecard."""
        ...

    def score(self, report: ClientReport, state: ScanState) -> Groundedness:
        """Judge one report."""
        ...


def _state_figures(state: ScanState, report: ClientReport) -> frozenset[str]:
    """Every decimal string the report is entitled to state.

    Money *and* the other computed decimals -- detector confidences, ranking
    components, composites. The first version of this collected money only, and
    promptly reported eight ungrounded claims that were all confidence scores the
    ranking rationale legitimately quotes. The judge was wrong, not the pipeline,
    which is a reminder that a strict check still has to be a correct one.

    A number in the prose that is not in this set was not computed by the pipeline,
    which is the definition of ungrounded for a system whose figures are all
    arithmetic.
    """
    amounts: list[Decimal | None] = []
    for recommendation in state.recommendations:
        amounts += [recommendation.savings.monthly.amount, recommendation.savings.annual.amount]
    amounts += [signal.monthly_cost.amount for signal in state.signals]
    if state.estate is not None:
        amounts += [
            reservation.monthly_amortised_cost.amount for reservation in state.estate.reservations
        ]
        amounts += [resource.monthly_cost.amount for resource in state.estate.resources]
    amounts += [
        report.totals.observed_monthly_spend.amount,
        report.totals.projected_monthly_savings.amount,
        report.totals.projected_annual_savings.amount,
    ]
    # Flagged-but-exempt items. Their current cost is a provider fact and their total
    # is arithmetic over it, so both are figures the report is entitled to state --
    # and leaving them out would have the judge report the exempt section's own
    # honest numbers as ungrounded.
    if report.totals.exempt_monthly_cost is not None:
        amounts.append(report.totals.exempt_monthly_cost.amount)
    amounts += [item.current_monthly_cost.amount for item in report.exempt_findings]
    for summary in report.waste_breakdown:
        amounts += [summary.monthly_saving.amount, summary.annual_saving.amount]

    # The computed non-monetary decimals: a detector's confidence and the ranking
    # components are quoted in the rationale, and they are just as much the
    # pipeline's own arithmetic as a saving is.
    amounts += [signal.confidence for signal in state.signals]
    amounts += [item.confidence for item in report.exempt_findings]
    amounts += [recommendation.confidence for recommendation in state.recommendations]
    for score in state.ranking:
        amounts += [
            score.savings_component,
            score.risk_component,
            score.confidence_component,
            score.composite,
        ]
    for cause in state.root_causes:
        amounts.append(cause.confidence)

    figures: set[str] = set()
    for amount in amounts:
        if amount is None:
            continue
        # Both with and without thousands separators, because the renderer groups
        # and a model may not.
        grouped = f"{amount:,.2f}"
        figures.add(grouped)
        figures.add(grouped.replace(",", ""))
    return frozenset(figures)


def _prose(report: ClientReport) -> tuple[tuple[str, str], ...]:
    """The model-authored parts of a report, paired with where they came from.

    Only the prose. The structured fields are domain objects carried through from
    state and are not the judge's business -- judging them would be judging the
    pipeline's arithmetic, which the cost-figure gate already does exactly.
    """
    parts: list[tuple[str, str]] = [("executive_summary", report.executive_summary)]
    for finding in report.findings:
        if finding.note:
            parts.append((f"note[{finding.resource_name}]", finding.note))
        if finding.ranking_rationale:
            parts.append((f"ranking_rationale[{finding.resource_name}]", finding.ranking_rationale))
        if finding.root_cause:
            parts.append((f"root_cause[{finding.resource_name}]", finding.root_cause))
    return tuple(parts)


class FakeJudge:
    """Deterministic, strict, offline. The default and the only one CI uses.

    Checks that every monetary figure in the report's prose appears in the state the
    report was built from. Not a plausibility judgement -- a traceability one.
    """

    @property
    def name(self) -> str:
        """Short identifier, recorded on the scorecard."""
        return "fake-traceability-judge"

    def score(self, report: ClientReport, state: ScanState) -> Groundedness:
        """Trace every figure in the prose back to state."""
        permitted = _state_figures(state, report)
        claims: list[Claim] = []

        for where, text in _prose(report):
            for figure in _DECIMAL.findall(text):
                supported = figure in permitted or figure.replace(",", "") in permitted
                claims.append(
                    Claim(
                        text=f"{where}: {figure}",
                        supported=supported,
                        reason=(
                            ""
                            if supported
                            else (
                                "this figure does not appear anywhere in the state the "
                                "report was built from, so it was not computed by the "
                                "pipeline"
                            )
                        ),
                    )
                )
        return Groundedness(judge=self.name, claims=tuple(claims))


class JudgeVerdict(Untrusted):
    """A real judge's structured response.

    No numeric fields beyond the count, and the count is of claims rather than of
    money -- the same rule the other contracts follow, for the same reason.
    """

    unsupported_claims: tuple[str, ...] = Field(
        default=(), max_length=50, description="Claims the judge could not trace to the state."
    )
    verdict: str = Field(min_length=1, max_length=1000)


class ModelJudge:
    """A real language model as judge. Opt-in, never used by CI.

    Shares the existing structured-output seam, so its response is schema-validated
    like any other and cannot introduce a figure. Its job is to find claims the
    traceability check cannot see -- a confident sentence about a cause nobody
    observed, say -- and not to re-check the numbers.
    """

    def __init__(self, llm: LLM) -> None:
        """Wrap a configured backend."""
        self._llm = llm

    @property
    def name(self) -> str:
        """Short identifier, recorded on the scorecard."""
        return f"model-judge:{self._llm.name}"

    def score(self, report: ClientReport, state: ScanState) -> Groundedness:
        """Ask the model which claims it cannot trace to the state."""
        evidence = [
            EvidenceBlock(
                label="report",
                fields=tuple(
                    EvidenceField(name=where, value=text) for where, text in _prose(report)
                ),
            ),
            EvidenceBlock(
                label="state",
                fields=(
                    *(
                        EvidenceField(
                            name=f"signal.{signal.signal_id}",
                            value=(
                                f"{signal.resource_name} is {signal.kind.value} costing "
                                f"{signal.monthly_cost.display()}"
                            ),
                        )
                        for signal in state.signals
                    ),
                    *(
                        EvidenceField(
                            name=f"recommendation.{rec.recommendation_id}",
                            value=(
                                f"{rec.action.value} on {rec.target_resource_name} saving "
                                f"{rec.savings.monthly.display()}"
                            ),
                        )
                        for rec in state.recommendations
                    ),
                ),
            ),
        ]
        verdict = self._llm.structured(
            task=TASK_JUDGE,
            prompt=Prompt(
                instruction=(
                    "You are auditing a cost report for groundedness. List any claim "
                    "in the report blocks that is not supported by the state blocks. "
                    "A claim is unsupported if it asserts a fact, a cause or a figure "
                    "the state does not contain. Do not restate supported claims, and "
                    "do not produce any figure of your own."
                ),
                output_contract=(
                    "JudgeVerdict: {unsupported_claims[] of strings, verdict}. No numeric fields."
                ),
                evidence=tuple(evidence),
            ),
            schema=JudgeVerdict,
        )
        prose = _prose(report)
        unsupported = set(verdict.unsupported_claims)
        return Groundedness(
            judge=self.name,
            claims=tuple(
                Claim(
                    text=f"{where}: {text[:120]}",
                    supported=text not in unsupported,
                    reason=verdict.verdict if text in unsupported else "",
                )
                for where, text in prose
            ),
        )


def merge(groundedness: Sequence[Groundedness]) -> Groundedness:
    """Combine per-dataset groundedness into one result."""
    if not groundedness:
        return Groundedness(judge="none")
    return Groundedness(
        judge=groundedness[0].judge,
        claims=tuple(claim for part in groundedness for claim in part.claims),
    )
