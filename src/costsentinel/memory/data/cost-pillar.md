# Cost-optimisation guidance (synthetic summary)

> **This document is synthetic.** It is a short restatement, in our own words, of
> widely-held cloud cost-optimisation practice, written for CostSentinel's demo
> estate. It is not a copy of Microsoft's Well-Architected Framework or of any other
> vendor's documentation, and it is not a substitute for reading the real thing. No
> figure in it is a price.

## Pay only for what is consumed

The dominant source of cloud waste is not inefficient workloads but resources that
exist and are not used. Orphaned storage, idle compute, unattached addresses and
forgotten test environments all bill at the full rate while delivering nothing. They
are also the easiest waste to remove, because removing them changes no behaviour that
anybody depends on.

Finding them requires inventory and utilisation together. Inventory alone shows what
exists; utilisation alone shows what is busy. Only the two correlated show what exists
and is not busy.

## Measure before resizing

A resource should be resized against observed utilisation over a window long enough
to include its real peak, not against a snapshot. A fortnight is a reasonable
minimum, and a month is better for anything with a weekly or monthly rhythm.

Resizing on average utilisation alone is a common and expensive mistake. A workload
averaging ten per cent with a daily spike to ninety per cent is correctly sized for
its spike, and shrinking it to fit the average produces an outage rather than a
saving. Peak utilisation plus deliberate headroom is the defensible basis.

## Prefer the reversible action

Where two remediations save comparable amounts, the reversible one is worth more than
the figure suggests. Deallocating a virtual machine can be undone in minutes;
deleting its disks cannot be undone at all. A recommendation's value is its saving
discounted by the cost of being wrong about it, and the cost of being wrong about a
deletion is unbounded.

This is why the order in which findings are presented matters as much as the findings
themselves. A list sorted purely by saving pushes the most dangerous actions to the
top, which is precisely the wrong advice to give first.

## Attribute cost to someone

Cost that is nobody's responsibility does not get optimised. Tagging resources with
an owner and a cost centre is a prerequisite for every other practice here, because
every remediation eventually requires asking a person whether something is still
needed.

Tag hygiene is therefore not administrative tidiness. An untagged estate cannot be
optimised at all, however good the detection is.

## Right-size commitments, carefully

Reserved capacity and savings plans reduce rates substantially in exchange for
commitment. The risk is symmetric: under-committing leaves money on the table, and
over-committing locks in spend on capacity that is not used.

Commitment waste is distinct from every other kind, because the money is already
spent. The question is not "how much can we avoid" but "how much of what we have
already paid for can we recover", and the answer depends on contractual exchange
terms rather than on anything observable in the estate.

## Review continuously, not annually

An estate drifts. Resources are created for a migration and never removed, a test
environment outlives its test, a machine is upsized for a load spike and never
brought back down. An annual review finds a year's accumulation, by which point the
money is gone.

Continuous review also produces better decisions, because the person who created a
resource is still there to be asked.

## Make the trade-off visible

A cost recommendation is a trade-off between money, risk and effort, and a client
can only accept it if they can see the trade-off. A saving figure with no stated
basis, no risk classification and no indication of what breaks if the assumption is
wrong is not actionable advice — it is a number.

The corollary is that advice which has already been declined should be presented as
such. Repeating it as a fresh discovery misrepresents the history and wastes the
reviewer's attention on a decision they have already made.
