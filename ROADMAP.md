# CostSentinel — Roadmap

**Current phase: 3**

Phases are sequential. Do not start work in a later phase before the current phase
is complete and its exit criteria are met. If an idea arrives mid-phase, record it
under "Parked ideas" at the bottom and stay on the current slice.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

> **Phase order has changed twice.** After Phase 1 the specialist graph and the MCP
> Azure tool server were swapped, because the graph is entirely mock-runnable and the
> MCP server is the first thing needing real credentials (ARCHITECTURE.md D34). After
> Phase 2, statistical cost-anomaly detection and the real-model hardening work were
> split out of the MCP phase into their own Phase 4, and Phases 4-10 renumbered to
> 5-11. The reason is that Phase 3 is a *capability unlock with a safety surface* --
> real prices, real reads, real models, and the guarantees that make a real run safe
> to point at someone's estate. Anomaly detection is ordinary feature work that
> happens to want real cost history, and bundling it would have meant shipping a
> safety-critical phase and a feature phase as one reviewable unit
> (ARCHITECTURE.md D49).

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

- [x] Root-Cause Analyst node, LLM-assisted over Scout evidence only (#3)
- [x] Savings Estimator as its own node, arithmetic only, no LLM import (#4)
- [x] Expand the deterministic detector set: stale snapshot, idle SQL database,
      oversized App Service plan, unused reservation (#5)
- [x] Rank recommendations by savings, risk and confidence, with an LLM-authored
      ranking rationale (#6)
- [x] Supervisor node and the conditional routing seam: retry, escalate,
      require-approval, short-circuit (#7)
- [x] Richer `ClientReport`: root causes, ranking inputs, per-waste-kind breakdown,
      deterministic per-client totals, approval queue (#8)
- [x] Langfuse tracing around the run and every LLM call, a no-op if unconfigured and
      never a hard dependency; per-run token, cost and latency metrics (#9)
- [x] Observable model-routing seam: light tasks to a small model, heavier reasoning
      to a larger one, with the choice recorded in the trace (#10)
- [x] `scripts/refresh_prices.py` pulling the public Azure Retail Prices API into the
      committed snapshot; run manually, never in CI (#11)
- [x] ARCHITECTURE.md decision log and graph diagram updated; ROADMAP progress ticked
      (#12)

**Exit criteria (met):** the six-specialist graph runs end to end on the mock estate
with zero credentials; detection and every monetary figure remain deterministic; a
trace span and a `RunMetrics` record are produced per run; 390 tests pass at 99%
coverage, with `domain`, `graph` and `guardrails` at 98-100%; `ruff`, `ruff format`
and `pyright` (strict on `src`) all clean.

**Deliberately *not* in this phase:** the approval-gate adapters and the durable
interrupt (Phase 7), real LLM backends (Phase 3), and statistical cost-anomaly
detection (Phase 3, where real cost history makes it calibratable).

**New dependencies to flag:** `langfuse` as an *optional* extra only. It is imported
lazily behind a `Tracer` protocol so that tracing is never a hard dependency, CI does
not install it, and the suite passes without it.

---

## Phase 3 — Real estate, read-only

**Goal:** unlock real Azure, read-only, and ship the guarantees that make a real run
safe to point at someone's estate. This is the first phase that needs credentials;
everything before it runs offline.

Milestone: *Phase 3 - Real estate, read-only*.

- [ ] Real prices for every catalogue SKU in both regions, fetched from the public
      Azure Retail Prices API, with each price tracing to a named meter (#15)
- [ ] FastMCP tool server over Resource Graph, Cost Management, Advisor and Monitor,
      typed in and out, with a verified / unknown / failed status on every response
      and **no write verb anywhere in the surface** (#16)
- [ ] Read-only `AzureProvider` backed by those tools, authenticated with
      `DefaultAzureCredential`, selected only when `MODE=real` (#17)
- [ ] Azure OpenAI and Gemini adapters implemented behind the existing routing seam
      (#18)
- [ ] A mechanical read-only assertion: no mutating tool, method, or even importable
      mutating client exists in the real surface (#19)
- [ ] A guard that refuses to commit a real-run artefact, and gitignores for real
      output and `.env` (#20)
- [ ] `SECURITY.md`: the real-scan runbook, the pilot-on-a-sandbox warning, the
      get-approval-first instruction, and the never-commit-real-output rule (#21)
- [ ] `costsentinel scan --real --subscription <id>`, guarded so CI never runs it
      (#22)
- [ ] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.3.0 (#23)

**Exit criteria:** a real read-only sweep of one personal or sandbox subscription
produces the same report shape as a mock run; the Phase 2 graph runs against the real
provider unchanged; CI stays fully offline with no secrets; the read-only guarantee is
asserted by test rather than by review.

**Deliberately *not* in this phase:** any write, delete or mutate path (Phase 11);
statistical cost-anomaly detection and real-model hardening (Phase 4, split out of
this one).

**New dependencies, all optional extras so CI stays lean and offline:** `fastmcp`,
`azure-identity`, `azure-mgmt-resourcegraph`, `azure-mgmt-costmanagement`,
`azure-mgmt-monitor`, `azure-mgmt-advisor`, `openai` and `google-genai`. Each is
imported lazily and absent from the default install, so the suite and the demo run
without them.

---

## Phase 4 — Cost anomalies and real-model hardening

**Goal:** the work that only becomes meaningful once real cost history and real models
are in hand. Split out of the original Phase 3 so that the safety-critical capability
unlock and this ordinary feature work are separately reviewable.

- [ ] Statistical cost-anomaly detection over the cost series, emitting `CostAnomaly`.
      Needs real cost history to calibrate thresholds against, which is why it waited.
- [ ] Anomaly root-cause correlation: tie a spend spike to the resource or service
      that caused it
- [ ] Egress and data-transfer anomaly detection
- [ ] Real-model hardening: retry and fallback from the primary backend to the
      secondary on failure or schema violation, with the fallback recorded in the trace
- [ ] A token-spend guardrail per sweep, so a large estate cannot run up an unbounded
      model bill
- [ ] Prompt-injection red-teaming against the real backends, using hostile resource
      names and tags drawn from the evidence path
- [ ] Calibrate detector thresholds and the ranking weights against observed real
      estates, replacing the Phase 2 defaults

**Exit criteria:** a spend spike in a real subscription is detected, attributed and
reported; a primary-backend failure falls back to the secondary without failing the
sweep; a hostile resource name cannot alter a recommendation.

---

## Phase 5 — Semantic and episodic memory

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

## Phase 6 — Evaluation harness and datasets

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

## Phase 7 — Reporting

**Goal:** output a client would actually accept as a deliverable.

- [ ] Markdown report template: executive summary, findings, ranked recommendations,
      projected savings, approval queue
- [ ] PDF rendering
- [ ] Per-client branding and formatting configuration
- [ ] Realised-vs-projected savings section, driven by episodic outcomes
- [ ] Groundedness assertion in tests: every number in the rendered report traces to
      a state field
- [ ] CLI `costsentinel report` to render a stored run
- [ ] **Residual spend per finding:** for a conservative action, report what is
      *still* being spent after it. Scaling an idle SQL database down from 4 vCore to
      2 leaves the smaller tier running, and a report that shows only the saving
      invites a client to think the problem is solved. Each finding should state the
      remaining monthly cost alongside the saving, and the report should total it, so
      "we recovered X and are still paying Y for something nobody uses" is visible.
      Deterministic arithmetic, like every other figure.
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

## Phase 8 — The approval gate and its adapters

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

## Phase 9 — A2A layer

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

## Phase 10 — Dashboard

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

## Phase 11 — Execution and hardening

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

## Later features, scheduled

- **Config-tunable ranking weights per client** (Phase 5, with the policy overlays).
  The Phase 2 weights -- 0.60 savings, 0.25 safety, 0.15 confidence -- are a product
  judgement baked into a module constant. Different clients have genuinely different
  appetites: a government production estate may want safety weighted far higher than a
  development sandbox. The weights should move into the client policy overlay
  alongside the action-class tightening, with the built-in values as the default and
  the effective weights reported on every finding so a client can see what was applied
  to them. The ranking must stay deterministic and the weights must stay data, never
  model output.

---

## Parked ideas

Ideas noted mid-phase, to keep scope from expanding. Not commitments.

- Reservation and savings-plan purchase recommendations (needs commitment modelling).
- Cross-client benchmarking ("your idle-VM rate vs the fleet median"), subject to a
  tenancy and confidentiality review.
- Cost forecasting and budget-breach prediction.
- A scheduled autonomous sweep cadence per client, with a digest.
- Tag-hygiene scoring as a standalone, fully `allow`-class mission.
