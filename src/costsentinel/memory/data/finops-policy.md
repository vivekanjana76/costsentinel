# Northwind Energy — Cloud Cost Policy (synthetic)

> **This document is synthetic.** It was written for CostSentinel's demo estate and
> describes no real organisation's rules. "Northwind Energy" is a fictional client in
> the mock provider. Replace it with your own policy before a real engagement, and do
> not commit a client's actual policy to a public repository.

## 1. Ownership and tagging

Every subscription carries a `client` tag naming the business the subscription
belongs to, and every resource carries an `owner` tag naming a person or a team. A
resource with no owner tag has no one to ask before changing it, so it may be
reported and may not be altered.

Untagged resources are a finding in their own right. Tagging is reversible and
costs nothing, so applying a missing tag is the one remediation this policy permits
without a human decision.

## 2. Orphaned and unattached resources

A managed disk that is not attached to any virtual machine is paid for in full and
serves nothing. It may be deleted only once two conditions hold: the disk has been
unattached for at least fourteen days, and a snapshot exists or the owner has
confirmed in writing that no restore is expected.

The fourteen-day window is not arbitrary. A disk detached during a maintenance
window and reattached the next morning is a normal operation, and a tool that
deleted it on day one would be actively harmful.

An unattached standard public IP address bills hourly whether or not traffic flows
through it. Release is low risk, but the address is not recoverable once released, so
any service holding a DNS record or a firewall allow-list entry for that address must
be checked first.

## 3. Idle and oversized compute

A virtual machine is considered idle when average processor utilisation stays below
five per cent across at least fourteen days of observation and no inbound network
traffic is recorded. Deallocation stops compute charges and retains the disks, so it
is reversible: the machine can be started again with its data intact.

Deallocation requires the owner's agreement. Not every idle machine is waste. A
disaster-recovery standby is idle by design, a quarter-end batch host is idle for
eleven weeks out of twelve, and a licence-bound appliance may be impossible to
restart on new hardware. Where a machine is idle by design the owner should say so,
and that decision should be remembered rather than re-raised each month.

Resizing a machine to a smaller SKU is permitted when the observed peak processor
utilisation, multiplied by the configured headroom factor, still fits inside the
smaller SKU. Resizing requires a restart, so it needs a maintenance window.

## 4. Non-production environments

Development, test and sandbox subscriptions are expected to be smaller and cheaper
than production. A non-production resource that has not been accessed for thirty days
should be deallocated, and after ninety days it should be deleted.

Production is treated more strictly in every case. Any action this policy classifies
as needing review in a development subscription needs explicit written approval in a
production one, and no deletion in a production subscription is ever automated.

## 5. Snapshots and backups

A snapshot older than ninety days is retained only where a documented retention
requirement says so. Snapshots accumulate silently: nobody notices the bill from a
snapshot taken before an upgrade two years ago, and nobody is willing to delete it
without being told it is safe.

Snapshots supporting a regulatory retention period are exempt and must be tagged
`retention` with the requirement reference.

## 6. Reservations and commitments

Reservation utilisation is reviewed monthly. A reservation running below seventy per
cent utilisation is wasting committed money that has already been spent, which is
materially different from other waste: the saving is not money avoided but money
recoverable by exchanging or re-scoping the commitment.

Reservations are never cancelled on CostSentinel's advice alone. Exchange and refund
rules are contractual, change without notice, and depend on the enrolment rather than
the subscription.

## 7. Approval and record-keeping

No action that deletes, resizes or deallocates a resource is performed without a
recorded human decision. A decision records who decided, when, and why. The reason is
mandatory: a decision with no reason cannot be explained to the client who has to
live with it, and cannot usefully inform the next review.

A declined recommendation is not forgotten and is not re-raised as though it were
new. It is reported again with the date and reason it was declined, so a client can
see that the cost is still being incurred and reconsider if circumstances changed.
