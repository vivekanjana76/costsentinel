# Eval datasets

Labelled synthetic Azure estates. Each file holds **both halves** — the estate and
what the correct output is — so a reviewer sees them side by side. Splitting them
across two files makes it easy for one to drift from the other, and a label that no
longer matches its estate is worse than no label.

**Every byte here is synthetic.** No real client's estate, no real subscription id, no
real resource name. Subscription ids use the nil-prefixed form
(`00000000-0000-4000-8000-…`) that no real Azure tenant issues, and
`scripts/check_no_real_data.py` is run over these files by a test.

## The datasets

| Id | Tier | What it is for |
| --- | --- | --- |
| `baseline-mixed` | smoke | One healthy and one wasteful resource of each kind the detectors cover, so precision and recall are both measurable. The broadest coverage in the fewest resources. |
| `adversarial-lookalikes` | full | The two ways a cost detector is wrong in the field: a healthy resource that looks wasteful (a DR standby, a compliance-retained snapshot) and a wasteful one that looks healthy (a machine just above the rightsizing threshold). |
| `injection-control` | full | The benign half of the prompt-injection differential. |
| `injection-hostile` | smoke | The same estate with attacker-controlled names and tags. Compared against its control: the advice must be identical. |

The **smoke** tier runs in the normal CI job on every commit and completes in a few
seconds. It is a genuine subset — `baseline-mixed` for detector coverage and
`injection-hostile` for the security gate, which pulls in its benign twin
automatically — and it enforces every gate. A smoke run that skipped the gates would
be worse than none, because it would look like coverage.

## Ground truth is committed, not recomputed

Each wasteful resource carries the **exact** monthly saving the pipeline should
report, as a decimal a human derived from the stated rules and the committed price
catalogue, and reviewed once.

The tempting alternative is to compute the expected figure at scoring time from the
same catalogue. That would make the cost-figure gate compare a calculation to itself
and pass unconditionally. The cost of committing it instead is that refreshing the
price catalogue breaks these files loudly — the same bargain the savings assertions in
`tests/test_graph.py` already make, and the right way round.

A label may say the expected saving is **unknown** (`null`). That is a real answer,
not a gap: a database already on the smallest tier the catalogue carries has no
cheaper target, and a tool that reported its full cost as a "saving" would be claiming
a figure for an action it cannot name.

## Prices come from the catalogue

A dataset names a SKU, a state, a size and whatever was measured.
`evals/harness/provider.py` prices it from
`src/costsentinel/providers/data/price_catalogue.json` — the same path the mock
provider and the real provider use. So a dataset cannot assert a price Azure does not
charge, and a SKU the catalogue does not cover reports an explicitly unknown cost
rather than a guess.

## Adding a dataset

1. Write the file. `evals/harness/dataset.py` is the schema; validation is strict and
   will tell you what is missing.
2. Label **every** resource and reservation, including the healthy ones. An estate of
   nothing but waste cannot measure precision, and an unlabelled resource is neither a
   true positive nor a false one — it is simply uncounted, which is the worst of the
   three.
3. Give every label a `note` saying **why** that is the right answer. Mandatory, and
   enforced: a future disagreement should be with a stated argument rather than with a
   bare boolean.
4. Derive each expected figure from the rules in `src/costsentinel/agents/savings.py`
   and the catalogue. Do not read it off a run — that is how ground truth stops being
   ground truth. (Three of the baseline figures were wrong on the first attempt, and
   the harness caught all three. Working out which side was right is the work.)
5. Run `uv run costsentinel eval` and regenerate `evals/REPORT.md`.

## A note on the expected failures

`adversarial-lookalikes` currently **fails** all three of its hard cases, and that is
deliberate. Those labels record three real gaps:

- the detectors have no tag exemption, so a disaster-recovery standby is flagged as
  idle and a snapshot with a HIPAA retention tag is flagged as stale;
- the rightsizing thresholds sit where a genuinely oversized machine can hide just
  above them.

They are in the dataset to make those gaps visible and measurable rather than to hide
them. Fixing them is Phase 6's job — calibration belongs to the phase that needs it,
and measuring something and immediately changing it would leave neither number
trustworthy.
