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

The graph is a star: every specialist returns to the supervisor, which re-reads the
state and decides what happens next. That shape is what makes retry, escalation,
short-circuit and the approval seam one mechanism instead of four.

```
                        +---------------------+
                        |    scan request     |
                        | (CLI / API / cron)  |
                        +----------+----------+
                                   |
                                   v
    +-------------------->+---------------------+
    |                     |     SUPERVISOR      |
    |   every specialist  |  the only node that |
    |   returns here      |  decides what next  |
    |                     +----------+----------+
    |                                |
    |      +-------------------------+-------------------------+
    |      |  proceed / retry / escalate / short-circuit       |
    |      +-------------------------+-------------------------+
    |                                |
    |   +------------+-----------+---+-------+-----------+
    |   |            |           |           |           |
    |   v            v           v           v           v
    | +-------+ +----------+ +---------+ +---------+ +----------+
    | |ANOMALY| |ROOT-CAUSE| | SAVINGS | |OPTIMIS. |  | POLICY  |
    | | SCOUT | | ANALYST  | |ESTIMATOR| | PLANNER |  | GUARD   |
    | |       | |          | |         | |         |  |         |
    | | reads | | why it   | | prices  | | chooses |  | allow / |
    | | the   | | exists,  | | EVERY   | | + ranks |  | review /|
    | | estate| | grounded | | option  | | (det.)  |  | block   |
    | +---+---+ +----+-----+ +----+----+ +----+----+ +----+-----+
    |     |          |            |           |           |
    +-----+----------+------------+-----------+-----------+
                                                          |
                                     review / block ------+
                                              |
                                              v
                                     +-----------------+
                                     |  APPROVAL SEAM  |  Phase 7: halt,
                                     |   (interrupt)   |  persist, resume on
                                     +--------+--------+  a recorded decision
                                              |
                                              v
                                     +-----------------+
                                     |  REPORT AUTHOR  |
                                     |  client-ready   |
                                     +--------+--------+
                                              |
                                              v
                                          +-------+
                                          |  END  |
                                          +-------+

  Cross-cutting (every node): AuditEvent | trace span | checkpoint write
  Cross-cutting (every LLM call): routing decision | tokens | cost | latency
```

**Ordering note.** The Savings Estimator runs *before* the Optimization Planner, and
prices every candidate action the policy store permits rather than one chosen action.
Two reasons: the planner then chooses knowing what each option is worth, as a human
would; and every monetary figure in the system is produced inside one node, so the
no-invented-numbers guarantee has a single place to hold. Ranking then happens in the
planner, because it is arithmetic over those prices.

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

**Savings Estimator.** Prices every candidate action for every signal,
arithmetically, from provider cost data and the committed price catalogue. **Never
LLM-derived** -- the module imports no language model at all, which a test asserts. If
an input is missing the estimate is marked unknown rather than guessed.

**Optimization Planner.** Chooses one of those priced options per signal and ranks the
set. Each `Recommendation` carries an `action`, a `risk_class`, a `rationale`, and
explicit `preconditions`. The model selects within the closed candidate set and writes
the rationale; the *ordering* is a deterministic weighted composite of savings, safety
and detector confidence, and the model is then asked only to narrate a ranking it has
already been given. A hostile model can change a report's wording and cannot change
what a client is told to do first.

**Policy Guard.** Classifies every proposed action against the policy store into
`allow`, `review` or `block`, and routes to the approval gate when the class
requires it. This is the safety chokepoint: no action reaches an execution path
without passing through it.

**Report Author.** Composes the client-ready report: executive summary, findings,
ranked recommendations, projected savings, and an explicit "what needs your
approval" section. LLM-assisted for prose; every number is interpolated from
validated model fields, never generated as text.

**Supervisor.** Owns conditional routing: short-circuit when there are no signals,
retry a node that produced nothing usable, escalate when retries are exhausted, and
route gated actions to the approval seam. Every decision is a recorded
`SupervisorDecision` in state with a mandatory reason, so a sweep can be explained
afterwards rather than inferred from control flow. Keeping routing in one place means
the specialists stay independently testable -- the routing tests build a state and
assert a decision without running a graph at all.

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

**`AzureLiveProvider`** (Phase 3) satisfies the same protocol against a live estate,
reaching Azure only through the MCP tool surface. It inherits read-only-ness rather
than restating it: there is no Azure client in the provider module to misuse. Three
properties are worth naming.

*Unknown is not zero.* A failed inventory read raises rather than returning an empty
estate; a failed cost read yields an empty series whose provenance says why, not a
zero-cost one; an unmeasured resource returns `None` so detectors read "cannot
conclude" rather than "idle". Those are the two ways a cost tool lies to a client
without anybody noticing.

*Scope is explicit.* Construction refuses an empty subscription list, and reading a
subscription outside the named scope raises. On a managed-services tenant, the
default anybody would reach for — everything the credential can see — means reading
other clients' estates because somebody forgot a flag.

*Translation is exact or absent.* Azure does not report the thing it bills you for: a
managed disk reports `Premium_LRS` and a size where the meter is `P15 LRS Disk`, a
public IP's meter depends on SKU and allocation method together, a storage account
reports redundancy and access tier separately. `providers/sku_mapping.py` does that
translation deterministically and returns `None` whenever it cannot be exact, so the
resource is priced as unknown rather than matched to the nearest meter. A plausible
wrong number in a client report is worse than an admitted gap.

The client boundary comes from a subscription tag (`AZURE_CLIENT_TAG`, default
`client`), because Azure has no notion of CostSentinel's client. An untagged
subscription is attributed to `unassigned` — a visible gap to fix, rather than a
guess that quietly folds it into somebody else's report.

Write operations remain absent entirely. They are a Phase 11 concern and will arrive
behind both the approval gate and an explicit opt-in flag.

### 4.1 Selection

`Settings.mode` selects the implementation through a single factory
(`providers.get_provider`). `MODE=mock` is the default everywhere, including CI. No
code outside the factory may name a concrete provider class. The real branch imports
the Azure SDKs inside the function rather than at module scope, so importing the
package never requires the optional `azure` extra — which CI does not install.

### 4.2 Authentication

`DefaultAzureCredential` and nothing else: an existing `az login`, a managed
identity, or a workload identity. There is deliberately no client-secret setting and
no `Settings` field for one; a test asserts its absence. A secret this codebase never
reads is a secret it cannot write to a log, attach to a trace, or carry into a
commit. `DefaultAzureCredential` has its own environment chain, so a service
principal still works — Azure's library reads those variables, not CostSentinel.

The credential is acquired eagerly at client construction, so a missing or expired
login fails with one legible message rather than surfacing as six tool failures
halfway through a sweep.

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

**MCP — how CostSentinel reaches tools (inbound capability).** Implemented in Phase
3. A FastMCP server (`tools/server.py`) exposes six typed, read-only tools:

| Tool | Azure service | Role required |
| --- | --- | --- |
| `azure_list_subscriptions` | Resource Graph | Reader (subscription or management group) |
| `azure_query_resources` | Resource Graph | Reader |
| `azure_get_cost_series` | Cost Management | Cost Management Reader |
| `azure_get_resource_metrics` | Monitor | Monitoring Reader |
| `azure_list_advisor_cost_recommendations` | Advisor | Reader |
| `azure_list_reservations` | Reservations | Reader on the reservation order, or Billing Reader |

The package is layered so that `schemas`, `registry`, `azure` (the handlers) and
`invoker` import without FastMCP or any Azure SDK. Only `clients.py` and `server.py`
touch the optional extras, and both import them lazily. That is what lets the entire
tool surface be covered offline, and it is why the real provider needs no running MCP
server to work locally — both it and the server dispatch through the same
`ToolInvoker`, so there is exactly one code path and the read-only guarantee holds for
both.

**The read-only guarantee is structural, in three independent ways.**

1. `ToolOperation` has exactly one member. There is no value a caller could pass to
   declare a mutating tool, so a write tool cannot be registered — it is absent
   rather than disabled. A boolean `read_only=True` would have been a convention; a
   single-member enum is a constraint.
2. The server registers exactly what the registry holds, by iterating it. There is no
   decorator-based registration, so enumerating the registry enumerates the whole
   surface — which is also what the `--real` banner prints, so the banner cannot
   drift from what is actually exposed.
3. No Azure SDK capable of mutating a resource is installed or imported anywhere in
   `src`. The capability is not present to misuse.

`tests/test_readonly.py` asserts all three mechanically, including that no tool name,
handler name or client method contains a mutating verb. A guarantee that depends on
nobody making a mistake is not a guarantee.

**Honesty model.** Every tool response carries a `ToolStatus` of `verified`,
`unknown` or `failed`, and a non-verified response must state a reason. `failed`
carries no data at all, enforced by a validator. The distinction between an empty
list of resources and a failed inventory query is the difference between "this
subscription is clean" and "we could not look" — conflating them is how a tool
quietly tells a client their estate is fine. `unknown` is not a soft failure: it
means the call succeeded and Azure does not know, which is itself a fact worth
carrying.

The mock provider mirrors the same `AzureProvider` interface, so agents cannot tell
the difference and tests exercise the real call path. Agents never import an Azure
SDK directly.

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

### Phase 2 decisions

**D34. The specialist graph was built before the MCP Azure tool server, swapping the
original Phase 2 and Phase 3.** *Alternatives:* keep the original order. *Why:* the
graph work is entirely mock-runnable and carries the product's visible value with no
cloud dependency, whereas the MCP server is the first thing that needs real
credentials and network access. Building the graph first keeps the zero-credential
invariant in place longer, and means that when the MCP boundary lands there is a
complete six-specialist graph to serve rather than a four-node slice.

**D35. The Savings Estimator prices every candidate action, and runs before the
planner.** *Alternatives:* planner chooses first, estimator prices the chosen action
(the Phase 1 order). *Why:* a planner choosing without knowing what each option is
worth is choosing blind, and a human would not. It also concentrates every monetary
figure in one node, so "no invented numbers" has a single place to hold rather than
being spread across the proposal and costing steps. The cost is pricing options that
are then discarded, which is cheap arithmetic over at most three candidates.

**D36. Ranking is a deterministic weighted composite; the model only narrates it.**
*Alternatives:* rank by savings alone (Phase 1); let the model rank. *Why:* savings
alone tells a client their best first move is the biggest number, which is wrong when
that number is risky and uncertain. The weights (0.60 savings, 0.25 safety, 0.15
confidence) are explicit policy constants reported on every finding, so a client sees
the trade-off rather than trusting an order. Letting the model rank would make the
most consequential output of the system unverifiable; a test asserts that swapping in
a hostile model changes the wording and not one rank.

**D37. Safety weighting does not guarantee a `block` action can never lead.**
*Context:* an earlier draft of the ranking module claimed it did. It does not, and the
claim was corrected rather than the behaviour. With the current weights an
`allow`-class finding at roughly 55% of the largest saving overtakes a `block`-class
finding holding the largest saving. *Why that is right:* if the single biggest
opportunity in an estate is something CostSentinel will not touch, a client should
still hear about it first -- they can do it by hand. The test pins the crossover
rather than a single data point.

**D38. A reservation is its own domain type, not a `Resource`.** *Alternatives:* model
it as a resource with a `RESERVATION` kind, which would have flowed through the
existing pipeline unchanged. *Why:* a reservation has a term, a quantity, an expiry
and a utilisation against a commitment, none of which a resource has, and the real
Azure Reservations API is a different surface from Resource Graph. The cost is a
`SavingsTarget` indirection so the savings functions do not care which they are
looking at -- a better boundary than either special-casing reservations or distorting
`Resource`.

**D39. Reservation waste is framed as loss already being incurred, not savings
available.** *Why:* the money is committed. Recovering any of it requires exchanging
or re-scoping the reservation under whatever terms the agreement allows, so the figure
is marked `is_estimated` and its basis says so. Reporting it as a straightforward
saving would overstate what a client can actually recover this month.

**D40. Every price comes from a committed catalogue snapshot, refreshed by hand.**
*Alternatives:* fetch from the Azure Retail Prices API at runtime -- the API is public
and unauthenticated, so it would not breach the no-secrets rule. *Why:* it would
breach the no-network rule for CI, and worse, it would make savings vary between runs
for reasons unrelated to the estate. `scripts/refresh_prices.py` pulls the real API on
demand and writes the snapshot, so a price change is a reviewable commit. The shipped
snapshot carries `is_real_api_pull: false` and its provenance is `UNVERIFIED`, because
those figures are plausible curated values that have *not* been fetched from Azure --
a cost tool must not imply otherwise.

**D41. Catalogue provenance is dated from the snapshot, not from load time.**
*Context:* dating it to load time broke the mock provider's determinism, which is how
it was found. *Why it is also more correct:* a price was retrieved when it was fetched
from Azure, not when a process happened to read the file.

**D42. `langfuse` is an optional extra, imported lazily, and the adapter is tested
with an injected double.** *Alternatives:* a core dependency; or no adapter tests.
*Why:* "never a hard dependency" is taken literally -- an absent or unconfigured
package degrades to `NoOpTracer` with a log line, CI does not install it, and the
suite passes without it. That would normally leave the adapter untested, so
`LangfuseTracer` accepts an injected client and the tests drive it with a fake. Every
tracer method also swallows backend exceptions: observability must be an aid, not a
liability.

**D43. `TracedLLM` wraps any backend to make routing observable.** *Alternatives:*
record routing inside each node, or inside each backend. *Why:* a wrapper satisfying
the same `LLM` protocol is transparent to the nodes and uniform across backends, so
routing, token usage, latency and computed cost are recorded in one place. The fake
backend ignores which model it was routed to, as it should -- but the decision is
still recorded, which is what makes the routing seam real rather than decorative.

**D44. Per-run cost goes through `MoneyAmount`, like any other figure.** *Why:* a
run's token cost is a cost. It is computed from a committed per-million-token rate
table, so it carries `CALCULATION` provenance and cannot be model-derived either. A
zero from the fake backend is a *measured* zero rather than an unknown, which is the
one place in the system where zero is a legitimate answer.

**D45. Run metrics travel alongside the reports, not inside them.** *Alternatives:*
put `RunMetrics` on `ClientReport`. *Why:* a client report is a deliverable about
their estate. What our model calls cost is our operational concern, and putting it in
a client-facing document would be both irrelevant to them and mildly embarrassing.
`ScanResult` carries both, keyed by run id.

**D46. The approval seam routes but does not yet halt.** *Why:* Phase 2 delivers the
routing decision and the `ApprovalRequest` records that Phase 7's durable interrupt
will act on. Classifying and routing without halting is safe because there is still no
execution path at all, so nothing can be mutated by a decision that has not been made.
The supervisor already treats an undecided gated action as the terminal state for that
action, which is the behaviour the interrupt will formalise.

**D47. Two new structured-output contracts, both without numeric fields.**
`RootCauseAnalysis` and `RankingRationale` join the existing two. Every contract in
the system remains free of monetary fields, so the "a model cannot produce a number"
property holds by construction at the seam rather than by inspection at the call site.
The root-cause contract adds a stronger constraint: each contributing factor must name
an observation the detector actually recorded, and the node drops any factor that
cites anything else. That is what makes groundedness a checkable property rather than
a hope -- a model asserting "the owner left the company" cannot get it into a report,
because there is no observation by that name to cite.

**D48. Four new actions were added to the closed vocabulary.**
`SCALE_DOWN_SQL_DATABASE`, `SCALE_DOWN_APP_SERVICE_PLAN`, `EXCHANGE_UNUSED_RESERVATION`
and `DELETE_STALE_SNAPSHOT`. *Why flagged:* the vocabulary being closed is a safety
property (D18), so growing it is a deliberate act rather than a detail. Each new
detector needed a remediation that could carry a real saving -- a waste kind whose only
option is "tell someone" produces an unknown figure and is of little use to a client.
Each new action has a floor, a full set of preconditions, and inherits the
production-tightening rule where it is a deletion.

### Phase 3 decisions

**D49. The phase was split: real-estate unlock now, statistical anomaly detection
later.** *Alternatives:* keep the original plan, which bundled cost-anomaly detection
and real-model work into the same phase as the real provider. *Why:* they answer
different questions. "Can CostSentinel safely read a live estate?" is a safety and
plumbing question with a binary answer, and everything in it — real prices, the tool
server, the read-only provider, the data-handling guard — is a prerequisite for anyone
using this at all. "Can CostSentinel tell a spend spike from a Tuesday?" is a
modelling question that needs real cost series to calibrate against, which only exist
*after* this phase. Sequencing them the other way round would have meant calibrating a
detector against synthetic data and then recalibrating it. Statistical anomaly
detection and real-model hardening are now Phase 4.

**D50. Read-only is enforced three ways, not one.** *Alternatives:* a single
mechanism — a `read_only` flag on each tool, or a code-review rule. *Why:* each of the
three fails differently, and the combination has no single point of failure. A flag can
be set wrong; a single-member enum cannot be. A registry can drift from what is
served; iterating it to register cannot. Both could be subverted by someone importing
an Azure management client directly; not having the dependency makes that impossible.
The cost is three tests instead of one, which is the cheapest insurance in the
repository.

**D51. The real provider reaches Azure only through a `ToolInvoker`.** *Alternatives:*
let the provider hold Azure clients directly and keep the MCP server as a separate
wrapper over the same handlers. *Why:* two wrappers over one set of handlers means two
places where read-only-ness has to hold, two caching policies, and two chances for
them to diverge. Going through the invoker means the provider module contains no Azure
client at all, so its read-only-ness is inherited rather than asserted separately —
and it needs no running MCP server for a local run.

**D52. An `unknown` or `failed` tool response becomes an explicitly unknown domain
value, never a zero or an empty list.** *Why:* this is the single most consequential
rule in the real provider. A failed inventory read that returned `()` would report a
clean subscription; a failed cost read that returned a zero series would tell a client
their estate is free. Both are worse than an error, because an error gets
investigated. A failed inventory read therefore raises, a failed cost read yields an
empty series whose provenance names the failure, and an unmeasured resource returns
`None` so the detectors read "cannot conclude".

**D53. A real run refuses to start without an explicit subscription scope.**
*Alternatives:* default to every subscription the credential can enumerate. *Why:* on
a managed-services tenant that default means reading other clients' estates because
somebody forgot a flag, and there is no way to undo having read them. The refusal
happens at provider construction and again in the CLI before any Azure import, so the
message is identical with or without the optional extra. `--subscription` also wins
over `AZURE_SUBSCRIPTION_IDS`, so a shell with a stale variable cannot widen the scope
of a deliberate command.

**D54. The resource SKU and the catalogue SKU are different things, and the
translation lives in one tested module.** *Alternatives:* price from the SKU Azure
reports, or match the nearest-priced meter. *Why:* Azure does not report the thing it
bills you for. A managed disk reports `Premium_LRS` and a size; the meter is `P15 LRS
Disk`, and which tier depends on the size, billed at the band the *provisioned* size
falls into rather than pro rata. Pricing from the reported SKU would be wrong; nearest
match would be plausibly wrong, which is worse. `providers/sku_mapping.py` does the
translation deterministically and returns `None` whenever it cannot be exact, so the
resource is priced as unknown.

**D55. A real reservation's amortised cost is the published *list* reserved price, and
the provenance says so.** *Why:* Azure exposes no API for what a customer actually
paid for a commitment. The list rate times the quantity is the closest defensible
figure, and a negotiated discount makes it an overstatement. Stating that in the
provenance — and marking the figure unverified — is the alternative to presenting it
as the contract value. The rate itself is real and names its meter; only the claim
"this is what you pay" would have been invented.

**D56. The client boundary comes from a subscription tag, and an untagged subscription
is named rather than guessed.** *Alternatives:* infer the client from the subscription
display name, or from the management-group hierarchy. *Why:* Azure has no notion of
CostSentinel's client, so something has to carry it, and a tag is the one thing a
managed-service provider can set uniformly across tenants they do not own. Inferring
from a display name would be a string heuristic deciding who sees whose cost data. An
untagged subscription becomes `unassigned`, which shows up as a gap to fix rather than
quietly joining a client's numbers.

**D57. Prices for a real run come from the committed catalogue, not from Azure at
runtime.** *Alternatives:* fetch Retail Prices during a sweep. *Why:* a sweep's
figures should be reproducible, and a price change should be a reviewable commit
rather than a silent difference between two runs of the same command. It also keeps
the live dependencies of a real run down to the four read APIs. The cost is that a
region the catalogue does not cover prices as unknown until somebody runs
`scripts/refresh_prices.py` — which the error message says.

**D58. Azure OpenAI authenticates with `DefaultAzureCredential` before an API key.**
*Alternatives:* require a key, which is the more commonly documented path. *Why:* one
`az login` then serves both the estate read and the model calls, and a key that is
never stored cannot be rotated late, committed, or read out of a log. A key is still
accepted because plenty of deployments only offer one. The decision is extracted into
`client_kwargs()` so it is tested without the SDK installed, because it is a security
property rather than a detail.

**D59. There is no client-secret setting at all.** *Alternatives:* keep
`AZURE_CLIENT_SECRET` in `Settings` as an option. *Why:* a secret this codebase never
reads cannot be logged, traced, serialised into a settings dump, or carried into a
commit — and the field existing invites somebody to put one there. Removing it loses
nothing, because `DefaultAzureCredential` reads its own environment chain, so a
service principal still works; Azure's library handles it, not us. A test asserts the
field's absence.

**D60. Both real backends share one validation gate and one retry.** *Alternatives:*
trust each SDK's own structured-output parse. *Why:* both SDKs will hand back
something shaped roughly like the requested schema, and trusting either would mean two
places where model output becomes a domain object. `llm/structured.py` revalidates
whatever came back, so "a model cannot produce a number" stays checkable at one seam.
Nothing is salvaged from a partial parse. A schema failure is retried once with the
validation error fed back as an instruction — retrying is only worth doing if the
second attempt knows what was wrong — and the correction goes in the instruction
rather than into an evidence block, because the fence is reserved for untrusted values
and widening it would weaken it. A transport failure is raised, not retried:
CostSentinel cannot tell a transient 503 from a misconfigured endpoint, and hammering
the latter is worse than failing.

**D61. Real model token rates come from configuration, and an unconfigured rate makes
the run cost unknown.** *Alternatives:* ship the published list prices as defaults,
which is what Phase 2 did as an acknowledged placeholder. *Why:* with real backends
live, that placeholder would have produced a per-run cost computed from a made-up rate
and presented as calculated — a straightforward golden rule 5 violation, in the one
place where the system reports on itself. Rates now come from
`LLM_PRICE_{SMALL,LARGE}_{PROMPT,COMPLETION}`, and `RunMetrics.total_cost` already
propagates an unknown. The fake backend stays priced at zero, because that is a
measured fact rather than an unknown standing in as zero: it makes no call.

**D62. An unconfigured real model resolves to a conspicuous `unconfigured`
identifier.** *Why:* a trace saying `unconfigured` is a configuration bug somebody can
find. A plausible-looking default model name would hide one. Relatedly, the small tier
falls back to the large deployment so a single-deployment setup needs no second
variable, but nothing falls back the other way — quietly sending synthesis work to a
small model would degrade a report with no sign of why.

**D63. A real run writes its report to a gitignored directory, not to stdout.**
*Alternatives:* print JSON as a mock run does, and leave redirection to the operator.
*Why:* this repository is public and a real report contains a client's subscription
ids, resource names and actual spend. The irreversible mistake available here is that
data reaching a commit, a ticket or a pasted terminal buffer, so the shape of the
command makes the safe thing the default one. Only a digest, the path and a warning go
to the terminal. A mock run is unchanged.

**D64. The commit guard matches ids in context rather than any GUID.** *Alternatives:*
flag every GUID-shaped string in a staged file, which was the first implementation.
*Why:* it flagged every Azure meter id in the committed price catalogue — public
pricing data every customer sees. A check that cries wolf on correct files gets
disabled, and a disabled check protects nothing, so the narrower rule is the safer one
in practice. It matches after `/subscriptions/`, beside a `subscription_id` key, after
`--subscription`, and treats nil-prefixed and single-character-repeated GUIDs as the
deliberately synthetic forms this repository is allowed to contain. The false positive
is pinned as a test.

**D65. The commit guard runs first among the pre-commit hooks.** *Why:* it is the only
failure in that list that cannot be undone. A lint error, a type error or a failing
test costs a minute. A real subscription id in a commit is permanent, and a force-push
does not help once somebody has a clone.

**D66. The JSON log handler resolves `sys.stderr` at write time.** *Alternatives:*
leave `logging.StreamHandler`'s default, which captures the stream at construction.
*Why:* found by a test that failed only when another test configured logging first,
but it is not a test artefact. A wrapper process, a supervisor reopening stderr on log
rotation, or a redirect would all silently lose CostSentinel's log lines — and a cost
tool whose audit trail can go quietly missing is worse than one that is loud.
