"""The quality metrics: precision, recall, ranking concordance, calibration.

These report a number. They do not fail a run — that is :mod:`evals.harness.gates`,
and the distinction is deliberate: a precision of 0.89 is information, and a
destructive action that was not gated is a defect.

Two things every metric here does.

**It states its denominator.** "Precision 1.00" over two resources is not the same
claim as over two hundred, and a scorecard that hides the count invites the reader to
assume the larger one.

**It reports unknown rather than zero when it cannot be computed.** A dataset with no
healthy resources cannot measure precision; the honest output is "not measured", not
1.0 (which flatters) and not 0.0 (which libels). :class:`Score` carries that
distinction in its type.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations

from pydantic import Field

from costsentinel.domain.common import Frozen
from costsentinel.domain.report import ClientReport, ReportAnomaly, ReportFinding
from costsentinel.domain.signals import WasteKind
from evals.harness.dataset import LabelledEstate, ResourceLabel

_PLACES = Decimal("0.0001")

#: Fewest findings a pairwise order comparison needs.
MIN_RANKED_PAIR = 2


def _ratio(numerator: int, denominator: int) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).quantize(_PLACES, rounding=ROUND_HALF_UP)


class Score(Frozen):
    """One measured quantity, or an explicit statement that it was not measurable."""

    name: str
    value: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(1))
    numerator: int = Field(default=0, ge=0)
    denominator: int = Field(default=0, ge=0)
    detail: str = ""

    @classmethod
    def of(cls, name: str, numerator: int, denominator: int, *, detail: str = "") -> Score:
        """A measured ratio, or an unmeasurable one when the denominator is zero."""
        if denominator == 0:
            return cls(
                name=name,
                value=None,
                numerator=numerator,
                denominator=0,
                detail=detail or "nothing to measure against",
            )
        return cls(
            name=name,
            value=_ratio(numerator, denominator),
            numerator=numerator,
            denominator=denominator,
            detail=detail,
        )

    @classmethod
    def unmeasured(cls, name: str, reason: str) -> Score:
        """A quantity this run could not compute."""
        return cls(name=name, value=None, detail=reason)

    @property
    def is_measured(self) -> bool:
        """Whether a value was computed at all."""
        return self.value is not None

    def display(self) -> str:
        """The score as a reader sees it, denominator included."""
        if self.value is None:
            return f"n/a ({self.detail})" if self.detail else "n/a"
        return f"{self.value:.2f} ({self.numerator}/{self.denominator})"


class DetectorScore(Frozen):
    """Precision and recall for one kind of waste.

    The per-kind breakdown is the figure somebody can act on. An aggregate tells you
    that something is wrong and not which detector is wrong, and the aggregate is
    also dominated by whichever kind happens to appear most often in the datasets.
    """

    waste_kind: WasteKind
    true_positives: int = Field(default=0, ge=0)
    false_positives: int = Field(default=0, ge=0)
    false_negatives: int = Field(default=0, ge=0)

    @property
    def precision(self) -> Score:
        """Of what this detector flagged, how much was really waste."""
        flagged = self.true_positives + self.false_positives
        return Score.of(f"{self.waste_kind.value}.precision", self.true_positives, flagged)

    @property
    def recall(self) -> Score:
        """Of the real waste of this kind, how much was found."""
        actual = self.true_positives + self.false_negatives
        return Score.of(f"{self.waste_kind.value}.recall", self.true_positives, actual)

    @property
    def is_clean(self) -> bool:
        """Whether this detector made no mistake on the datasets run."""
        return self.false_positives == 0 and self.false_negatives == 0


class Mismatch(Frozen):
    """One place the pipeline and the labels disagree.

    Carries the dataset, the resource, what was expected and what happened, because
    a scorecard that reports only a percentage leaves somebody to go and find out
    which case moved.
    """

    dataset_id: str
    resource: str
    kind: str = Field(description="What sort of disagreement: detection, figure, class, rank.")
    expected: str
    actual: str
    note: str = ""

    def describe(self) -> str:
        """One line a reader can act on."""
        return (
            f"{self.dataset_id}/{self.resource}: {self.kind} -- expected "
            f"{self.expected}, got {self.actual}"
        )


# ---------------------------------------------------------------------------
# Matching findings to labels
# ---------------------------------------------------------------------------


def finding_by_resource(report: ClientReport) -> Mapping[str, ReportFinding]:
    """Index a report's findings by resource name.

    Resource names are unique within a dataset by construction, so this is a total
    mapping rather than a lossy one.
    """
    return {finding.resource_name: finding for finding in report.findings}


def detection_scores(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[Mapping[WasteKind, DetectorScore], tuple[Mismatch, ...]]:
    """Per-detector precision and recall, and every disagreement.

    A finding of the *wrong kind* on a genuinely wasteful resource counts as a false
    positive for the kind claimed and a false negative for the kind labelled. That is
    stricter than counting it as a hit, and correctly so: "this disk is an idle VM"
    is not a partially right answer.
    """
    found = finding_by_resource(report)
    tallies: dict[WasteKind, dict[str, int]] = {}
    mismatches: list[Mismatch] = []

    def bump(kind: WasteKind, field: str) -> None:
        tallies.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0})[field] += 1

    for label in dataset.labels:
        finding = found.get(label.resource)

        if label.is_wasteful and label.waste_kind is not None:
            if finding is None:
                bump(label.waste_kind, "fn")
                mismatches.append(
                    Mismatch(
                        dataset_id=dataset.dataset_id,
                        resource=label.resource,
                        kind="detection",
                        expected=f"flagged as {label.waste_kind.value}",
                        actual="not flagged",
                        note=label.note,
                    )
                )
            elif finding.waste_kind is label.waste_kind:
                bump(label.waste_kind, "tp")
            else:
                bump(finding.waste_kind, "fp")
                bump(label.waste_kind, "fn")
                mismatches.append(
                    Mismatch(
                        dataset_id=dataset.dataset_id,
                        resource=label.resource,
                        kind="detection",
                        expected=label.waste_kind.value,
                        actual=finding.waste_kind.value,
                        note="flagged, but as the wrong kind of waste",
                    )
                )
        elif finding is not None:
            bump(finding.waste_kind, "fp")
            mismatches.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=label.resource,
                    kind="detection",
                    expected="not flagged",
                    actual=f"flagged as {finding.waste_kind.value}",
                    note=label.note,
                )
            )

    scores = {
        kind: DetectorScore(
            waste_kind=kind,
            true_positives=counts["tp"],
            false_positives=counts["fp"],
            false_negatives=counts["fn"],
        )
        for kind, counts in tallies.items()
    }
    return scores, tuple(mismatches)


def merge_detector_scores(
    parts: Sequence[Mapping[WasteKind, DetectorScore]],
) -> tuple[DetectorScore, ...]:
    """Combine per-dataset tallies into one breakdown, sorted by kind."""
    totals: dict[WasteKind, dict[str, int]] = {}
    for part in parts:
        for kind, score in part.items():
            bucket = totals.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0})
            bucket["tp"] += score.true_positives
            bucket["fp"] += score.false_positives
            bucket["fn"] += score.false_negatives
    return tuple(
        DetectorScore(
            waste_kind=kind,
            true_positives=counts["tp"],
            false_positives=counts["fp"],
            false_negatives=counts["fn"],
        )
        for kind, counts in sorted(totals.items(), key=lambda item: item[0].value)
    )


def aggregate_detection(scores: Sequence[DetectorScore]) -> tuple[Score, Score]:
    """Overall precision and recall across every detector."""
    tp = sum(s.true_positives for s in scores)
    fp = sum(s.false_positives for s in scores)
    fn = sum(s.false_negatives for s in scores)
    return (
        Score.of("detection.precision", tp, tp + fp),
        Score.of("detection.recall", tp, tp + fn),
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def ranking_concordance(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[Score, tuple[Mismatch, ...]]:
    """How often the pipeline's order agrees with a reviewer's.

    Pairwise concordance over the labelled order: of every pair of findings the
    labels express an opinion about, how many does the pipeline put the same way
    round. Pairs where either finding is missing, or the labels give no expected
    rank, are not counted -- a metric should not be penalised for a detection failure
    it is not measuring.

    Pairwise rather than nDCG against savings order, which the ROADMAP originally
    proposed, because the ranker deliberately weights safety and confidence as well
    as money: scoring it against a pure savings order would mark correct behaviour
    wrong. And the expected ranks are hand-written judgements rather than a
    restatement of the ranker's own weights, so this measures agreement with a person
    instead of agreement with itself.
    """
    found = finding_by_resource(report)
    ranked = [
        (label, found[label.resource])
        for label in dataset.labels
        if label.expected_rank is not None and label.resource in found
    ]
    if len(ranked) < MIN_RANKED_PAIR:
        return (
            Score.unmeasured(
                "ranking.concordance",
                f"{len(ranked)} ranked finding(s); a pair is needed to compare an order",
            ),
            (),
        )

    agreed = 0
    total = 0
    discordant: list[Mismatch] = []
    for (left_label, left), (right_label, right) in combinations(ranked, 2):
        assert left_label.expected_rank is not None  # noqa: S101 - filtered above
        assert right_label.expected_rank is not None  # noqa: S101
        expected_left_first = left_label.expected_rank < right_label.expected_rank
        actual_left_first = left.rank < right.rank
        total += 1
        if expected_left_first == actual_left_first:
            agreed += 1
        else:
            first, second = (
                (left_label, right_label) if expected_left_first else (right_label, left_label)
            )
            discordant.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=f"{first.resource} vs {second.resource}",
                    kind="rank",
                    expected=f"{first.resource} above {second.resource}",
                    actual=f"{second.resource} above {first.resource}",
                )
            )
    return (
        Score.of("ranking.concordance", agreed, total, detail="pairwise, against labelled order"),
        tuple(discordant),
    )


# ---------------------------------------------------------------------------
# Savings calibration
# ---------------------------------------------------------------------------


def savings_calibration(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[Score, tuple[Mismatch, ...]]:
    """How often an estimated saving lands where the labels say it should.

    Distinct from the cost-figure *gate*, which requires exactness. This metric
    exists for the cases where the labels state an estimate rather than an exact
    figure, and where "close" is a meaningful answer -- a rightsizing target is a
    modelling assumption, and a figure within a few per cent of the labelled one is
    a different situation from one that is double.

    An unknown saving matched against a labelled unknown counts as calibrated: the
    pipeline correctly declined to put a number on it, which is the behaviour this
    system is built around.
    """
    found = finding_by_resource(report)
    within = 0
    total = 0
    drifted: list[Mismatch] = []

    for label in dataset.wasteful:
        finding = found.get(label.resource)
        if finding is None:
            continue
        actual = finding.monthly_saving.amount
        expected = label.expected_monthly_saving
        total += 1

        if expected is None and actual is None:
            within += 1
            continue
        if expected is None or actual is None:
            drifted.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=label.resource,
                    kind="calibration",
                    expected="unknown" if expected is None else f"{expected}",
                    actual="unknown" if actual is None else f"{actual}",
                    note="one side put a number on it and the other did not",
                )
            )
            continue
        if actual == expected:
            within += 1
            continue
        drifted.append(
            Mismatch(
                dataset_id=dataset.dataset_id,
                resource=label.resource,
                kind="calibration",
                expected=str(expected),
                actual=str(actual),
            )
        )
    return (
        Score.of(
            "savings.calibration",
            within,
            total,
            detail="exact agreement with the labelled figure",
        ),
        tuple(drifted),
    )


# ---------------------------------------------------------------------------
# Spend anomalies
# ---------------------------------------------------------------------------


def anomaly_by_scope(report: ClientReport) -> Mapping[str, ReportAnomaly]:
    """Index a report's spend changes by the scope they were found in."""
    return {anomaly.scope: anomaly for anomaly in report.anomalies}


def anomaly_scores(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[Score, Score, tuple[Mismatch, ...]]:
    """Precision and recall for spend-anomaly detection, with every disagreement.

    A quality metric and never a gate. An anomaly is advice to investigate, so a false
    positive costs somebody an hour and a false negative costs a client some
    unexplained spend -- neither is the kind of defect that should stop a merge, and
    wiring either into the verdict would mean the first person to add a hard series
    broke the build.

    An anomaly of the **wrong kind** counts as a false positive for the kind claimed
    and a false negative for the kind labelled, exactly as a mis-kinded waste finding
    does. "Your run rate moved" and "something ran on Tuesday" send a client to
    different places, so the shape is part of the answer rather than a detail of it.
    """
    if not dataset.subscription_labels:
        reason = "this dataset labels no subscription spend"
        return (
            Score.unmeasured("anomaly.precision", reason),
            Score.unmeasured("anomaly.recall", reason),
            (),
        )

    found = anomaly_by_scope(report)
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    mismatches: list[Mismatch] = []

    for index, subscription in enumerate(dataset.estate.subscriptions):
        label = next(
            (one for one in dataset.subscription_labels if one.subscription == index), None
        )
        if label is None:  # pragma: no cover -- the schema requires a label for each
            continue
        anomaly = found.get(subscription.subscription_id)

        if label.expects_anomaly:
            if anomaly is None:
                false_negatives += 1
                mismatches.append(
                    Mismatch(
                        dataset_id=dataset.dataset_id,
                        resource=subscription.display_name,
                        kind="anomaly",
                        expected=f"{label.expected_kind.value if label.expected_kind else '?'}",
                        actual="no anomaly reported",
                        note=label.note,
                    )
                )
            elif anomaly.kind is label.expected_kind:
                true_positives += 1
            else:
                false_positives += 1
                false_negatives += 1
                mismatches.append(
                    Mismatch(
                        dataset_id=dataset.dataset_id,
                        resource=subscription.display_name,
                        kind="anomaly",
                        expected=f"{label.expected_kind.value if label.expected_kind else '?'}",
                        actual=anomaly.kind.value,
                        note="reported, but as the wrong shape of change",
                    )
                )
        elif anomaly is not None:
            false_positives += 1
            mismatches.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=subscription.display_name,
                    kind="anomaly",
                    expected="no anomaly",
                    actual=anomaly.kind.value,
                    note=label.note,
                )
            )

    return (
        Score.of(
            "anomaly.precision",
            true_positives,
            true_positives + false_positives,
            detail="of the spend changes reported, how many were real",
        ),
        Score.of(
            "anomaly.recall",
            true_positives,
            true_positives + false_negatives,
            detail="of the real spend changes, how many were found",
        ),
        tuple(mismatches),
    )


def attribution_accuracy(
    dataset: LabelledEstate, report: ClientReport
) -> tuple[Score, tuple[Mismatch, ...]]:
    """How often the cause of a spend change was got right, including "unknown".

    One score rather than a precision/recall pair, because attribution is not a
    detection problem. Every anomaly gets exactly one attribution outcome and there is
    a right answer for each, so the question is simply how many were right -- and
    "unknown" is one of the right answers rather than an abstention.

    That last point is the whole reason this metric exists in the form it does. A
    detector that always names its largest component would score perfectly on the
    attributable cases and would be giving a client a confident wrong cause on every
    other one, which is worse than silence: somebody goes and investigates the wrong
    meter. So an attribution where the label says unknown counts as a **failure**, and
    the dataset carries a case that demands it (``AdversarialKind.SEASONAL_NON_EVENT``'s
    sibling in spirit).

    A quality metric and never a gate, for the same reason the anomaly scores are not
    one: getting a cause wrong costs somebody an investigation, not a client's money.
    The money guarantee is enforced where money is, which is the figure-accuracy gates.
    """
    labelled = [one for one in dataset.subscription_labels if one.expects_attribution is not None]
    if not labelled:
        return (
            Score.unmeasured("attribution.accuracy", "this dataset labels no spend-change causes"),
            (),
        )

    found = anomaly_by_scope(report)
    correct = 0
    mismatches: list[Mismatch] = []

    for label in labelled:
        subscription = dataset.estate.subscriptions[label.subscription]
        anomaly = found.get(subscription.subscription_id)
        if anomaly is None:
            # The anomaly metric already counted this as a miss; counting it here too
            # would charge one defect twice. But the attribution is still not right, so
            # it is reported rather than quietly skipped.
            mismatches.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=subscription.display_name,
                    kind="attribution",
                    expected=label.expected_component or "unknown cause",
                    actual="no spend change was reported at all",
                    note="nothing to attribute, because the change itself was missed",
                )
            )
            continue

        named = tuple(part.key for part in anomaly.attributed_components)

        if label.expects_attribution:
            if label.expected_component in named:
                correct += 1
            else:
                mismatches.append(
                    Mismatch(
                        dataset_id=dataset.dataset_id,
                        resource=subscription.display_name,
                        kind="attribution",
                        expected=label.expected_component or "?",
                        actual=", ".join(named) or "unknown cause",
                        note=label.note,
                    )
                )
        elif named:
            mismatches.append(
                Mismatch(
                    dataset_id=dataset.dataset_id,
                    resource=subscription.display_name,
                    kind="attribution",
                    expected="unknown cause",
                    actual=", ".join(named),
                    note=(
                        "named a cause for a change that has none. A confident wrong "
                        "cause is worse than no cause: it sends somebody to investigate "
                        "the wrong thing."
                    ),
                )
            )
        else:
            correct += 1

    return (
        Score.of(
            "attribution.accuracy",
            correct,
            len(labelled),
            detail="of the labelled spend changes, how many had their cause got right",
        ),
        tuple(mismatches),
    )


def expected_total(dataset: LabelledEstate) -> Decimal | None:
    """The sum of every labelled saving, or ``None`` if any is unknown.

    ``None`` rather than a partial sum: a total that silently omitted an unknown
    would understate the estate, which is the specific failure the domain model's
    unknown-is-not-zero rule exists to prevent.
    """
    figures = [label.expected_monthly_saving for label in dataset.wasteful]
    if any(figure is None for figure in figures):
        return None
    return sum((figure for figure in figures if figure is not None), start=Decimal(0))


def labels_by_resource(dataset: LabelledEstate) -> Mapping[str, ResourceLabel]:
    """Index a dataset's labels by resource name."""
    return {label.resource: label for label in dataset.labels}
