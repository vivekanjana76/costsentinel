# Cost review — acme-industrial

> **This report describes a synthetic demonstration estate.** Every subscription, resource and figure in it is invented. It is not any real client's data and must not be read as a statement about one.

| | |
| --- | --- |
| Period | 2026-09-02 to 2026-10-01 (30 days) |
| Generated | 2026-10-19 |
| Subscriptions in scope | 1 |
| Resources examined | 12 |
| Headline | high |
| Report id | `rpt-run-golden` |

---

## Executive summary

| | |
| --- | --- |
| Observed spend over the period | 2,400.00 USD |
| **Projected actionable savings** | **628.22 USD / month** |
| Annualised | 7,538.64 USD / year |
| Actionable findings | 4 |
| Awaiting your approval | 4 |
| CostSentinel may apply automatically | 0 |
| Advice for a person to carry out by hand | 1 |
| Findings we could not price | 1 (listed below, excluded from the total) |
| Flagged but exempt | 1 (costing 140.16 USD / month) |
| Spend changes to investigate | 1 |
| Subscriptions whose spend we could not assess | 1 |

CostSentinel identified 2 actionable finding(s) worth 560.64 USD per month against observed spend of 2,400.00 USD.

_The paragraph above was written by a language model from the figures in the table. Every figure in this report is arithmetic over provider data; no number in it came from a language model._

---

## Recommended actions

Ordered by a weighted composite of the saving, how safe the action is and the detector's confidence — not by saving alone. A smaller, safer, more certain saving is often the better first move.

**Nothing in this report has been applied.** CostSentinel has no execution path: every action below is a proposal, and the gated ones cannot proceed without a recorded decision from you.

### 1. vm-reporting-03 — deallocate virtual machine

| | |
| --- | --- |
| What | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000c001` |
| Costs now | 280.32 USD / month |
| **Saving if applied** | **280.32 USD / month** (3,363.84 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.90 |

**How the saving was derived.** Deallocating vm-reporting-03 removes the whole 280.32 USD monthly charge.

**Why this exists.** vm-reporting-03 outlived the workload it was provisioned for.

- cpu max pct was observed as 2.1

**Why it sits here.** vm-reporting-03 sits here because its saving is the largest available.

**Evidence.**

- cpu_max_pct: 2.1
- observation_days: 30

**Re-checked immediately before anything changes.**

- Virtual machine still exists and is still running.

### 2. vm-etl-02 — deallocate virtual machine

| | |
| --- | --- |
| What | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000c001` |
| Costs now | 280.32 USD / month |
| **Saving if applied** | **280.32 USD / month** (3,363.84 USD / year) |
| Figure | **estimated** -- depends on a modelling assumption, stated in the basis |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.90 |

**How the saving was derived.** Deallocating vm-etl-02 removes the whole 280.32 USD monthly charge.

**Why this exists.** vm-etl-02 outlived the workload it was provisioned for.

- cpu max pct was observed as 2.1

**Why it sits here.** vm-etl-02 sits here because its saving is the largest available.

**Evidence.**

- cpu_max_pct: 2.1
- observation_days: 30

**Re-checked immediately before anything changes.**

- Virtual machine still exists and is still running.

### 3. snap-old — deallocate virtual machine

| | |
| --- | --- |
| What | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000c001` |
| Costs now | 280.32 USD / month |
| **Saving if applied** | **67.58 USD / month** (810.96 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `block` -- CostSentinel will never perform this itself, however it is approved. It is advice for a person to carry out by hand. |
| Detector confidence | 0.90 |

**How the saving was derived.** Deallocating snap-old removes the whole 280.32 USD monthly charge.

**Why this exists.** snap-old outlived the workload it was provisioned for.

- cpu max pct was observed as 2.1

**Why it sits here.** snap-old sits here because its saving is the largest available.

**Evidence.**

- cpu_max_pct: 2.1
- observation_days: 30

**Re-checked immediately before anything changes.**

- Virtual machine still exists and is still running.

---

## Worth investigating — saving unknown

These are real findings. CostSentinel could not derive a figure for them, so they are **excluded from the savings total rather than counted as zero**, and they rank below every priced finding because we will not claim something we cannot cost is your bigger opportunity. That is not the same as saying they do not matter — each one says why no figure could be derived, and several are worth a decision.

### sql-reporting — deallocate virtual machine

| | |
| --- | --- |
| What | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000c001` |
| Costs now | 280.32 USD / month |
| Saving | unknown USD — see below |
| Approval | `review` -- requires your explicit approval before anything changes. |

**Why no figure.** sql-reporting: no cheaper tier exists, so no saving can be quantified.

**Why this exists.** sql-reporting outlived the workload it was provisioned for.

**Why it sits here.** sql-reporting sits here because its saving is the largest available.

---

## Flagged but exempt — no action proposed

Real waste signals on resources **your own tags** mark as protected: a disaster-recovery standby, a retention requirement, an explicit cost exemption. They are shown because the spend is real and you may want to revisit it, and they are **excluded from the savings total** because nobody is proposing to recover it. The figure beside each one is what you are currently paying, not a saving available to you.

### vm-dr-standby — disaster recovery

| | |
| --- | --- |
| What we detected | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000c001` |
| **Costs now** | **140.16 USD / month** |
| Exempt because | `role=disaster-recovery-standby` |
| Rule | `builtin-exemptions-v1:disaster_recovery` |

This resource is tagged as a disaster-recovery standby. Being idle is what a standby is for. Tag: role=disaster-recovery-standby.

---

## Spend changes — investigate

Spend in these subscriptions moved. This is **not** a list of waste and not a list of actions: a change in spend is something to look into, and the difference shown is what you are now paying differently rather than an amount anyone has identified a way to recover. None of it appears in the savings total, and CostSentinel proposes nothing here.

### 00000000-0000-4000-8000-00000000c001 — level shift

| | |
| --- | --- |
| Baseline | 120.24 USD / day |
| Observed | 209.79 USD / day |
| **Difference** | **89.55 USD / day** (increase) |
| Window | 2026-09-25 to 2026-10-01 |
| How unusual | 25.31 robust standard deviations |
| Attributed to | SQL Database - vCore hours |
| Detector confidence | 0.85 |

Daily spend rose from a baseline of 120.24 USD to 209.79 USD over 2026-09-25 to 2026-10-01, a difference of 89.55 USD.

**What changed.**

| Meter | Was | Now | Difference | Share of the change |
| --- | --- | --- | --- | --- |
| SQL Database - vCore hours | 24.04 USD / day | 113.95 USD / day | 89.91 USD / day (increase) | 100% |

A model's note about the level shift.

---

## Spend we could not assess

These subscriptions were examined and no conclusion was drawn. Days missing from a cost series are **unknown, not zero** — reading a gap as zero spend would produce a confident and entirely fictional drop, so nothing is claimed instead.

- 00000000-0000-4000-8000-00000000c001: 5 of 30 day(s) have no data, which is unknown, not zero.

---

## Awaiting your decision

| Resource | Action | Class | Saving / month |
| --- | --- | --- | --- |
| vm-reporting-03 | deallocate virtual machine | `review` | 280.32 USD |
| vm-etl-02 | deallocate virtual machine | `review` | 280.32 USD |
| snap-old | deallocate virtual machine | `block` | 67.58 USD |
| sql-reporting | deallocate virtual machine | `review` | unknown USD |

---

## Notes

- Nothing in this report has been applied.

---

## How this report was produced

| | |
| --- | --- |
| Figures derived from | Azure Retail Prices |
| Data retrieved | 2026-10-01 |
| Savings that are observed costs | 2 |
| Savings that are modelled estimates | 1 |

**Estimated or observed.** An *observed* saving is the provider's reported cost for something being removed outright — delete the disk, that charge stops. An *estimated* saving depends on a modelling assumption, such as which smaller SKU a workload will fit on; each one states its assumption in its basis. Both are arithmetic over provider data and neither is produced by a model, but only the first is a measurement.

**Unknown is not zero.** Where CostSentinel cannot derive a figure it prints `unknown` and excludes the finding from the totals. It never prints a zero it has not measured.

**Synthetic data.** The rates above are real published Azure prices, but the estate they are applied to is invented: these subscriptions, resources and utilisation figures describe no real client. Any total in this report is therefore arithmetic that is correct about nothing.
