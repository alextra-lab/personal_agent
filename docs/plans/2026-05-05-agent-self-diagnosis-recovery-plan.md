# Agent Self-Diagnosis Recovery Plan

**Date**: 2026-05-05
**Status**: Proposed priority interruption
**Master Plan impact**: Pause normal sequencing from `docs/plans/MASTER_PLAN.md` until the recovery gates in this plan pass.
**Related decisions**: ADR-0039, ADR-0053, ADR-0054, ADR-0059, ADR-0060, ADR-0061, ADR-0062, ADR-0063, ADR-0065
**Primary concern**: Recent gates, primitive-tool migration, skill routing, context compression, and memory pipeline changes may have combined to reduce the agent's ability to inspect itself, use tools effectively, retain working context, and write/retrieve useful knowledge.

---

## Purpose

Restore the agent's ability to diagnose itself before continuing feature work.

The current failure is not one bug. It is a system-level regression risk across five interacting layers:

1. Tool availability and tool-loop gates.
2. Skill injection and primitive-tool guidance.
3. Within-session and request-entry context compression.
4. Captain's Log, entity extraction, and Neo4j memory writes.
5. Elasticsearch, Neo4j, Redis, and service startup readiness.

This plan sequences evidence gathering before design changes. No permanent architectural changes should be made until the canaries and trace analysis identify the failing boundaries.

---

## Definition Of Done

Normal Master Plan sequencing can resume when all of these are true:

1. A self-diagnosis canary prompt reliably causes the agent to inspect source, logs, Elasticsearch, and Neo4j without premature forced synthesis.
2. A memory canary proves end-to-end flow: user fact -> capture -> entity extraction -> Neo4j Turn/Entity/Relationship -> later retrieval -> final answer uses retrieved memory.
3. Skill selection telemetry proves which skill docs were available, selected, omitted, and why.
4. Tool-gate telemetry distinguishes useful exploration from loops and shows no diagnostic prompt is blocked before enough evidence is collected.
5. Compression telemetry proves the original task, current objective, recent tool results, selected skills, and memory slab survive long diagnostic sessions.
6. A small regression suite of real prompts passes against the chosen diagnostic/recovery profile.

---

## Immediate Operating Rules

Until this plan is complete:

- Do not implement `FRE-265` legacy tool deletion.
- Do not further tighten loop gates or context budgets.
- Do not treat primitive-tool eval success as evidence that self-diagnosis works.
- Do not tune memory retrieval before proving memory writes are happening.
- Do not tune entity extraction quality before proving the consolidator is actually running.
- Do not make multiple subsystem changes in one PR.
- Prefer read-only diagnostic capability over cost reduction while investigating.

---

## Current Evidence

The recent history points to plausible interaction failures:

- `FRE-282` introduced intent-based skill injection: `bash.md` plus the first keyword-matched skill doc. This can miss relevant skills if the user prompt does not contain the expected trigger words.
- `FRE-263` deprecated all 8 legacy tools behind `AGENT_LEGACY_TOOLS_ENABLED=false`, increasing dependence on primitive tools and skill docs.
- `FRE-251` added hard and soft within-session compression. The implementation protects head and tail, but the current default `context_window_max_tokens=2048` and `within_session_min_tail_tokens=2000` leave very little room for system prompt, skills, memory, tool definitions, summaries, and active reasoning state.
- `FRE-302` through `FRE-307` added cost gating and retry telemetry. This improves budget safety but can affect background extraction if role caps deny or delay entity-extraction calls.
- `39cde53` fixed infrastructure startup waiting for Elasticsearch and Neo4j. Before that, the service could run while writes to ES/Neo4j were absent or degraded.
- `MemoryService` and the service lifespan intentionally degrade when Neo4j is unavailable. That is operationally safe, but dangerous for diagnosis because the service can look healthy while memory is absent.

---

## Workstreams

### Workstream A - Recovery Harness And Baseline

**Goal**: Create a repeatable way to reproduce the regression and compare changes.

**Scope**:

- Build a prompt set from real failure modes:
  - self-diagnosis of a recent regression
  - Elasticsearch log investigation
  - Neo4j memory inspection
  - memory canary recall
  - long diagnostic session with multiple tool results
  - primitive-tool task requiring a non-obvious skill
  - service startup health inspection
  - loop-prone query refinement
- Record, per run:
  - tools exposed
  - skill docs injected
  - tool calls requested and executed
  - loop-gate decisions
  - forced synthesis events
  - context compression events
  - memory_context injected
  - Captain's Log capture id
  - entity extraction result
  - Neo4j write counts
  - final answer quality

**Artifacts**:

- `telemetry/evaluation/EVAL-agent-self-diagnosis/prompts.yaml`
- `telemetry/evaluation/EVAL-agent-self-diagnosis/README.md`
- `telemetry/evaluation/EVAL-agent-self-diagnosis/<run-id>/report.md`

**Gate**:

- At least one baseline run exists with enough telemetry to identify where each failure happens.

---

### Workstream B - Startup, ES, Redis, And Neo4j Health

**Goal**: Prove that required observability and memory infrastructure is available before testing higher layers.

**Analysis sequence**:

1. Start the service in the intended environment.
2. Verify startup events:
   - `elasticsearch_logging_enabled`
   - `captains_log_es_indexing_enabled`
   - `memory_service_initialized`
   - `event_bus_ready`
   - scheduler and consumer startup logs
3. Call `/health` and verify database, Elasticsearch, Neo4j, second brain, event bus, and MCP status.
4. Confirm ES receives a fresh service log from the current process.
5. Confirm Redis streams receive and deliver at least one synthetic request/capture event.
6. Confirm Neo4j accepts a simple read and write through the same service credentials the agent uses.

**Likely fixes if this fails**:

- Strengthen health checks from "TCP reachable" to "read/write ready" for optional services.
- Add an explicit degraded health state when memory graph is disabled or disconnected.
- Make missing ES/Neo4j startup events visible in `/health`.

**Gate**:

- The system cannot proceed to memory analysis until ES, Redis, and Neo4j readiness are proven with live writes.

---

### Workstream C - Memory Write Pipeline Canary

**Goal**: Prove that memories are created before debugging retrieval quality.

**Canary conversation**:

1. User says a unique durable fact, for example: "Memory canary: the diagnostic color for Recovery Plan 2026-05-05 is ultramarine."
2. Agent responds normally.
3. Wait for consolidation or trigger the scheduler in a controlled way.
4. Query ES for the trace and capture.
5. Query consolidation attempts for the trace.
6. Query Neo4j for:
   - the `Turn`
   - extracted `Entity` nodes
   - `DISCUSSES` relationships
   - extracted relationships between entities
7. Ask the agent later: "What is the diagnostic color for the recovery plan?"

**Analysis questions**:

- Was the turn captured?
- Was the capture indexed to ES?
- Was a `request.captured` event published?
- Did the scheduler receive it?
- Did entity extraction start?
- Did extraction complete, timeout, fail JSON parsing, or hit `BudgetDenied`?
- Did the consolidator skip fallback results?
- Did `MemoryService.create_conversation()` write?
- Did `MemoryService.create_entity()` write?
- Did `MemoryService.create_relationship()` write?
- Did later retrieval find the memory?
- Was the memory injected into the prompt?

**Likely fixes if this fails**:

- If capture is absent: fix Captain's Log write path first.
- If ES is absent: fix ES handler/indexer startup before memory.
- If scheduler is absent: fix Redis consumer/scheduler wiring.
- If extraction fails: fix model role, budget gate, timeout, or JSON parsing.
- If Neo4j writes fail: fix `MemoryService` connection, visibility, schema, or write queries.
- If retrieval fails but writes exist: tune proactive memory and recall separately.

**Gate**:

- One canary must survive all seven stages before any retrieval tuning begins.

---

### Workstream D - Skill Injection And Skill Use

**Goal**: Replace brittle exact-keyword routing with inspectable, higher-recall skill selection.

**Current risk**:

`get_skill_block(message)` always includes `bash.md` when primitive preference is enabled, then injects only the first keyword-routed skill. If the original user message does not contain the expected trigger, the agent never sees the relevant operational recipe.

**Analysis sequence**:

1. Add telemetry around current skill routing:
   - user message preview
   - matched route
   - candidate skills
   - injected skill files
   - token count of injected block
2. Run prompts where the needed skill is implied but not named.
3. Compare current keyword routing against:
   - always-on compact skill index
   - top-k lexical retrieval
   - top-k embedding retrieval
   - model-selected skill request after first reasoning pass
4. Measure:
   - task success
   - tool correctness
   - tool iterations
   - prompt token cost
   - cache hit behavior

**Proposal direction**:

- Always inject a compact skill manifest:
  - skill name
  - purpose
  - when to use
  - key commands or APIs
- Inject full skill docs by top-k retrieval over:
  - current user message
  - active task state
  - last tool error
  - selected intent
  - available infrastructure status
- Allow the model to request a skill mid-task through a read-only skill loader or an explicit `skill_request` mechanism.
- Log "needed but missing" signals when the model fails a recipe that an omitted skill would have covered.

**Gate**:

- The selected approach must beat keyword routing on the self-diagnosis prompt set without increasing average prompt cost by more than an agreed threshold.

---

### Workstream E - Tool Gates And Model Looping Alternatives

**Goal**: Separate destructive-loop prevention from diagnostic exploration.

**Current risk**:

The agent has multiple overlapping stop mechanisms:

- per-task tool iteration caps
- budget-warning injections
- forced synthesis when the cap is reached
- per-tool loop gates
- output-identity blocking
- cost-gate pressure for cloud calls
- skill sparsity that can cause bad tool choices
- context compression that may hide why a tool was called

Any one mechanism may be reasonable. Together, they can stop diagnosis before the model has enough evidence.

**Sequenced analysis**:

1. Build a trace table for each prompt:
   - desired information need
   - tool requested
   - gate decision
   - new information gained
   - whether the next call depended on prior output
2. Classify blocked calls:
   - destructive or unsafe
   - exact repeated no-new-information call
   - legitimate retry after transient failure
   - legitimate polling
   - legitimate multi-file/source exploration
   - model confusion due to missing skill
   - model confusion due to missing context
3. Evaluate loop gates separately for:
   - read-only source/log inspection
   - external network calls
   - paid model calls
   - writes
   - destructive shell commands

**Industry/SOTA alternatives to evaluate**:

1. **Progressive friction gate**
   - Allow initial exploration.
   - Warn on repeated patterns.
   - Require the model to state expected information gain before additional repeated calls.
   - Hard-block only stable identical outputs, unsafe actions, or no-information loops.

2. **Information-gain ledger**
   - Each tool call records a short structured claim: what new fact was obtained.
   - Repeated calls are allowed only if they target a missing fact or changed condition.
   - The gate blocks when the call cannot name a new expected fact.

3. **Plan-observe-reflect loop**
   - Before tool use, the model emits a compact investigation plan.
   - After tool results, it updates the plan state.
   - The gate compares proposed calls against unresolved plan items.

4. **Diagnostic mode budget class**
   - Self-diagnosis, incident response, and system-health tasks get a higher read-only budget.
   - Writes remain approval-gated.
   - Forced synthesis is delayed until the model has inspected required observability surfaces.

5. **Tool-call circuit breakers by risk**
   - Read-only calls: high budget, advisory gates.
   - Paid calls: budget gate plus role caps.
   - Writes: approval gate.
   - Destructive shell: hard deny or explicit approval.

6. **Auto-tuned per-tool thresholds**
   - Use telemetry to propose Linear issues for threshold changes.
   - Do not self-apply threshold changes without approval.

**Recommended direction**:

Adopt progressive friction plus risk-class budgets. Keep hard blocks for unsafe actions and identical stable-output loops. Move diagnostic read-only work away from strict caps.

**Gate**:

- Diagnostic prompts must complete without hitting forced synthesis unless they have already inspected the required sources.
- Known pathological loops still terminate quickly.

---

### Workstream F - Context Compression And Context Budget Calibration

**Goal**: Ensure compression preserves reasoning-critical state.

**Current risk**:

The head-middle-tail design is sound, but the configured budget geometry may be too small for real diagnostic sessions. A `2048` token context window with a `2000` token tail floor leaves almost no room for system prompt, skill docs, memory context, tool definitions, summaries, and current reasoning state.

**Analysis sequence**:

1. For each baseline prompt, capture:
   - estimated tokens before compression
   - hard-trigger occurrence
   - soft-trigger occurrence
   - head tokens
   - middle tokens in/out
   - tail tokens
   - selected skill docs
   - memory slab size
   - whether current objective remained visible
2. Inspect compressed prompts for:
   - original user task
   - latest subgoal
   - unresolved questions
   - selected skills
   - recent tool results
   - memory facts
3. Compare configurations:
   - current defaults
   - larger true model context window
   - lower tail floor as a percentage of context
   - explicit task-state system message
   - compression disabled for diagnostic mode

**Proposal direction**:

- Set `context_window_max_tokens` to the real usable model context by profile.
- Express `within_session_min_tail_tokens` as a ratio or profile-specific setting.
- Add an explicit "active diagnostic state" message that is regenerated rather than compressed away.
- Include selected skills and memory evidence in compression quality checks.

**Gate**:

- No diagnostic run loses the original objective, current subgoal, or last successful evidence source after compression.

---

### Workstream G - Integrated Recovery Profile

**Goal**: Add a safe temporary profile for recovering self-diagnosis without weakening production safety permanently.

**Profile behavior**:

- Read-only diagnostics have expanded tool budgets.
- Skill manifest is always injected.
- Full skill docs are selected by top-k retrieval rather than exact keyword match.
- Loop gates are advisory for source/log/read-only inspection.
- Writes and destructive shell remain gated or denied.
- Compression preserves a diagnostic state document.
- Memory canary status is shown in health output.

**Gate**:

- The recovery profile passes the regression suite.
- Production defaults are either updated intentionally or left unchanged with a documented operator switch.

---

## Proposed Next Steps

### Step 1 - Declare the pause

Update the working priority to this plan. Do not start new Master Plan items until Workstream A and Workstream B gates pass.

### Step 2 - Create the self-diagnosis eval harness

Create the prompt set and report template first. This prevents the team from judging changes by anecdote.

### Step 3 - Run infrastructure and memory canaries

Do not inspect skill routing or gate design until ES, Redis, Neo4j, capture, extraction, and writes are proven.

### Step 4 - Run baseline with current settings

Capture current failures without changing settings. This is the control run.

### Step 5 - Try a diagnostic recovery profile

Temporarily relax only read-only diagnostic constraints and inject a skill manifest. This is the treatment run.

### Step 6 - Decide permanent changes

After the baseline/treatment comparison, choose specific implementation work:

- skill selector replacement
- loop-gate policy change
- context-budget calibration
- memory pipeline repair
- health/readiness hardening

Each should become a separate approved issue or small PR.

---

## Suggested Work Order

```text
Wave 0 - Planning and freeze
  ├─ Mark normal sequencing paused
  └─ Keep legacy-tool deletion blocked

Wave 1 - Evidence harness
  ├─ Self-diagnosis prompt set
  ├─ Report template
  └─ Telemetry checklist

Wave 2 - Infrastructure and memory canaries
  ├─ ES write/read canary
  ├─ Redis stream canary
  ├─ Neo4j write/read canary
  └─ memory end-to-end canary

Wave 3 - Baseline run
  ├─ Current settings
  ├─ Current keyword skill routing
  ├─ Current loop gates
  └─ Current compression settings

Wave 4 - Recovery-profile experiment
  ├─ Skill manifest always on
  ├─ Read-only diagnostic budget expanded
  ├─ Progressive-friction loop gate treatment
  └─ Compression/task-state treatment

Wave 5 - Decision
  ├─ Choose permanent skill injection design
  ├─ Choose gate policy changes
  ├─ Choose compression calibration
  ├─ Fix memory pipeline if needed
  └─ Resume Master Plan sequencing only after gates pass
```

---

## Risks

| Risk | Mitigation |
| --- | --- |
| The team changes gates before identifying root cause | Baseline run is mandatory before treatment changes |
| Memory retrieval is tuned while writes are broken | Memory write canary must pass first |
| Skill routing is fixed but compression still removes relevant context | Compression analysis follows skill baseline and checks selected skills survive |
| Read-only diagnostic relaxation leaks into destructive actions | Risk-class budgets: read-only only; writes/destructive shell remain approval-gated |
| Cost rises during diagnosis | Treat recovery as temporary incident mode; measure cost but prioritize restoring introspection |
| Existing eval success gives false confidence | Use self-diagnosis-specific prompts, not primitive-tool replacement prompts |

---

## Open Questions

1. Should the recovery profile be an operator mode, an execution profile, or a temporary feature flag bundle?
2. Should skill selection be deterministic retrieval, model-selected, or hybrid?
3. Should diagnostic mode be available only to the owner or to all authenticated users?
4. What prompt-cost ceiling is acceptable for self-diagnosis runs?
5. Should memory canaries run continuously after deploy, or only as a manual verification command?

---

## Resume Criteria For `MASTER_PLAN.md`

Resume the prior sequence when:

- Workstream A baseline exists.
- Workstream B infrastructure readiness passes.
- Workstream C memory canary passes or has a specific fix plan.
- Workstream D and E have a chosen implementation direction.
- Workstream F confirms compression is not cutting off the active task, or a calibration task is created.
- The project owner approves returning to the next Master Plan item.
