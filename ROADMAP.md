# CostSentinel — Roadmap

**Current phase: 10** — the durable approval gate, and the first A2A surface.

Phases are sequential. Do not start work in a later phase before the current phase
is complete and its exit criteria are met. If an idea arrives mid-phase, record it
under "Parked ideas" at the bottom and stay on the current slice.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

> **Phase order has changed five times.** After Phase 1 the specialist graph and the
> MCP Azure tool server were swapped, because the graph is entirely mock-runnable and
> the MCP server is the first thing needing real credentials (ARCHITECTURE.md D34).
> After Phase 2, statistical cost-anomaly detection and real-model hardening were
> split out of the MCP phase into their own phase, because Phase 3 is a *capability
> unlock with a safety surface* and bundling ordinary feature work with it would have
> meant shipping both as one reviewable unit (D49).
>
> After Phase 3, **the memory layer and the anomaly phase swapped**: memory became
> Phase 4 (ARCHITECTURE.md D67). Two reasons. Memory is what makes CostSentinel a
> system that *learns* rather than one that re-pitches the same rejected advice every
> month, and that is the product's central claim -- it should not sit behind a
> detector improvement. And anomaly detection wants real cost history to calibrate
> thresholds against, which only accumulates once somebody is running real sweeps;
> memory needs nothing but its own store, so it can be built and proven offline today.
>
> After Phase 4, **the evaluation harness and the anomaly phase swapped**: evals
> became Phase 5 (ARCHITECTURE.md D85). CLAUDE.md section 8 calls evaluations "a
> first-class deliverable, not an afterthought", and four phases had shipped
> detectors, ranking weights, savings arithmetic and a memory penalty with no
> measurement of precision, recall or ranking quality at all. The safety-recall gate
> also protects every later phase, so the longer it waited the more unguarded work
> accumulated behind it -- and anomaly detection is *exactly* the kind of work that
> needs a scorecard, since its thresholds are tuning parameters and tuning them
> without a measurement is guessing.
>
> After Phase 5, **context-aware detection was split out ahead of the anomaly work**:
> it is now Phase 6 and anomalies Phase 7 (ARCHITECTURE.md D97). The harness did its
> job immediately and found three real defects -- the first *measured* defects in the
> system rather than guesses at one. Two of them are false positives on resources a
> client will defend: a disaster-recovery standby reported as idle, and a snapshot
> carrying a regulatory retention tag reported as stale. Those are the findings that
> lose a client's trust in a cost tool, because the client knows the answer and the
> tool does not, and no amount of anomaly detection compensates for being wrong about
> something obvious.
>
> It also makes the sequencing honest. Phase 7 is supposed to calibrate thresholds
> against the scorecard; calibrating against a scorecard that contains known false
> positives would tune the detectors around a defect. Fixing what is already measured
> comes before measuring more. Phases 8-12 were renumbered.
>
> After Phase 6, **the anomaly phase was split in two**: spend-anomaly detection and
> the ranking rule are now Phase 7, and anomaly attribution plus real-model hardening
> become Phase 8 (ARCHITECTURE.md D103). Phases 8-13 renumbered again.
>
> The split is along one line: **what can be measured offline against committed data,
> and what needs a real estate or a real model to mean anything.** A statistical
> detector over a cost series can be scored completely against committed synthetic
> series -- step change, reverting spike, seasonal pattern -- so it belongs in a phase
> whose exit criteria CI can check. Retry-and-fallback between two real backends, a
> token-spend guardrail and red-teaming against a live model cannot. Bundling them
> would have produced a phase that could not be verified without a subscription and an
> API key, which is the opposite of how every phase so far has been closed.
>
> The ranking rule joins this phase rather than waiting, because an anomaly is a
> finding with **no saving at all**. Shipping anomalies while "how does an unquantified
> finding rank" was still an open question would have meant every anomaly inheriting an
> unsettled rule, and the open decision becoming load-bearing before it was decided.
>
> After Phase 7, **client reporting swapped ahead of anomaly attribution**: reporting
> is now Phase 8 and attribution plus real-model hardening Phase 9 (ARCHITECTURE.md
> D107). Nothing else renumbered.
>
> Two reasons, and the second is the stronger one. Phase 9 is the work that needs a
> real estate to mean anything -- and before anybody points this at a client's
> subscriptions there has to be something a client can be *shown*. Today the
> deliverable exists as a Python object and a terminal digest, and "a client would
> actually accept this" is the one claim the project has never tested.
>
> And the guarantee has a hole in exactly the place it matters. Every figure in the
> internal `ClientReport` is checked -- the cost-figure gate asserts each one against
> committed ground truth, and the groundedness judge traces each model-written sentence
> back to state. The *rendered document* is checked by nothing, and it is the only
> artefact a client ever reads. Phases 6 and 7 each added a section the renderer has
> never had to represent (flagged-but-exempt, spend anomalies) plus a ranking tier that
> changes what order a reader sees; the longer that waits the more unguarded surface
> accumulates behind a guarantee that stops one layer short of the output.
>
> In Phase 9, **the part that genuinely needs real traffic was split off as Phase 9b**
> rather than left as an open tail. The split that created Phase 9 (D103) separated
> work that could be *scored* offline from work that could not, and treated both halves
> of real-model hardening as belonging to the second. That was half right. Fallback
> logic and a token budget can be built and tested with fakes -- a primary that raises,
> a secondary that serves, deterministic token counts crossing a ceiling -- so they
> belong in a phase that can be closed. Calibration against real traffic, red-teaming a
> real model and a baseline that needs real seasonal history cannot be faked at all, and
> keeping them attached would mean a phase with no reachable exit criteria.
>
> Attribution moved for a related reason. Phase 7 recorded that it "needs a real cost
> series broken down by resource", which read the dependency the wrong way round: the
> breakdown is a shape on the provider seam, and a seam that only exists once a real
> tenant is connected is a seam nobody can test. The synthetic series gains a seeded
> breakdown and attribution is scored against it like every other detector; real data
> calibrates the threshold, it does not define the arithmetic (ARCHITECTURE.md D112).
>
> In Phase 10 the **approval adapters became Phase 10b** and **one A2A card moved
> forward from Phase 11**. Same split as before: the durable interrupt, the decision
> record and the CLI gate can be finished and tested offline, while Teams, Slack, a
> signature-verified webhook and approver authorisation need recorded payloads from real
> workspaces and two new dependencies. The card moved the other way because the approval
> queue is the first thing another agent would have a reason to ask about, and an A2A
> surface built after half a dozen specialists are already exposed is one where
> "is this read-only?" has to be re-established for each of them (ARCHITECTURE.md D117).

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
interrupt (Phase 10), real LLM backends (Phase 3), and statistical cost-anomaly
detection (Phase 7, where a clean scorecard makes it calibratable).

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

**Deliberately *not* in this phase:** any write, delete or mutate path (Phase 13);
statistical cost-anomaly detection (Phase 7) and real-model hardening (Phase 9), both
split out of this one.

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
adapters (Phase 10). `decide` is a recording CLI, not a gate -- it writes an outcome
after the fact rather than halting a run. Separating them is what lets the learning
loop be demonstrated now instead of waiting for the adapters.

**New dependencies, all optional extras so CI stays offline:** `psycopg`, `pgvector`,
and whatever the real embedder needs. The default store uses the standard library's
`sqlite3`, so the demo path adds no dependency at all.

---

## Phase 5 — Evaluation harness and scorecard

**Goal:** the product's claims are measured rather than asserted, and a safety
regression fails the build. CLAUDE.md section 8.

Four phases have shipped detectors, ranking weights, savings arithmetic and a memory
penalty, and every claim about their quality currently rests on hand-written tests of
specific cases. This phase replaces "the tests pass" with a number, and turns the one
claim that must never regress -- that every destructive action is gated -- into a gate
rather than a score.

Milestone: *Phase 5 - Evaluation harness*.

- [x] Labelled synthetic estates under `evals/datasets/`, each paired with
      ground-truth labels: which resources are genuinely wasteful and of what kind,
      which are healthy, and for destructive remediations the correct action class
      (#36)
- [x] Deliberately adversarial cases: a healthy resource that looks wasteful, a
      wasteful one that looks healthy, and a prompt-injection attempt in a resource
      name and a tag (#37)
- [x] A harness under `evals/harness/` that runs the **real** graph against each
      estate -- no special-casing, no production code aware it is being evaluated
      (#38)
- [x] Quality metrics: waste-detection precision and recall with a per-detector
      breakdown, recommendation ranking quality, and savings-estimate calibration
      (#39)
- [x] Report groundedness via LLM-as-judge, behind a deterministic `FakeJudge` so it
      is scored offline in CI, with the real judge opt-in (#40)
- [x] **Hard gates, not scores:** safety recall below 100%, or any reported money
      figure that does not equal the committed ground-truth arithmetic, fails the run
      (#41)
- [x] Prompt-injection resistance scored as a gate: a hostile name or tag must not
      alter a single action or classification (#42)
- [x] A typed `Scorecard` rendered as a committed Markdown report and a terminal
      summary, with the adversarial cases called out (#43)
- [x] `costsentinel eval`, plus a fast smoke subset in the normal CI job and the full
      run on demand, both documented (#44)
- [x] An asserted boundary: `evals` imports from `costsentinel`, and nothing under
      `src/costsentinel` imports from `evals` (#45)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.5.0 (#46)

**Exit criteria (met):** `uv run costsentinel eval` emits a scorecard and writes
`evals/REPORT.md`; the smoke subset runs in CI on every commit, offline, in under four
seconds, enforcing every gate; a deliberately broken classifier fails the run with
exit 1, demonstrated red and then green; 850 tests pass at 94% coverage with `domain`,
`agents`, `guardrails` and `graph` at 97-100%; and four separate tests assert the
pipeline contains no knowledge of the harness.

**Current scores**, from the committed `evals/REPORT.md`: all three gates pass;
detection precision 0.86, recall 0.92, ranking concordance 0.97, savings calibration
1.00, report groundedness 0.96.

**Three adversarial cases fail, deliberately and on the record.** The detectors have
no tag exemption, so a disaster-recovery standby reads as idle and a snapshot carrying
a HIPAA retention tag reads as stale; and the rightsizing thresholds sit where a
genuinely oversized machine hides just above them. Those are the first real findings
the harness produced, and fixing them is Phase 6's job -- see "Deliberately not in
this phase".

**Deliberately *not* in this phase:** tuning any threshold or weight in response to a
score. Measuring and then immediately changing what is measured would leave neither
number trustworthy. Closing them is Phase 6; calibrating thresholds against a then-clean
scorecard is Phase 7.

---

## Phase 6 — Context-aware detection

**Goal:** close the three defects the Phase 5 scorecard measured. A resource that is
idle *by design* must stop being reported as waste to act on, and a genuinely
oversized machine must stop hiding just above a threshold.

These are the first measured defects in the system rather than guesses at one, and two
of the three are the kind that lose a client's trust: the client knows a
disaster-recovery standby is idle on purpose, and a tool that recommends deallocating
it is a tool they stop reading.

Milestone: *Phase 6 - Context-aware detection*.

- [x] A typed, deterministic `ExemptionPolicy` reading resource **tags** -- data,
      never model interpretation -- and returning a verdict with a machine-readable
      reason. Per-client tag conventions are configuration, not code (#48)
- [x] Exemption wired in **without silent suppression**: an exempt resource is still
      surfaced as a finding, annotated with the reason, excluded from actionable
      recommendations and from the actionable savings total, and recorded as an
      `AuditEvent` (#49)
- [x] The report distinguishes **actionable savings** from **flagged but exempt**,
      because a total that quietly absorbed exempt waste would misstate both (#50)
- [x] **The load-bearing invariant, implemented and asserted:** exemption may only
      make the system do *less*. It may suppress a recommendation; it may never
      downgrade a safety classification or make something automatable. A tag can move
      a finding toward "leave alone" and never toward "act" (#51)
- [x] Prompt-injection resistance through the tag path: because exemption is
      deterministic string matching, a hostile tag is a non-matching string. Asserted
      with the differential-twin technique (#52)
- [x] The two `looks_wasteful_is_healthy` cases become surfaced-but-exempt, and the
      eval ground truth redefines "handled correctly" as "not actionably
      recommended" -- the point is honest handling, not pretending the resource is
      idle (#53)
- [x] The `looks_healthy_is_wasteful` case closed by a **documented, general** change
      to the oversized-VM threshold and headroom logic, with precision and recall
      before and after, and no new false positive on the healthy set (#54)
- [x] The scorecard moves from 0/3 to 3/3 adversarial, the safety gate stays green,
      and a test asserts no money figure moves for any non-exempt resource (#55)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.6.0 (#56)

**Exit criteria:** `uv run costsentinel eval` reports 3/3 adversarial cases handled
correctly with all three gates green; a report shows a disaster-recovery standby
surfaced with its exemption reason and sitting outside the actionable savings total;
and a test proves no tag value can turn a `review` or `block` action into an
automatable one.

**Deliberately *not* in this phase:** statistical anomaly detection (Phase 7), and
any exemption that depends on anything other than a tag. Inferring intent from a
resource name, a resource group or a model's reading of a description would make
exemption non-deterministic and attacker-influenceable, which is the one thing it
must not be.

**Landed.** The scorecard moved from 0/3 to 3/3 adversarial, detection precision from
0.86 (12/14) to 1.00 (13/13) and recall from 0.92 (12/13) to 1.00 (13/13), with all
three gates green and no money figure moved for any non-exempt resource. The two
false positives are now surfaced-but-exempt with the tag that withheld the advice and
their cost shown outside the actionable total, and the oversized-VM rule was
generalised rather than tuned (ARCHITECTURE.md D98-D102).

Three things to carry forward rather than claim as finished:

- **Exemption depends on the client's tagging discipline.** An untagged standby is
  still reported as idle, and that is the accepted cost of refusing to infer intent
  from a model's reading of a resource name (D98). The mitigation is a product one:
  tell the client which tag to add. A finding that says "this looks like a standby;
  tag it `role: disaster-recovery` and we will stop asking" is worth more than a
  silent guess, and belongs with the reporting phase.
- **Reservations cannot be exempted**, because a commitment carries no tags. That is
  a statement about the Azure API surface rather than a policy choice, and if it
  matters it needs a scope-level exemption rather than a resource-level one.
- **The test suite is now about three minutes with coverage.** Watch it: the
  end-to-end determinism tests are the ones to consolidate first.

---

## Phase 7 — Spend anomalies and the ranking rule

**Goal:** detect that spend *changed* -- as distinct from "this resource is wasteful"
-- using transparent arithmetic over a cost series, and settle how a finding with no
quantified saving ranks.

Deferred three times to get here: behind the memory layer because threshold
calibration wants history (D67), behind the evaluation harness because tuning a
threshold without a measurement is guessing (D85), and behind context-aware detection
because calibrating against a scorecard containing known false positives would tune
the detectors around a defect (D97). This is the first phase that can calibrate
against a clean measurement.

The ranking rule comes first, in its own issue, because an anomaly carries **no saving
at all**. Shipping anomalies while "how does an unquantified finding rank" was still
open would mean every anomaly inheriting an unsettled rule.

Milestone: *Phase 7 - Spend anomalies and the ranking rule*.

- [x] The phase split recorded, and CLAUDE.md section 14 gains the rule that a
      test asserts *intended* behaviour: a deliberately open gap is a strict
      `xfail` naming its closing issue, never a wrong answer asserted as correct
      (#58)
- [x] **The ranking rule, resolved and documented:** an unknown or unquantified saving
      ranks **low until quantified**, so a known saving of any positive magnitude
      outranks an unknown one, and reversibility breaks ties between comparable known
      magnitudes rather than letting an unknown outrank a known (#59)
- [x] The eval's expected ranks updated to the chosen rule, with the previously
      documented discordant pair resolved **by rule** rather than by relabelling one
      case, and concordance reported before and after (#60)
- [x] A deterministic statistical detector over a cost or usage series, per
      subscription and per resource where available, finding level shifts and spikes
      against a trailing baseline by a transparent method. Every alert states the
      baseline, the observed value and the deviation in plain numbers (#61)
- [x] Committed, seeded synthetic series in the mock provider: a genuine step change,
      a one-off spike that reverts, a steady healthy series, and a seasonal pattern
      that must **not** alert (#62)
- [x] `CostAnomaly` as a first-class finding distinct from a waste signal. It says
      "spend changed, investigate", never "this resource is wasteful, act", and
      **cannot** produce an automatable recommendation -- asserted, not asserted-about
      (#65)
- [x] Labelled anomaly cases in the eval with precision and recall as a quality
      metric, plus an adversarial case the seasonal pattern must not fire on (#63)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.7.0 (#64)

**Exit criteria:** `uv run costsentinel eval` detects the step change and the spike,
does **not** alert on the seasonal pattern or the healthy series, and reports anomaly
precision and recall; a test proves no unknown saving can outrank a known one and no
anomaly can reach an automatable action; and a gap in a series is reported as unknown
rather than as a zero or an anomaly.

**Deliberately *not* in this phase:** attributing a spike to the resource that caused
it, egress and data-transfer anomalies, real-model retry and fallback, a token-spend
guardrail, and threshold recalibration against real estates. All of those are Phase 9,
because none of them can be scored without a real subscription or a real model, and a
phase whose exit criteria CI cannot check is a phase nobody can close honestly.

**Landed.** The ranking rule is settled (ARCHITECTURE.md D104) and ranking concordance
moved from 0.97 (29/30) to 1.00 (30/30) -- by rule, applied to every label carrying an
unquantified saving, rather than by editing the one case that disagreed. Spend-anomaly
detection finds the step change and the spike, reports nothing on the seasonal pattern
or the steady series, and scores 1.00 precision and 1.00 recall over four labelled
shapes. Adversarial coverage went from 3/3 to 4/4 and detection from 13/13 to 14/14,
with all three gates green and no money figure moved.

Three things to carry forward rather than claim as finished:

- **The detector does not attribute.** It says a subscription's spend changed, not
  which resource changed it, because a per-resource cost series is not in the provider
  seam and the mock provider cannot usefully fake one. Phase 9, and the reason
  `assess_series` takes a bare series and a scope label.
- **The constants are defensible, not calibrated.** 3.5 and 5.0 are the conventional
  cuts, the 2% scale floor and the 15% materiality floor are product judgements, and
  all four were chosen against four synthetic shapes rather than against real
  estates. Phase 9's calibration item is where they meet real data; the scorecard is
  what will show the move.
- **The weekday profile handles a weekly rhythm and nothing else.** A month-end batch,
  a quarterly close or a ramp would all read as a level shift. That is the right
  trade for now -- a client can check a weekday median by hand and cannot check an STL
  decomposition -- but it is a known limitation rather than a complete answer.

---

## Phase 8 — Client reporting

**Goal:** output a client would actually accept as a deliverable, and a guarantee that
reaches it.

Swapped ahead of attribution (D107) for two reasons. Phase 9 needs a real estate to be
meaningful, and before this is pointed at a client's subscriptions there should be
something a client can be shown. More importantly, every figure in the internal
`ClientReport` is already checked -- the cost-figure gate against committed ground
truth, the groundedness judge over each model-written sentence -- and the *rendered
document* is checked by nothing, while being the only artefact a client ever reads.

Milestone: *Phase 8 - Client reporting*.

- [x] A **Markdown renderer** producing a client-ready document with five distinct
      sections: a deterministic executive summary, the ranked actionable
      recommendations with their plain-number basis, **flagged but exempt** (Phase 6),
      **spend anomalies -- investigate** (Phase 7), and **worth investigating, saving
      unknown** (#68)
- [x] A **PDF renderer** behind an optional `reports` extra that CI does not install.
      The Markdown path works with zero extras, and the PDF is produced from the same
      source so the two cannot diverge (#69)
- [x] **Groundedness and figure accuracy over the rendered output**, not only the
      internal object. A sentence asserting a number in the final document must match
      a source figure, and `cost.figure_accuracy` becomes a hard gate over the
      rendered text as well (#70)
- [x] **Provenance and honesty in the document:** an unknown renders as "unknown" and
      never as zero or blank, an exempt item shows a cost and never a saving, the
      report states where its prices came from, and a disclaimer distinguishes
      estimated from verified figures (#71)
- [x] **Byte-identical rendering** for the same input, with a committed golden file
      and the generated-at timestamp the only injected variable (#72)
- [x] `costsentinel report`, and one **committed sample** rendered from the mock
      estate so a repository visitor can read a real output without running anything
      (#73)
- [x] ARCHITECTURE and ROADMAP updated; CHANGELOG entry for v0.8.0 (#74)

**Exit criteria:** `costsentinel report` produces a Markdown document from a scan; a
test corrupts one number in the rendered text and the figure-accuracy gate fails, then
passes when it is restored; the golden file matches byte for byte; and a committed
sample report is readable in the repository.

**Deliberately *not* in this phase:** per-client branding, the
realised-versus-projected savings section (which needs episodic outcomes to be worth
rendering), residual spend per finding and the portfolio roll-up. All four are
recorded under "Later features, scheduled" with their reasoning. This phase is the
*renderer and its guarantee*; adding four content features on top of a renderer whose
output nothing checks would be building on the same gap again.

**New dependencies to flag:** a PDF toolchain, behind an optional extra. The Markdown
renderer uses the standard library only -- no template engine, because a report with
five fixed sections is a function and not a template, and a template is one more place
a figure could be reformatted on the way out.

---

## Phase 9 — Anomaly attribution and real-model hardening

**Goal:** say *what* a spend change was, make the real-model path survive a backend
failure and a spend ceiling, and close the sample-report gap Phase 8 exposed.

Split out of the anomaly phase (D103) because none of it could be *scored* offline: a
fallback between two backends needs two backends, and a token-spend guardrail needs
token spend. That reasoning holds for scoring and not for building, and this phase acts
on the distinction. Both features are built and tested offline with fakes -- a primary
that raises and a secondary that serves, a FakeLLM reporting deterministic token counts
that cross a budget -- so the *logic* is covered without a key. What still cannot be
done offline is calibration against real traffic and red-teaming a real backend, and
those stay out (see below).

**One reordering, flagged.** The Phase 7 roadmap recorded that attribution "needs a
real cost series broken down by resource, which the synthetic series deliberately does
not model", and treated that as a reason to wait for a real estate. On inspection that
is backwards: the breakdown is a *provider-seam* shape, and a seam that only exists
once a real tenant is connected is a seam nobody can test. So the synthetic series
gains a seeded per-dimension breakdown (#77) and attribution is built and scored
against it, exactly as every other detector was. Real data will calibrate the
materiality threshold; it is not needed to define the arithmetic.

Milestone: *Phase 9 - Anomaly attribution and real-model hardening*.

- [x] **Close the Phase 8 sample gap first.** Give the mock estate a resource that is
      both detector-flagged and tag-exempt, and one finding that genuinely cannot be
      priced, so `docs/sample-report.md` exercises all six renderer sections. The tests
      that pin the estate by count and total are updated to the new numbers
      deliberately, in the same change, rather than loosened to ranges -- a test that
      stops naming a number stops being able to catch one changing (#76)
- [x] A **seeded per-dimension cost breakdown** behind the provider seam, reconciling
      with the per-subscription totals it explains. An absent breakdown is *unknown*,
      never an empty one, because an empty breakdown reads as "no cause" (#77)
- [x] **Deterministic anomaly attribution:** rank the components by their contribution
      to the delta and name what accounts for it, with before, after, contribution and
      share as plain numbers. No model sets the attribution; unknown stays unknown when
      the data cannot support one (#78)
- [x] **Attribution in the document**, inside the existing anomaly section, with an
      unattributed anomaly saying so rather than quietly omitting the line (#79)
- [x] **Labelled attribution cases**, including one that is genuinely unattributable and
      must report unknown, plus attribution accuracy as a quality metric (#80)
- [x] **Backend fallback:** a primary failure or schema violation retries on the
      secondary, with the trace recording which backend served each call and why the
      fallback happened. Both real backends stay opt-in; tested offline with two fakes
      (#81)
- [x] **A per-run token budget** that stops new model calls and degrades to the
      no-prose path, leaving every figure, classification and finding intact and
      marking the gap as "narrative unavailable (token budget reached)" (#82)
- [x] **The prose-only invariant, asserted:** the same estate produces identical money,
      actions and findings whether prose came from the primary, from the secondary after
      a fallback, or not at all (#83)
- [x] ARCHITECTURE, README and SECURITY.md updated; CHANGELOG entry for v0.9.0 (#84)

**Exit criteria:** a spend change in the synthetic estate is attributed to a named
component and rendered with its numbers; an unattributable change reports unknown; a
primary-backend failure falls back to the secondary without failing the sweep; a budget
cutoff costs the prose and nothing else, proved by comparing reports.

**Deliberately *not* in this phase, and now scheduled as Phase 9b:** egress and
data-transfer anomaly detection, prompt-injection red-teaming against the real
backends, the monthly and quarterly-aware anomaly baseline, and calibrating the
detector thresholds and ranking weights. The first is a new detector rather than
hardening; the other three genuinely need real traffic, real history or a real key, and
grouping them with work that can be finished offline would mean a phase that cannot be
closed.

---

## Phase 9b — Calibration against real traffic

**Goal:** the residue of Phase 9 that no fake can stand in for. Cannot be closed
without a real estate, a real key and real history, so it is its own phase rather than
an open tail on one that can be finished.

- [ ] Egress and data-transfer anomaly detection
- [ ] Prompt-injection red-teaming against the real backends, using hostile resource
      names and tags drawn from the evidence path. The offline injection gate proves
      the *contracts* hold; it cannot prove a particular real model is not persuadable
- [ ] **A monthly and quarterly-aware anomaly baseline.** The Phase 7 weekday profile
      handles a weekly rhythm and nothing else, so a month-end batch, a quarter-end
      close or an annual renewal all read as a level shift -- a false positive that
      recurs on a schedule, which is the same failure mode the weekday profile was
      added to fix, one period up. It needs a baseline that knows about the month and
      the quarter before this detector is pointed at a real estate, and real history to
      calibrate against
- [ ] Calibrate detector thresholds and the ranking weights against the Phase 5
      scorecard and observed real estates, replacing the Phase 2 defaults. The anomaly
      constants in particular are *defensible* rather than calibrated: 3.5 and 5.0 are
      the conventional modified-z cuts, and the 2% scale floor and 15% materiality
      floor are product judgements chosen against four synthetic shapes
- [ ] The attribution materiality threshold, for the same reason: the share a component
      must account for before it is named is a product judgement until real changes
      have been examined

**Exit criteria:** a spend change in a real subscription is attributed and reported; a
hostile resource name cannot alter a recommendation on a real backend; the thresholds
in the code are the ones the data supports.

---

## Phase 10 — The durable approval gate, and the first A2A surface

**Goal:** a gated action halts the graph until a human decides, durably enough to
survive the process exiting -- and approving it still cannot change anything in a
cloud, by construction.

The approval seam has existed since Phase 1 and the supervisor has routed
`REQUIRE_APPROVAL` since Phase 2, always to the report author, always with the reason
that nothing has an execution path anyway. That was honest and it was a placeholder.
This phase makes the halt real: the sweep pauses at a checkpoint, the process may exit,
and a decision recorded later from a different process resumes the right sweep.

**One pull-forward, flagged.** A single read-only A2A Agent Card moves here from Phase
11. The approval queue is the first thing in this system another agent would have a
reason to ask about, and an A2A surface built *after* a half-dozen specialists are
exposed is one where "is this read-only?" has to be re-established for each of them.
Built once, now, against the one capability whose read-only-ness is the phase's own
invariant. The rest of Phase 11 -- per-specialist cards, per-caller authorisation,
Foundry interop, consuming other agents -- stays in Phase 11.

Milestone: *Phase 10 - The durable approval gate and the first A2A surface*.

- [ ] **The approval gate as a durable LangGraph interrupt**, after the report rather
      than before it: a client always gets the document, and the pause is about the
      decisions rather than the deliverable. A sweep with nothing gated does not pause
      (#86)
- [ ] **A typed decision record** carrying the approver, the decision, the reason, the
      timestamp and *the figures as they stood when the decision was made* -- an
      approver agrees to a specific saving on a specific resource, and a later reprice
      must not retroactively change what they agreed to. Written to episodic memory
      with an `AuditEvent` (#87)
- [ ] **Approving never mutates, proved four ways:** structurally (no execution module,
      nothing imports a mutating client), representationally (the approved state carries
      no executable handle), exhaustively (no decision on any action class reaches a
      mutation), and end to end (an approved deletion yields a recorded hand-off and
      zero provider calls) (#88)
- [ ] **`costsentinel approvals list` and `approvals decide`**, resuming the correct
      sweep from the checkpoint in a separate process. The paused state key is the
      `run_id`, documented because a second process has to find it (#89)
- [ ] **Durability demonstrated:** a test pauses a run, discards the in-memory graph,
      reloads from the checkpoint, decides, and resumes to a completed report (#90)
- [ ] **One read-only A2A v1 Agent Card** with a signed card and an in-process task
      lifecycle (submit, status, artifact), returning the report or the pending queue as
      the artifact. Opt-in server; CI exercises a fake client with no network (#91)
- [ ] **No write capability on the advertised surface**, asserted against a vocabulary
      rather than an exact skill list so a skill added later has to pass it (#92)
- [ ] ARCHITECTURE, README, SECURITY.md updated; CHANGELOG entry for v0.10.0 (#93)

**Exit criteria:** a destructive recommendation halts the graph and persists; the
process exits; `approvals list` in a fresh process shows it; `approvals decide
--approve` resumes that sweep to a completed report with the decision recorded; and the
four no-mutation proofs pass.

**Deliberately *not* in this phase, and scheduled as Phase 10b:** the Teams, Slack and
webhook adapters, approver authorisation, decision deduplication on replayed callbacks,
and the timeout-and-escalation policy. Those are about *where a human already works* and
they need recorded payloads, a signature-verification library and an HTTP client. The
interrupt they all drive is the part that has to exist first, and it is the part that can
be finished offline.

---

## Phase 10b — Approval adapters

**Goal:** the decision reaches the human where they already are, instead of in a
terminal.

Split from Phase 10 for the same reason 9b was split from 9: these need recorded
payloads from real workspaces and two new dependencies, and bundling them with the
interrupt would mean a phase that could not be closed without a Slack workspace. The
interrupt, the decision record and the CLI gate are the contract they implement.

- [ ] Teams adapter: adaptive card with the proposal, savings and risk class
- [ ] Slack adapter: Block Kit message with approve and reject actions
- [ ] Webhook receiver for decision callbacks, signature-verified
- [ ] Approver identity and authorisation checks: who may approve what, for which client
- [ ] Decision deduplication and idempotency on replayed callbacks. A replayed approval
      must not become a second approval
- [ ] Timeout and escalation policy. **No decision never means approved** -- the
      `Decision` enum has no such member and the gate has no path to one, so this is
      about who gets asked next rather than about what happens by default
- [ ] Adapters tested against recorded payloads, with no live workspace in CI

**Exit criteria:** a destructive recommendation halts; a decision recorded from Teams or
from Slack resumes the correct sweep; a replayed callback changes nothing.

**New dependencies to flag:** `slack-sdk`, an HTTP client, and a signature-verification
library. All behind an optional extra CI does not install.

---

## Phase 11 — A2A layer

**Goal:** specialists are discoverable and invocable by external agents, without
routing around governance.

- [ ] A2A v1 Agent Cards for **each specialist**. Phase 10 published one card for
      the read-only report-and-queue capability; this is the rest of them
- [ ] A2A server exposing the specialists, beyond the opt-in single-capability
      server Phase 10 built
- [ ] **Consuming other agents**: CostSentinel as an A2A *client*, discovering and
      invoking an external agent. Deliberately not started in Phase 10, which
      published a card and nothing that reads anybody else's
- [ ] Cards declare capability, input and output schema, and the action class of
      anything the agent can propose
- [ ] Authentication and per-caller authorisation
- [ ] Interop check against Azure AI Foundry
- [ ] Test asserting an external caller cannot obtain execution of a `review` or
      `block` action without a recorded `ApprovalDecision`

**Exit criteria:** an external agent invokes the Savings Estimator and receives a
typed result; an external attempt to execute a gated action is refused.

---

## Phase 12 — Dashboard

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

## Phase 13 — Execution and hardening

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

- **Config-tunable ranking weights per client** (Phase 10, with the client policy
  overlays the approval gate needs anyway).
  The Phase 2 weights -- 0.60 savings, 0.25 safety, 0.15 confidence -- are a product
  judgement baked into a module constant. Different clients have genuinely different
  appetites: a government production estate may want safety weighted far higher than a
  development sandbox. The weights should move into the client policy overlay
  alongside the action-class tightening, with the built-in values as the default and
  the effective weights reported on every finding so a client can see what was applied
  to them. The ranking must stay deterministic and the weights must stay data, never
  model output.

- **Per-client report branding and formatting** (after the reporting phase, whenever
  a second client's deliverable is actually required). A logo, a colour and a footer are
  configuration, not code, and the right time to build it is when a real client asks
  rather than when it is imagined. Deliberately left out of the reporting phase so
  that phase could be about the *renderer and its guarantee*: adding presentation
  options on top of output nothing checks would be building on the same gap the
  reordering existed to close.

- **A realised-versus-projected savings section** (after a few real sweeps, with
  episodic outcomes). CostSentinel records what each client accepted or rejected
  (Phase 4) but has no record of what an accepted recommendation actually saved,
  because nothing has executed yet and no real sweep has run twice. A section
  comparing projection against realisation is the single most useful thing a report
  could gain and is also the easiest to fabricate, so it waits for data rather than
  being modelled. It needs execution (Phase 13) or at least a second sweep over an
  estate where a client acted.

- **A portfolio roll-up for the provider's own leadership**, as a deliberate exception
  to the per-client reporting rule. Savings, findings and approval backlog totalled
  *across* clients. It must not reuse `ClientReport`: it needs its own
  `PortfolioRollup` type, its own access control, and a test asserting it is
  unreachable from any client-scoped path, because the whole point of D12 is that no
  client-facing artefact can span clients. Totals summed deterministically from
  per-client reports, never modelled. Scheduled after reporting rather than inside it,
  so the per-client guarantee is settled before an exception to it exists.

- **Report residual spend alongside the saving, for every conservative action**
  (Phase 8, with reporting). A finding currently says "resize this VM, saves
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

- **A real-run acceptance checklist** (Phase 7, alongside threshold calibration).
  Phase 3 made a real read-only run safe but did not execute one. The first real sweep
  against a sandbox subscription will surface a predictable class of gap -- a SKU the
  catalogue does not price, a resource type `sku_mapping` does not translate, an
  untagged subscription, a region nobody refreshed prices for. Each of those currently
  degrades to an explicit unknown, which is correct behaviour and poor experience. The
  checklist should turn the first run into a diagnostic: what fraction of resources
  priced, which SKUs and regions were missed, and the exact command to fix each gap.

---

## Open decisions

Things that need a judgement rather than an implementation. Recorded here rather than
settled quietly in code, because each one changes what a client is told. Resolved ones
stay, with the reasoning and the position that lost, because the argument is worth more
than the conclusion.

- ~~**How should an unknown saving participate in ranking?**~~ **Resolved in Phase 7**
  (ARCHITECTURE.md D104): an unquantified saving ranks **low until quantified**. A
  quantified saving of any positive magnitude outranks an unquantified one, and
  reversibility breaks ties between comparable known magnitudes rather than promoting
  an unknown above a known.

  Of the two positions recorded here, the first won. The deciding argument was the
  product one: a FinOps tool must not tell a client that the thing it cannot cost is
  their biggest opportunity, because that is a claim about money made with no number
  behind it. The second position's appeal -- that a reversible action with no figure
  is still a better first move than an irreversible one with a small figure -- is
  real, and it is exactly what the rule now overrules. `sql-reporting` (scale down an
  idle database, reversible, unquantifiable) sits beneath `pip-decommissioned-lb`
  (release an address, 3.65, irreversible), and on a reviewer's instinct that reads
  backwards. It is accepted on purpose and recorded in the label.

  The stated risk of the winning position -- that a large unquantified waste sinks to
  the bottom and is never actioned, which is how an unknown quietly becomes a zero --
  is **not** mitigated by the ranking and is mitigated instead by the report: an
  unpriced finding is counted, listed, excluded from the total rather than zeroed, and
  carries the reason no figure could be derived. If that proves insufficient in
  practice, the answer is a better *price* for the finding, not a better rank for an
  unknown.

  The rule is a tier rather than a weight, so it survives the per-client weight
  configuration scheduled for Phase 10. Ranking concordance moved from 0.97 (29/30) to
  1.00 (30/30) when the labels were updated to express the rule -- by rule, across
  every label carrying an unquantified saving, rather than by editing the one case that
  disagreed.

---

## Parked ideas

Ideas noted mid-phase, to keep scope from expanding. Not commitments.

- Reservation and savings-plan purchase recommendations (needs commitment modelling).
- Cross-client benchmarking ("your idle-VM rate vs the fleet median"), subject to a
  tenancy and confidentiality review.
- Cost forecasting and budget-breach prediction.
- A scheduled autonomous sweep cadence per client, with a digest.
- Tag-hygiene scoring as a standalone, fully `allow`-class mission.
