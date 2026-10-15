# Pricing and meter notes (synthetic)

> **This document is synthetic and contains no prices.** It explains how Azure's
> billing *shape* works, because that shape is what makes a cost figure defensible or
> indefensible. Every actual rate CostSentinel uses comes from the committed price
> catalogue, which is pulled from the public Azure Retail Prices API and names the
> meter each rate came from. Nothing here is a number to reason from.

## Azure does not bill you for the thing it reports

The SKU a resource reports and the meter that bills it are different identifiers, and
the mapping between them is not always obvious.

A managed disk reports a redundancy type and a provisioned size. It is billed from a
tier meter, and which tier depends on the size — and on the *provisioned* size, at the
band it falls into, not pro rata. A disk provisioned at slightly over a band boundary
is billed at the next band up for its whole life.

A public IP address reports a SKU and an allocation method, and the meter depends on
both together. A storage account reports a redundancy and an access tier separately,
and the meter is the combination.

The consequence for a cost tool is that translating a reported SKU into a billed
meter must be exact or absent. A near-match produces a plausible figure for the wrong
meter, and a plausible wrong number in a client report is worse than an admitted gap.

## Per-hour, per-month and per-GB are not interchangeable

Compute and addresses bill per hour. Disks bill per month at a tier. Snapshots and
blob storage bill per gigabyte-month, which means the rate alone cannot produce a
monthly figure — it needs a quantity, and the quantity belongs to the resource rather
than to the price.

A per-gigabyte rate with no quantity is a known rate and an unknown cost. Those are
different states, and collapsing them produces either a missing finding or an
invented figure.

## Spot and low-priority rates are not on-demand rates

The retail price feed returns several meters per SKU: on-demand, spot, low-priority,
and operating-system variants. Spot capacity is substantially cheaper and can be
evicted at any time, so quoting a spot rate as though it were the on-demand rate
understates a workload's cost dramatically.

Any automated price collection must name the meter it selected rather than taking the
cheapest match, because the cheapest match is almost always the one that does not
apply.

## Regional prices differ, and some meters are global

The same SKU costs different amounts in different regions, so a price is only
meaningful alongside a region. Some meters are genuinely global and carry the same
identifier and rate everywhere, which means two regions showing an identical price is
not necessarily a bug.

A region the catalogue has no price for yields an unknown cost. That is the correct
outcome, and it is fixed by refreshing the catalogue for that region rather than by
substituting a nearby one.

## Reserved rates are list rates

Published reserved-instance pricing is a list rate. What a specific customer paid for
a specific commitment depends on their agreement, and Azure exposes no interface for
retrieving it.

A commitment's amortised cost computed from the list rate is therefore an upper bound
on what the customer actually pays, and any figure derived from it must say so. A
negotiated discount makes it an overstatement, and presenting it as the contract
value would be a fabrication dressed as arithmetic.

## Actual spend and estimated savings are different claims

Actual spend comes from the billing system and is a fact about the past. A projected
saving is arithmetic over a current rate and an assumption about what will change,
and it is a claim about the future.

The two should never be added together or presented in the same column without
distinction. A report that shows them side by side must say which is which, and must
mark a saving that rests on a modelling assumption — a rightsizing target, for
instance — differently from one that is simply the observed cost of something being
removed outright.
