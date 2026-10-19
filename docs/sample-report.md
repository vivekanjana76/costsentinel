# Cost review — northwind-energy

> **This report describes a synthetic demonstration estate.** Every subscription, resource and figure in it is invented. It is not any real client's data and must not be read as a statement about one.

| | |
| --- | --- |
| Period | 2026-09-02 to 2026-10-01 (30 days) |
| Generated | 2026-10-19 |
| Subscriptions in scope | 2 |
| Resources examined | 18 |
| Headline | high |
| Report id | `rpt-run-sample` |

---

## Executive summary

| | |
| --- | --- |
| Observed spend over the period | 5,387.88 USD |
| **Projected actionable savings** | **1,421.97 USD / month** |
| Annualised | 17,063.64 USD / year |
| Actionable findings | 8 |
| Awaiting your approval | 8 |
| CostSentinel may apply automatically | 0 |
| Advice for a person to carry out by hand | 1 |
| Spend changes to investigate | 2 |

Across 2 subscription(s) for northwind-energy, CostSentinel identified 8 actionable finding(s) worth 1,421.97 USD per month (17,063.64 USD annually) against observed spend of 5,387.88 USD. The largest single opportunity is vm-analytics-01, where the recommended action is deallocate virtual machine, worth 560.64 USD per month. 8 of these finding(s) require your explicit approval before anything is changed; nothing has been or will be modified without it.

_The paragraph above was written by a language model from the figures in the table. Every figure in this report is arithmetic over provider data; no number in it came from a language model._

---

## Recommended actions

Ordered by a weighted composite of the saving, how safe the action is and the detector's confidence — not by saving alone. A smaller, safer, more certain saving is often the better first move.

**Nothing in this report has been applied.** CostSentinel has no execution path: every action below is a proposal, and the gated ones cannot proceed without a recorded decision from you.

### 1. vm-analytics-01 — deallocate virtual machine

| | |
| --- | --- |
| What | idle virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000a002` |
| Costs now | 560.64 USD / month |
| **Saving if applied** | **560.64 USD / month** (6,727.68 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.90 |

**How the saving was derived.** Deallocating the virtual machine, whose compute charge then ceases (attached disks bill separately and are unaffected) removes the whole 560.64 USD monthly charge for vm-analytics-01. Taken from the provider's reported cost for this virtual_machine, not modelled.

**Why this exists.** vm-analytics-01 in subscription 00000000-0000-4000-8000-00000000a002 shows idle virtual machine because cpu max pct is 3.8; idle threshold pct is 5.0; observation days is 30; state is running; sku is Standard_D16s_v5; monthly cost is 560.64 USD; cpu avg pct is 1.2; network in gb is 4.1. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- cpu max pct was observed as 3.8
- idle threshold pct was observed as 5.0
- observation days was observed as 30
- state was observed as running
- sku was observed as Standard_D16s_v5
- monthly cost was observed as 560.64 USD
- cpu avg pct was observed as 1.2
- network in gb was observed as 4.1

**Why it sits here.** Ranked 1 of 8: quantified; savings 1.000, safety 0.600, confidence 0.900 -> 0.885. Worth 560.64 USD per month at review risk, with detector confidence 0.90.

**Evidence.**

- cpu_max_pct: 3.8
- idle_threshold_pct: 5.0
- observation_days: 30
- state: running
- sku: Standard_D16s_v5
- monthly_cost: 560.64 USD
- cpu_avg_pct: 1.2
- network_in_gb: 4.1

**Re-checked immediately before anything changes.**

- Virtual machine still exists and is still running.
- CPU utilisation has remained below the idle threshold since detection.
- The virtual machine is not a member of an availability set or scale set whose capacity guarantee would be affected.
- No active maintenance or change freeze applies to the subscription.

### 2. vm-batch-02 — resize virtual machine

| | |
| --- | --- |
| What | oversized virtual machine |
| Subscription | `00000000-0000-4000-8000-00000000a001` |
| Costs now | 735.84 USD / month |
| **Saving if applied** | **367.92 USD / month** (4,415.04 USD / year) |
| Figure | **estimated** -- depends on a modelling assumption, stated in the basis |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.75 |

**How the saving was derived.** Resizing vm-batch-02 from Standard_E16s_v5 (16 vCPU, 735.84 USD/month) to Standard_E8s_v5 (8 vCPU, 367.92 USD/month) saves the difference. Both prices are provider catalogue prices for eastus. The target is the smallest same-family SKU that still covers the observed peak of 18.5% with a 2.0x headroom.

**Why this exists.** vm-batch-02 in subscription 00000000-0000-4000-8000-00000000a001 shows oversized virtual machine because cpu avg pct is 6.1; cpu max pct is 18.5; peak threshold pct is 50.0; observation days is 30; sku is Standard_E16s_v5; monthly cost is 735.84 USD. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- cpu avg pct was observed as 6.1
- cpu max pct was observed as 18.5
- peak threshold pct was observed as 50.0
- observation days was observed as 30
- sku was observed as Standard_E16s_v5
- monthly cost was observed as 735.84 USD

**Why it sits here.** Ranked 2 of 8: quantified; savings 0.656, safety 0.600, confidence 0.750 -> 0.656. Worth 367.92 USD per month at review risk, with detector confidence 0.75.

**Evidence.**

- cpu_avg_pct: 6.1
- cpu_max_pct: 18.5
- peak_threshold_pct: 50.0
- observation_days: 30
- sku: Standard_E16s_v5
- monthly_cost: 735.84 USD

**Re-checked immediately before anything changes.**

- Virtual machine still exists and is still running.
- The target SKU is available in the resource's region and zone.
- Observed peak CPU still fits the target SKU with the configured headroom.
- A restart window has been agreed -- resizing restarts the virtual machine.
- No active maintenance or change freeze applies to the subscription.

### 3. sql-dev-sandbox — scale down sql database

| | |
| --- | --- |
| What | idle sql database |
| Subscription | `00000000-0000-4000-8000-00000000a002` |
| Costs now | 444.47 USD / month |
| **Saving if applied** | **222.23 USD / month** (2,666.76 USD / year) |
| Figure | **estimated** -- depends on a modelling assumption, stated in the basis |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.80 |

**How the saving was derived.** Scaling down sql-dev-sandbox from GP_Gen5_4 (4 vCPU, 444.47 USD/month) to GP_Gen5_2 (2 vCPU, 222.24 USD/month) saves the difference. Both prices are provider catalogue prices for eastus. The target is the smallest same-family SKU that still covers the observed peak of 2.1% with a 2.0x headroom.

**Why this exists.** sql-dev-sandbox in subscription 00000000-0000-4000-8000-00000000a002 shows idle sql database because connection count is 0; connection threshold is 0; observation days is 30; sku is GP_Gen5_4; monthly cost is 444.47 USD; cpu avg pct is 0.4. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- connection count was observed as 0
- connection threshold was observed as 0
- observation days was observed as 30
- sku was observed as GP_Gen5_4
- monthly cost was observed as 444.47 USD
- cpu avg pct was observed as 0.4

**Why it sits here.** Ranked 3 of 8: quantified; savings 0.396, safety 0.600, confidence 0.800 -> 0.508. Worth 222.23 USD per month at review risk, with detector confidence 0.80.

**Evidence.**

- connection_count: 0
- connection_threshold: 0
- observation_days: 30
- sku: GP_Gen5_4
- monthly_cost: 444.47 USD
- cpu_avg_pct: 0.4

**Re-checked immediately before anything changes.**

- Database still exists and connection count has remained at zero since detection.
- No scheduled job, report or batch window depends on the current tier.
- The target tier supports the database's current size and feature usage.
- A brief failover has been agreed -- changing tier interrupts connections.
- No active maintenance or change freeze applies to the subscription.

### 4. nw-compute-dsv5-3y — exchange unused reservation

| | |
| --- | --- |
| What | unused reservation |
| Subscription | `00000000-0000-4000-8000-00000000a001` |
| Costs now | 221.44 USD / month |
| **Saving if applied** | **145.04 USD / month** (1,740.48 USD / year) |
| Figure | **estimated** -- depends on a modelling assumption, stated in the basis |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.92 |

**How the saving was derived.** nw-compute-dsv5-3y is 34.5% utilised, so 65.5% of its 221.44 USD monthly amortised cost buys capacity nobody consumes. That money is already committed: this figure is waste being incurred now, and recovering it depends on the exchange terms in the agreement, which is why it is marked estimated.

**Why this exists.** nw-compute-dsv5-3y in subscription 00000000-0000-4000-8000-00000000a001 shows unused reservation because utilisation pct is 34.5; utilisation threshold pct is 60.0; reserved sku is Standard_D4s_v5; quantity is 4; term months is 36; monthly amortised cost is 221.44 USD; expires on is 2028-02-01. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- utilisation pct was observed as 34.5
- utilisation threshold pct was observed as 60.0
- reserved sku was observed as Standard_D4s_v5
- quantity was observed as 4
- term months was observed as 36
- monthly amortised cost was observed as 221.44 USD
- expires on was observed as 2028-02-01

**Why it sits here.** Ranked 4 of 8: quantified; savings 0.259, safety 0.600, confidence 0.920 -> 0.443. Worth 145.04 USD per month at review risk, with detector confidence 0.92.

**Evidence.**

- utilisation_pct: 34.5
- utilisation_threshold_pct: 60.0
- reserved_sku: Standard_D4s_v5
- quantity: 4
- term_months: 36
- monthly_amortised_cost: 221.44 USD
- expires_on: 2028-02-01

**Re-checked immediately before anything changes.**

- Reservation is still active and its utilisation is still below the threshold.
- The exchange or refund window permits the change under the current agreement.
- Finance has confirmed the commercial impact of the exchange.
- A replacement commitment has been sized against actual current usage.

### 5. disk-analytics-01-data — delete orphaned managed disk

| | |
| --- | --- |
| What | orphaned managed disk |
| Subscription | `00000000-0000-4000-8000-00000000a002` |
| Costs now | 38.01 USD / month |
| **Saving if applied** | **38.01 USD / month** (456.12 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.95 |

**How the saving was derived.** Deleting the orphaned disk removes the whole 38.01 USD monthly charge for disk-analytics-01-data. Taken from the provider's reported cost for this managed_disk, not modelled.

**Why this exists.** disk-analytics-01-data in subscription 00000000-0000-4000-8000-00000000a002 shows orphaned managed disk because state is unattached; attached to is none; sku is Premium_LRS_P15; monthly cost is 38.01 USD; age days is 241. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- state was observed as unattached
- attached to was observed as none
- sku was observed as Premium_LRS_P15
- monthly cost was observed as 38.01 USD
- age days was observed as 241

**Why it sits here.** Ranked 5 of 8: quantified; savings 0.068, safety 0.600, confidence 0.950 -> 0.333. Worth 38.01 USD per month at review risk, with detector confidence 0.95.

**Evidence.**

- state: unattached
- attached_to: none
- sku: Premium_LRS_P15
- monthly_cost: 38.01 USD
- age_days: 241

**Re-checked immediately before anything changes.**

- Disk is still unattached, with no attachment recorded since detection.
- A snapshot has been taken and its restore has been verified.
- Disk is not referenced by any image, restore point or backup policy.
- No active maintenance or change freeze applies to the subscription.

### 6. pip-legacy-api — delete unattached public ip

| | |
| --- | --- |
| What | unattached public ip |
| Subscription | `00000000-0000-4000-8000-00000000a002` |
| Costs now | 3.65 USD / month |
| **Saving if applied** | **3.65 USD / month** (43.80 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.95 |

**How the saving was derived.** Releasing the unattached address removes the whole 3.65 USD monthly charge for pip-legacy-api. Taken from the provider's reported cost for this public_ip, not modelled.

**Why this exists.** pip-legacy-api in subscription 00000000-0000-4000-8000-00000000a002 shows unattached public ip because state is unattached; associated with is none; sku is Standard_Static; monthly cost is 3.65 USD; age days is 398. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- state was observed as unattached
- associated with was observed as none
- sku was observed as Standard_Static
- monthly cost was observed as 3.65 USD
- age days was observed as 398

**Why it sits here.** Ranked 6 of 8: quantified; savings 0.007, safety 0.600, confidence 0.950 -> 0.296. Worth 3.65 USD per month at review risk, with detector confidence 0.95.

**Evidence.**

- state: unattached
- associated_with: none
- sku: Standard_Static
- monthly_cost: 3.65 USD
- age_days: 398

**Re-checked immediately before anything changes.**

- Address is still unassociated with any network interface or load balancer.
- Address does not appear in any DNS record, firewall allowlist or client configuration known to the client.
- No active maintenance or change freeze applies to the subscription.

### 7. snap-analytics-baseline — delete stale snapshot

| | |
| --- | --- |
| What | stale snapshot |
| Subscription | `00000000-0000-4000-8000-00000000a002` |
| Costs now | 16.90 USD / month |
| **Saving if applied** | **16.90 USD / month** (202.80 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `review` -- requires your explicit approval before anything changes. |
| Detector confidence | 0.85 |

**How the saving was derived.** Deleting the stale snapshot removes the whole 16.90 USD monthly charge for snap-analytics-baseline. Taken from the provider's reported cost for this snapshot, not modelled.

**Why this exists.** snap-analytics-baseline in subscription 00000000-0000-4000-8000-00000000a002 shows stale snapshot because age days is 412; stale threshold days is 180; sku is Premium_LRS_Snapshot; monthly cost is 16.90 USD. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- age days was observed as 412
- stale threshold days was observed as 180
- sku was observed as Premium_LRS_Snapshot
- monthly cost was observed as 16.90 USD

**Why it sits here.** Ranked 7 of 8: quantified; savings 0.030, safety 0.600, confidence 0.850 -> 0.296. Worth 16.90 USD per month at review risk, with detector confidence 0.85.

**Evidence.**

- age_days: 412
- stale_threshold_days: 180
- sku: Premium_LRS_Snapshot
- monthly_cost: 16.90 USD

**Re-checked immediately before anything changes.**

- Snapshot is still present and still past the retention threshold.
- Snapshot is not the most recent one for its source disk.
- Snapshot is not referenced by any image, restore point or backup policy.
- The source disk still exists, or its loss has been accepted in writing.
- No active maintenance or change freeze applies to the subscription.

### 8. snap-web-01-pre-upgrade — delete stale snapshot

| | |
| --- | --- |
| What | stale snapshot |
| Subscription | `00000000-0000-4000-8000-00000000a001` |
| Costs now | 67.58 USD / month |
| **Saving if applied** | **67.58 USD / month** (810.96 USD / year) |
| Figure | **observed** -- the provider's reported cost for something being removed |
| Approval | `block` -- CostSentinel will never perform this itself, however it is approved. It is advice for a person to carry out by hand. |
| Detector confidence | 0.85 |

**How the saving was derived.** Deleting the stale snapshot removes the whole 67.58 USD monthly charge for snap-web-01-pre-upgrade. Taken from the provider's reported cost for this snapshot, not modelled.

**Why this exists.** snap-web-01-pre-upgrade in subscription 00000000-0000-4000-8000-00000000a001 shows stale snapshot because age days is 603; stale threshold days is 180; sku is Premium_LRS_Snapshot; monthly cost is 67.58 USD. Taken together these indicate the resource outlived the workload it was provisioned for, and no change has been made to it since.

- age days was observed as 603
- stale threshold days was observed as 180
- sku was observed as Premium_LRS_Snapshot
- monthly cost was observed as 67.58 USD

**Why it sits here.** Ranked 8 of 8: quantified; savings 0.121, safety 0.200, confidence 0.850 -> 0.250. Worth 67.58 USD per month at block risk, with detector confidence 0.85.

**Evidence.**

- age_days: 603
- stale_threshold_days: 180
- sku: Premium_LRS_Snapshot
- monthly_cost: 67.58 USD

**Re-checked immediately before anything changes.**

- Snapshot is still present and still past the retention threshold.
- Snapshot is not the most recent one for its source disk.
- Snapshot is not referenced by any image, restore point or backup policy.
- The source disk still exists, or its loss has been accepted in writing.
- No active maintenance or change freeze applies to the subscription.

---

## Spend changes — investigate

Spend in these subscriptions moved. This is **not** a list of waste and not a list of actions: a change in spend is something to look into, and the difference shown is what you are now paying differently rather than an amount anyone has identified a way to recover. None of it appears in the savings total, and CostSentinel proposes nothing here.

### 00000000-0000-4000-8000-00000000a002 — spike

| | |
| --- | --- |
| Baseline | 35.07 USD / day |
| Observed | 137.90 USD / day |
| **Difference** | **103.21 USD / day** (increase) |
| Window | 2026-09-27 to 2026-09-27 |
| How unusual | 99.25 robust standard deviations |
| Detector confidence | 0.70 |

Daily spend rose from a baseline of 35.07 USD to 137.90 USD on 2026-09-27, a difference of 103.21 USD (294.3% of baseline). That is 99.2 robust standard deviations from the 23-day baseline, against a threshold of 5.0.

Spend in 00000000-0000-4000-8000-00000000a002 shows a spike (increase): 35.07 USD per day before, 137.90 USD after. Typically one day stood out and spend then returned to normal, so something ran once. Worth confirming against your change log for that window; nothing here is an action CostSentinel proposes.

### 00000000-0000-4000-8000-00000000a001 — level shift

| | |
| --- | --- |
| Baseline | 120.24 USD / day |
| Observed | 209.79 USD / day |
| **Difference** | **90.24 USD / day** (increase) |
| Window | 2026-09-25 to 2026-10-01 |
| How unusual | 25.31 robust standard deviations |
| Detector confidence | 0.85 |

Daily spend rose from a baseline of 120.24 USD to 209.79 USD over 2026-09-25 to 2026-10-01, a difference of 90.24 USD (75.0% of baseline). That is 25.3 robust standard deviations from the 23-day baseline, against a threshold of 3.5.

Spend in 00000000-0000-4000-8000-00000000a001 shows a level shift (increase): 120.24 USD per day before, 209.79 USD after. Typically the daily run rate has moved and stayed moved, so something was added, resized or left running. Worth confirming against your change log for that window; nothing here is an action CostSentinel proposes.

---

## Awaiting your decision

| Resource | Action | Class | Saving / month |
| --- | --- | --- | --- |
| vm-analytics-01 | deallocate virtual machine | `review` | 560.64 USD |
| vm-batch-02 | resize virtual machine | `review` | 367.92 USD |
| sql-dev-sandbox | scale down sql database | `review` | 222.23 USD |
| nw-compute-dsv5-3y | exchange unused reservation | `review` | 145.04 USD |
| disk-analytics-01-data | delete orphaned managed disk | `review` | 38.01 USD |
| pip-legacy-api | delete unattached public ip | `review` | 3.65 USD |
| snap-analytics-baseline | delete stale snapshot | `review` | 16.90 USD |
| snap-web-01-pre-upgrade | delete stale snapshot | `block` | 67.58 USD |

---

## Notes

- CostSentinel has no execution path in this phase, so nothing in this report has been or can be applied automatically.
- Findings are ranked by a weighted composite of savings, safety and detector confidence, not by savings alone. Each finding shows its own breakdown.
- 2 spend change(s) were detected and are listed separately from the findings. A spend change is something to investigate, not something to act on: its difference is what you are now paying differently, not an amount anyone has identified a way to recover, and none of it appears in the projected savings above.
- 1 finding(s) are classified as blocked: they are reported as advice for a person to carry out by hand, and CostSentinel will not perform them however they are approved.

---

## How this report was produced

| | |
| --- | --- |
| Figures derived from | Azure Retail Prices |
| Data retrieved | 2026-10-01 |
| Savings that are observed costs | 5 |
| Savings that are modelled estimates | 3 |

**Estimated or observed.** An *observed* saving is the provider's reported cost for something being removed outright — delete the disk, that charge stops. An *estimated* saving depends on a modelling assumption, such as which smaller SKU a workload will fit on; each one states its assumption in its basis. Both are arithmetic over provider data and neither is produced by a model, but only the first is a measurement.

**Unknown is not zero.** Where CostSentinel cannot derive a figure it prints `unknown` and excludes the finding from the totals. It never prints a zero it has not measured.

**Synthetic data.** The rates above are real published Azure prices, but the estate they are applied to is invented: these subscriptions, resources and utilisation figures describe no real client. Any total in this report is therefore arithmetic that is correct about nothing.
