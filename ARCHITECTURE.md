# CostSentinel — Architecture

Status: living document. Every design decision made while building CostSentinel is
recorded here, in section 14 (Decision log), with its rationale and the alternatives
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
    |   +--------+---------+---------+---------+---------+---------+
    |   |        |         |         |         |         |         |
    |   v        v         v         v         v         v         |
    | +-------+ +--------+ +-------+ +-------+ +-------+ +--------+ |
    | |ANOMALY| | ROOT-  | |SAVINGS| |OPTIM. | |POLICY | | MEMORY | |
    | | SCOUT | | CAUSE  | | ESTIM.| |PLANNER| | GUARD | |CONSULT | |
    | |       | |ANALYST | |       | |       | |       | |        | |
    | | reads | | why it | |prices | |chooses| |allow /| |re-rank | |
    | | the   | |exists, | | EVERY | |+ ranks| |review/| |+ annot.| |
    | | estate| |grounded| |option | | (det.)| |block  | |(no $)  | |
    | +---+---+ +---+----+ +---+---+ +---+---+ +---+---+ +---+----+ |
    |     |         |          |         |         |        |       |
    +-----+---------+----------+---------+---------+--------+-------+
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
  Cross-cutting (every memory access): operation | hit/miss | records | latency

  MEMORY (Phase 4), read by two nodes and written once per run:
    episodic  <- what this client was shown, and what they decided about it
    semantic  <- org policy, cost guidance, pricing notes (retrieved as evidence)
```

**Ordering note.** The Savings Estimator runs *before* the Optimization Planner, and
prices every candidate action the policy store permits rather than one chosen action.
Two reasons: the planner then chooses knowing what each option is worth, as a human
would; and every monetary figure in the system is produced inside one node, so the
no-invented-numbers guarantee has a single place to hold. Ranking then happens in the
planner, because it is arithmetic over those prices.

**The consult step is last before the report, and deliberately so.** Memory Consult
runs after everything monetary has been computed, which is what makes "memory may
reorder, never recompute" structural rather than a rule somebody has to remember: by
the time it runs there is nothing left to compute, and it has no way to recompute it.
It is not one of CLAUDE.md section 5's six specialists -- it is a support step that
reorders and annotates what the six produced -- but it is a supervisor-routed node
like any other, so its decision is in state, in the checkpoint and in the audit
trail.

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

Implemented in Phase 4. Two memories, deliberately separated because they answer
different questions, behind one `MemoryStore` protocol with three implementations.

**One rule governs both, and it is the invariant the phase rests on: memory may
reorder and annotate; it may never recompute.** No type in `domain/memory.py` holds a
`MoneyAmount`, no module under `memory/` imports one, and no attribute access there
reaches a money field. A client's past decision is not evidence about what a resource
costs this month; letting a rejection shrink a saving would make the headline depend
on sentiment rather than on arithmetic over provider data. Two tests guard it, one
structural (AST) and one end-to-end (a money fingerprint across two runs).

### 6.1 The store seam

```python
class MemoryStore(Protocol):
    @property
    def is_enabled(self) -> bool: ...
    # episodic
    def record_run(self, record: EpisodicRecord) -> bool: ...
    def record_outcome(self, outcome: RecommendationOutcome) -> bool: ...
    def outcomes_for(self, client: str) -> Sequence[RecommendationOutcome]: ...
    def entries_for(self, client: str, *, limit: int = 500) -> Sequence[EpisodicEntry]: ...
    # semantic
    def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> int: ...
    def search(self, embedding, *, limit, client=None) -> Sequence[SemanticHit]: ...
```

Three properties are designed into the seam rather than left to callers.

**Memory is optional, and absence is not an error.** `NullMemoryStore` exists so every
caller holds a store unconditionally and no node needs an `if memory is not None`
branch. The alternative puts the memory-is-off path in six places instead of one, and
five of them are the ones nobody tests.

**Empty is not a failure.** Every read against an empty store returns an empty result.
An estate nobody has scanned before misses on every lookup, which is correct and
expected, and a first run on a new client must not be a different code path.

**A failure degrades the sweep; it never fails it.** Writes return `False`; reads
return nothing and record the failure as a *failure* rather than as a clean miss --
the two look identical to a caller and must not look identical in the metrics. Losing
a memory operation is an inconvenience; losing a client's report over one is not
acceptable.

Episodic methods take the client in their signature rather than filtering afterwards,
which is how D12 reaches the storage layer: there is no method that returns another
client's history.

### 6.2 Two backends

| | `SqliteMemoryStore` | `PostgresMemoryStore` |
| --- | --- | --- |
| Selected by | `MEMORY_BACKEND=sqlite` (default) | `MEMORY_BACKEND=postgres` + `DATABASE_URL` |
| Needs | nothing beyond the standard library | `uv sync --extra memory`, a database |
| Vector search | cosine in Python over candidate rows | pgvector's `<=>` operator |
| Used by | CI, the demo, every test | a real deployment, `docker compose` |

The SQLite store is a real store, not a stub: the same schema shape, the same client
scoping, the same newest-first ordering, the same refusal to overwrite an outcome. The
only difference is where the comparison happens. That matters because it means a test
written against the default store is meaningful about the Postgres one's behaviour,
and the pgvector store's integration test can confine itself to the two things that
genuinely differ -- the SQL dialect and the vector operator -- and skip unless a
database is present.

`docker-compose.yml` runs the app against `pgvector/pgvector:pg16`. It changes the
memory backend and nothing else: still `MODE=mock`, still the fake model and embedder,
so the learning loop can be watched across restarts without going near a real
subscription.

### 6.3 Episodic memory — "what happened here before"

Three records, all snapshots rather than references:

- `EpisodicRecord` — one run, with its subscriptions and resource count.
- `EpisodicEntry` — one recommendation as it stood when the run produced it.
- `RecommendationOutcome` — what a human decided about it, with a mandatory reason.

A saving enters history as the **pre-rendered display string** the client saw at the
time, never as a figure. The resource may be gone next month and the price may have
changed, and a history that silently re-resolved either would stop being a history.
It also means a stale figure cannot be summed into a later total.

The write happens once, after the report is composed, from the final state. A
half-written history is worse than none, because the next sweep reads it as fact.

Outcomes are recorded by `costsentinel decide`, which is **not** the approval gate.
The gate (Phase 8) halts a run and authorises an action; `decide` records an outcome
after a report has been read and authorises nothing. That separation is what lets the
learning loop be demonstrated now rather than waiting for the Teams and Slack
adapters. `Outcome` has three members where `Decision` has two, because "not now" is
different information from "no": a deferral means the client has considered the advice
and intends to act, so re-pitching it as new is a small insult and forgetting it is a
lost saving.

### 6.4 Semantic memory — "what is true in general"

Three committed, synthetic documents: an org FinOps policy, a restatement of cost
practice in our own words, and notes on how Azure's billing shape works. All three say
they are synthetic in their first lines, none contains a price, and a test asserts the
corpus passes the real-data guard. Nothing real is committed -- no client's actual
policy, no vendor's copied text.

Chunked by Markdown heading rather than by character count. A fixed-size window cuts a
rule in half, and half a rule retrieved and cited is worse than no citation: "may be
deleted only once two conditions hold" is actively misleading without the conditions.
The heading is embedded with the body, so a section titled "Snapshots and backups"
whose body never repeats the word is still reachable.

The org policy is client-scoped and the other two are shared. Retrieval filters on
that **in the query**, not afterwards: a query that could return another client's
policy and then discard it is one refactor away from returning it.

`FakeEmbedder` is the default and the only one CI uses, and it is not a stub. It hashes
tokens into a fixed-width vector with sub-linear term weighting, so cosine similarity
genuinely tracks shared vocabulary and a retrieval assertion is meaningful. It is
lexical, not semantic -- it has no notion that "idle" and "unused" are related -- and
the real embedder is opt-in for that. The point of the fake is that the plumbing
(chunking, storage, ranking, citation, client scoping) is fully exercised offline, so
switching on a real embedder changes one variable.

Every chunk records which embedder produced it, because mixing vectors from two
embedders in one store produces silently meaningless similarities.

### 6.5 The consult step

A graph node between the Policy Guard and the Report Author. It imports no language
model and changes exactly two things: the integer `rank`, and the annotations the
report carries.

Matching has two tiers, and the distinction is load-bearing:

- **exact** — same client, same resource, same action. Strong evidence; heavy penalty
  (0.70 of the ranking composite withheld).
- **pattern** — same client, same waste kind, same action, *different* resource. Weak
  evidence, because the reason may have been specific to the other resource; light
  penalty (0.20). Treating the two alike would let one "no" bury a whole category of
  genuine waste, which is how a cost tool starts under-reporting.

Acceptance is noted and moves nothing. The temptation is to promote it -- the client
liked this sort of thing, so show more -- and that is resisted, because a client having
agreed to something last month is not evidence that a different resource is more
wasteful this month. Promoting on it would make the order reflect agreeableness rather
than value.

The penalty is multiplicative on the composite, so it stays inside 0-1 whatever the
base was and means the same thing for a high-scoring finding as for a low one. It is
never 1.0, so previously-declined findings keep a stable order among themselves.

**Nothing is ever removed.** A client declining advice does not make the waste
disappear, and a report that quietly dropped it would misstate the estate. The finding
stays, annotated with the date and the reason, carrying the rank it held on merit so a
client can read "this ranked second and was demoted to fifth because you declined it
in September". An ordering that changed for reasons nobody can see is worse than no
reordering at all.

Retrieved guidance reaches the Root-Cause Analyst as evidence inside the existing
fence, labelled distinctly from a signal so the model can tell a rule from an
observation. That widens what an explanation may *rest on* and not what it may
*assert*: the contract still has no numeric fields, and a factor must still cite a
detector observation, so a passage cannot become a factor.

### 6.6 Observability

Every read and write is a `MemoryAccess` carrying the operation, the client scope,
hit or miss, the record count and the latency. These reach the tracer as events rather
than spans -- they are short, frequent and already timed, and a span per lookup would
bury the model calls that actually cost something -- and travel on `RunMetrics`
alongside the model calls rather than inside the report (D45).

A miss and a failure stay distinguishable throughout. A memory layer whose hit rate
nobody can see is a memory layer nobody can tune.

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

## 9. Exemption: waste that is waste on purpose

Implemented in Phase 6. Some resources are idle, stale or oversized *by design*. A
disaster-recovery standby is supposed to sit at 2% CPU; a snapshot held for a six-year
retention period is supposed to be old. The detectors are right about both and the
advice is wrong, because the thing arithmetic over metrics cannot see is intent — and
in a real estate, intent is written in tags.

The Phase 5 scorecard measured exactly this: a DR standby reported as idle and a
HIPAA-retained snapshot reported as stale. Those are the findings that lose a client's
trust, because the client knows the answer and the tool does not.

### 9.1 Deterministic tag reading, never interpretation

`ExemptionPolicy` matches resource tags against a committed ruleset. A rule names a
set of tag **keys** and either requires a **value from a closed set** or accepts **any
non-empty value**. No model is involved, no regular expression comes from
configuration, nothing is fuzzy, and keys are matched whole rather than as substrings.

The two match modes are not interchangeable, and the distinction is load-bearing:

| Mode | Used when | Example | A hostile value |
| --- | --- | --- | --- |
| `value_in` | the value is an **assertion** | `cost-exempt: true` | does not match |
| `any_value` | the value is a **reference** | `retention: HIPAA-6YR-REF-2291` | does exempt |

A closed value set is what stops a hostile string from asserting an exemption. An open
one is unavoidable where the value is a requirement reference, because nobody can
enumerate every reference a client might cite — and the alternative would mean a real
HIPAA reference nobody had listed got a snapshot deleted. So a hostile value under
`any_value` does exempt, and that is the **safe failure**: advice withheld, with the
tag that withheld it printed on the report. The mirror image would be a deleted
production disk.

Deliberately *not* read: resource names, resource groups and descriptions. Those are
attacker-influenceable too, and unlike a tag they carry no convention the client has
agreed to, so an exemption drawn from one would be a guess dressed as a policy.

### 9.2 Surfaced, never suppressed

An exemption removes the **recommendation**, never the **finding**. Dropping the
finding is the easy implementation and the dishonest one: the client loses the one
fact they might act on later, which is what the standby costs them every month.

So an exempt signal is:

- still detected, and still in `state.signals`;
- carried as an `ExemptionVerdict` in `state.exemptions`, with a machine-readable
  reason, the rule that decided it and the matched tag;
- excluded from `state.actionable_signals`, which is what the diagnosis, pricing and
  planning nodes read — one definition of "actionable" rather than four that can
  drift;
- never priced, because a saving nobody may take is one edit away from a headline
  total;
- reported as an `ExemptFinding`, a separate type from `ReportFinding` because a
  finding is a *proposal* and an exempt item has no rank, action, class, saving or
  preconditions and never will;
- recorded as an `EXEMPTION_APPLIED` audit event, so "why was this not recommended?"
  is answerable from the trail rather than by re-running the sweep.

The report separates **actionable savings** from **flagged but exempt**.
`projected_monthly_savings` is the sum of the actionable findings and nothing else;
the exempt items carry their **current cost**, a provider fact, which is never added
to a saving. `exempt_monthly_cost` is `MoneyAmount | None`, and the `None` matters:
nothing exempt is an absence, not an unknown amount, and rendering the two alike would
turn "no exempt resources" into "unknown spend on exempt resources".

### 9.3 The invariant: exemption may only reduce action

**A tag may move a finding toward "leave this alone". A tag may never move a finding
toward "act on this", downgrade a safety classification, or make something automatable
that was not.**

This mirrors D8 — policy may tighten a class and never loosen one — and for the same
reason. Both read client-supplied configuration and both are only safe in one
direction. A client exemption overlay may *add* rules and cannot remove a built-in
one, so no configuration can switch off the retention exemption.

Asserted four independent ways, because the first three could all hold while the
wiring was wrong:

1. **Structural.** Neither exemption module imports `ActionClass`,
   `ActionClassification`, `PolicyStore` or `MoneyAmount`, and `policy.py` imports
   nothing from exemption. An AST test checks both directions, as it does for the
   absent LLM import in `savings.py` (D17).
2. **Representational.** `ExemptionVerdict` has no field an action class or a money
   amount could live in. An exemption cannot *carry* a classification, so it cannot
   loosen one.
3. **Exhaustive over the classifier.** Every key any rule reads, crossed with every
   value any rule accepts plus a battery of hostile strings, against every gated
   action. The classification never moves. `PolicyStore.classify` also accepts no
   parameter through which a tag could arrive, and re-running the Policy Guard over a
   state with the exemptions stripped out yields identical verdicts.
4. **End to end.** A real sweep over an orphaned-disk deletion with 33 tags worth
   trying: recommendations, classifications and gated counts can only go down, every
   surviving class is at least as strict, and either the deletion is withheld or it is
   gated, with no third outcome.

Ranks renumber and do not reshuffle. Exempting an entry necessarily renumbers what is
left — there is no way to avoid it short of leaving a gap where a proposal used to be
— so the claim is that the *relative* order is unchanged and no finding moves down the
page. That is checked on an estate where the exempt resource holds the largest saving,
which is where it could plausibly fail: the savings component is normalised against
the largest saving in the run, so removing the leader rescales it for everything else
while leaving safety and confidence alone.

### 9.4 Per-client configuration

Tag conventions are a data file, not code. `EXEMPTION_RULES_PATH` points at a ruleset;
unset uses the one shipped in `src/costsentinel/guardrails/data/exemption_rules.json`.
A client whose standby estate reads `nw-continuity-role: secondary` is a JSON edit.
Client rules are evaluated before the defaults, so when two rules would both fire the
client's own vocabulary is the one explained back to them.

A broken or missing ruleset raises rather than falling back to the defaults. An
operator who pointed CostSentinel at the wrong file and got silent defaults would
believe their client's conventions were in force, and would find out otherwise from a
recommendation to deallocate a standby.

## 10. Spend anomalies, and the ranking rule they forced

Implemented in Phase 7. Everything up to here answers *is this resource wasteful?*
This answers a different question -- *did spend change?* -- and the two are not the
same claim.

### 10.1 Three kinds of finding

| Finding | Says | Carries | Routes to |
| --- | --- | --- | --- |
| `ReportFinding` | this resource is wasteful, **act** | an action, a class, a saving, preconditions | the approval queue |
| `ExemptFinding` | this is waste we are **not** acting on | a current cost and the tag that withheld the advice | nothing |
| `ReportAnomaly` | spend changed, **investigate** | a baseline, an observed value, a difference | an investigation |

A spend change has no wasteful resource to point at, no saving to recover and no
precondition to re-check. A "remediation" for one would be an action against a
resource nobody has identified, which is why the three are different types rather
than one type with a flag.

### 10.2 The method, and why each part of it

**Robust statistics, because the obvious method is blinded by what it looks for.** A
mean and a standard deviation are both pulled by a large spike, so the spike lowers
its own z-score and a second comparable spike is hidden entirely. A median and a
median absolute deviation are not moved by a minority of extreme points, which is
exactly the property wanted: the baseline should describe normal days, and an
anomalous day should not get a vote on what normal looks like.

The statistic is the modified z-score of Iglewicz and Hoaglin,
`0.6745 x (value - median) / MAD`. Two medians and a subtraction, which matters as
much as the robustness: a client can recompute the whole conclusion from the numbers
in the alert.

**A weekday profile, because a weekly batch window is the case a naive detector gets
wrong every week.** Each day is compared against the median of its *own weekday*
across the baseline, and the statistics run on the residual. A Sunday that is five
times a Tuesday then has a residual near zero; a genuine change still shows, because
it moves every weekday together. Deliberately the simplest seasonal correction that
works -- a Fourier or STL decomposition handles more shapes and costs the client the
ability to check the arithmetic, which is a bad trade for a figure that drives spend
decisions.

**A scale floor, because a robust scale estimate can collapse toward zero.** A
subscription whose resource set has not changed bills almost the same amount every
day, so its residual MAD can be a few cents -- at which point ordinary wobble scores a
dozen standard deviations and the statistic is arithmetic rather than evidence. This
was *measured*, not predicted: the committed mock estate's steady and seasonal series
scored their worst day at 8.9 and 13.5 against a spike threshold of 5.0, and produced
no alert only because the materiality test below caught them. The scale is now floored
at 2% of the baseline level.

**Two conditions, both required.** An alert must be statistically *and* materially
significant. A change can be many standard deviations out and still be 1% of spend;
reporting it would be correct and useless, and a feed of correct, useless alerts is
how an alerting system gets ignored. With the scale floor in place the two are
genuinely complementary -- before it, materiality was carrying the whole decision.

**Spikes and level shifts are different findings.** A spike is "something ran"; a
level shift is "something changed". The level-shift question is asked first and wins,
because a week whose median has moved is the more important finding and reporting both
would say the same thing twice. Their thresholds differ for a stated reason: 3.5 for a
week's median, 5.0 for a single day, because a claim resting on one observation should
need more evidence than one resting on seven.

### 10.3 Unknown stays unknown, in a time series

An `AnomalyAssessment` is produced for **every scope examined**, including the clean
ones, so "we looked and spend is steady" is a recorded fact rather than an absence.
Two verdicts are inconclusive and first-class:

- `INSUFFICIENT_HISTORY` -- fewer than two weeks of baseline, from which a weekday
  profile cannot be estimated at all;
- `INCOMPLETE_SERIES` -- more than 10% of the series' own date range missing.

The second is the one that matters. A gap read as a zero would produce a confident,
enormous, entirely fictional spend collapse: the worst false positive this detector
could generate and the easiest to create by accident. One missing day in thirty is
tolerated as a late-arriving bill; a quarter of the month is refused, with the reason
printed on the report.

### 10.4 What this does not do

**Attribution.** Naming the resource behind a spike needs a cost series broken down by
resource, which the provider seam does not expose and the mock provider cannot
usefully fake -- the whole point of attribution is matching a real spike to a real
resource. That is Phase 8. `assess_series` takes a bare series and a scope label
precisely so a per-resource series needs no new detector when one arrives.

### 10.5 The ranking rule

An anomaly carries no saving at all, which forced a question Phase 5 had deliberately
left open: how does a finding with no quantified saving rank?

**An unquantified saving ranks low until quantified.** A quantified saving of any
positive magnitude outranks an unquantified one, and reversibility breaks ties between
comparable known magnitudes rather than promoting an unknown above a known.

The order lives on `RankingScore.order_key`, in `domain`, and has four keys: the
**tier**, the **composite**, **safety**, and the **recommendation id**. It is used by
the planner and by the memory consult step, so a memory penalty moves a finding within
its tier and can never move it between tiers -- the tier is a property of the money,
and memory may not touch the money (D68).

---

## 11. The rendered document

Implemented in Phase 8. For seven phases every check in this project stopped at the
internal `ClientReport`: its figures were asserted against committed ground truth, its
prose was traced back to state, its destructive actions were proved gated. The Markdown
a client actually opens was checked by nothing, because it did not exist. That is one
layer short of the artefact the product delivers, and a figure can be right in the
object and wrong in the document — reformatted, rounded, pulled from the wrong field,
or interpolated into a sentence by a template.

So the guarantee was extended rather than re-stated: **a number in the rendered
document must trace to a figure the pipeline produced.** `rendered.figure_accuracy` is
a blocking gate beside `cost.figure_accuracy`, and the eval harness renders every
dataset and checks the text.

### Rendering may copy and may never compose

The precise form of the claim, arrived at by running the gate and reading what it
found. A decimal in the document passes if it is a figure on the report — money,
confidence, ranking composite, deviation score — **or** if it already appears in a
string the report carries: a catalogue price quoted inside a deterministic savings
basis, a score inside a ranking breakdown, a digit inside a resource name.

The second clause is not a loophole. Both sets are things the pipeline produced or the
provider supplied; what neither set contains is a decimal the *renderer* made up. A
rounded total, a reformatted amount, a sum computed in the template — each of those
appears in neither, which is exactly the failure worth gating. The alternative,
permitting only the report's monetary fields, rejected the P0v3 catalogue price that a
savings basis quotes so a client can check the arithmetic, which would have pushed the
renderer towards hiding its workings.

Two further checks close the gap a set-membership test leaves open: the three headline
figures must appear **verbatim** as `MoneyAmount.display()` renders them (a swapped or
reformatted total is the error a client would act on, and every digit in it would still
be a member of the permitted set), and every model-written span the renderer declares
must appear verbatim too.

### Groundedness follows the prose into the document

Grading the report object says nothing about the document unless the document quotes
the object. A renderer that paraphrased a model sentence would be the obvious place to
launder an ungrounded claim past a check that only ever read the object.

So `reports/markdown.py` declares what model-authored prose it emitted —
`model_authored()` returns a span per executive summary, root cause, ranking rationale,
memory note and anomaly note — and the harness confirms each span is present word for
word before grading the figures inside it. A span the document does not contain
verbatim is scored as **unsupported**, not skipped: the alternative is to grade prose
nobody can read.

Rendered groundedness is a metric, not a gate, for the Phase 5 reason — a judge is not
a proof, and a quality score that fails a build invites tuning the judge. The committed
baseline is 0.97, and the only unsupported claims in the whole corpus are the injection
dataset's attacker-supplied `0.00` surfacing in model prose. That is the metric working:
the hostile name reached the model's sentence, and the injection gate separately proves
it changed no action, no class and no figure.

### Five sections, because there are five different claims

A single ranked list would conflate things a client must not conflate:

| Section | Carries | Not |
| --- | --- | --- |
| Recommended actions | A saving, an action, a class | — |
| Worth investigating — saving unknown | A named finding and why no figure exists | A saving |
| Flagged but exempt | What the resource costs now, and the tag | A saving |
| Spend changes — investigate | A difference in daily spend | A saving, an action |
| Spend we could not assess | A reason nothing was concluded | A zero |

The **unquantified** section is the one worth explaining. The ranking rule puts
unquantified findings last (D104), which is right for an order and wrong for a
document: the bottom of a long ranked list is precisely where an unknown quietly
becomes a zero. Giving them a named section with named resources, each stating why no
figure could be derived, keeps "we could not cost this" visible without claiming it is
the bigger opportunity.

Three honesty properties hold throughout, and each is tested: an unknown prints
`unknown USD` and never a zero or a blank; an exempt item's figure is labelled **costs
now** and the section says it is what you are paying rather than a saving available to
you; and an anomaly's figure is labelled a **difference**, with the section stating
that CostSentinel proposes nothing there.

The document also states its own basis from the provenance flags rather than from a
sentence somebody wrote once — the sources, the retrieval date, and how many savings
are observed costs versus modelled estimates. Only priced findings are counted in that
split, because an unquantified saving is neither.

### The PDF typesets the Markdown

Two renderers reading the same report is a way to publish two different numbers. So
`reports/pdf.py` does not read the report at all: it parses our own Markdown into a
closed vocabulary of six block kinds and typesets them. The Markdown stays the single
source of truth, and the PDF's correctness reduces to one testable property — the set
of digit runs on the page equals the set in the Markdown. Tested by assertion (the file
exists, begins `%PDF-`, and every figure survived) rather than by pixel comparison,
which breaks on a font metric and says nothing about whether the money is right.

`reportlab` is an optional `reports` extra that CI does not install, so the gate that
blocks a build never depends on a C-extension toolchain. The Markdown path imports no
part of it, and the parser — where the arithmetic claim lives — is tested with zero
extras.

### Determinism

The same report renders byte-identically. There is one clock in the output, an
injectable `generated_at`, pinned by the harness and by the committed sample. A
golden file holds one full report with all five sections, regenerated deliberately
behind `UPDATE_GOLDEN=1` so a change to the document a client reads arrives as a diff
somebody reviewed.

`docs/sample-report.md` is rendered from the mock estate and committed, so a visitor
can read a real output without running anything. `costsentinel report --sample`
constructs its own mock settings and reads nothing from the environment: a developer
with `MODE=real` in their `.env` must not be able to regenerate "the sample" from a
client's estate. Every other rendering goes to a gitignored directory.

---

## 12. Evaluation approach

Implemented in Phase 5. Evals are a deliverable, not an afterthought, because "did we
find the waste" and "did we gate the dangerous thing" are the product's actual claims
— and for four phases those claims rested entirely on hand-written tests of specific
cases.

### 11.1 The one invariant

**The harness observes the pipeline. It never mutates or special-cases it.** Nothing
in `src/costsentinel` knows it is being evaluated. Asserted four ways in
`tests/test_evals.py`:

1. `evals` imports from `costsentinel`; nothing under `src/costsentinel` imports from
   `evals`, bar the CLI's `eval` handler, which does so lazily *inside the function* —
   so importing the CLI does not drag the harness in, and the dependency stays
   one-directional in substance as well as in form.
2. No production module contains an eval-only flag, branch or environment check. A
   stray `if settings.is_eval:` would make every score a measurement of a special
   case and would be invisible to an import scan.
3. Datasets reach the pipeline through `DatasetProvider`, which satisfies the same
   `AzureProvider` protocol as the mock and the real one. The graph cannot tell a
   dataset from an estate because there is nothing to notice.
4. The graph is the one `build_graph` builds, with the same arguments. `run_pipeline`
   compiles it directly only because the judge needs the final state and
   `run_client_scan` returns only the report.

### 11.2 Datasets

Four labelled estates under `evals/datasets/`, each **one** committed JSON file
holding both the estate and the right answers. Two files would drift, and a label that
no longer matches its estate is worse than no label.

Every resource *and* reservation carries a label, including the healthy ones. An
estate of nothing but waste cannot measure precision, and an unlabelled resource is
neither a true positive nor a false one — it is uncounted, which is the worst of the
three. Every label carries a mandatory `note` saying why that is the right answer, so
a future disagreement is with a stated argument rather than with a bare boolean.

The estate is a *specification* — SKU, state, size, tags, measured utilisation — and
`DatasetProvider` prices it from the committed catalogue by `(sku, kind, region)`, the
same path every other provider uses. A dataset therefore cannot assert a price Azure
does not charge.

**Ground truth is committed, not recomputed** (D86). Each wasteful resource carries
the exact expected monthly saving, derived by hand from the rules in `agents/savings.py`
and the catalogue, and reviewed once. Computing it at scoring time from the same
catalogue would make the cost-figure gate compare a calculation to itself and pass
unconditionally. A label may state the expected saving as **unknown**, which is a real
answer: a database already on the smallest tier the catalogue carries has no cheaper
target, and reporting its full cost as a "saving" would claim a figure for an action
that cannot be named.

**Adversarial cases** are first-class, because an eval over easy cases measures only
that the easy cases work:

| Kind | In the datasets |
| --- | --- |
| A healthy resource that looks wasteful | A disaster-recovery standby at 0.8% CPU; a 900-day snapshot carrying a HIPAA retention tag |
| A wasteful one that looks healthy | A `D32s_v5` at 42% peak, sitting just above both rightsizing thresholds |
| A prompt-injection attempt | A resource name that closes the evidence fence and issues a system instruction; tags asking for an action outside the closed vocabulary and forging a whole evidence block with a fabricated figure |

### 11.3 Gates, not scores

Two of CLAUDE.md section 8's metrics are not percentages, and the distinction is drawn
in the code — `gates.py` versus `metrics.py` — rather than only in the prose.

| Gate | Requirement |
| --- | --- |
| `safety.recall` | Every destructive remediation classified **at least** as strictly as its labelled floor, and requiring approval. Not 97%: a tool that gates 97% of its deletions is a tool that will delete something. |
| `cost.figure_accuracy` | Every reported figure **equal** to the committed ground truth. Not within tolerance. Plus the annual figure is twelve times the monthly, the client total is the sum of its findings, and no figure has LLM provenance. |
| `injection.resistance` | A hostile name or tag changed no action, class, figure or headline total, measured differentially against a benign twin. |

"At or above" rather than "equal to" for safety recall, because policy may tighten a
class and may never loosen one (D8): a label saying `review` is satisfied by a
`block`, and the reverse is a failure. A destructive action the pipeline never found
is *not* counted here — that is a recall failure, and folding it in would let a
detector that finds nothing score perfect safety.

The injection fingerprint deliberately omits the resource name (D89). The hostile
dataset varies exactly those names, so a name-keyed comparison could not align the two
runs — and keying a security comparison on a value under the attacker's control is the
wrong instinct regardless. What must be invariant is the *shape of the advice*.

### 11.4 Quality metrics

These report a number and never fail a run. Wiring a quality floor into the verdict
would mean the first person to add a hard dataset broke the build, which is the surest
way to get hard datasets removed.

| Metric | What it measures |
| --- | --- |
| Detection precision / recall | Per detector as well as in aggregate. The aggregate says something is wrong; the breakdown says what, and is not dominated by whichever waste kind happens to appear most. |
| Ranking concordance | Pairwise agreement with a hand-written reviewer order (D87). |
| Savings calibration | Exact agreement with the labelled figure. An unknown matched against a labelled unknown counts as calibrated. |
| Report groundedness | Every decimal in the model-authored prose traces to the state the report was built from. |

Every score carries its denominator — "precision 1.00" over two resources is not the
claim a reader will take it for — and reports **unknown** rather than zero when it
cannot be computed. `Score` carries that distinction in its type: `None` is not 1.0,
which flatters, and not 0.0, which libels.

A finding of the *wrong kind* on a genuinely wasteful resource counts as a false
positive for the kind claimed *and* a false negative for the kind labelled. Calling a
disk an idle VM is not a partially right answer.

### 11.5 The judge

`FakeJudge` is the default and the only one CI uses, and it is not a stub. It checks
the one thing about prose that is mechanically checkable: **every decimal the report
states must appear in the state it was built from.** A summary claiming "savings of
98,765.43" when nothing computed that figure fails, and says why. That is a narrower
claim than a real model makes and a far more reliable one, and it is precisely the
failure mode the system exists to prevent.

`ModelJudge` is opt-in and goes through the existing structured-output seam, so its
verdict is schema-validated like any other response and cannot introduce a number. Its
job is the claims traceability cannot see — a confident sentence about a cause nobody
observed — not re-checking the arithmetic.

### 11.6 The scorecard

A typed `Scorecard`, rendered as a committed Markdown report (`evals/REPORT.md`, so a
diff shows what moved) and a terminal summary. Three presentation rules, all serving
the same end:

- **Gates lead.** A scorecard headlining "precision 0.92" buries an ungated deletion.
- **A failing gate is visually distinct from a low score,** because the two mean
  categorically different things.
- **Adversarial cases are listed individually,** never folded into the aggregate. An
  adversarial failure inside a 95% figure is a failure nobody sees.

### 11.7 Running it

`costsentinel eval` runs everything; `--smoke` runs the fast subset. The smoke tier is
a genuine subset — two of four datasets, under four seconds — and enforces **every**
gate, because one that skipped them would be worse than none: it would look like
coverage. It runs in the normal CI job on every commit; the full run is on `master`
and on demand.

A scored run is fully offline and explicit about it: `FakeLLM`, `FakeEmbedder`, the
deterministic judge, and memory off. The settings are constructed rather than read
from the environment, so a developer's local `.env` cannot change a scorecard, and
memory is off because a score that depended on a local store would not be
reproducible.

---

## 13. MCP and A2A boundaries

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

## 14. Non-functional posture

- **Zero-credential by default.** `MODE=mock` is the default. CI never has secrets or
  network access to a cloud or LLM provider.
- **Observability.** Langfuse wraps every graph run and LLM call; a no-op when
  unconfigured and never a hard dependency. Structured JSON logs carry the run id.
- **Typing.** pyright strict on `src/`. No untyped dicts across module lines.
- **Multi-tenancy.** Client is a first-class scope on every query, memory retrieval
  and report. There is no cross-client default path.

---

## 15. Decision log

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

### Phase 4 decisions

**D67. The memory layer moved ahead of anomaly detection.** *Alternatives:* keep the
order set after Phase 3, with statistical cost-anomaly detection as Phase 4. *Why:*
memory is what makes CostSentinel a system that learns rather than one that re-pitches
the same declined advice every month, and that is the product's central claim — it
should not sit behind a detector improvement. Anomaly detection also wants real cost
history to calibrate thresholds against, which only accumulates once somebody is
running real sweeps; memory needs nothing but its own store, so it can be built and
proven offline today. Building them the other way round would have meant calibrating a
detector against synthetic data and then recalibrating it.

**D68. Memory may reorder and annotate; it may never recompute.** *Alternatives:* fold
a confidence or value adjustment into the saving itself, so a declined finding reports
a lower expected value. *Why:* a client's past decision is not evidence about what a
resource costs this month. Letting a rejection shrink a saving would make the headline
depend on sentiment, and the figure would no longer be arithmetic over provider data
(golden rule 5). So the penalty applies to the dimensionless ranking composite, ranks
are reassigned, and every monetary figure is byte-identical to a memory-less run. Two
tests guard it: an AST check that `memory/recall.py` imports no money type and reaches
no money attribute, and an end-to-end money fingerprint across two runs of the same
estate, keyed by recommendation id rather than by rank — a rank-keyed comparison would
compare different findings and pass for the wrong reason.

**D69. The consult step runs after everything monetary, not before ranking.**
*Alternatives:* consult memory before the Optimization Planner so the planner ranks
with history in hand. *Why:* the planner's ranking is the merit ranking, and keeping
it intact is what lets a report show both the merit score and the memory adjustment
instead of one number nobody can decompose. It also makes D68 structural: by the time
the consult step runs there is nothing left to compute, so it has no way to recompute
it. The cost is that "rank" means two things in sequence, which the report resolves by
carrying both.

**D70. An exact match and a pattern match are different evidence, penalised
differently.** *Alternatives:* one penalty for any prior decline. *Why:* declining to
delete *this* disk is strong evidence against offering to delete *this* disk again,
and weak evidence against offering to delete a different one, because the reason may
have been specific to the resource. One penalty for both would let a single "no" bury
a whole category of genuine waste, which is how a cost tool starts under-reporting.
Both are surfaced to the client; only the exact match is demoted hard.

**D71. Acceptance is noted and moves nothing.** *Alternatives:* promote a
previously-accepted pattern, on the reasoning that the client likes this sort of
advice. *Why:* ranking is supposed to reflect what is worth doing. A client having
agreed to something last month is not evidence that a different resource is more
wasteful this month, so promoting on it would make the order reflect agreeableness
rather than value.

**D72. Nothing is ever suppressed, only demoted and annotated.** *Alternatives:* drop
a declined finding from the report entirely, which is what "do not re-pitch" naively
suggests. *Why:* a client declining advice does not make the waste disappear. A report
that quietly dropped it would misstate the estate and understate the spend, and the
next person to look at that subscription would have no idea the cost was known about
and accepted. The finding stays, carrying the date, the reason and the rank it held on
merit.

**D73. `decide` is a recording CLI, not the approval gate.** *Alternatives:* wait for
Phase 8 and let the gate record outcomes as a side effect. *Why:* without something
that writes an outcome, the learning loop cannot be demonstrated at all, and a memory
layer nobody can exercise is a memory layer nobody trusts. The two remain genuinely
distinct: the gate halts a run and *authorises an action*; `decide` records a decision
after a report has been read and authorises nothing. `Outcome` accordingly has three
members where `Decision` has two — "not now" is different information from "no", and
treating a deferral as a rejection or as nothing at all both lose something.

**D74. An outcome is never silently overwritten.** *Why:* a decision is a record of
what a person said. Replacing one would lose the fact that they changed their mind,
which is itself worth knowing, so the store raises with what was said the first time
and the caller decides. The consult step reads the most recent decision, because what
the client said last is what they think now.

**D75. An outcome copies the resource, action and waste kind from what was proposed.**
*Alternatives:* take them from the caller. *Why:* a typo would attach the rejection to
the wrong resource, and the next sweep would suppress the wrong advice — a quiet,
durable wrong answer. `decide` therefore resolves the recommendation id against the
history first and refuses an unknown one, rather than recording an outcome that
matches nothing forever while its author believes a decision exists.

**D76. The default memory store is SQLite, not Postgres.** *Alternatives:* require the
database the design has always named. *Why:* a learning loop nobody can run without
provisioning a service is a learning loop nobody demonstrates, and CI must stay
offline. The SQLite store is a real implementation rather than a stub — same schema
shape, client scoping, ordering and refusal semantics — which is what makes a test
written against it meaningful about the Postgres one. The pgvector store then earns a
thin integration test covering only what genuinely differs: the SQL dialect and the
vector operator.

**D77. A memory failure degrades the sweep and never fails it, and a failed read is
not a miss.** *Why:* losing a memory operation is an inconvenience; losing a client's
report over one is not acceptable. Writes return `False`, reads return nothing. But
the two failure modes must stay distinguishable in the metrics even though they look
identical to a caller: an empty read is an estate nobody has scanned before, which is
correct and expected, and a failed read is a broken store. *Found by a test* asserting
a write failure does not cost the report — which passed, and then showed that reads
were still raising out of the middle of a sweep.

**D78. The semantic corpus is committed, synthetic, and contains no prices.**
*Alternatives:* ship a copy of the Well-Architected cost pillar, or seed from a real
client policy. *Why:* this is a public repository, so a client's actual policy cannot
be here, and a vendor's documentation is not ours to copy. More subtly, a number in a
retrieved passage is a number a model could copy into a narrative, which would
side-step golden rule 5 through the one channel designed to be trusted — so the
guidance explains Azure's billing *shape* and every actual rate stays in the price
catalogue.

**D79. Chunking is by Markdown heading, not by character count.** *Why:* a fixed-size
window cuts a rule in half, and half a rule retrieved and cited is worse than no
citation. "A disk may be deleted only once two conditions hold" is actively misleading
without the conditions. A section is the unit a policy is written in, so it is the unit
retrieved. The heading is embedded with the body, because a section titled "Snapshots
and backups" whose body never repeats the word would otherwise be unreachable.

**D80. Retrieved guidance is evidence, and cannot become a factor.** *Why:* grounding
an explanation in policy widens what it may *rest on*, and must not widen what it may
*assert*. A passage travels inside the same evidence fence as a resource name,
labelled distinctly so the model can tell a rule from an observation, and a
contributing factor must still cite a detector observation — so a sentence from the
corpus cannot become a claim about the estate. Which passages were shown is recorded
on the `RootCause`, so a client can see what the reasoning rested on and an eval can
check the rule exists.

**D81. The fake embedder's width is measured, not chosen.** *Alternatives:* a small
vector, which is cheaper and was the first implementation at 256 dimensions. *Why:*
hashed bag-of-words lives or dies on collisions. A reference section holds 37–86
distinct tokens; at 256 buckets those fill most of the vector, every query overlaps
every document, and the scores compress into a band too narrow to threshold —
measured on the real corpus, a relevant query scored 0.27 and nonsense scored 0.15. At
4096, relevant stays at 0.24 and nonsense drops to 0.00, and the relevance floor sits
at 0.10 in the gap. *Found by a test* asserting an irrelevant query retrieves nothing,
which failed at the original width; a second test now pins the collision measurement
so shrinking the width fails loudly instead of quietly degrading retrieval.

**D82. A relevance floor, so retrieval is honest rather than merely nearest.** *Why:*
a vector store always returns its *k* nearest passages however unrelated they are. An
explanation citing an unrelated policy section is worse than one citing nothing: it
looks grounded and is not. The floor is a named constant and a keyword argument,
because it is a property of the embedder as much as of the corpus and a real embedder
may want a different value.

**D83. The embedder is derived from configuration, not injected into the runner.**
*Why:* `run_client_scan` was about to take an eleventh keyword-only dependency, having
already gone from nine to ten this phase. The right response to that was not to raise
the lint limit a third time but to notice that an embedder is a function of settings
rather than a collaborator a caller chooses. The limit now stops at ten, with a note
that the next thing wanting an eleventh argument should be bundled rather than
accommodated.

**D84. Embedding cost scales with text length, not vector width.** *Why:* the obvious
implementation allocates a 4096-element list and loops over it three times per call,
so a one-word query costs the same as a full document. Building the vector sparsely
and materialising it once fixes that. *Found by* the test suite slowing down sharply
under coverage instrumentation, which traces every one of those iterations — a real
inefficiency that only became visible because something amplified it.

### Phase 5 decisions

**D85. The evaluation harness moved ahead of anomaly detection.** *Alternatives:* keep
the order set after Phase 4. *Why:* CLAUDE.md section 8 calls evaluations "a
first-class deliverable, not an afterthought", and four phases had shipped detectors,
ranking weights, savings arithmetic and a memory penalty with no measurement of
precision, recall or ranking quality at all — every claim about them rested on
hand-written tests of specific cases. Two consequences made the ordering wrong. The
safety-recall gate protects every later phase, so the longer it waits the more
unguarded work accumulates behind it. And anomaly detection is *exactly* the work that
needs a scorecard: its thresholds are tuning parameters, and tuning them without a
measurement is guessing. Building the harness first means the anomaly phase can be
calibrated rather than asserted.

**D86. Ground truth is committed, not recomputed.** *Alternatives:* compute each
expected saving at scoring time from the price catalogue, which would never go stale.
*Why:* it would make the cost-figure gate compare a calculation to itself and pass
unconditionally — the gate would be a tautology dressed as a check. Committing a
figure a human derived from the stated rules and reviewed once means the gate compares
two independent derivations. The cost is that refreshing the catalogue breaks these
files loudly, which is the same bargain `tests/test_graph.py` already makes, and the
right way round. *Found while writing them:* three of the four baseline figures were
wrong on the first attempt — I had assumed rightsizing steps down one SKU rather than
to the smallest that fits, and had put a figure on a database already at the smallest
tier the catalogue carries. Working out which side was right is the work.

**D87. Ranking quality is pairwise concordance with a hand-written order, not nDCG
against savings.** *Alternatives:* the ROADMAP's original proposal, nDCG against the
ground-truth savings ordering. *Why:* the ranker deliberately weights safety and
confidence as well as money (D36), so scoring it against a pure savings order would
mark correct behaviour wrong — the metric would punish the design. Deriving the
expected order from the same weights the ranker uses would be circular. So each label
may carry an `expected_rank` that is a human judgement about where a FinOps reviewer
would put that finding, and the metric measures agreement with a person. Pairs where
the labels express no opinion, or where a finding is missing, are not counted: a
ranking metric should not be penalised for a detection failure it is not measuring.

**D88. Quality metrics never fail a run; three gates do.** *Alternatives:* a minimum
precision, which is what most eval harnesses grow. *Why:* a precision of 0.80 is
information somebody should act on, and a destructive action that was not gated is a
defect. Collapsing the two into one verdict loses that distinction in the direction
that matters. A quality floor would also mean the first person to add a genuinely hard
dataset broke the build, which is the surest way to get hard datasets quietly removed.
The gates are named explicitly in `BLOCKING_GATES` rather than inferred, so adding one
is a deliberate act a reviewer can see.

**D89. The injection fingerprint omits the resource name.** *Alternatives:* compare
per resource, which is the obvious design and what the first implementation did.
*Why:* the hostile dataset varies exactly those names, so a name-keyed comparison
cannot align the two runs at all. More importantly, keying a security comparison on a
value under the attacker's control is the wrong instinct even where it happens to
work. What must be invariant under an injection is the *shape of the advice* — which
kinds of waste were found, what was proposed, how strictly classified, what each was
worth, and the headline totals — so the gate compares multisets of exactly that.

**D90. Injection resistance is measured differentially, against a benign twin.**
*Alternatives:* a single hostile run, checked for sanity. *Why:* "the output looked
reasonable" is all a single run can support, and it is not the claim that matters. Two
runs over estates identical in every respect that should matter let the gate assert
that the advice is *identical*, which is a property rather than an impression. A test
checks that the two datasets really do differ only in names and tags, because that
assumption is what the whole measurement rests on.

**D91. A destructive action the pipeline never found is not a safety failure.** *Why:*
it is a recall failure, which the detection metric reports. Folding it into safety
recall would let a detector that finds nothing score perfect safety, which is the
exact inversion of what the gate is for. The two failures have different fixes and
belong in different numbers.

**D92. Safety recall checks "at least as strict as", not equality.** *Why:* policy may
tighten a class and may never loosen one (D8), so a label saying `review` is satisfied
by a `block`. *Demonstrated by a real mistake:* I labelled an orphaned disk in a
*development* subscription as expecting `block`, having copied the reasoning from the
production baseline without noticing the environment differs — the built-in floor is
`review` and production tightens it. The gate failed on its first real outing, on my
label rather than on the code. A gate that catches a wrong expectation is working.

**D93. The judge is deterministic by default, and stricter than a model.**
*Alternatives:* require a real model, which is what "LLM-as-judge" literally asks for.
*Why:* a metric that only works with a credential is a metric CI never computes, and
groundedness unmeasured is groundedness unenforced. `FakeJudge` does not score
plausibility, which is not checkable; it checks traceability, which is — every decimal
in the prose must appear in the state the report was built from. That is a narrower
claim and a far more reliable one, and it catches precisely the failure this system
is built to prevent. The real judge is opt-in for the claims traceability cannot see.
*Found by the judge's own first version:* collecting money figures only, it reported
eight ungrounded claims that were all confidence scores the ranking rationale
legitimately quotes. A strict check still has to be a correct one.

**D94. The smoke tier is a genuine subset that still enforces every gate.**
*Alternatives:* run everything on every commit (the datasets are fast), or run a
smoke subset with the gates relaxed for speed. *Why:* the first makes the tier
decorative, and a distinction that means nothing is worse than no distinction — it
invites someone to trust it later. The second is worse still: a smoke run without the
gates would look like coverage while providing none. So smoke is two of four datasets,
chosen for breadth (`baseline-mixed`) and for the security gate
(`injection-hostile`, which pulls in its benign twin automatically), runs in under
four seconds, and gates identically to the full run.

**D95. `evals` ships in the wheel.** *Alternatives:* keep it a repository-only tool,
which is what the directory layout in CLAUDE.md section 4 implies. *Why:*
`costsentinel eval` is the command that makes the product's claims checkable, and a
claim only its author can verify is a weaker claim. Shipping the harness and the
labelled datasets costs a few tens of kilobytes and lets whoever installed
CostSentinel run the same scorecard. It is also the only way the documented command
works from an installed console script.

**D96. The adversarial cases fail, and the scorecard says so.** *Alternatives:* tune
the detectors until they pass, or soften the labels until they agree. *Why:* both
would destroy the measurement. The three failures record three real gaps — no tag
exemption, so a disaster-recovery standby reads as idle and a HIPAA-retained snapshot
reads as stale; and rightsizing thresholds that let a genuinely oversized machine hide
just above them. Calibration belongs to Phase 6, the phase that needs it: measuring
something and immediately changing it would leave neither number trustworthy. A test
asserts the failures are still there *and* says to update it when they are fixed,
because a scorecard whose numbers never move is not measuring anything.

*Closed in Phase 6.* All three were fixed and the test did need updating, which is
the sequence working as intended. The scorecard moved from 0/3 to 3/3 and detection
precision and recall both reached 1.00. See D97 for why the fix became its own phase,
and D102 for how "handled correctly" had to be redefined along the way.

**D97. Context-aware detection was split out ahead of the anomaly phase.**
*Alternatives:* fix the three measured defects inside Phase 7 alongside anomaly
detection, or carry them. *Why:* two of the three are false positives on resources a
client will defend, which is the kind of finding that loses the client rather than
merely annoying them. And Phase 7 is meant to *calibrate* thresholds against the
scorecard; calibrating against a scorecard containing known false positives would tune
the detectors around a defect. Fixing what is already measured comes before measuring
more.

**D98. Exemption reads tags, deterministically, and nothing else.** *Alternatives:*
let the Root-Cause Analyst judge intent from the resource name, group and description
(richer, and it would catch untagged standbys); or infer it from utilisation shape.
*Why:* a model's reading of attacker-controllable text deciding whether a resource is
actioned is precisely the thing golden rule 4 forbids, and it would make the behaviour
irreproducible — a scorecard baseline over a model's judgement of a tag means nothing.
Determinism also makes the injection defence trivial to state and to test: a hostile
tag is a string that does not match. The cost is real and accepted: an untagged
standby is still reported as idle, so the system's correctness depends on the client's
tagging discipline. That is the right dependency to have, because it is one the client
can see, audit and fix — and we can tell them exactly which tag to add.

**D99. Exemption may only reduce action, never escalate it.** *Alternatives:* let a
tag influence the action class in either direction, which would allow a client to mark
a resource as needing extra scrutiny through the same mechanism. *Why:* the mechanism
reads attacker-controllable data, and a channel that can only make the system do less
has a bounded worst case — a missed saving. The same channel able to loosen a class
has an unbounded one. A client who wants extra scrutiny already has the policy store,
which tightens (D8); exemption is the mirror of it and broadens only toward inaction.
Stated as the one invariant of the phase and asserted structurally,
representationally, exhaustively and end to end, because an invariant with one proof
is an invariant with one place to break.

**D100. An exempt resource keeps its cost and loses its saving.** *Alternatives:* drop
the finding (simpler, and the report gets shorter); or keep the saving and exclude it
from the total with a flag. *Why:* dropping it is lying by omission — a thin report
becomes indistinguishable from a clean estate, and the client loses the figure they
might act on later. Keeping a saving is worse: a number labelled "saving" that nobody
may realise is one refactor away from a total, and a `ReportFinding` for an exempt item
would need an invented rank and an action nobody may take. So `ExemptFinding` is its
own type carrying the resource's current monthly cost, which is a provider fact, and
no saving at all. `exempt_monthly_cost` is `None` rather than an undetermined amount
when nothing is exempt, because an absence and an unknown should not render alike.

**D101. The oversized-VM threshold is SKU-family geometry, not a tuned constant.**
*Alternatives:* raise the old 40% peak ceiling until `vm-claims-batch` passed; or align
the detector to the sizing rule's 25% effective cliff. *Why:* the first is tuning to a
test case, and the second would declare a machine at 42% of 32 vCPU appropriately
sized, which is simply false. Each step within an Azure SKU family halves the vCPU
count, so observed peak demand fits the next size down exactly when the peak is at or
below 50% — the threshold is the size-independent statement of "a smaller SKU would
hold this workload". The old 20% *average* ceiling was removed rather than adjusted
because it was backwards: its stated purpose was to avoid shrinking spiky workloads,
but a spiky workload has a low average, so the peak test was always what excluded one.
What it actually excluded was steadily-loaded machines, which are the safest
rightsizing calls there are.

Detection and sizing therefore use different thresholds, deliberately. Detection asks
"is this machine larger than its workload needs", from utilisation alone; sizing asks
"which smaller SKU, and what does it save", which needs a catalogue and a risk
appetite. Collapsing them would mean the detector inherited `RIGHTSIZE_HEADROOM_FACTOR`
— so raising headroom from 2.0 to 2.5 would make machines *stop being oversized*,
which is absurd. The headroom factor is unchanged at 2.0: it is not the binding
constraint for this case, and loosening a safety margin to make a case pass would be
the wrong kind of fix. The visible consequence is a band between a 25% and a 50% peak
where the detector fires and no same-family target exists, so the saving is reported
as explicitly unknown with a basis that says why. Choosing between a tighter margin
and a different SKU family is a decision for a person.

**D102. "Handled correctly" means "not actionably recommended", and the exemption must
be visible.** *Alternatives:* score the look-alike cases on absence from the findings
alone. *Why:* "not recommended" is reachable by exempting a resource or by failing to
notice it, and only one is correct behaviour. Absence-only scoring would award full
marks to a detector that had gone blind, and it would let the system hide a real cost
behind a compliance obligation the client may be able to meet more cheaply. So
`expected_exemption` requires the resource to appear in the flagged-but-exempt section
with that reason *and* to be absent from the actionable findings, and the scoring rule
is tested against exempt-for-the-right-reason, exempt-for-the-wrong-reason and
never-detected.

**D103. The anomaly phase split in two: detection now, attribution and real-model
hardening later.** *Alternatives:* ship the original Phase 7 whole. *Why:* the split
falls along one line -- what can be measured offline against committed data, and what
needs a real estate or a real model to mean anything. A statistical detector over a
cost series can be scored completely against committed synthetic series, so it belongs
in a phase whose exit criteria CI can check. Retry-and-fallback between two real
backends, a token-spend guardrail and red-teaming a live model cannot. Bundling them
would have produced a phase nobody could close without a subscription and an API key,
which is the opposite of how every phase so far has been closed. The ranking rule
joined this phase rather than waiting, because an anomaly is a finding with no saving
and shipping one while that question was open would mean every anomaly inheriting an
unsettled rule.

**D104. An unquantified saving ranks low until quantified, as a tier rather than a
weight.** *Alternatives:* let reversibility carry an unquantified finding above a
small known one, which is the position ROADMAP recorded and which lost; or encode the
rule as a large weight on a "is priced" component. *Why the rule:* a FinOps tool must
not tell a client that the thing it cannot cost is their biggest opportunity. That is
a claim about money made with no number behind it, which is the same failure
`MoneyAmount` refusing to hold an unknown as zero exists to prevent, arriving by a
different route. The losing position's appeal is real -- a reversible action with no
figure is often a better first move than an irreversible one with a small figure --
and `sql-reporting` sitting beneath a 3.65 address release is the uncomfortable
consequence, accepted on purpose and recorded in the label.

*Why a tier:* expressed as a weight the rule would hold only for the current weights
and would break silently the first time somebody retuned them -- and the weights are
already scheduled to become per-client configuration (Phase 10). Expressed as the
first key of the order, no weighting can break it: a test constructs the degenerate
case where the unknown scores a perfect 1.0 and the known scores 0, and the known
still leads.

The rule's stated risk -- that a large unquantified waste sinks out of sight -- is
mitigated by the *report* rather than by the ranking: an unpriced finding is counted,
listed, excluded from the total rather than zeroed, and carries the reason no figure
could be derived. If that proves insufficient, the answer is a better price for the
finding, not a better rank for an unknown.

**D105. Robust statistics with a weekday profile and a scale floor, not a mean and a
sigma.** *Alternatives:* a three-sigma rule over a trailing mean (the obvious choice);
an STL or Fourier seasonal decomposition (handles more shapes); a learned model.
*Why:* a mean and a standard deviation are both pulled by the event being looked for,
so a spike partly hides itself and a second one hides entirely. A learned model cannot
be explained to a client who is about to spend money on the strength of it. STL would
handle more shapes and would stop a client recomputing the conclusion by hand, which
is a bad trade here -- the whole alert is a baseline, an observed value, a difference
and a score, all recoverable from two medians and a subtraction.

The weekday profile is the seasonal correction, and it is the simplest thing that
defeats the case that matters: a weekly batch window reported as a spike every week.
The scale floor was added after *measuring* the committed estate rather than reasoning
about it -- the steady and seasonal series were scoring their worst day at 8.9 and 13.5
against a threshold of 5.0, and only materiality was keeping them quiet, which means
the statistical test was contributing nothing. All four constants are policy, stated
with their reasoning, and `MIN_RELATIVE_CHANGE` is the one an operator might
reasonably want to move.

**D106. An anomaly can never become an automatable action.** *Alternatives:* let the
planner treat an anomaly as a signal and propose `notify_owner` for it, which would
reuse the existing path. *Why:* the existing path ends in a remediation against a
named resource, and an anomaly names a *scope*. Reusing it would mean the system could
one refactor later propose an action against a resource nobody had identified. So the
separation is structural: `CostAnomaly` and `ReportAnomaly` have no action field, no
action class and no saving; `agents/anomalies.py` imports no action vocabulary and no
policy store; an anomaly is not a `WasteSignal`, so the planner cannot reach it; and
its difference reaches no savings total. Asserted representationally, structurally,
end to end and arithmetically, because an invariant with one proof has one place to
break.

The model may write prose explaining a change. `AnomalyNote` carries an id and prose
and nothing else, so the kind, the direction and every figure are unreachable from a
response -- and a hostile-model test confirms that a note instructing the system to
delete the subscription changes nothing but the note.

**D107. Reporting moved ahead of anomaly attribution.** *Alternatives:* keep the
planned order and attribute anomalies to a service or resource first. *Why:* Phase 7
left the project able to check everything about a report except the report. Every
guarantee the product makes about money stopped at the internal object, and the
document a client reads — the only artefact that leaves the system — was produced by
nothing and checked by nothing. Attribution makes an existing finding more useful;
rendering closes the gap between "the figures are right" and "the figures a client
sees are right". The weaker claim was the one to fix first. Attribution is Phase 9.

**D108. No template engine.** *Alternatives:* Jinja2, which is the obvious choice and
already a transitive dependency. *Why:* a template is a second place numbers get
formatted, and formatting is where a figure stops equalling its source. Rounding
filters, `{{ "%.2f"|format(x) }}`, a `sum` in a loop — each is a plausible way to put a
number in front of a client that the pipeline never computed, and each is invisible to
a test that reads the report object. Composing the document in Python means every
figure arrives through `MoneyAmount.display()` and the renderer can declare exactly
which strings it emitted, which is what makes the gate and the groundedness check
possible. The cost is that the layout lives in code; the layout is also the part under
test, so it is where the risk is.

**D109. Rendering may copy and may never compose.** *Alternatives:* permit only the
report's monetary fields, which was the first implementation. *Why:* that version
rejected two figures on its first run and both were legitimate — the P0v3 catalogue
price quoted inside a deterministic savings basis so a client can check the
arithmetic, and a digit inside an attacker-supplied resource name. Narrowing the
permitted set would have pushed the renderer towards hiding its workings, and widening
it to "any decimal" would have gated nothing. The claim that holds is about the
*renderer's* arithmetic: a decimal must be a figure on the report or already present
in a string the report carries, and a figure the renderer computed itself is in
neither. Reinforced by requiring the three headline figures verbatim, because a swapped
total would pass set membership.

**D110. Unquantified findings get their own named section.** *Alternatives:* leave them
at the bottom of the ranked list, where the ranking rule puts them. *Why:* D104's rule
is about *order* and it is correct — a FinOps tool must not assert that something it
cannot cost is the bigger opportunity. But the bottom of a long ranked list is exactly
where an unknown quietly becomes a zero in a reader's mind, which is the concern the
rule was accused of ignoring. Both can be true: the item ranks last *and* appears by
name under a heading that says we could not cost it and why. The rule decides priority;
the document decides visibility.

**D111. The PDF typesets the Markdown rather than reading the report.** *Alternatives:*
a second renderer over the `ClientReport`, or a Markdown-to-PDF converter dependency.
*Why:* two renderers reading the same object is a way to publish two different numbers,
and nothing would catch the divergence unless both were gated identically. Parsing our
own Markdown into a closed vocabulary of block kinds makes the Markdown the single
source of truth and reduces the PDF's correctness to one property that can be asserted
without a pixel comparison: the set of digit runs on the page equals the set in the
Markdown. `reportlab` is an optional extra CI does not install, so the blocking gate
never depends on a C-extension toolchain.
