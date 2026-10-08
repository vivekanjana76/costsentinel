# CostSentinel — Roadmap

**Current phase: 2**

Phases are sequential. Do not start work in a later phase before the current phase
is complete and its exit criteria are met. If an idea arrives mid-phase, record it
under "Parked ideas" at the bottom and stay on the current slice.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

> **Phase order changed after Phase 1.** The specialist graph and observability were
> originally Phase 3, and the MCP Azure tool server was Phase 2. They are swapped:
> the graph work is entirely mock-runnable and delivers the product's visible value
> with no cloud dependency, whereas the MCP server is the first thing that needs real
> Azure credentials and network access. Building the graph first keeps the
> zero-credential invariant in place for longer, and means that when the MCP boundary
> does land there is a complete six-specialist graph to point at it rather than a
> four-node slice. Recorded as ARCHITECTURE.md D34.

---

## Phase 1 — Scaffold and thin vertical slice

**Goal:** the pipeline runs end to end on mock data with zero credentials, and the
shape of every seam that later phases fill in is already in place and typed.

- [x] `pyproject.toml` via uv, Phase 1 dependencies only
- [x] ruff + pyright (strict on `src`) configuration
- [x] `.gitignore`, `.env.example`, `.pre-commit-config.yaml`
- [x] GitHub Actions CI: ruff, pyright, pytest with coverage
- [x] Full directory skeleton from CLAUDE.md section 4, with module docstrings
- [x] `Settings` via pydantic-settings, `MODE` flag, defaults to mock + fake
- [x] Domain models: the full core type set from CLAUDE.md section 6, with
      provenance and a verified/unknown flag
- [x] `AzureProvider` protocol + `MockAzureProvider` (deterministic, seeded estate
      containing an orphaned disk, an idle oversized VM and an unattached public IP)
- [x] `LLM` protocol + `FakeLLM`; Azure OpenAI and Gemini adapters as
      configuration-error stubs; the task-routing seam
- [x] `ScanState` + a LangGraph graph: Anomaly Scout, Optimization Planner,
      Policy Guard (classify-only), Report Author
- [x] SQLite checkpointer wired, so a run is persisted and resumable
- [x] CLI `costsentinel scan` and FastAPI `POST /scan`, both returning the report as JSON
- [x] Tests: domain models, mock-provider determinism, FakeLLM, full graph run
- [x] `README.md` with problem statement and quickstart

**Exit criteria (met):** `uv run pytest` green -- 229 tests, 99% coverage (100% on
`domain`, `guardrails` and `graph`); `ruff check`, `ruff format --check` and
`pyright` clean; `uv run costsentinel scan` produces reports flagging the three
known wasteful resources plus the oversized VM; no credentials required anywhere.

---

## Phase 2 — Full specialist graph, observability and model routing

**Goal:** the complete six-specialist graph from CLAUDE.md section 5, with a
supervisor owning conditional routing, tracing and per-run metrics, and an observable
model-routing seam. Still mock- and fake-runnable end to end: no new secrets, CI
stays offline.

Milestone: *Phase 2 - Full specialist graph, observability and model routing*.

- [ ] Root-Cause Analyst node, LLM-assisted over Scout evidence only (#3)
- [ ] Savings Estimator as its own node, arithmetic only, no LLM import (#4)
- [ ] Expand the deterministic detector set: stale snapshot, idle SQL database,
      oversized App Service plan, unused reservation (#5)
- [ ] Rank recommendations by savings, risk and confidence, with an LLM-authored
      ranking rationale (#6)
- [ ] Supervisor node and the conditional routing seam: retry, escalate,
      require-approval, short-circuit (#7)
- [ ] Richer `ClientReport`: root causes, ranking inputs, per-waste-kind breakdown,
      deterministic per-client totals, approval queue (#8)
- [ ] Langfuse tracing around the run and every LLM call, a no-op if unconfigured and
      never a hard dependency; per-run token, cost and latency metrics (#9)
- [ ] Observable model-routing seam: light tasks to a small model, heavier reasoning
      to a larger one, with the choice recorded in the trace (#10)
- [ ] `scripts/refresh_prices.py` pulling the public Azure Retail Prices API into the
      committed snapshot; run manually, never in CI (#11)
- [ ] ARCHITECTURE.md decision log and graph diagram updated; ROADMAP progress ticked
      (#12)

**Exit criteria:** the six-specialist graph runs end to end on the mock estate with
zero credentials; detection and every monetary figure remain deterministic; a trace
and a `RunMetrics` record are produced per run; coverage holds at the CLAUDE.md
targets.

**Deliberately *not* in this phase:** the approval-gate adapters and the durable
interrupt (Phase 7), real LLM backends (Phase 3), and statistical cost-anomaly
detection (Phase 3, where real cost history makes it calibratable).

**New dependencies to flag:** `langfuse` as an *optional* extra only. It is imported
lazily behind a `Tracer` protocol so that tracing is never a hard dependency, CI does
not install it, and the suite passes without it.

---

## Phase 3 — MCP Azure tool server and real backends

**Goal:** all cloud access moves behind a typed, read-only MCP tool boundary, with a
real Azure implementation alongside the mock, and the real LLM backends behind the
routing seam Phase 2 built.

This is the first phase that needs credentials. Everything before it runs offline.

- [ ] FastMCP server exposing Azure tools with typed schemas
- [ ] Tool wrappers: Cost Management (actuals, forecast), Resource Graph (inventory),
      Advisor (hints), Monitor (metrics)
- [ ] `MockAzureProvider` re-exposed through the identical tool interface, so agents
      cannot distinguish mock from real
- [ ] Read-only enforcement at the tool boundary (no write verb may be registered in
      this phase)
- [ ] Caching, retry with backoff, and rate limiting applied once at the boundary
- [ ] Real `AzureProvider` using `azure-identity` + the management SDKs, read-only
- [ ] Credential handling via `DefaultAzureCredential`, env-configured, never in code
- [ ] Integration tests against the MCP server using the mock backend
- [ ] `MODE=real` smoke path, manually verified, excluded from CI
- [ ] Real LLM adapters implemented behind the Phase 2 routing seam: Azure OpenAI
      primary, Gemini fallback. Moved here from the original Phase 3 because they
      need credentials and CI must stay offline.
- [ ] Statistical cost-anomaly detection over the cost series, emitting
      `CostAnomaly`. Moved here because real cost history is what makes the
      thresholds calibratable.
- [ ] `Dockerfile` and `docker-compose.yml` (CLAUDE.md section 4). Deliberately
      deferred from Phase 1: compose is specified as app + postgres-with-pgvector,
      and Postgres has nothing to do until memory lands in Phase 4. This phase adds
      the app image; Phase 4 adds the Postgres service.

**Exit criteria:** the Phase 1 graph runs unchanged against the MCP boundary in mock
mode; CI still needs no secrets; a real-mode read-only sweep works against one live
subscription.

**New dependencies to flag:** `fastmcp`, `azure-identity`, `azure-mgmt-*`,
`azure-mgmt-costmanagement`, `azure-mgmt-resourcegraph`, `openai` (Azure OpenAI) and
`google-genai`.

---

## Phase 4 — Semantic and episodic memory

**Goal:** the system remembers reference knowledge and prior outcomes, and uses both
to rank better.

- [ ] Postgres + pgvector in docker-compose
- [ ] Schema and migrations for `semantic_chunks` and `episodic_outcomes`
- [ ] Embedding seam with a deterministic fake embedder for tests
- [ ] Semantic ingest: Azure pricing, the WAF cost pillar, the org FinOps policy
- [ ] Client-filtered retrieval, with a test asserting no cross-client leakage
- [ ] Episodic write path: findings, decisions, accept/reject/revert outcomes
- [ ] Hybrid episodic retrieval: exact scope match plus vector similarity on rationale
- [ ] Planner consumes episodic memory to suppress and re-rank prior rejections
- [ ] Graceful degradation with no database configured, verified by test
- [ ] Postgres checkpointer for compose deployments

**Exit criteria:** a recommendation previously rejected by a client is demoted and
annotated on the next sweep; the full suite still passes with no database present.

**New dependencies to flag:** `psycopg`, `pgvector`, `sqlalchemy` or `alembic` for
migrations.

---

## Phase 5 — Evaluation harness and datasets

**Goal:** the product's claims are measured, and a safety regression fails the build.

- [ ] Extend the estate generator to vary topology by seed
- [ ] Labelled synthetic estates under `evals/datasets/`, with ground-truth waste,
      remediation and savings annotations
- [ ] Harness running the real pipeline against a dataset, emitting a scorecard
- [ ] Metric: waste-detection precision and recall
- [ ] Metric: recommendation ranking quality (nDCG vs ground-truth savings order)
- [ ] Metric: cost-figure accuracy — hard assertion that no reported number has LLM
      provenance
- [ ] Metric: savings-estimate calibration against ground truth
- [ ] Metric: report groundedness via LLM-as-judge
- [ ] Metric: safety recall — every destructive action classified and gated
- [ ] Fast smoke subset wired into CI
- [ ] Full suite runnable on demand, with a committed scorecard baseline
- [ ] CI fails the build on a safety-recall regression

**Exit criteria:** `uv run costsentinel eval` emits a scorecard; CI runs the smoke
subset on every commit; an intentionally mis-classified destructive action fails CI.

---

## Phase 6 — Reporting

**Goal:** output a client would actually accept as a deliverable.

- [ ] Markdown report template: executive summary, findings, ranked recommendations,
      projected savings, approval queue
- [ ] PDF rendering
- [ ] Per-client branding and formatting configuration
- [ ] Realised-vs-projected savings section, driven by episodic outcomes
- [ ] Groundedness assertion in tests: every number in the rendered report traces to
      a state field
- [ ] CLI `costsentinel report` to render a stored run
- [ ] **Portfolio roll-up:** a provider-level executive view totalling savings,
      findings and approval backlog *across all clients*, as a deliberate
      exception to the per-client reporting rule. It is for the managed-service
      provider's own leadership, never shared with a client, so it must not reuse
      `ClientReport`; it needs its own `PortfolioRollup` type, its own access
      control, and a test asserting it is unreachable from any client-scoped path.
      Totals are summed deterministically from per-client reports, never modelled.

**Exit criteria:** a Markdown and a PDF report are produced from a stored run, and a
test asserts no ungrounded number appears in either.

**New dependencies to flag:** a PDF toolchain (`weasyprint` or `reportlab`) and a
template engine (`jinja2`).

---

## Phase 7 — The approval gate and its adapters

**Goal:** a gated action halts the graph until a human decides, wherever that human
already works.

The durable interrupt and the local CLI gate moved here from the original Phase 3:
Phase 2 delivers the routing seam that routes to the gate, and the gate
implementations belong with the adapters that drive them.

- [ ] Approval gate as a durable LangGraph interrupt at the Policy Guard boundary
- [ ] `LocalCLIGate`: prompt an operator, record and persist the `ApprovalDecision`
- [ ] Resume-after-approval path, tested end to end across a process restart

- [ ] Teams adapter: adaptive card with the proposal, savings and risk class
- [ ] Slack adapter: Block Kit message with approve and reject actions
- [ ] Webhook receiver for decision callbacks, signature-verified
- [ ] Approver identity and authorisation checks
- [ ] Decision deduplication and idempotency on replayed callbacks
- [ ] Timeout and escalation policy (no decision never means approved)
- [ ] Adapters tested against recorded payloads, with no live workspace in CI

**Exit criteria:** a destructive recommendation halts the graph and persists; the
process exits; the run then resumes correctly on a decision recorded from the CLI,
from Teams or from Slack.

**New dependencies to flag:** `slack-sdk`, an HTTP client, and a signature
verification library.

---

## Phase 8 — A2A layer

**Goal:** specialists are discoverable and invocable by external agents, without
routing around governance.

- [ ] A2A v1 Agent Cards for each specialist
- [ ] A2A server exposing the specialists
- [ ] Cards declare capability, input and output schema, and the action class of
      anything the agent can propose
- [ ] Authentication and per-caller authorisation
- [ ] Interop check against Azure AI Foundry
- [ ] Test asserting an external caller cannot obtain execution of a `review` or
      `block` action without a recorded `ApprovalDecision`

**Exit criteria:** an external agent invokes the Savings Estimator and receives a
typed result; an external attempt to execute a gated action is refused.

---

## Phase 9 — Dashboard

**Goal:** an operator view across the estate.

- [ ] Streamlit dashboard: estate overview, findings, pending approvals, savings
      realised vs projected
- [ ] Approval actions from the dashboard, via the same gate interface
- [ ] Per-client views with multi-tenancy enforced in the query layer
- [ ] Run history and audit-trail browser
- [ ] React rewrite for polish (optional, after Streamlit proves the content)

**Exit criteria:** an operator can run a sweep, review findings and approve a gated
action entirely from the dashboard.

**New dependencies to flag:** `streamlit`.

---

## Phase 10 — Execution and hardening

**Goal:** approved changes are actually applied, safely.

- [ ] Execution engine, dry-run by default, behind an explicit opt-in flag
- [ ] Precondition re-check immediately before execution
- [ ] Idempotency keys, and a rollback or revert path per action type
- [ ] `allow`-class actions only in the first execution release (tagging)
- [ ] `review`-class execution gated on a recorded approval, with the full audit chain
- [ ] Outcome write-back to episodic memory, including reverts
- [ ] Load and resilience testing across 12+ subscriptions

**Exit criteria:** an approved tagging action is applied to a real subscription, with
a complete audit chain from signal to execution, and the outcome recorded.

---

## Future missions (reuse the spine, not scheduled)

- **Compliance mission:** policy drift and control-failure detection over the same
  inventory and governance spine.
- **Change-risk mission:** pre-change blast-radius assessment using the same
  specialists, policy store and approval gate.

---

## Parked ideas

Ideas noted mid-phase, to keep scope from expanding. Not commitments.

- Reservation and savings-plan purchase recommendations (needs commitment modelling).
- Cross-client benchmarking ("your idle-VM rate vs the fleet median"), subject to a
  tenancy and confidentiality review.
- Cost forecasting and budget-breach prediction.
- A scheduled autonomous sweep cadence per client, with a digest.
- Tag-hygiene scoring as a standalone, fully `allow`-class mission.
