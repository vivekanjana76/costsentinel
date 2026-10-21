# The operator surface

Four captures of `costsentinel`'s dashboard running against the **synthetic mock
estate**. Every subscription, resource and figure in them is invented; the synthetic
banner at the top of each page is derived from the report's own provenance and its
nil-prefixed subscription ids, which is the same marker `scripts/check_no_real_data.py`
uses — so a screenshot cannot omit it.

Taken from the served pages (`DASHBOARD_ENABLED=true`, `DASHBOARD_CAN_DECIDE=true`), not
from a mock-up.

| | |
|---|---|
| [1 — estate overview](1-estate-overview.jpg) | Who is in scope and what is waiting. The note under *Awaiting a decision* is on every page that shows a proposal. |
| [2 — a client's report](2-client-report.jpg) | The same `ClientReport` the Markdown renderer turns into a document, with the same sections. Every figure here is a string the report already produced; the view layer computes none. |
| [3 — the approval queue](3-approval-queue.jpg) | What a paused sweep is waiting on, with the figures the report showed. Approve, reject or defer, each needing a stated reason. |
| [4 — after a decision](4-after-a-decision.jpg) | `vm-analytics-01` has gone and the count has dropped from 11 to 10. The decision went through the same path `costsentinel approvals decide` uses, and produced the same record and audit events. |

**Approving changes nothing in a cloud.** It records a decision and hands the action to
a person — CostSentinel has no execution path, which Phase 10 proves four independent
ways and this surface does not weaken: it constructs no decision record of its own and
calls the same three functions the CLI calls.
