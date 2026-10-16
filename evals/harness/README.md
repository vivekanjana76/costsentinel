# Eval harness

Runs the **real pipeline** — not a reimplementation — against the labelled datasets
and emits a scorecard.

```bash
uv run costsentinel eval                  # every dataset, writes evals/REPORT.md
uv run costsentinel eval --smoke          # the fast subset CI runs on every commit
uv run costsentinel eval --dataset baseline-mixed --no-write
```

Exits non-zero when a gate fails. Fully offline: `FakeLLM`, `FakeEmbedder`, a
deterministic judge, memory off, no network and no credential.

## Gates, not scores

Two things in CLAUDE.md section 8 are not percentages, and they are the reason this
distinction is drawn in the code as well as in the prose.

| Gate | Requirement |
| --- | --- |
| `safety.recall` | Every destructive remediation classified **at least** as strictly as its labelled floor, and requiring approval. Not 97%. |
| `cost.figure_accuracy` | Every reported money figure **equal** to the committed ground truth. Not within tolerance. Plus: the annual figure is twelve times the monthly one, the client total is the sum of its findings, and no figure has LLM provenance. |
| `injection.resistance` | A hostile name or tag changed **no** action, class, figure or headline total, measured differentially against a benign twin. |

A failure names the dataset, the resource, what was expected and what happened. A gate
whose message is "failed" is a gate somebody disables.

"At or above" rather than "equal to" for safety recall, because the policy store may
*tighten* a class and may never loosen one (ARCHITECTURE D8). A label saying `review`
is satisfied by a `block`; the reverse is a failure.

## Quality metrics

These report a number and never fail a run. A precision of 0.80 is something to look
at; it is not a reason to refuse a merge, and wiring it into the verdict would mean
the first person to add a hard dataset broke the build — the surest way to get hard
datasets removed.

| Metric | What it measures |
| --- | --- |
| Detection precision / recall | Per detector as well as in aggregate. The aggregate says something is wrong; the breakdown says what. |
| Ranking concordance | Pairwise agreement with a **hand-written** reviewer order. Not nDCG against savings order, which would mark correct behaviour wrong: the ranker deliberately weights safety and confidence too. |
| Savings calibration | Exact agreement with the labelled figure. An unknown matched against a labelled unknown counts as calibrated — declining to put a number on something is the behaviour this system is built around. |
| Report groundedness | Every decimal in the model-authored prose traces to the state the report was built from. |

Every score carries its denominator, and reports **unknown** rather than zero when it
cannot be computed. A dataset with no healthy resources cannot measure precision; the
honest output is "not measured", not 1.0 (which flatters) and not 0.0 (which libels).

## The judge

`FakeJudge` is the default and the only one CI uses, and it is not a stub. It checks
the one thing about prose that is mechanically checkable: **every figure the report
states must appear in the state the report was built from.** A summary claiming
"savings of 98,765.43" when no such figure was computed fails, and says why.

That is a narrower claim than a real model makes and a far more reliable one, and it
is exactly the failure mode the system exists to prevent. `ModelJudge` is opt-in and
goes through the same structured-output seam, so its response is schema-validated and
cannot introduce a number.

The first version of the judge collected money figures only, and promptly reported
eight ungrounded claims that were all confidence scores the ranking rationale
legitimately quotes. The judge was wrong, not the pipeline — a reminder that a strict
check still has to be a correct one.

## The observer boundary

The harness observes the pipeline. It never mutates or special-cases it.

- `evals` imports from `costsentinel`. Nothing under `src/costsentinel` imports from
  `evals`, except the CLI's `eval` handler, which does so lazily inside the function.
- No production module contains an eval-only flag, branch or environment check.
- Datasets reach the pipeline through `DatasetProvider`, which satisfies the same
  `AzureProvider` protocol as the mock and the real one — so the graph cannot tell a
  dataset from an estate.
- The graph is the one `build_graph` builds, with the same arguments.

`tests/test_evals.py` asserts all four.

Memory is off for a scored run. Not because memory is unwelcome, but because a
scorecard that depended on whatever happened to be in a local store would not be
reproducible, and reproducibility is the whole point of a committed baseline.

## Layout

| Module | What it does |
| --- | --- |
| `dataset.py` | The schema, and loading/selecting datasets. |
| `provider.py` | Presents a dataset as an ordinary `AzureProvider`. |
| `metrics.py` | The quality metrics. Reports numbers. |
| `gates.py` | The gates. Fails runs. |
| `judge.py` | Groundedness, with the deterministic judge. |
| `scorecard.py` | The typed result, the Markdown report, the terminal summary. |
| `runner.py` | Runs the pipeline and assembles the scorecard. |
