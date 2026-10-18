"""The scorecard: a typed result, a Markdown report, and a terminal summary.

Three presentation rules, all for the same reason — that the thing which must not
regress should be impossible to miss.

**Gates lead.** A scorecard whose headline is "precision 0.92" buries the fact that a
deletion was not gated. The gates come first, in both renderings, and a failing gate
is visually distinct from a low score because the two mean categorically different
things.

**Adversarial cases are listed individually.** An adversarial failure folded into a
95% aggregate is a failure nobody sees. They get their own section, named, with the
labelled reasoning beside the outcome.

**Every score shows its denominator.** "1.00" over two cases is not the claim a
reader will take it for.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import Field

from costsentinel.domain.common import Frozen, utc_now
from evals.harness.dataset import AdversarialKind, Tier
from evals.harness.gates import GateResult
from evals.harness.judge import Groundedness
from evals.harness.metrics import DetectorScore, Mismatch, Score

#: The only two things that can fail a run. Named here rather than inferred from
#: ``GateResult``, so adding a gate is a deliberate act and a reader can see at a
#: glance which checks are load-bearing.
BLOCKING_GATES: tuple[str, ...] = (
    "safety.recall",
    "cost.figure_accuracy",
    "injection.resistance",
)


class AdversarialOutcome(Frozen):
    """What happened on one deliberately hard case."""

    dataset_id: str
    resource: str
    kind: AdversarialKind
    passed: bool
    expected: str
    actual: str
    note: str

    def line(self) -> str:
        """One line for the report."""
        verdict = "pass" if self.passed else "FAIL"
        return f"{verdict} | {self.dataset_id} | {self.resource} | {self.kind.value}"


class DatasetResult(Frozen):
    """One dataset's scores and gate outcomes."""

    dataset_id: str
    title: str
    tier: Tier
    findings: int = Field(default=0, ge=0)
    labelled_wasteful: int = Field(default=0, ge=0)
    labelled_healthy: int = Field(default=0, ge=0)
    precision: Score
    recall: Score
    ranking: Score
    calibration: Score
    groundedness: Groundedness
    gates: tuple[GateResult, ...] = ()
    mismatches: tuple[Mismatch, ...] = ()
    adversarial: tuple[AdversarialOutcome, ...] = ()

    @property
    def gates_passed(self) -> bool:
        """Whether every blocking gate held for this dataset."""
        return all(gate.passed for gate in self.gates if gate.name in BLOCKING_GATES)


class Scorecard(Frozen):
    """The whole run: per-dataset results, aggregates and gate outcomes."""

    generated_at: datetime = Field(default_factory=utc_now)
    tier: Tier | None = None
    judge: str = ""
    datasets: tuple[DatasetResult, ...] = ()
    detectors: tuple[DetectorScore, ...] = ()
    precision: Score
    recall: Score
    ranking: Score
    calibration: Score
    groundedness: Groundedness
    gates: tuple[GateResult, ...] = ()

    # --- verdict ---------------------------------------------------------

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        """The gates that can fail a run."""
        return tuple(gate for gate in self.gates if gate.name in BLOCKING_GATES)

    @property
    def passed(self) -> bool:
        """Whether every blocking gate held.

        The quality metrics are deliberately not consulted. A precision of 0.80 is a
        number somebody should look at; it is not a reason to refuse a merge, and
        wiring it into the verdict would mean the first person to add a hard dataset
        broke the build.
        """
        return all(gate.passed for gate in self.blocking)

    @property
    def failures(self) -> tuple[Mismatch, ...]:
        """Every gate failure across the run."""
        return tuple(failure for gate in self.blocking for failure in gate.failures)

    @property
    def adversarial(self) -> tuple[AdversarialOutcome, ...]:
        """Every adversarial outcome across the run."""
        return tuple(outcome for dataset in self.datasets for outcome in dataset.adversarial)

    @property
    def adversarial_passed(self) -> int:
        """How many adversarial cases the pipeline got right."""
        return sum(1 for outcome in self.adversarial if outcome.passed)

    # --- rendering -------------------------------------------------------

    def terminal_summary(self) -> str:
        """A screen's worth, gates first."""
        lines = [
            "CostSentinel evaluation",
            f"  datasets          {len(self.datasets)}"
            f"{f' ({self.tier.value} tier)' if self.tier else ' (all)'}",
            f"  judge             {self.judge}",
            "",
            f"  VERDICT           {'PASS' if self.passed else 'FAIL'}",
            "",
            "  Gates (a failure here fails the run):",
        ]
        lines += [f"    {gate.summary()}" for gate in self.gates]
        lines += [
            "",
            "  Quality (reported, never blocking):",
            f"    detection precision   {self.precision.display()}",
            f"    detection recall      {self.recall.display()}",
            f"    ranking concordance   {self.ranking.display()}",
            f"    savings calibration   {self.calibration.display()}",
            f"    report groundedness   {_groundedness_display(self.groundedness)}",
        ]

        if self.adversarial:
            lines += [
                "",
                f"  Adversarial cases     {self.adversarial_passed}/{len(self.adversarial)} "
                f"handled correctly:",
            ]
            lines += [f"    {outcome.line()}" for outcome in self.adversarial]

        if self.failures:
            lines += ["", "  Gate failures:"]
            lines += [f"    {failure.describe()}" for failure in self.failures]

        imperfect = [d for d in self.detectors if not d.is_clean]
        if imperfect:
            lines += ["", "  Detectors with a mistake on these datasets:"]
            lines += [
                f"    {d.waste_kind.value}: "
                f"precision {d.precision.display()}, recall {d.recall.display()}"
                for d in imperfect
            ]
        return "\n".join(lines)

    def markdown(self) -> str:
        """The committed report, so a diff shows what moved."""
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            "# CostSentinel evaluation scorecard",
            "",
            "<!-- Generated by `uv run costsentinel eval`. Committed so a diff shows",
            "     what moved between runs. Do not edit by hand. -->",
            "",
            f"**Verdict: {verdict}**",
            "",
            f"- Generated: `{self.generated_at.isoformat(timespec='seconds')}`",
            f"- Datasets: {len(self.datasets)}"
            f"{f' ({self.tier.value} tier)' if self.tier else ' (all)'}",
            f"- Judge: `{self.judge}`",
            "",
            "## Gates",
            "",
            "A failure in this table fails the run. These are not scores: a tool that",
            "gates 97% of its deletions is a tool that will delete something.",
            "",
            "| Gate | Verdict | Checks | Failures |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{gate.name}` | {'PASS' if gate.passed else '**FAIL**'} | "
            f"{gate.checked} | {len(gate.failures)} |"
            for gate in self.gates
        ]

        if self.failures:
            lines += [
                "",
                "### Gate failures",
                "",
            ]
            lines += [f"- {failure.describe()}" for failure in self.failures]

        lines += [
            "",
            "## Quality metrics",
            "",
            "Reported, never blocking. A low score is something to look at; it is not",
            "a reason to refuse a merge.",
            "",
            "| Metric | Score | Measured over |",
            "| --- | --- | --- |",
            f"| Detection precision | {_value(self.precision)} | "
            f"{self.precision.denominator} finding(s) |",
            f"| Detection recall | {_value(self.recall)} | "
            f"{self.recall.denominator} labelled waste |",
            f"| Ranking concordance | {_value(self.ranking)} | "
            f"{self.ranking.denominator} pair(s) |",
            f"| Savings calibration | {_value(self.calibration)} | "
            f"{self.calibration.denominator} figure(s) |",
            f"| Report groundedness | {_groundedness_display(self.groundedness)} | "
            f"{len(self.groundedness.claims)} claim(s) |",
            "",
            "Ranking concordance is pairwise agreement with a **hand-written** reviewer",
            "order, not nDCG against savings order -- the ranker deliberately weights",
            "safety and confidence too, so scoring it against a pure savings order would",
            "mark correct behaviour wrong.",
            "",
            "The labelled order expresses one stated rule: **an unquantified saving ranks",
            "below every quantified one** (ARCHITECTURE.md D104). Phase 5 recorded a single",
            "discordant pair -- an unpriced idle database sitting above a small,",
            "irreversible, priced snapshot deletion -- and deliberately left it, because",
            "editing one label to match the code would have made this metric vacuous.",
            "Phase 7 resolved it by **settling the rule and applying it to every label that",
            "carries an unquantified saving**, which is a different thing: the pipeline and",
            "the labels now agree because both follow a rule that is written down, tested",
            "and reported on each finding.",
            "",
            "## Per-detector breakdown",
            "",
            "The aggregate tells you something is wrong. This tells you what.",
            "",
            "| Waste kind | Precision | Recall | TP | FP | FN |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{d.waste_kind.value}` | {_value(d.precision)} | {_value(d.recall)} | "
            f"{d.true_positives} | {d.false_positives} | {d.false_negatives} |"
            for d in self.detectors
        ]

        lines += [
            "",
            "## Adversarial cases",
            "",
            "Listed individually and never folded into the aggregate: an adversarial",
            "failure hidden inside a 95% precision figure is a failure nobody sees.",
            "",
            "For a resource that is idle or stale *by design*, **handled correctly means",
            "not actionably recommended, and exempt is the correct outcome.** The",
            "disaster-recovery standby really is idle and the retained snapshot really is",
            "old; the right behaviour is to surface both, say which tag withholds the",
            "advice, and leave them out of the savings total. A label that expects an",
            "exemption is only satisfied when that exemption is visible -- scoring on",
            "absence alone would award full marks to a system that never noticed the",
            "standby at all, which is a different and worse system.",
            "",
        ]
        if not self.adversarial:
            lines.append("_No adversarial cases in this run._")
        else:
            lines += [
                f"{self.adversarial_passed} of {len(self.adversarial)} handled correctly.",
                "",
                "| Dataset | Resource | Why it is hard | Outcome |",
                "| --- | --- | --- | --- |",
            ]
            lines += [
                f"| `{o.dataset_id}` | `{o.resource}` | {o.kind.value.replace('_', ' ')} | "
                f"{'pass' if o.passed else '**FAIL**'} |"
                for o in self.adversarial
            ]
            lines += ["", "Detail:", ""]
            for outcome in self.adversarial:
                lines += [
                    f"- **`{outcome.resource}`** ({outcome.kind.value}) — "
                    f"{'handled correctly' if outcome.passed else '**failed**'}. "
                    f"Expected {outcome.expected}; got {outcome.actual}.",
                    f"  - Labelled reasoning: {outcome.note}",
                ]

        lines += [
            "",
            "## Per dataset",
            "",
            "| Dataset | Tier | Findings | Precision | Recall | Gates |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{d.dataset_id}` | {d.tier.value} | {d.findings} | "
            f"{_value(d.precision)} | {_value(d.recall)} | "
            f"{'pass' if d.gates_passed else '**FAIL**'} |"
            for d in self.datasets
        ]
        lines += ["", "### What each dataset is for", ""]
        lines += [f"- **`{d.dataset_id}`** — {d.title}" for d in self.datasets]

        unsupported = self.groundedness.unsupported
        if unsupported:
            lines += ["", "## Ungrounded claims", ""]
            lines += [f"- {claim.text} — {claim.reason}" for claim in unsupported]

        return "\n".join(lines) + "\n"


def _value(score: Score) -> str:
    """A score's value for a table cell."""
    return "n/a" if score.value is None else f"{score.value:.2f}"


def _groundedness_display(groundedness: Groundedness) -> str:
    """Groundedness as a reader sees it."""
    score = groundedness.score
    if score is None:
        return "n/a (no checkable claim)"
    return f"{score:.2f} ({groundedness.supported}/{len(groundedness.claims)})"


def aggregate_score(name: str, parts: Sequence[Score]) -> Score:
    """Sum the numerators and denominators of per-dataset scores.

    Summing the ratios would weight a two-resource dataset as heavily as a
    two-hundred-resource one, which is the standard way to produce a reassuring
    number that means nothing.
    """
    measurable = [part for part in parts if part.denominator > 0]
    if not measurable:
        return Score.unmeasured(name, "no dataset could measure this")
    return Score.of(
        name,
        sum(part.numerator for part in measurable),
        sum(part.denominator for part in measurable),
        detail=measurable[0].detail,
    )
