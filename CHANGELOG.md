# Changelog

All notable changes to CostSentinel. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

CostSentinel is pre-1.0, so the public API may change between minor versions.

## [Unreleased]

Nothing yet. Next up is Phase 3: the MCP Azure tool server and the real backends.

## [0.2.0] - 2026-10-08

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

[Unreleased]: https://github.com/vivekanjana76/costsentinel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vivekanjana76/costsentinel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vivekanjana76/costsentinel/releases/tag/v0.1.0
