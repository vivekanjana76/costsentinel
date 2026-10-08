# CostSentinel — Roadmap

**Current phase: 1**

Phases are sequential. Do not start work in a later phase before the current phase
is complete and its exit criteria are met. If an idea arrives mid-phase, record it
under "Parked ideas" at the bottom and stay on the current slice.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

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

## Phase 2 — MCP Azure tool server

**Goal:** all cloud access moves behind a typed, read-only MCP tool boundary, with a
real Azure implementation alongside the mock.

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
- [ ] `Dockerfile` and `docker-compose.yml` (CLAUDE.md section 4). Deliberately
      deferred from Phase 1: compose is specified as app + postgres-with-pgvector,
      and Postgres has nothing to do until memory lands in Phase 4. Phase 2 adds
      the app image; Phase 4 adds the Postgres service.

**Exit criteria:** the Phase 1 graph runs unchanged against the MCP boundary in mock
mode; CI still needs no secrets; a real-mode read-only sweep works against one live
subscription.

**New dependencies to flag:** `fastmcp`, `azure-identity`, `azure-mgmt-*`,
`azure-mgmt-costmanagement`, `azure-mgmt-resourcegraph`.

---

## Phase 3 — Full specialist agents and the supervisor

**Goal:** the complete agent graph from ARCHITECTURE.md section 2, with real
conditional routing and a working approval gate.

- [ ] Supervisor node owning all conditional routing (retry, escalate, approve, skip)
- [ ] Root-Cause Analyst node, LLM-assisted over Scout evidence only
- [ ] Savings Estimator as its own node, arithmetic only, no LLM import
- [ ] Expand the detector set: stale dev/test resources, unused reservations,
      egress anomalies, oversized App Service plans, idle SQL
- [ ] Statistical cost-anomaly detection over the cost series
- [ ] Policy store as data, with the built-in action-class floor enforced
- [ ] Approval gate as a durable LangGraph interrupt
- [ ] `LocalCLIGate`: prompt an operator, record and persist the `ApprovalDecision`
- [ ] Resume-after-approval path, tested end to end
- [ ] `AuditEvent` emission at every node, persisted and queryable
- [ ] Langfuse tracing wired, a no-op when unconfigured
- [ ] Real LLM adapters implemented: Azure OpenAI primary, Gemini fallback
- [ ] Task-based model routing table

**Exit criteria:** a destructive recommendation halts the graph, persists, and
resumes correctly on a recorded decision after the process has exited and restarted.

**New dependencies to flag:** `langfuse`, `openai` (Azure OpenAI),
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

**Exit criteria:** a Markdown and a PDF report are produced from a stored run, and a
test asserts no ungrounded number appears in either.

**New dependencies to flag:** a PDF toolchain (`weasyprint` or `reportlab`) and a
template engine (`jinja2`).

---

## Phase 7 — Approval-gate adapters (Teams and Slack)

**Goal:** approvals happen where the humans already are.

- [ ] Teams adapter: adaptive card with the proposal, savings and risk class
- [ ] Slack adapter: Block Kit message with approve and reject actions
- [ ] Webhook receiver for decision callbacks, signature-verified
- [ ] Approver identity and authorisation checks
- [ ] Decision deduplication and idempotency on replayed callbacks
- [ ] Timeout and escalation policy (no decision never means approved)
- [ ] Adapters tested against recorded payloads, with no live workspace in CI

**Exit criteria:** a destructive recommendation is approved from Teams or Slack and
the halted run resumes on that decision.

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
