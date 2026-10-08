# Eval harness (Phase 5)

Runs the **real pipeline** — not a reimplementation — against a dataset and emits a
scorecard.

Metrics:

| Metric | What it asserts |
|---|---|
| Waste-detection precision / recall | Detectors find real waste and few false positives. |
| Recommendation ranking quality | nDCG against ground-truth savings ordering. |
| Cost-figure accuracy | **Hard assertion**: no number in a report has LLM provenance. |
| Savings-estimate calibration | Estimated vs ground-truth savings, within tolerance. |
| Report groundedness | LLM-as-judge: every claim traces to a state field. |
| Safety recall | **Every** destructive action was correctly classified and gated. |

CI runs a fast smoke subset on every commit; the full suite runs on demand. A drop in
**safety recall** fails the build — it is the one metric where a regression is a
safety incident rather than a quality one.

Note that the cost-figure accuracy metric is already structurally guaranteed in
Phase 1: `MoneyAmount` refuses LLM provenance at validation time, so the metric
asserts a property the type system enforces rather than hoping for it.

See ROADMAP.md Phase 5.
