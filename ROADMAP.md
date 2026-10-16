# CostSentinel — Roadmap

**Current phase: 5** — cost anomalies and real-model hardening.

Phases are sequential. Do not start work in a later phase before the current phase
is complete and its exit criteria are met. If an idea arrives mid-phase, record it
under "Parked ideas" at the bottom and stay on the current slice.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

> **Phase order has changed three times.** After Phase 1 the specialist graph and the
> MCP Azure tool server were swapped, because the graph is entirely mock-runnable and
> the MCP server is the first thing needing real credentials (ARCHITECTURE.md D34).
> After Phase 2, statistical cost-anomaly detection and real-model hardening were
> split out of the MCP phase into their own phase, because Phase 3 is a *capability
> unlock with a safety surface* and bundling ordinary feature work with it would have
> meant shipping both as one reviewable unit (D49).
>
> After Phase 3, **the memory layer and the anomaly phase swapped**: memory is now
> Phase 4 and anomalies Phase 5 (ARCHITECTURE.md D67). Two reasons. Memory is what
> makes CostSentinel a system that *learns* rather than one that re-pitches the same
> rejected advice every month, and that is the product's central claim -- it should
> not sit behind a detector improvement. And anomaly detection wants real cost history
> to calibrate thresholds against, which only accumulates once somebody is running
> real sweeps; memory needs nothing but its own store, so it can be built and proven
> offline today. Nothing after Phase 5 was renumbered.

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

- [x] Real prices for every catalogue SKU in both regions, fetched from the public
      Azure Retail Prices API, with each price tracing to a named meter (#15)
- [x] FastMCP tool server over Resource Graph, Cost Management, Advisor and Monitor,
      typed in and out, with a verified / unknown / failed status on every response
      and **no write verb anywhere in the surface** (#16)
- [x] Read-only `AzureProvider` backed by those tools, authenticated with
      `DefaultAzureCredential`, selected only when `MODE=real` (#17)
- [x] Azure OpenAI and Gemini adapters implemented behind the existing routing seam
      (#18)
- [x] A mechanical read-only assertion: no mutating tool, method, or even importable
      mutating client exists in the real surface (#19)
- [x] A guard that refuses to commit a real-run artefact, and gitignores for real
      output and `.env` (#20)
- [x] `SECURITY.md`: the real-scan runbook, the pilot-on-a-sandbox warning, the
      get-approval-first instruction, and the never-commit-real-output rule (#21)
- [x] `costsentinel scan --real --subscription <id>`, guarded so CI never runs it
      (#22)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.3.0 (#23)

**Exit criteria:** 671 tests green at 96.5% coverage, with `domain`, `agents`,
`guardrails` and `graph` at 98-100%; `ruff check`, `ruff format --check` and `pyright`
clean; every committed price traces to a named Azure meter and nothing in the
repository claims a synthetic price; the read-only guarantee asserted by 17 mechanical
tests rather than by review; CI installs no optional extra and no credential, and a
test asserts the workflow never invokes the real path.

**One criterion is not yet met, deliberately.** "A real read-only sweep of one
personal or sandbox subscription produces the same report shape as a mock run" has not
been executed, because doing it requires an Azure tenant and a human decision about
which subscription to point at. Everything it depends on is in place and tested
offline: the provider satisfies the same protocol, the graph is unchanged, the CLI
path works up to the point of needing a credential, and `SECURITY.md` section 5 is the
runbook. The first real run is the operator's call, not the build's -- and it is
exactly the step this phase was built to make safe rather than to take on somebody's
behalf.

**Deliberately *not* in this phase:** any write, delete or mutate path (Phase 11);
statistical cost-anomaly detection and real-model hardening (Phase 4, split out of
this one).

**New dependencies, all optional extras so CI stays lean and offline:** `fastmcp`,
`azure-identity`, `azure-mgmt-resourcegraph`, `azure-mgmt-costmanagement`,
`azure-mgmt-monitor`, `azure-mgmt-advisor`, `openai` and `google-genai`. Each is
imported lazily and absent from the default install, so the suite and the demo run
without them.

---

## Phase 4 — Durable memory: episodic and semantic

**Goal:** CostSentinel remembers. Every run's findings, recommendations and
classifications persist per subscription and per client, a human's accept / reject /
defer decision is recorded against a recommendation, and the next sweep uses both --
so a client is not re-pitched advice they already declined, and an explanation can
cite the policy or guidance it rests on.

This is the phase that turns a stateless analyser into a system with a memory of the
estate it looks after. Everything in it is provable offline: the default store needs
no database, and the embedder has a deterministic fake.

Milestone: *Phase 4 - Durable memory*.

- [x] Typed memory-store interface covering both memory kinds, with an in-memory /
      SQLite implementation as the default and the only one CI and the demo use (#25)
- [x] PostgreSQL + pgvector implementation, selected only when a database is
      configured, with a thin integration test skipped unless a DB URL is present (#26)
- [x] `docker-compose.yml` earns its place: app plus postgres-with-pgvector, for
      real and local runs only, never CI (#27)
- [x] Episodic memory: persist findings, recommendations and policy classifications
      per subscription and client, correlated by `run_id`, with timestamps and
      provenance (#28)
- [x] `costsentinel decide <recommendation-id> --accept|--reject|--defer --reason`,
      writing a typed, validated, audited outcome into episodic memory (#29)
- [x] Semantic memory: a vector store over committed synthetic FinOps policy,
      Well-Architected cost-pillar guidance and pricing/meter notes, behind a
      pluggable embedder with a deterministic `FakeEmbedder` (#30)
- [x] Memory consult step wired into the graph: previously declined recommendations
      annotated and deprioritised, previously accepted patterns noted, and the
      Root-Cause Analyst able to ground an explanation in retrieved guidance (#31)
- [x] A determinism test: the same estate with and without a seeded rejection
      produces **identical** money figures and only reordered, annotated output (#32)
- [x] Memory reads and writes traced with hit/miss and latency, a no-op when tracing
      is unconfigured (#33)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.4.0 (#34)

**Exit criteria (met):** a recommendation declined through the `decide` CLI is demoted
and annotated with its decline date and reason on the next sweep, carrying the rank it
held on merit; the report says how many findings were previously declined; 805 tests
pass with no database, no network and no real embedder, at 94% coverage with `domain`,
`agents`, `guardrails` and `graph` at 97-100%; `ruff check`, `ruff format --check` and
`pyright` clean; and the determinism test proves memory changed only the ordering and
the annotations, with every money figure byte-identical.

**One thing to know about the test suite.** It now takes about 50 seconds without
coverage and about two and a half minutes with it, up from well under a minute. The
cause is volume rather than any single slow test -- the suite grew by 70 tests, many
of which run complete sweeps -- and the slowest individual test is under two seconds.
Worth watching rather than acting on; if it keeps climbing, the end-to-end determinism
tests are the ones to consolidate.

**Deliberately *not* in this phase:** the approval gate itself and its Teams and Slack
adapters (Phase 8). `decide` is a recording CLI, not a gate -- it writes an outcome
after the fact rather than halting a run. Separating them is what lets the learning
loop be demonstrated now instead of waiting for the adapters.

**New dependencies, all optional extras so CI stays offline:** `psycopg`, `pgvector`,
and whatever the real embedder needs. The default store uses the standard library's
`sqlite3`, so the demo path adds no dependency at all.

---

## Phase 5 — Cost anomalies and real-model hardening

**Goal:** the work that only becomes meaningful once real cost history and real models
are in hand. Split out of the original Phase 3 so that the safety-critical capability
unlock and this ordinary feature work are separately reviewable, then deferred behind
the memory layer because threshold calibration wants history that only real sweeps
accumulate (ARCHITECTURE.md D67).

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

- **Config-tunable ranking weights per client** (Phase 8, with the client policy
  overlays the approval gate needs anyway).
  The Phase 2 weights -- 0.60 savings, 0.25 safety, 0.15 confidence -- are a product
  judgement baked into a module constant. Different clients have genuinely different
  appetites: a government production estate may want safety weighted far higher than a
  development sandbox. The weights should move into the client policy overlay
  alongside the action-class tightening, with the built-in values as the default and
  the effective weights reported on every finding so a client can see what was applied
  to them. The ranking must stay deterministic and the weights must stay data, never
  model output.

- **Report residual spend alongside the saving, for every conservative action**
  (Phase 7, with reporting). A finding currently says "resize this VM, saves
  $140/month". For a conservative action it does not say what remains: downsizing
  `Standard_D8s_v5` to `Standard_D4s_v5` saves half and leaves the other half
  running. A client reading a report with a $12,000 annual savings headline cannot
  tell whether that is most of the waste or a third of it, which makes the headline
  simultaneously true and misleading.

  The report should carry, per finding and per client total: the spend today, the
  saving if the recommendation is accepted, and the **residual spend** that remains
  afterwards -- plus, where a more aggressive action exists, what that residual would
  be under it. That last part is what turns the number into a decision: "we are
  recommending the reversible change; the irreversible one would save this much more".

  All three figures are differences between catalogue prices and provider quantities,
  so they stay deterministic and go through `MoneyAmount` like any other cost. Where
  the more aggressive alternative is a `block`-class action, the residual must be
  stated without the action being proposed -- the point is to inform the client, not
  to route around the approval gate. Needs the reporting layer in place first, because
  it is a presentation change to a number the Savings Estimator can already compute.

- **A real-run acceptance checklist** (Phase 5, alongside threshold calibration).
  Phase 3 made a real read-only run safe but did not execute one. The first real sweep
  against a sandbox subscription will surface a predictable class of gap -- a SKU the
  catalogue does not price, a resource type `sku_mapping` does not translate, an
  untagged subscription, a region nobody refreshed prices for. Each of those currently
  degrades to an explicit unknown, which is correct behaviour and poor experience. The
  checklist should turn the first run into a diagnostic: what fraction of resources
  priced, which SKUs and regions were missed, and the exact command to fix each gap.

---

## Parked ideas

Ideas noted mid-phase, to keep scope from expanding. Not commitments.

- Reservation and savings-plan purchase recommendations (needs commitment modelling).
- Cross-client benchmarking ("your idle-VM rate vs the fleet median"), subject to a
  tenancy and confidentiality review.
- Cost forecasting and budget-breach prediction.
- A scheduled autonomous sweep cadence per client, with a digest.
- Tag-hygiene scoring as a standalone, fully `allow`-class mission.
