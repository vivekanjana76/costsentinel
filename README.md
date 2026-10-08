# CostSentinel

Autonomous, multi-agent FinOps governance for multi-subscription Azure estates.

> **Phase 1 — mock only.** Everything in this repository runs offline against a
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
specialist agents — Anomaly Scout, Root-Cause Analyst, Optimization Planner,
Savings Estimator, Policy Guard and Report Author — with a supervisor owning
retries, escalation and approval routing. Detection is deterministic, so a finding
can never be hallucinated; the language model only explains and ranks. Every
externally derived fact carries provenance and a verified / unverified / unknown
flag, so no number in a report can come from a model. Anything destructive is
classified `allow`, `review` or `block` and cannot reach an execution path without
a recorded human decision.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, the graph diagram, and
the decision log.

### What Phase 1 contains

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
Phase 3 is also the mechanism that lets the graph halt for days awaiting a human
approval.

## Repository layout

```
src/costsentinel/
  config.py          settings; MODE and LLM_BACKEND flags (default mock + fake)
  domain/            Pydantic v2 types -- every module boundary
  providers/         AzureProvider protocol + MockAzureProvider
  llm/               LLM protocol, FakeLLM, real adapters, task routing
  agents/            specialist nodes
  graph/             graph assembly + checkpointer
  guardrails/        policy store, action classes, approval-gate seam
  reports/           report rendering
  observability/     structured logging; Langfuse wiring
  tools/ memory/ a2a/   seams for Phases 2, 4 and 8
  api/               FastAPI app
  cli.py             CLI entry point
evals/               labelled datasets + scorecard harness (Phase 5)
tests/
```

## Safety posture

- Nothing destructive runs. Phase 1 has no execution path at all.
- Every action is classified; the built-in action-class floor cannot be loosened by
  configuration.
- LLM output and cloud resource metadata are both treated as untrusted: validated
  against a schema, and passed to models as delimited data, never as instructions.
- No secrets in code or git. Configuration is environment-driven, and CI needs no
  secrets and no network access to a cloud or LLM provider.
