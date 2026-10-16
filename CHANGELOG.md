# Changelog

All notable changes to CostSentinel. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

CostSentinel is pre-1.0, so the public API may change between minor versions.

## [Unreleased]

Nothing yet. Next up is Phase 5: statistical cost-anomaly detection and real-model
hardening.

## [0.4.0] - 2026-10-16

Phase 4: durable memory. CostSentinel stops re-pitching advice a client already
declined. CI is unchanged -- no database, no network, no real embedder, no optional
extra installed -- because the default memory store is local SQLite and the default
embedder is deterministic.

### Added

- **A typed `MemoryStore` seam** with three implementations: `SqliteMemoryStore` (the
  default, and the only one CI and the demo use), `NullMemoryStore`, and
  `PostgresMemoryStore` behind `MEMORY_BACKEND=postgres`. The SQLite store is a real
  store, not a stub: the same schema shape, client scoping, ordering and refusal
  semantics as the Postgres one, differing only in where the vector comparison
  happens.
- **Episodic memory.** Every run's findings, recommendations and policy
  classifications persist per subscription and client, correlated by `run_id`, with
  timestamps and provenance. Savings are stored as the pre-rendered strings the client
  saw at the time, never as figures, so a stale number cannot be summed into a later
  total.
- **`costsentinel decide <id> --accept|--reject|--defer --reason "..."`**, writing a
  typed, validated, audited outcome. Plus `costsentinel decisions` to list what has
  been decided and what is still decidable. This is deliberately *not* the approval
  gate: it records a decision after a report has been read and authorises nothing,
  which is what lets the learning loop be demonstrated before the Phase 8 adapters
  exist.
- **Semantic memory** over three committed synthetic documents -- an org FinOps
  policy, a restatement of cost practice, and notes on Azure's billing shape -- chunked
  by Markdown heading, behind a pluggable embedder with a deterministic `FakeEmbedder`.
  The org policy is client-scoped and a test asserts another client never retrieves
  it. The corpus contains no prices.
- **A memory consult step** in the graph, between the Policy Guard and the Report
  Author. It imports no language model and changes exactly two things: the integer
  rank, and the annotations. A previously declined or deferred finding is annotated
  with the date and the reason and ranked lower, carrying the rank it held on merit.
  Nothing is ever removed -- a client declining advice does not make the waste
  disappear.
- **Grounded explanations.** The Root-Cause Analyst may retrieve policy and guidance
  passages and show them to the model inside the existing evidence fence, labelled
  distinctly from an observation. A passage can ground a narrative and cannot become a
  cited factor.
- **`docker-compose.yml` and a `Dockerfile`**, for local and real runs only. CLAUDE.md
  has listed compose since Phase 1 and there was nothing to compose until now. The
  stack changes the memory backend and nothing else -- still the mock estate and the
  fake model -- so the learning loop can be watched across restarts without going near
  a real subscription.
- **Memory observability.** Every read and write carries its operation, client scope,
  hit or miss, record count and latency, reaches the tracer as an event, and travels
  on `RunMetrics` alongside the model calls.

### Changed

- **`MEMORY_BACKEND` defaults to `sqlite`,** so `memory_enabled` is now true on a
  fresh clone. That does not weaken "requires no credentials": the default store and
  embedder need none, and the defaults test asserts both together.
- **The graph has a seventh node.** `SPECIALIST_NODES` now holds seven entries: the
  six specialists from CLAUDE.md section 5 plus the consult step, which is a support
  node rather than a seventh specialist.
- **The fake embedder's default width is 4096,** measured rather than chosen. At 256
  buckets a 45-token reference section fills most of the vector, so every query
  overlaps every document and relevance cannot be thresholded.
- **Embedding builds its vector sparsely,** so cost scales with text length rather
  than with vector width.
- The ruff `max-args` limit went from 9 to 10 for the runner, and stops there: when
  the embedder would have made eleven, it was derived from settings instead.

### Fixed

- **Memory reads did not degrade the way writes did.** A closed or corrupt store
  raised out of the middle of a sweep, which would cost a client their report over a
  lookup. Reads now return nothing and record the failure as a failure rather than as
  a clean miss -- the two look identical to a caller and must not look identical in the
  metrics. Found by a test asserting a write failure does not cost the report, which
  passed and then revealed the read path.
- **The exception base shadowed a builtin.** `MemoryError` is a Python builtin, and
  shadowing it had already turned one `raise` in the embedder into the wrong exception
  type. Now `MemoryStoreError`.

### Notes

- New dependencies, both optional extras CI does not install: `psycopg` and `pgvector`
  under `--extra memory`. New dev dependency: `pyyaml`, so the compose assertions parse
  the file rather than grep it -- text matching would pass on a commented-out line,
  which is exactly the mistake they exist to catch.
- The test suite is slower: about 50 seconds without coverage, two and a half minutes
  with. Volume rather than any single slow test; the slowest is under two seconds.
- 805 tests, 94% coverage. `domain`, `agents`, `guardrails` and `graph` at 97-100%.

## [0.3.0] - 2026-10-15

Phase 3: the real-estate, read-only unlock. The first release that can read a live
Azure subscription -- and the first that needed a safety surface to make doing so
defensible. CI is still fully offline, installs no optional extra and no credential,
and runs the entire suite against the deterministic mock estate.

### Added

- **Real prices for every catalogue SKU**, pulled from the public Azure Retail Prices
  API for `eastus` and `qatarcentral`. Each of the 27 committed prices names the Azure
  meter it came from, and provenance is now `verified`. Nothing in the repository
  claims a synthetic price any more. Savings assertions were re-baselined to the real
  figures.
- **A read-only MCP tool server** (`fastmcp`) over Resource Graph, Cost Management,
  Advisor, Monitor and Reservations: six typed tools, Pydantic in and out, with a
  `verified` / `unknown` / `failed` status on every response. A `failed` response
  carries no data, enforced by a validator -- because an empty resource list and a
  failed inventory query mean very different things to a cost report.
- **`AzureLiveProvider`**, satisfying the same `AzureProvider` protocol as the mock, so
  the Phase 2 graph runs against a live estate unchanged. It reaches Azure only through
  the tool surface, so it holds no Azure client of its own.
- **`providers/sku_mapping.py`**, which translates what Azure reports into what Azure
  bills. A managed disk reports `Premium_LRS` and a size where the meter is a `P15`
  tier; a public IP's meter depends on SKU and allocation method together. Returns
  `None` whenever it cannot be exact, so the resource is priced as unknown rather than
  matched to the nearest meter.
- **Azure OpenAI and Gemini backends**, opt-in via `LLM_BACKEND`, with model names from
  the environment and the routed model recorded in the trace. Both share one validation
  gate and one schema-failure retry that feeds the validation error back.
- **`costsentinel scan --real --subscription <id>`**: a local, read-only sweep via
  `DefaultAzureCredential`. Refuses to run without an explicit scope, prints the
  identity, scope, roles and every tool it may call before reading anything, and writes
  its report to a gitignored directory rather than stdout.
- **`SECURITY.md`**: the real-scan runbook, the permissions table, the three warnings
  (pilot on a sandbox, get written approval before any client estate, never commit the
  output), and how to report a vulnerability.
- **`scripts/check_no_real_data.py`**, wired as the first pre-commit hook: refuses a
  file under a real-output directory, a `.env`, a subscription or tenant id that does
  not look deliberately synthetic, and a real-provider provenance marker in a data
  file.
- **17 mechanical read-only assertions** (`tests/test_readonly.py`) and **45
  data-handling assertions** (`tests/test_data_handling.py`), including a test that
  runs the commit guard over every tracked file so it stays honest in both directions.

### Changed

- **Read-only is now structural in three independent ways**: `ToolOperation` has a
  single member so a write tool cannot be declared; the MCP server registers exactly
  what the registry holds so it cannot drift; and no Azure SDK able to mutate a
  resource is installed or imported. Each fails differently, so the combination has no
  single point of failure.
- **Real model token rates come from configuration.** The published list prices shipped
  in v0.2.0 as an acknowledged placeholder would, with real backends live, have
  produced a per-run cost computed from a made-up rate and labelled as calculated. Set
  `LLM_PRICE_{SMALL,LARGE}_{PROMPT,COMPLETION}` to get a figure; unset, the run reports
  its model cost as unknown. The fake backend stays priced at zero, which is a measured
  fact rather than an unknown standing in as zero.
- **The version is stated once.** `pyproject.toml`, `__version__` and the API all now
  derive from one value, with tests asserting they agree and that the CHANGELOG has an
  entry for it.
- `run_scan` takes `subscription_ids`, so the CLI, the API and the tests still share
  one code path.

### Removed

- **The `AZURE_CLIENT_SECRET` setting.** Authentication is `DefaultAzureCredential`
  only. A secret this codebase never reads cannot be logged, traced, serialised into a
  settings dump or carried into a commit, and the field existing invited somebody to
  put one there. A service principal still works -- `DefaultAzureCredential` reads its
  own environment chain. A test asserts the field's absence.

### Fixed

- **The JSON log handler captured `sys.stderr` at install time**, so once anything
  replaced the stream the log lines went to the old object. Found by a test that failed
  only when another test configured logging first, but a wrapper process, a supervisor
  reopening stderr on log rotation, or a redirect would all have silently lost
  CostSentinel's audit trail. The handler now resolves the stream at write time.
- **The commit guard's first implementation flagged every Azure meter id** in the
  committed price catalogue -- public pricing data every customer sees. A check that
  cries wolf on correct files gets disabled, so the rule now matches ids in context.
  The false positive is pinned as a test.
- **A read-only test had a bug in itself**: `startswith("azure.mgmt.resource")` matched
  the read-only Resource Graph client. Module-boundary matching now.

### Notes

- **No real sweep has been executed.** Everything a real run needs is in place and
  tested offline, but pointing it at a subscription requires an Azure tenant and a
  human decision about which one. That is the operator's call; `SECURITY.md` section 5
  is the runbook. ROADMAP Phase 4 carries a real-run acceptance checklist for the
  predictable gaps a first run will surface.
- A real reservation's amortised cost is the published **list** reserved price times
  the quantity, marked unverified, because Azure exposes no API for what a customer
  actually paid. A negotiated discount makes it an overstatement, and the provenance
  says so.
- 671 tests, 96.5% coverage; `domain`, `agents`, `guardrails` and `graph` at 98-100%.

## [0.2.0] - 2026-10-10

Phase 2: the full six-specialist graph, observability, and an observable
model-routing seam. Still mock- and fake-runnable end to end, with no new secrets and
an offline CI.

### Added

- **Root-Cause Analyst node.** Explains why each signal exists. Every contributing
  factor must cite an observation the detector actually recorded; a factor citing
  anything else is dropped and the rejection recorded. A model cannot get an invented
  fact into a report by asserting it.
- **Savings Estimator node.** Prices *every* candidate action the policy store
  permits, before the planner chooses, so the planner selects knowing what each option
  is worth. Imports no language model, which a test asserts.
- **Supervisor node and conditional routing.** Proceed, retry (bounded), escalate,
  require-approval and short-circuit. Every transition is a recorded
  `SupervisorDecision` with a mandatory reason.
- **Four new deterministic detectors**: stale snapshot, idle SQL database, oversized
  App Service plan, and unused reservation. Each has a positive case and a healthy
  counter-example in the mock estate.
- **Composite ranking** over savings, safety and detector confidence, with explicit
  documented weights. The ordering is deterministic; the model supplies the rationale
  only.
- **`Reservation` domain type** and `AzureProvider.list_reservations`, modelled
  separately from `Resource` because a commitment has a term, a quantity and an
  expiry that a resource does not.
- **Four new actions**: `SCALE_DOWN_SQL_DATABASE`, `SCALE_DOWN_APP_SERVICE_PLAN`,
  `EXCHANGE_UNUSED_RESERVATION`, `DELETE_STALE_SNAPSHOT`. Each with a class floor, a
  full set of preconditions, and the production-tightening rule where it deletes.
- **Committed price catalogue** at `providers/data/price_catalogue.json`. Every
  monetary figure in the system now traces to this one snapshot.
- **`scripts/refresh_prices.py`** to refresh that snapshot from the public Azure
  Retail Prices API. Run by hand, never by CI and never by a test.
- **Tracing seam**: `Tracer` protocol, `NoOpTracer` default, and a `LangfuseTracer`
  that imports langfuse lazily. `langfuse` is an optional extra; an absent or
  unconfigured package degrades to the no-op and never raises.
- **Per-run metrics**: token usage, computed cost and latency per model call and per
  run, with the routing decision recorded on every call.
- **`TracedLLM`** wrapper making the routing choice observable without the nodes
  knowing it is there.
- **Richer `ClientReport`**: root cause and ranking breakdown per finding, a
  per-waste-kind breakdown, blocked and unpriced counts, and the supervisor's
  escalations surfaced as `incomplete_reasons` so a thin report is not mistaken for a
  clean estate.
- Git and GitHub workflow in CLAUDE.md section 14.

### Changed

- **Phase order**: the specialist graph moved ahead of the MCP Azure tool server. The
  graph is mock-runnable; the MCP server is the first thing needing credentials.
  Recorded as ARCHITECTURE.md D34.
- Findings are no longer ranked by savings alone. A smaller, safer, more certain
  saving can now outrank a bigger, riskier one.
- The mock estate grew from 17 to 21 resources plus 3 reservations, and is priced
  entirely from the catalogue rather than from inline figures.
- Per-client projected savings for the sample estate changed accordingly: the
  northwind-energy report now shows 8 findings worth 1,350.41 USD a month, and
  ministry-of-transport 2 findings worth 208.70 USD a month.

### Fixed

- Catalogue provenance was dated from load time rather than from the snapshot, which
  broke the mock provider's determinism and also misreported when a price was
  retrieved.
- `ranking.py` claimed the safety weighting prevented a `block`-class action from ever
  leading a report. It does not; the claim was corrected rather than the behaviour,
  and the test now pins the real crossover (ARCHITECTURE.md D37).

### Notes

- Still no execution path. Nothing CostSentinel reports can be applied automatically.
- CI remains offline: no secrets, no cloud calls, no model calls.

## [0.1.0] - 2026-10-08

Phase 1: the scaffold and a thin vertical slice.

### Added

- Four-node LangGraph (Anomaly Scout, Optimization Planner, Policy Guard, Report
  Author) over a deterministic seeded mock Azure estate.
- Typed domain model where `MoneyAmount` refuses LLM provenance, so cost figures
  cannot originate with a model.
- `AzureProvider` protocol with a deterministic `MockAzureProvider`; `LLM` protocol
  with a deterministic `FakeLLM` and task-based routing.
- Policy store with an action-class floor that policy may tighten and never loosen.
- SQLite checkpointer: runs are persisted and resumable.
- `costsentinel scan` CLI and `POST /scan` API, both mock-only.
- ARCHITECTURE.md with a 33-entry decision log, and the phased ROADMAP.

### Notes

- Landed as an honest commit series dated when it was actually committed; it predates
  the issue-driven workflow and has no backing issues.

[Unreleased]: https://github.com/vivekanjana76/costsentinel/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/vivekanjana76/costsentinel/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vivekanjana76/costsentinel/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vivekanjana76/costsentinel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vivekanjana76/costsentinel/releases/tag/v0.1.0
