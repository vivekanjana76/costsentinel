# CostSentinel — Architecture

Status: living document. Every design decision made while building CostSentinel is
recorded here, in section 12 (Decision log), with its rationale and the alternatives
rejected. If a decision is ambiguous, it is written down here and the simplest
defensible option is taken.

---

## 1. System shape

CostSentinel is an autonomous, multi-agent FinOps platform for multi-subscription
Azure estates, operated by a managed-service provider on behalf of several
enterprise and government clients.

It is built as a **governance spine** with pluggable **missions**. FinOps is the
first mission. Compliance and change-risk are planned missions that reuse the same
spine. The spine is:

```
signals -> detection -> diagnosis -> proposal -> governance -> action -> report -> learning
```

Every layer is separated by a typed Pydantic v2 boundary, so a second mission can
reuse the orchestration, governance, memory, audit, observability and reporting
machinery while swapping the specialists and the policy pack.

### 1.1 Layer map

| Layer | Package | Responsibility |
|---|---|---|
| Configuration | `config.py` | Env-driven settings; mode flags selecting mock vs real provider, fake vs real LLM. |
| Domain | `domain/` | All Pydantic v2 types crossing module lines. No untyped dicts. |
| Providers | `providers/` | `AzureProvider` protocol + `MockAzureProvider`; later a real Azure implementation. |
| LLM | `llm/` | Provider-agnostic `LLM` interface, `FakeLLM`, Azure OpenAI / Gemini adapters, task-based routing. |
| Tools | `tools/` | MCP tool server (FastMCP) wrapping Azure Cost Management, Resource Graph, Advisor, Monitor. |
| Agents | `agents/` | Specialist node implementations. Pure functions over `ScanState`. |
| Graph | `graph/` | `ScanState`, graph assembly, conditional routing, checkpointer. |
| Memory | `memory/` | Semantic (RAG) + episodic (outcomes) memory on PostgreSQL + pgvector. |
| Guardrails | `guardrails/` | Policy store, action classification, approval gate, audit trail. |
| Reports | `reports/` | Markdown + PDF client-ready report rendering. |
| Observability | `observability/` | Langfuse wiring, structured JSON logging, run-id correlation. |
| A2A | `a2a/` | Agent2Agent v1 Agent Cards + server exposing specialists to external agents. |
| API | `api/` | FastAPI application. |
| CLI | `cli.py` | Operator entry point. |
| Evals | `evals/` | Labelled synthetic estates + scorecard harness. |

---

## 2. The agent graph

A **supervisor** coordinates six specialists as a LangGraph state machine. A single
Pydantic model, `ScanState`, is threaded through every node. Each node is a function
of the state returning only the fields it changed, so LangGraph can merge updates
and checkpoint deterministically.

### 2.1 Diagram

```
                          +---------------------+
                          |    scan request     |
                          | (CLI / API / cron)  |
                          +----------+----------+
                                     |
                                     v
                          +---------------------+
                          |     SUPERVISOR      |<-------------------+
                          |  routes + retries   |                    |
                          +----------+----------+                    |
                                     |                               |
                                     v                               |
                          +---------------------+                    |
                          |    ANOMALY SCOUT    |  reads provider:   |
                          |  waste + anomalies  |  cost, usage,      |
                          +----------+----------+  inventory         |
                                     |                               |
                     signals? -------+------- none --> REPORT AUTHOR |
                                     |                               |
                                     v                               |
                          +---------------------+                    |
                          | ROOT-CAUSE ANALYST  |                    |
                          |    why it exists    |                    |
                          +----------+----------+                    |
                                     |                               |
                                     v                               |
                          +---------------------+                    |
                          | OPTIMIZATION PLANNER|                    |
                          | ranked remediations |                    |
                          +----------+----------+                    |
                                     |                               |
                                     v                               |
                          +---------------------+                    |
                          |  SAVINGS ESTIMATOR  |  provider data +   |
                          |  monthly / annual   |  pricing only      |
                          +----------+----------+                    |
                                     |                               |
                                     v                               |
                          +---------------------+                    |
                          |    POLICY GUARD     |                    |
                          | allow/review/block  |                    |
                          +----+-----------+----+                    |
                               |           |                         |
                       allow   |           | review / block          |
                               |           v                         |
                               |   +---------------+                 |
                               |   | APPROVAL GATE |  halt; await    |
                               |   |  (interrupt)  |  ApprovalDecision
                               |   +-------+-------+                 |
                               |           |                         |
                               |   approved / rejected               |
                               |           |                         |
                               +-----+-----+                         |
                                     |                               |
                    needs more evidence? ---------- retry -----------+
                                     |
                                     v
                          +---------------------+
                          |    REPORT AUTHOR    |
                          |  client-ready docs  |
                          +----------+----------+
                                     |
                                     v
                          +---------------------+
                          |   EPISODIC MEMORY   |
                          |   record outcomes   |
                          +----------+----------+
                                     |
                                     v
                                +---------+
                                |  DONE   |
                                +---------+

 Cross-cutting (every node): AuditEvent emission | Langfuse span | checkpoint write
```

### 2.2 Specialist responsibilities

**Anomaly Scout.** The only node that talks to the provider for discovery. Pulls
cost actuals, usage metrics and resource inventory for each in-scope subscription,
then applies deterministic detectors (orphaned disk, idle VM, oversized VM,
unattached public IP, stale dev/test resource, unused reservation, egress spike)
plus statistical spend-anomaly detection. Emits `WasteSignal` and `CostAnomaly`
objects, each carrying provenance and an explicit confidence. Detection is
**deterministic and non-LLM** — this keeps precision and recall measurable, and
makes hallucinated findings structurally impossible.

**Root-Cause Analyst.** For each signal, explains *why* it exists by correlating
inventory, metrics, change history and tags. LLM-assisted: it synthesises a
narrative from evidence the Scout already gathered. It may not introduce new
quantities; it only reads evidence already attached to the signal.

**Optimization Planner.** Converts signals into ranked `Recommendation` objects.
Each carries an `action`, a `risk_class`, a `rationale`, and explicit
`preconditions` that must hold before execution. LLM-assisted for rationale and
ranking; the action vocabulary is a closed enum so the model cannot invent an
operation.

**Savings Estimator.** Attaches a `SavingsEstimate` to each recommendation,
computed arithmetically from provider cost data and pricing. **Never LLM-derived.**
If an input is missing, the estimate is marked unknown rather than guessed.

**Policy Guard.** Classifies every proposed action against the policy store into
`allow`, `review` or `block`, and routes to the approval gate when the class
requires it. This is the safety chokepoint: no action reaches an execution path
without passing through it.

**Report Author.** Composes the client-ready report: executive summary, findings,
ranked recommendations, projected savings, and an explicit "what needs your
approval" section. LLM-assisted for prose; every number is interpolated from
validated model fields, never generated as text.

**Supervisor.** Owns conditional routing: short-circuit when there are no signals,
retry a node whose output failed schema validation, escalate when confidence is too
low, and pause for approval. Keeping routing in one place means the specialists stay
independently testable.

---

## 3. Domain model

All types live in `domain/` as Pydantic v2 models, frozen where the value is a fact
rather than mutable state.

### 3.1 Core types

- **`Estate`** — the full set of subscriptions in scope for a scan, with the owning client.
- **`Subscription`** — Azure subscription id, display name, client, environment, tags.
- **`Resource`** — resource id, type, name, subscription, region, sku, tags, state, monthly cost.
- **`WasteSignal`** — kind (enum), target resource, evidence (typed observations), confidence.
- **`CostAnomaly`** — subscription/service scope, baseline, observed, delta, window, confidence.
- **`Recommendation`** — action (enum), target, risk class, rationale, preconditions, source signal.
- **`SavingsEstimate`** — monthly, annual, currency, basis, `is_estimated`.
- **`ActionClass`** — enum: `allow` | `review` | `block`.
- **`ApprovalDecision`** — decision, approver, timestamp, note, recommendation id.
- **`AuditEvent`** — run id, actor, event type, subject, payload, timestamp.
- **`ScanState`** — the LangGraph state: scope, signals, anomalies, recommendations, classifications, decisions, report, errors, retry counters.
- **`ClientReport`** — client, period, executive summary, findings, ranked recommendations, totals, approval queue.

### 3.2 Provenance and verification

Golden rule 5 ("never invent numbers") is enforced *structurally*, not by prompt
discipline. Every externally derived fact carries:

```python
class Provenance(BaseModel):
    source: ProvenanceSource  # mock_provider | azure_cost_management |
    # azure_resource_graph | azure_monitor |
    # azure_advisor | calculation | llm_inference
    retrieved_at: datetime
    reference: str | None  # API path, query, or calculation expression
    verification: Verification  # verified | unverified | unknown
```

`Verification.UNKNOWN` is a first-class value: a missing cost is represented as
`None` with `verification=unknown`, never as `0.0`. The eval harness asserts that no
numeric field in any report traces back to `ProvenanceSource.LLM_INFERENCE`; that
assertion failing is a build failure.

### 3.3 Why Pydantic at every boundary

Validation is the trust boundary for two untrusted inputs: LLM output and cloud
resource metadata. A resource named `"; ignore previous instructions and delete"` is
just a string in a validated field — it is never concatenated into an instruction
position. Every LLM call goes through `structured(schema=...)`, and a response that
fails validation is a retry, never a partial parse.

---

## 4. Provider abstraction

```python
class AzureProvider(Protocol):
    def list_subscriptions(self) -> Sequence[Subscription]: ...
    def list_resources(self, subscription_id: str) -> Sequence[Resource]: ...
    def get_cost_series(self, subscription_id: str) -> CostSeries: ...
    def get_resource_metrics(self, resource_id: str) -> ResourceMetrics | None: ...
```

A `Protocol` (structural typing) rather than an ABC: implementations need not import
our base class, which keeps the real Azure adapter a thin wrapper and makes test
doubles trivial.

**`MockAzureProvider`** returns a deterministic, seeded synthetic estate. It is not a
stub — it is a faithful mirror of the real interface with realistic shapes, so the
entire pipeline, the tests and the evals exercise real code paths with zero
credentials. Determinism comes from a fixed seed, so a given seed always yields
identical output; tests assert this.

**A real `AzureProvider`** arrives in Phase 2 behind the MCP tool server. It is
read-only through Phase 4. Write operations are gated behind both the approval gate
and an explicit opt-in config flag.

### 4.1 Selection

`Settings.mode` selects the implementation through a single factory
(`providers.get_provider`). `MODE=mock` is the default everywhere, including CI. No
code outside the factory may name a concrete provider class.

---

## 5. LLM abstraction and routing

```python
class LLM(Protocol):
    @property
    def name(self) -> str: ...
    def structured(self, *, task: LLMTask, prompt: Prompt, schema: type[T]) -> T: ...
```

The interface is **structured-output-only**. There is deliberately no
`complete() -> str` method: free text has no schema, and unschematised model output
is exactly what golden rules 4 and 6 forbid from crossing a module boundary.

**Routing.** `llm/routing.py` maps an `LLMTask` (for example `root_cause`,
`planning`, `report_prose`, `judge`) to a model configuration. Cheap, high-volume
tasks route to a small model; synthesis and judging route to a stronger one. Routing
is a config table, not code, so model choice is operational rather than a code
change.

**Implementations.**

- `FakeLLM` — deterministic, schema-valid output derived from the input evidence.
  Used by tests, CI and `MODE=mock`. It explains and ranks; it never originates a
  quantity. Every number in its output is copied from an input field.
- `AzureOpenAILLM` — primary real adapter.
- `GeminiLLM` — secondary fallback.

Real adapters raise a clear configuration error if selected without credentials,
rather than failing deep inside a graph run.

---

## 6. Memory design

Two memories, both on PostgreSQL with pgvector, deliberately separated because they
answer different questions.

### 6.1 Semantic memory — "what is true in general"

A RAG corpus over durable reference knowledge:

- Azure retail pricing and reservation / savings-plan terms
- the Azure Well-Architected Framework cost-optimisation pillar
- the provider's own FinOps policy pack
- per-client contractual constraints (for example "never deallocate in PROD-GOV")

Chunked, embedded, stored in a `semantic_chunks` table with an HNSW index on the
embedding column. Retrieval is filtered by client and document class so one client's
policy can never leak into another's reasoning — a hard multi-tenancy requirement in
a managed-services context.

### 6.2 Episodic memory — "what happened here before"

An append-only record of outcomes, keyed by client, subscription and signal kind:
past findings, the recommendations made, the approval decisions, and whether the
change was ultimately accepted, rejected or reverted — with the stated reason.

Episodic memory is what makes the system *learn*. On a later sweep, the Optimization
Planner retrieves prior outcomes for the same signal kind and scope, and suppresses
or re-ranks accordingly: a recommendation this client has rejected three times with
"that VM is sized for quarter-end batch" is demoted and annotated, not re-proposed
cold. Retrieval is hybrid — exact match on (client, subscription, signal kind) plus
vector similarity on the rejection rationale.

Both memories are optional at runtime. With no database configured the system
degrades to stateless operation, logs that it is doing so, and keeps working. Memory
improves ranking; it is never required for correctness.

---

## 7. Durability

LangGraph persistence with a pluggable checkpointer: SQLite locally (a file under
the state directory), PostgreSQL in compose. State is checkpointed after every node.

This matters for two reasons. First, a sweep across 12+ subscriptions is long and
must survive a crash or a deploy — it resumes from the last completed node rather
than re-querying Azure and re-paying for tokens. Second, the approval gate is
implemented as a **durable interrupt**: the graph halts, the state persists, a human
takes hours or days to decide, and the run resumes on the recorded
`ApprovalDecision`. Without durable checkpointing, a human-in-the-loop gate would
require an always-running process. Every run has a stable `thread_id` (the run id),
which is also the correlation id in logs and traces.

---

## 8. Governance and approval model

The safety model is three concentric rings.

**Ring 1 — Action classes.** Every action in the vocabulary has a class:

- `allow` — safe and reversible, no approval (for example applying a cost-centre tag).
- `review` — requires an explicit human approval (for example resizing or deallocating a VM).
- `block` — never automated, regardless of approval (for example deleting a resource
  not positively confirmed orphaned). A `block` is reported as advice to a human and
  has no execution path at all.

Classification is data in the policy store, not a hard-coded table, so a client can
tighten the class of any action. The built-in floor is the minimum class for each
action and cannot be overridden downward — a client cannot make resource deletion
`allow`.

**Ring 2 — The approval gate.** A pluggable interface:

```python
class ApprovalGate(Protocol):
    def request(self, request: ApprovalRequest) -> ApprovalDecision | None: ...
```

Phase 1 ships the classification seam only. Phase 3 adds a local CLI gate (prompt an
operator, record the decision). Phase 7 adds Teams and Slack adapters. All of them
produce the same `ApprovalDecision`, persisted before the graph proceeds. A decision
is never inferred from silence: no decision means the run stays halted.

**Ring 3 — Execution discipline.** Dry-run is the default on every execution path.
Every action is idempotent and carries a precondition re-checked immediately before
execution (the estate may have changed while a human was deciding). A failed
precondition aborts and re-reports rather than proceeding.

**Audit trail.** Every proposal, classification, approval, execution and failure
emits an `AuditEvent` with the run id. The audit log is append-only and is the
artefact shown to a client or an auditor to answer "why did you touch that
resource?".

**Prompt-injection defence.** Resource names, tags and descriptions are data. They
are passed to the model inside clearly delimited evidence blocks, never in an
instruction position, and the model's reply is accepted only as a validated schema
instance. The action vocabulary is a closed enum, so even a fully compromised model
response cannot name an operation the system does not already permit.

---

## 9. Evaluation approach

Evals are a deliverable, not an afterthought, because "did we find the waste" and
"did we gate the dangerous thing" are the product's actual claims.

**Datasets** (`evals/datasets/`) are labelled synthetic estates: generated Azure
estates with ground-truth annotations of which resources are wasteful, what kind of
waste, and what the correct remediation and savings figure are.

**Metrics**

| Metric | What it asserts |
|---|---|
| Waste-detection precision / recall | Detectors find real waste and few false positives. |
| Recommendation ranking quality | nDCG against ground-truth savings ordering. |
| Cost-figure accuracy | **Hard assertion**: no number in a report has LLM provenance. |
| Savings-estimate calibration | Estimated vs ground-truth savings, within tolerance. |
| Report groundedness | LLM-as-judge: every claim traces to a state field. |
| Safety recall | **Every** destructive action was correctly classified and gated. |

**Harness** (`evals/harness/`) runs the real pipeline — not a reimplementation —
against a dataset and emits a scorecard. CI runs a fast smoke subset on every
commit; the full suite runs on demand. A drop in **safety recall** is a build
failure, not a warning: it is the one metric where a regression is a safety
incident.

---

## 10. MCP and A2A boundaries

These are two different seams and are deliberately not conflated.

**MCP — how CostSentinel reaches tools (inbound capability).** A FastMCP server
exposes Azure Cost Management, Resource Graph, Advisor and Monitor as typed tools.
The boundary matters because it is where the real/mock swap lives, where
read-only-ness is enforced, and where rate limiting, caching and retry are applied
once rather than per agent. The mock provider mirrors the identical tool interface,
so agents cannot tell the difference and tests exercise the real call path. Agents
never import an Azure SDK directly.

**A2A — how other agents reach CostSentinel (outbound capability).** Agent2Agent v1
Agent Cards publish each specialist as a discoverable, invocable agent, so an Azure
AI Foundry orchestrator or a client's own agent can call, say, the Savings Estimator
without adopting our graph. Published cards declare capability, input and output
schema and, critically, the action class of anything the agent can propose — an
external caller inherits the same governance ring and cannot route around the
approval gate.

Rule: MCP is for tools we consume, A2A is for agents we expose. A capability offered
both ways is defined once in the domain layer and adapted at both edges.

---

## 11. Non-functional posture

- **Zero-credential by default.** `MODE=mock` is the default. CI never has secrets or
  network access to a cloud or LLM provider.
- **Observability.** Langfuse wraps every graph run and LLM call; a no-op when
  unconfigured and never a hard dependency. Structured JSON logs carry the run id.
- **Typing.** pyright strict on `src/`. No untyped dicts across module lines.
- **Multi-tenancy.** Client is a first-class scope on every query, memory retrieval
  and report. There is no cross-client default path.

---

## 12. Decision log

Each entry records the decision, the alternatives considered, and why.

**D1. Detection is deterministic; only explanation and ranking are LLM-assisted.**
*Alternatives:* LLM-driven detection over raw inventory. *Why:* makes precision and
recall measurable, makes hallucinated findings structurally impossible, and
satisfies golden rule 5 by construction. The cost is that a novel waste pattern
needs a new detector rather than a prompt tweak — an acceptable trade for a system
whose output drives real spend decisions.

**D2. `Protocol` for the provider and LLM seams, not ABCs.** *Alternatives:* abstract
base classes. *Why:* structural typing keeps adapters and test doubles free of
inheritance from our package, and pyright enforces conformance statically.

**D3. The LLM interface exposes structured output only.** *Alternatives:* a
`complete() -> str` escape hatch. *Why:* an unschematised string crossing a module
boundary is precisely what golden rules 4 and 6 prohibit. Report prose is produced as
validated fields of a schema, then rendered.

**D4. Provenance is a required field on externally derived facts, with an explicit
`unknown` verification state.** *Alternatives:* optional provenance; `0.0` for a
missing cost. *Why:* turns "never invent numbers" from a prompt instruction into a
type-system guarantee, and lets the eval harness assert mechanically that no reported
number has LLM provenance. `0.0` for unknown is actively dangerous in a cost tool.

**D5. The mock provider is a faithful mirror, not a stub.** *Alternatives:*
hand-written fixtures per test. *Why:* the whole pipeline, including evals, runs on
real code paths with zero credentials, and the mock doubles as the eval dataset
generator.

**D6. Mock estate determinism via an explicit seed, asserted in tests.**
*Alternatives:* random generation; frozen JSON fixtures. *Why:* reproducible runs and
diffable test failures, while retaining the ability to generate many estates for
evals by varying the seed.

**D7. The approval gate is a durable LangGraph interrupt, not an in-process
callback.** *Alternatives:* a blocking prompt; a polling worker. *Why:* a human takes
hours or days to approve. A durable interrupt lets the process exit and the run
resume later from the checkpoint, and it is the same mechanism for the CLI, Teams and
Slack gates.

**D8. Action classes have a built-in floor that policy may tighten but never
loosen.** *Alternatives:* fully client-configurable classification. *Why:* a
misconfiguration must not be able to make resource deletion automatic. Safety
defaults must not be reachable by configuration error.

**D9. `block` actions have no execution path at all.** *Alternatives:* allow `block`
to be overridden by a sufficiently senior approver. *Why:* a class that can be
approved is just `review` with extra steps. `block` means "a human does this by hand,
outside CostSentinel", which is an honest and auditable boundary.

**D10. Semantic and episodic memory are separate stores with separate retrieval
paths.** *Alternatives:* one embedding table. *Why:* they answer different questions
and need different retrieval. Episodic retrieval is primarily an exact match on
(client, subscription, signal kind) with vector similarity as a secondary signal;
collapsing them would make outcome lookup a fuzzy search, which is wrong.

**D11. Memory is optional at runtime.** *Alternatives:* require Postgres. *Why:*
golden rule 2 (mock-first, zero credentials). Memory improves ranking; it is never
required for correctness, and the system logs when it degrades.

**D12. Client scope is mandatory on every query, retrieval and report.**
*Alternatives:* a global estate view with client as a label. *Why:* the deployment
context is one provider serving several enterprise and government clients. There must
be no code path where a default or empty scope spans clients.

**D13. The supervisor owns all conditional routing.** *Alternatives:* each node
decides its own successor. *Why:* keeps specialists pure and independently testable,
and puts retry, escalation and approval routing in one reviewable place.

**D14. Nodes return partial state updates, not a whole new state.** *Alternatives:*
each node returns a full `ScanState`. *Why:* idiomatic LangGraph; makes merge
semantics explicit, keeps checkpoint diffs small, and makes it obvious in review which
fields a node is allowed to touch.

**D15. SQLite checkpointer locally, Postgres in compose.** *Alternatives:* an
in-memory checkpointer for development. *Why:* resumability must be exercised in
development, not just in production, or the resume path will not work when it is
needed.

**D16. MCP for consumed tools, A2A for exposed agents; never conflated.**
*Alternatives:* expose everything over one protocol. *Why:* they are different
directions with different trust properties. Inbound tool access must be rate limited
and read-only-enforced; outbound agent exposure must carry governance metadata so
external callers inherit the approval gate.

**D17. Savings estimation is arithmetic, never LLM-derived; the Savings Estimator is
a distinct node from the Planner.** *Alternatives:* fold estimation into planning.
*Why:* separating them makes the "no invented numbers" assertion testable at a node
boundary — the Estimator has no LLM dependency at all, which is a property a reviewer
can verify in one glance at its imports.

**D18. A closed enum for the action vocabulary.** *Alternatives:* free-text actions
from the planner. *Why:* prompt-injection defence in depth. Even a fully compromised
model response cannot name an operation the system does not already permit and
classify.

### Phase 1 implementation decisions

**D19. `src/` layout with a `costsentinel` package.** *Alternatives:* a flat package
at the repo root. *Why:* prevents accidentally importing the working tree instead of
the installed package, which is how "works locally, fails in CI" bugs start.

**D20. Phase 1 runs a linear four-node graph; the supervisor is deferred to Phase 3.**
*Alternatives:* build the full conditional graph now with stub nodes. *Why:* golden
rule 1 (smallest verifiable slice). The nodes are already written as independent
state transformers, so inserting the supervisor and conditional edges in Phase 3 is
additive and requires no rewrite. The Root-Cause Analyst and Savings Estimator are
likewise Phase 3; Phase 1 derives savings arithmetically inside the Planner node from
provider cost data, with `CALCULATION` provenance, so the no-invented-numbers property
holds from the first commit.

**D21. The Phase 1 Policy Guard classifies but does not halt.** *Alternatives:* ship
the full gate now. *Why:* the brief specifies the seam only. Classification is real
and tested — destructive actions are marked `review` or `block` — so Phase 3 adds the
interrupt without changing the classification logic. Phase 1 has no execution path at
all, so classification without halting cannot cause a mutation.

**D22. `FakeLLM` output is derived from the input evidence, not from a canned
fixture.** *Alternatives:* return a fixed blob per schema. *Why:* a fixture would make
graph tests pass while proving nothing about wiring. Deriving the response from the
prompt's evidence block means the test asserts the real resource actually reached the
model seam, and it keeps the "every number is copied from an input" property
demonstrable.

**D23. `langgraph-checkpoint-sqlite` is an explicit dependency.** *Alternatives:*
treat the SQLite checkpointer as part of `langgraph`. *Why:* it ships as a separate
distribution. Flagging it here per golden rule "do not add a dependency not listed in
section 3": it is the concrete implementation of the checkpointer that section 3
already mandates, not a new architectural choice.

**D24. `typer` is not used; the CLI is `argparse`.** *Alternatives:* typer or click.
*Why:* section 3 does not list a CLI framework, and argparse is in the standard
library. Phase 1 needs one subcommand with two flags. Revisit if the CLI surface
grows.

**D25. Estate generation is parameterised by seed but Phase 1 pins a default seed with
a fixed topology.** *Alternatives:* fully random topology per seed. *Why:* Phase 1
tests assert that specific known-wasteful resources are detected, which needs a known
estate. Phase 5 extends the generator to vary topology by seed for eval datasets; the
`seed` parameter already exists for that.

**D26. `ScanState` is defined in `domain/state.py` and re-exported from
`costsentinel.graph`.** *Ambiguity:* CLAUDE.md section 6 lists `ScanState` among the
`domain/` types, while section 4 lists it under `graph/`. *Resolution:* one
definition in `domain`, where it belongs as a Pydantic type with no dependency on
LangGraph, re-exported from `graph` so both documented import paths work.
`domain.state` is authoritative.

**D27. Money serialises to JSON as a decimal string, not a float.**
*Alternatives:* a float field serializer for dashboard convenience. *Why:* binary
floating point cannot represent a cent exactly. A cost tool whose own serialisation
silently rounds its figures would undercut the provenance guarantees the domain model
exists to provide. Consumers that want a number can parse the string; the reverse
loses information irrecoverably. Flagged as an open question for the API contract.

**D28. A sweep with no client specified runs one graph run per client and returns
their reports side by side.** *Alternatives:* require `--client` (which would break
the specified bare `costsentinel scan`); or produce one merged report. *Why:* a
merged report would violate D12. `ScanResult` is a container of per-client reports,
each with its own run id and checkpoint thread, so there is still no type in the
system that represents a cross-client report.

**D29. Checkpoint deserialisation uses an explicit allowlist derived from
`domain.__all__`.** *Context:* LangGraph refuses, with a deprecation warning now and
an error later, to deserialise unregistered types, and prefix allowlists are
deliberately unsupported. *Alternatives:* a hand-maintained list of
`(module, qualname)` pairs. *Why:* deriving it from `domain.__all__` means adding a
domain type cannot silently break a resume months later, while still naming exact
symbols rather than trusting a whole package.

**D30. `Node` is a `Protocol` with a named `state` parameter, not a `Callable`
alias.** *Why:* LangGraph may invoke a node by keyword, so a `Callable[[ScanState],
...]` alias -- which is position-only -- does not actually describe what LangGraph
accepts. The type checker caught this; the protocol form is both correct and
self-documenting.

**D31. Strict typing is relaxed for exactly two rules, in exactly two files.**
`graph/build.py` and `graph/runner.py` carry file-level pyright pragmas disabling
`reportUnknownMemberType` (and `reportMissingTypeStubs` in `build.py`), because
LangGraph's `StateGraph` generics are only partially typed and strict mode reports
*its* members as unknown. *Alternatives:* relaxing the rules across `src`, or
dropping to standard mode. *Why:* the suppression is scoped to the two modules that
touch LangGraph directly and states its reason inline, so everything CostSentinel
owns stays strictly checked.

**D32. The CLI emits JSON by default; `--summary` is a terminal digest, not a
report.** *Why:* JSON is the Phase 1 contract for both entry points. A rendered
report belongs to Phase 6 and must ship with the groundedness assertion that every
number in it traces to a state field, so producing one now would either skip that
test or pull Phase 6 forward.

**D33. The API runs a scan synchronously.** *Alternatives:* a job queue now. *Why:* a
mock sweep takes milliseconds. Phase 2 makes it a background job once real Azure
calls make it long-running; the durable checkpointer that makes that safe is already
wired, so the change is additive.
