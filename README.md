# CostSentinel

Autonomous, multi-agent FinOps governance for multi-subscription Azure estates.

> **Phase 2 — mock only.** Everything in this repository runs offline against a
> deterministic synthetic estate and a deterministic fake LLM. There are no cloud
> calls, no model calls, and no credentials of any kind. See
> [ROADMAP.md](ROADMAP.md) for what each later phase adds.

## The problem

Large Azure estates quietly waste money. Orphaned disks keep billing after the VM
they belonged to is gone, dev and test resources outlive the project that created
them, VMs are provisioned for a peak that never comes, public IPs sit unattached,
reservations go unused, and egress charges surprise everyone. A managed-service
provider running 12-plus subscriptions for several enterprise and government clients
cannot review this by hand at the cadence it changes — and even when the waste is
found, acting on it is risky without a human signing off, while the client wants
savings they can actually point to in a report. CostSentinel closes that loop: it
continuously ingests cost, usage and inventory signals, detects waste and spend
anomalies, diagnoses root cause, proposes ranked and costed remediations, routes
anything destructive through a human-approval gate, produces client-ready savings
reports, and learns from what each client accepts or rejects.

## How it works

A LangGraph state machine threads one typed state object through a team of
specialist agents — Anomaly Scout, Root-Cause Analyst, Savings Estimator,
Optimization Planner, Policy Guard and Report Author — with a supervisor owning every
retry, escalation and approval route.

Detection is deterministic, so a finding can never be hallucinated. All money
arithmetic is deterministic, so a savings figure is either provider data or an
explicit unknown — `MoneyAmount` raises a validation error if handed LLM provenance,
which makes "never invent numbers" a property of the type system rather than of
prompt discipline. Ranking is deterministic too: the model is asked to *narrate* an
order it has already been given, so a compromised model can change a report's wording
and not what a client is told to do first.

What the model does: explains why waste exists (grounded in evidence the detector
recorded, with any unsupported claim dropped), selects a remediation from a closed
candidate set, and writes the prose. Anything destructive is classified `allow`,
`review` or `block` and cannot reach an execution path without a recorded human
decision.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, the graph diagram, and
the decision log.

### What Phase 2 contains

The complete six-specialist graph from CLAUDE.md section 5, with a supervisor owning
every conditional edge:

| Piece | Status |
|---|---|
| Domain model | Provenance and verification on every derived fact; `MoneyAmount` refuses LLM provenance |
| Provider seam | `AzureProvider` protocol + deterministic `MockAzureProvider` (21 resources, 3 reservations, 3 subscriptions, 2 clients) |
| Pricing | Every figure traces to a committed catalogue snapshot; refreshed by hand with `scripts/refresh_prices.py` |
| LLM seam | `LLM` protocol + `FakeLLM`; Azure OpenAI and Gemini adapters are configuration-error stubs until Phase 3 |
| Detectors | Eight deterministic detectors: orphaned disk, unattached IP, idle VM, oversized VM, stale snapshot, idle SQL, oversized App Service plan, unused reservation |
| Graph | Supervisor + Anomaly Scout, Root-Cause Analyst, Savings Estimator, Optimization Planner, Policy Guard, Report Author |
| Routing | Proceed, retry (bounded), escalate, require-approval, short-circuit — each a recorded decision with a reason |
| Ranking | Deterministic weighted composite of savings, safety and confidence. The model narrates the order; it cannot change it |
| Observability | Structured JSON logs correlated by `run_id`; tracing seam with a no-op default; token, cost and latency per run |
| Governance | Every action classified `allow` / `review` / `block`. **No execution path exists** |
| Entry points | `costsentinel scan` and `POST /scan` |

### What Phase 1 contained

A thin vertical slice that runs end to end:

| Piece | Status in Phase 1 |
|---|---|
| Domain model | Complete — provenance and verification on every derived fact |
| Provider seam | `AzureProvider` protocol + deterministic `MockAzureProvider` |
| LLM seam | `LLM` protocol + `FakeLLM`; Azure OpenAI and Gemini adapters are configuration-error stubs |
| Graph | Four nodes: Anomaly Scout, Optimization Planner, Policy Guard, Report Author |
| Durability | SQLite checkpointer — runs are persisted and resumable |
| Governance | Policy Guard classifies every action; destructive ones are marked `review` or `block`. No execution path exists yet |
| Entry points | `costsentinel scan` and `POST /scan` |

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install
uv sync --all-groups

# 2. Run the tests (with coverage)
uv run pytest

# 3. Run a scan against the mock estate
uv run costsentinel scan

# ... or as JSON
uv run costsentinel scan --json

# ... or for a single client
uv run costsentinel scan --client northwind-energy
```

No `.env` is needed. Copy `.env.example` to `.env` only when you want to change the
defaults; `MODE=mock` and `LLM_BACKEND=fake` are the defaults and require nothing.

### The learning loop

CostSentinel remembers what a client decided and does not re-pitch it. No database
needed: the default store is a local SQLite file and the default embedder is
deterministic.

```bash
# 1. Scan. The run's findings are written to episodic memory.
uv run costsentinel scan --client northwind-energy --summary

# 2. Record what the client said about one of them.
uv run costsentinel decisions --client northwind-energy      # ids you can decide on
uv run costsentinel decide <recommendation-id> --reject   --reason "owner confirmed this is a quarter-end batch host"

# 3. Scan again. It comes back annotated and ranked lower.
uv run costsentinel scan --client northwind-energy --summary
```

The second scan reports `1 previously declined`, and the finding carries
`memory: Previously declined on <date>, reason: ... (was #1 on merit)`.

**Memory reorders and annotates. It never recomputes.** Every monetary figure in the
second report is byte-identical to the first, the same findings are present, and only
the order and the annotations differ — a declined recommendation does not make the
waste disappear, so it is demoted rather than dropped. A test asserts the whole
property end to end.

For a durable store shared across runs and operators:

```bash
docker compose up --build     # app plus postgres-with-pgvector
```

### Measuring whether any of this works

The product's claims are measured rather than asserted. `costsentinel eval` runs the
**real** pipeline against committed labelled estates and scores it.

```bash
uv run costsentinel eval            # every dataset; writes evals/REPORT.md
uv run costsentinel eval --smoke    # the fast subset CI runs on every commit
```

Fully offline: the fake model, the deterministic judge, memory off, no credential.
The current scorecard is committed at [evals/REPORT.md](evals/REPORT.md).

**Two things are gates, not scores.** Safety recall must be 100% — every destructive
remediation classified at least as strictly as its floor and requiring approval — and
every reported money figure must equal the committed ground truth exactly, not within
tolerance. A prompt injection that changed any action, class or figure fails too.
Those exit non-zero and fail a build. Precision, recall, ranking and groundedness
report a number and never block: a low score is something to look at, not a reason to
refuse a merge.

**The adversarial cases currently fail, deliberately.** Three of them record real gaps
the harness found: the detectors read no tag exemption, so a disaster-recovery standby
is reported as idle and a snapshot carrying a HIPAA retention tag is reported as stale;
and the rightsizing thresholds let a genuinely oversized machine hide just above them.
They are in the datasets to make those gaps visible, not to hide them.

### Scanning a real Azure estate

Read [SECURITY.md](SECURITY.md) first. The short version:

```bash
uv sync --extra real
az login
uv run costsentinel scan --real --subscription <subscription-id>
```

The run is read-only -- there is no write operation anywhere in the codebase -- and
it writes its report to `var/real-runs/`, which is gitignored. It needs `Reader` and
`Cost Management Reader` on the named subscriptions and nothing more, and it refuses
to run without an explicit `--subscription`: there is no wildcard.

Pilot on a personal or sandbox subscription, get written approval before any client
estate, and never commit the output.

### Lint and type-check

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

### The API

```bash
uv run uvicorn costsentinel.api.app:app --reload
```

Then:

```bash
curl -X POST localhost:8000/scan -H 'content-type: application/json' -d '{}'
curl -X POST localhost:8000/scan -H 'content-type: application/json' \
  -d '{"client": "northwind-energy"}'
```

`GET /health` reports the active mode and LLM backend. The OpenAPI docs are at
`/docs`.

### Resumability

Each client's scan is a LangGraph thread keyed by its run id, checkpointed to
`.costsentinel/checkpoints.sqlite` after every node. A crashed or interrupted sweep
resumes from the last completed node rather than re-reading the estate — which in
Phase 7 is also the mechanism that lets the graph halt for days awaiting a human
approval.

### Observability

Structured JSON logs carry the `run_id` on every event in a sweep. Each run records
token usage, computed cost and latency per model call, along with the routing decision
that chose the model.

Tracing is a seam with a no-op default. To send traces to Langfuse:

```bash
uv sync --extra tracing
# then set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY
```

`langfuse` is an *optional* extra and is never a hard dependency: if it is absent or
unconfigured, tracing degrades to a no-op and logs that it did. CI does not install
it.

### Refreshing prices

Every monetary figure traces to the committed snapshot at
`src/costsentinel/providers/data/price_catalogue.json`. The figures shipped with this
repository are plausible curated values and are **not** a real Azure pull — the
snapshot says so in `is_real_api_pull`, and their provenance is marked unverified.

To replace them with a real pull from the public Azure Retail Prices API:

```bash
uv run python scripts/refresh_prices.py --dry-run   # fetch and report, write nothing
uv run python scripts/refresh_prices.py             # write the snapshot
```

Run it by hand only. It is never invoked by CI or by any test, and a test asserts
that. Review the diff before committing: savings assertions are baselined against
these figures and will need re-baselining.

## Repository layout

```
src/costsentinel/
  config.py          settings; MODE and LLM_BACKEND flags (default mock + fake)
  domain/            Pydantic v2 types -- every module boundary
  providers/         AzureProvider protocol + MockAzureProvider
  llm/               LLM protocol, FakeLLM, real adapters, task routing
  providers/data/    committed price catalogue snapshot
  agents/            the six specialists, the supervisor, the memory consult step,
                     detectors, savings, ranking
  graph/             graph assembly + checkpointer + sweep runner
  guardrails/        policy store, action classes, approval-gate seam
  memory/            episodic + semantic stores, embedder, recall, decisions
  memory/data/       committed synthetic policy and guidance corpus
  reports/           report rendering (Phase 7)
  observability/     structured logging + the tracing seam
  tools/             read-only MCP Azure tool server
  a2a/               Agent Cards (Phase 9)
  api/               FastAPI app
  cli.py             CLI entry point
scripts/             refresh_prices.py (manual, never CI)
evals/datasets/      committed labelled estates, with ground truth
evals/harness/       the scorecard harness: metrics, gates, judge, runner
evals/REPORT.md      the committed scorecard
tests/
```

## Safety posture

See [SECURITY.md](SECURITY.md) for the full picture, the permissions a real run
needs, and how to report a vulnerability.

- Nothing destructive runs. There is no execution path at all. Read-only is
  structural, not configured: `ToolOperation` has one member, the MCP server serves
  exactly what the registry holds, and no Azure SDK able to mutate a resource is
  installed. `tests/test_readonly.py` asserts all three.
- Real Azure access is `DefaultAzureCredential` only. There is no client-secret
  setting, so there is no secret for this codebase to log, trace or commit.
- A real run's output is gitignored and a pre-commit hook refuses to commit it,
  along with any subscription id that does not look deliberately synthetic.
- Memory may change the order of findings and their annotations, and may never change
  a number. Enforced by test, not by convention.
- Safety recall is a gate, not a metric. A destructive action that is not correctly
  gated fails the build, and `costsentinel eval` has been shown to go red on a
  deliberately broken classifier and green again when it is restored.
- Every action is classified; the built-in action-class floor cannot be loosened by
  configuration.
- LLM output and cloud resource metadata are both treated as untrusted: validated
  against a schema, and passed to models as delimited data, never as instructions.
- No secrets in code or git. Configuration is environment-driven, and CI needs no
  secrets and no network access to a cloud or LLM provider.
