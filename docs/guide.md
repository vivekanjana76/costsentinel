# CostSentinel — operator guide

The [README](../README.md) is the front door: what CostSentinel is, what makes it
different, and how to run it in two minutes. This guide is the rest: every command, what
each surface does, and the safety properties behind them. The design and its decision log
are in [ARCHITECTURE.md](../ARCHITECTURE.md); a first real run starts with
[SECURITY.md](../SECURITY.md).

Everything below runs offline with no credentials unless a section says otherwise.

- [What ships today](#what-ships-today)
- [Install and test](#install-and-test)
- [Scanning](#scanning)
- [Reading a report](#reading-a-report)
- [The operator surface](#the-operator-surface)
- [Approving something](#approving-something)
- [The learning loop](#the-learning-loop)
- [Measuring whether any of this works](#measuring-whether-any-of-this-works)
- [When the model is unavailable](#when-the-model-is-unavailable)
- [Talking to another agent](#talking-to-another-agent)
- [Scanning a real Azure estate](#scanning-a-real-azure-estate)
- [The API](#the-api)
- [Resumability](#resumability)
- [Observability](#observability)
- [Refreshing prices](#refreshing-prices)
- [Repository layout](#repository-layout)
- [Safety posture](#safety-posture)

## What ships today

| Piece | Status |
|---|---|
| Domain model | Provenance and verification on every derived fact; `MoneyAmount` refuses LLM provenance |
| Provider seam | `AzureProvider` protocol; a deterministic `MockAzureProvider` (a synthetic estate of four subscriptions across two clients, with committed cost-series shapes); and a read-only real provider behind the `azure` extra |
| Pricing | Every figure traces to a committed snapshot of the public Azure Retail Prices API, each price naming its meter; refreshed by hand with `scripts/refresh_prices.py` |
| LLM seam | `LLM` protocol + `FakeLLM` by default; Azure OpenAI (keyless or keyed) and Gemini adapters behind the `llm` extra |
| Detectors | Eight deterministic detectors: orphaned disk, unattached IP, idle VM, oversized VM, stale snapshot, idle SQL, oversized App Service plan, unused reservation |
| Graph | Supervisor + Anomaly Scout, Root-Cause Analyst, Savings Estimator, Optimization Planner, Policy Guard, Report Author, plus the memory consult step and the approval gate |
| Routing | Proceed, retry (bounded), escalate, require-approval, short-circuit — each a recorded decision with a reason |
| Ranking | A deterministic weighted composite of savings, safety and confidence. The model narrates the order; it cannot change it. An unquantified saving ranks below every quantified one, as a tier rather than a weight |
| Exemption | Deterministic tag reading: a resource that is idle or stale by design is surfaced with its reason and excluded from the actionable savings total |
| Spend anomalies | Robust z-score with a weekday profile over the daily cost series. Spikes and level shifts distinguished; a gap in the series is unknown, not zero; an anomaly never reaches an action |
| Spend attribution | Deterministic arithmetic over a per-meter cost breakdown: the component whose share of the change exceeds its share of the spend. A proportional rise is reported as having no single cause |
| Reporting | Markdown renderer; every number in the document must trace to a figure the pipeline produced, as a blocking gate. PDF behind an optional `reports` extra |
| Model resilience | Optional fallback to a secondary backend and an optional per-sweep token ceiling. Either one degrades the prose and no figure, finding or classification |
| Governance | Every action classified `allow` / `review` / `block`. **No execution path exists** |
| Approval gate | A gated action halts the sweep at a durable checkpoint. The process may exit; a decision recorded later from another process resumes it. Approving hands the action to a person and records the hand-off |
| Operator surface | A read-only dashboard over the **same** `ClientReport` the report renders, computing no figure. The queue page can record a decision through the one audited path the CLI uses; off loopback without an authenticator it refuses to start |
| A2A | One read-only Agent Card in the A2A v1 wire shape, JWS-signed and verified in CI by an independent implementation; tasks can be submitted, polled, collected and cancelled. Off by default, no advertised write |
| Observability | Structured JSON logs correlated by `run_id`; a tracing seam with a no-op default; token, cost and latency per run |
| Entry points | `costsentinel scan`, `report`, `approvals list\|decide`, `decide`, `decisions`, `eval`, `dashboard`, `serve`, `config`, `POST /scan`, and the opt-in A2A and dashboard surfaces |

## Install and test

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run pytest                 # with coverage; fails under 90%
uv run ruff check .
uv run ruff format --check .
uv run pyright                # strict on src
```

No `.env` is needed. `MODE=mock` and `LLM_BACKEND=fake` are the defaults and require
nothing. Copy `.env.example` to `.env` only to change a default.

## Scanning

```bash
uv run costsentinel scan                                  # every client, as JSON
uv run costsentinel scan --client northwind-energy --summary   # one client, readable
```

## Reading a report

[`sample-report.md`](sample-report.md) is a real rendered output, committed so it can be
read without running anything. It describes the synthetic mock estate and is
banner-marked as such: the rates in it are real published Azure prices, applied to an
invented estate.

```bash
uv run costsentinel report                                   # writes under tmp/reports (gitignored)
uv run costsentinel report --client northwind-energy --stdout
uv sync --extra reports && uv run costsentinel report --pdf  # PDF needs the optional extra
```

The document separates five claims a client must not conflate: ranked **recommended
actions**; **worth investigating — saving unknown**, where a finding appears by name with
the reason no figure could be derived; **flagged but exempt**, which shows what a
resource costs now and never a saving; **spend changes — investigate**, which shows a
difference and proposes nothing; and **spend we could not assess**, because a gap in a
cost series is unknown rather than zero. A section with nothing in it is omitted, so the
committed sample shows four of them (its cost series have no gap);
[`tests/golden/full-report.md`](../tests/golden/full-report.md) shows all five.

Every number in the rendered document must trace to a figure the pipeline produced. That
is a blocking gate (`rendered.figure_accuracy`): rendering may copy a figure and may
never compose one, so a rounded total or a sum computed in the layout fails the build.
Model-written sentences are quoted verbatim and graded for groundedness in the rendered
text.

## The operator surface

A read-only dashboard over the same `ClientReport` the Markdown report renders — the same
object, the same sections, in the same order. [Four captures](dashboard/) of it on the
mock estate are committed.

```bash
uv run costsentinel dashboard                       # write the pages to files
DASHBOARD_ENABLED=true uv run costsentinel serve    # or serve them, on BIND_HOST (loopback by default)
```

**The view computes no figure.** Every value on a page is a string the report already
produced, checked by the same function the rendered-report gate uses. The stylesheet is
inline and nothing is fetched at runtime.

The queue page can record a decision when `DASHBOARD_CAN_DECIDE=true`, by calling the
same three functions `costsentinel approvals decide` calls. There is no second decision
implementation, asserted in both directions.

**An unsafe decision surface refuses to start.** With `DASHBOARD_CAN_DECIDE=true`, a
non-loopback `BIND_HOST` and no authenticator, the server does not start, and the error
says how to fix it. A decision that arrives on a non-loopback socket is refused even if
the server was started some other way. Nothing verifies who an approver is yet (a Phase
10b item), so every decision records its approver as **self-asserted and unverified**.

## Approving something

A sweep that proposes a `review` or `block` action writes the report, halts at a durable
checkpoint and exits. The decision can be made later, from a different terminal.
[`demo/approval-resume.txt`](demo/approval-resume.txt) is a captured run of exactly this,
one process per command.

```bash
uv run costsentinel approvals list
uv run costsentinel approvals decide rec-orphdisk-2923594012 \
  --approve --reason "confirmed orphaned; platform team will delete it" \
  --approver "you@example.com"
```

`--approve` hands the action to a person and records the hand-off. `--reject` resolves
it. `--defer` records "not now" and **leaves it in the queue**, and the queue shows how
many times something has been deferred. A reason is mandatory on all three.

**Approving does not apply anything.** There is no execution path, and that is
structural: no module imports a mutating Azure client, no approval record carries
anything callable, no combination of decision and action class reaches a mutation, and
approving the mock estate's disk deletion makes zero provider calls. Four separate tests
in [`tests/test_approval_gate.py`](../tests/test_approval_gate.py).

## The learning loop

CostSentinel remembers what a client decided and does not re-pitch it. The default store
is a local SQLite file and the default embedder is deterministic, so no database is
needed. [`demo/learning-loop.txt`](demo/learning-loop.txt) is a captured run.

```bash
uv run costsentinel scan --client northwind-energy --summary
uv run costsentinel decisions --client northwind-energy      # ids you can decide on
uv run costsentinel decide rec-idlevm-4d7f269994 --reject \
  --reason "owner confirmed this is a quarter-end batch host"
uv run costsentinel scan --client northwind-energy --summary
```

The second scan reports `1 previously declined`, and the finding carries
`memory: Previously declined on <date>, reason: ... (was #1 on merit)`.

**Memory reorders and annotates. It never recomputes.** Every monetary figure in the
second report is byte-identical to the first and the same findings are present. A
declined recommendation is demoted rather than dropped, because declining it does not
make the waste disappear.

For a durable store shared across runs and operators:

```bash
docker compose up --build     # app plus postgres-with-pgvector
```

## Measuring whether any of this works

`costsentinel eval` runs the **real** pipeline against committed labelled estates and
scores it, fully offline. The current scorecard is [`evals/REPORT.md`](../evals/REPORT.md).

```bash
uv run costsentinel eval            # every dataset; writes evals/REPORT.md
uv run costsentinel eval --smoke    # the fast subset CI runs on every commit
```

**Four things are gates, not scores**, and any failure exits non-zero:

- `safety.recall` — every destructive remediation classified at least as strictly as its
  floor and requiring approval. 100% or the build fails.
- `cost.figure_accuracy` — every reported money figure equals the committed ground truth
  exactly, not within a tolerance.
- `rendered.figure_accuracy` — every number in the rendered document is a figure the
  pipeline produced.
- `injection.resistance` — a hostile resource name or tag changes no action, class or
  figure.

Precision, recall, ranking, attribution and groundedness report a number and never block.

**The adversarial cases are the point of the datasets.** When the harness landed in
Phase 5, all three failed: a disaster-recovery standby was reported as idle, a snapshot
under a HIPAA retention tag as stale, and a genuinely oversized machine hid just above
the rightsizing thresholds. Phase 6 closed all three — measure first, then fix — and the
scorecard went from 0/3 to 3/3 with detection precision and recall at 1.00. For the two
resources that are idle *by design*, handled correctly means **surfaced but not
actionably recommended**.

## When the model is unavailable

Every figure, action class, rank and approval-queue entry is arithmetic over provider
data. The model narrates those facts and cannot change one, so a backend outage, a
fallback to the secondary, or a token ceiling costs the **prose** and nothing else.

```bash
LLM_FALLBACK_BACKEND=gemini   # retry on a second backend (must differ from LLM_BACKEND)
LLM_TOKEN_BUDGET=200000       # cap model tokens per sweep; unset means uncapped
```

A degraded report says so: the executive summary reads "Narrative unavailable (token
budget reached)" and a note states that the figures are unaffected. The property is
tested by building the same estate's report three ways and comparing every deterministic
field ([`tests/test_degradation.py`](../tests/test_degradation.py)).

## Talking to another agent

One read-only A2A v1 Agent Card, served at `/.well-known/agent-card.json` when
`A2A_ENABLED=true`, with three skills: request a cost review, fetch the current
findings, fetch the pending-approval queue. No advertised skill decides an approval or
changes a resource, and a vocabulary of twenty-two mutating verbs is checked against
every string on the card and every route.

The card is **signed with a JWS per A2A v1** (ES256 over the RFC 8785 canonical card,
detached payload), verified in CI by PyJWT, an independent implementation, and by the
spec's own canonicalisation example. The served card is committed at
[`a2a/agent-card.json`](a2a/agent-card.json). Interop with a real external agent is
Phase 12b and has not been tested.

## Scanning a real Azure estate

Read [SECURITY.md](../SECURITY.md) first. The short version:

```bash
uv sync --extra real
az login
uv run costsentinel scan --real --subscription <subscription-id>
```

The run is read-only — no write operation exists in the codebase — and its report goes to
`var/real-runs/`, which is gitignored and refused by a pre-commit guard. It needs
`Reader` and `Cost Management Reader` on the named subscriptions and nothing more, and it
refuses to run without an explicit `--subscription`. Pilot on a personal or sandbox
subscription you own, get written approval before any client estate, and never commit
the output. No real run has been executed as part of building this project.

## The API

```bash
uv run costsentinel serve       # or: uv run uvicorn costsentinel.api.app:app --reload
curl -X POST localhost:8000/scan -H 'content-type: application/json' -d '{}'
curl -X POST localhost:8000/scan -H 'content-type: application/json' \
  -d '{"client": "northwind-energy"}'
```

`GET /health` reports the active mode and LLM backend. The OpenAPI docs are at `/docs`.

## Resumability

Each client's scan is a LangGraph thread keyed by its run id, checkpointed to
`.costsentinel/checkpoints.sqlite` after every node. An interrupted sweep resumes from
the last completed node, and the same mechanism lets the graph halt for days awaiting an
approval.

## Observability

Structured JSON logs carry the `run_id` on every event in a sweep. Each run records token
usage, computed cost and latency per model call, and the routing decision that chose the
model. Tracing is a seam with a no-op default; to send traces to Langfuse:

```bash
uv sync --extra tracing
# then set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY
```

`langfuse` is an optional extra and never a hard dependency. CI does not install it.

## Refreshing prices

Every monetary figure traces to the committed snapshot at
`src/costsentinel/providers/data/price_catalogue.json`, a pull of the public Azure Retail
Prices API (`is_real_api_pull: true`), in which every price names the meter it came from.
Pay-as-you-go Linux rates only.

```bash
uv run python scripts/refresh_prices.py --dry-run   # fetch and report, write nothing
uv run python scripts/refresh_prices.py             # write the snapshot
```

Run it by hand only; CI never does, and a test asserts that. Review the diff before
committing, because savings assertions are baselined against these figures.

## Repository layout

```
src/costsentinel/
  config.py          settings; MODE and LLM_BACKEND flags (default mock + fake)
  domain/            Pydantic v2 types -- every module boundary
  providers/         AzureProvider protocol, MockAzureProvider, the read-only real provider
  providers/data/    committed price catalogue snapshot
  llm/               LLM protocol, FakeLLM, real adapters, fallback and token budget, routing
  agents/            the six specialists, the supervisor, memory consult, approval gate,
                     detectors, savings, ranking, anomalies, attribution
  graph/             graph assembly, checkpointer, sweep runner, approvals
  guardrails/        policy store, action classes, exemption policy, approval types
  memory/            episodic + semantic stores, embedder, recall, decisions
  reports/           Markdown and PDF rendering
  dashboard/         the operator surface: view model, HTML, routes, authenticator seam
  a2a/               the read-only Agent Card, JWS signing, task lifecycle
  observability/     structured logging + the tracing seam
  tools/             read-only MCP Azure tool server
  api/               FastAPI app
  cli.py             CLI entry point
scripts/             refresh_prices.py (manual), check_no_real_data.py (pre-commit), demo.sh
evals/datasets/      committed labelled estates, with ground truth
evals/harness/       the scorecard harness: metrics, gates, judge, runner
evals/REPORT.md      the committed scorecard
docs/                this guide, the sample report, dashboard captures, demo transcripts, the A2A card
tests/
```

## Safety posture

See [SECURITY.md](../SECURITY.md) for the full picture, the permissions a real run needs,
and how to report a vulnerability.

- **Nothing destructive runs. There is no execution path.** `ToolOperation` has one
  member, the MCP server serves exactly what the registry holds, and no Azure SDK able
  to mutate a resource is installed — [`tests/test_readonly.py`](../tests/test_readonly.py).
- Real Azure access is `DefaultAzureCredential` only. There is no client-secret setting.
- A real run's output is gitignored and a pre-commit hook refuses to commit it, along
  with any subscription id that does not look deliberately synthetic.
- Memory may change the order of findings and their annotations, and never a number.
- Safety recall is a gate, not a metric, and `costsentinel eval` has been shown to go
  red on a deliberately broken classifier and green again when it is restored.
- Every action is classified; the built-in action-class floor cannot be loosened by
  configuration.
- A spend anomaly says "investigate" and can never become an action, asserted four ways.
- Exemption reads tags and can only reduce what is proposed: a tag can suppress a
  recommendation and can never loosen a class, make something automatable, or raise a
  rank. A client's tag overlay may add exemptions and never remove a built-in one.
- LLM output and cloud resource metadata are untrusted: validated against a schema and
  passed to models as delimited data, never as instructions.
- No secrets in code or git. CI needs no secrets and no network access to a cloud or LLM
  provider.
