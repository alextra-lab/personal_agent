# ADR-0036: Expansion Controller — Deterministic Workflow Enforcement

**Date:** 2026-03-28
**Status:** Accepted
**Deciders:** Alex (project lead)
**Linear Issue:** FRE-154
**Depends on:** EVAL-07 (evaluation findings synthesis), EVAL-08 (Slice 3 priority ranking)
**Blocks:** Slice 3 expansion controller implementation

---

## Context

The evaluation phase (runs baseline through run-04) identified a persistent **gateway→agent gap**: the Pre-LLM Gateway correctly classifies requests as `HYBRID` or `DECOMPOSE`, but the primary agent treats the expansion flag as advice rather than a contract. The LLM can choose to answer directly, silently bypassing the sub-agent expansion path.

This produces two categories of failure:

- **Silent strategy mismatch** (CP-16): Gateway says HYBRID, agent answers directly in 29s. Answer quality is acceptable but the telemetry contract is violated — no `hybrid_expansion_start` event.
- **Monologue timeout** (CP-17): Gateway says DECOMPOSE, agent attempts a long single-pass answer, hits the 180s LLM timeout. No expansion, no graceful degradation.

Cross-run data proves this is non-deterministic:

| Run | CP-09 HYBRID | CP-10 DECOMPOSE | CP-16 HYBRID | CP-17 DECOMPOSE |
|-----|-------------|-----------------|-------------|-----------------|
| Baseline | pass | fail | pass | pass |
| Run-03 | fail | fail | pass | fail |
| Run-04 | pass | pass | fail | fail (timeout) |

Same prompts, same model, same gateway output — different expansion behavior. The decision lives in LLM sampling, not deterministic code.

### Root Cause

The current implementation in `executor.py` (lines 745–756, 1084–1096, 1235–1290) works as follows:

1. Gateway sets `ctx.expansion_strategy` to `"hybrid"` or `"decompose"` (line 749)
2. A system prompt hint instructs the LLM to produce a numbered task list (lines 1084–1096)
3. The executor parses the LLM response for sub-task specs (lines 1242–1245)
4. If specs are found, sub-agents execute; **if not, the response falls through silently** (line 1284)

Step 4 is the gap. When the LLM ignores the decomposition instruction, the system logs a warning and returns the direct answer — no retry, no fallback, no enforcement.

### Industry Alignment

This diagnosis aligns with established agent architecture guidance:

- **Anthropic** ("Building Effective Agents"): Use deterministic workflows for predetermined control flow; reserve agent autonomy for parts that benefit from it.
- **LangGraph**: Distinguishes between workflow nodes (deterministic) and agent nodes (dynamic tool use).
- **Google ADK**: Workflow agents own sequencing; LLM agents own content generation.
- **GPT-5.4 second opinion** (EVAL-04): "Move 'whether expansion must happen' out of the model and into code."

The pattern is consistent: **workflow decisions that affect correctness belong in deterministic code; the LLM contributes plan content and synthesis, not branch compliance.**

### Design Inputs

| Document | Key contribution |
|----------|-----------------|
| `docs/research/evaluation-orchestration-analysis.md` | Root cause analysis, cross-run data, parked-for-Slice-3 decision |
| `docs/research/evaluation-run-04-second-opinion-response.md` | GPT-5.4 architectural recommendations A–E |
| `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` | State machine, telemetry schema, pseudocode |

---

## Decision

Introduce an **Expansion Controller** — a deterministic runtime component that enforces expansion when the gateway sets `strategy ∈ {HYBRID, DECOMPOSE}`. The LLM generates plan content and synthesis only; it does not decide whether to expand.

Additionally, implement **dual-mode orchestration** via a single configuration switch:

- `enforced` (production default): Gateway decisions are binding. The expansion controller deterministically enforces expansion.
- `autonomous` (research mode): Gateway decisions are advisory. The LLM retains full agency over whether to expand, matching current behavior.

---

## Key Design Decisions

### Decision 1: Move "whether to expand" from LLM to deterministic code

**Options considered:**

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Stronger system prompt | Emphasize expansion instruction more forcefully | Rejected — prompt compliance is exactly what is failing now |
| B. Tool-call enforcement | Require structured plan output or tool call before proceeding | **Selected** — makes expansion explicit, observable, and testable |
| C. Executor gate | If no plan/tool call occurs, retry once with stricter scaffold | Selected as safety net (complement to B) |
| D. Accept non-determinism | Live with strategy mismatch as an expected variance | Rejected — undermines gateway correctness and makes telemetry unverifiable |

**Selected approach: B + C.** The expansion controller requires structured plan output from the LLM. If the plan is invalid or absent, the executor retries once with a constrained prompt. If that also fails, a deterministic fallback planner generates the plan.

**Rationale:** Tool/schema enforcement (B) makes expansion observable and measurable. The executor gate (C) provides a safety net without adding complexity to the happy path. Prompt-only approaches (A) have already failed. Accepting non-determinism (D) weakens the gateway's value.

### Decision 2: Tool-call enforcement over prompt compliance

The LLM must produce a structured plan conforming to a defined schema before expansion proceeds. This replaces the current approach of injecting a prompt hint and parsing free-form numbered lists.

**Plan schema:**

```json
{
  "strategy": "HYBRID | DECOMPOSE",
  "tasks": [
    {
      "name": "string — task identifier",
      "goal": "string — what this sub-agent should answer",
      "constraints": ["string — scope or focus limits"],
      "expected_output": "string — output shape description"
    }
  ]
}
```

**Enforcement mechanism:** The expansion controller calls the LLM with structured output constraints (JSON schema or tool-call contract). If the response does not conform to the schema, it is invalid and triggers the fallback path.

**Implementation options** (decide at implementation time based on model stack support):

1. **Structured JSON output** — require JSON-mode response matching the plan schema
2. **Tool call** — expose `plan_subtasks(query, strategy, max_tasks) -> Plan` as a required tool
3. **Parse + validate** — parse free-form response against the schema, treat non-conformance as failure

Option 1 or 2 is preferred. Option 3 is a fallback for model stacks that don't support structured output or required tool calls.

### Decision 3: Deterministic fallback planner — scoped to enumerated comparisons

When the LLM planner fails (invalid output, timeout, empty plan), a deterministic fallback planner generates a plan from the prompt structure.

**Scope:** The fallback planner handles prompts with **explicitly enumerated entities or dimensions**. Examples:

- "Compare Redis, Memcached, and Hazelcast for 10k rps" → one task per system + recommendation task
- "Analyze performance, memory, and operational complexity" → one task per dimension

**Out of scope:** Open-ended prompts without enumerable structure ("Research the best approach to scaling our API layer"). For these, the fallback planner emits a generic 2-task split (research + recommendation) rather than attempting structural decomposition.

**Fallback planner rules:**

| Strategy | Rule |
|----------|------|
| `HYBRID` | Extract up to 3 dimensions/entities from the prompt. Create one task per dimension. Add a synthesis task. |
| `DECOMPOSE` | Extract all enumerated entities/dimensions. Create one task per evaluation axis. Add a recommendation task. Optionally add a consolidation task. |
| Either (no entities found) | Generic 2-task split: (1) research/analysis, (2) recommendation/synthesis. |

**Why scope matters:** The second opinion presented the fallback planner as general-purpose. In practice, it works well for prompts with explicit structure (CP-17: "Redis vs Memcached vs Hazelcast") but cannot reliably decompose open-ended analysis. Over-scoping the fallback planner would trade one unreliability (LLM non-compliance) for another (bad deterministic plans).

### Decision 4: Per-phase time budgets

Replace the single 180s execution envelope with phase-bounded budgets:

| Phase | Budget | On timeout |
|-------|--------|------------|
| Planner | 5–15s | Invoke fallback planner |
| Plan validation | <500ms | Invoke fallback planner |
| Sub-agent execution | 15–45s per worker, 60–90s global | Synthesize partial results |
| Synthesis | 10–25s | Emit condensed synthesis from partial aggregation |

**Hard rules:**

- If planner exceeds budget → fallback planner, not retry
- If some sub-agents fail → synthesize partial results with explicit gap acknowledgment
- If all sub-agents fail → concise direct answer with "analysis compressed due to execution constraints"
- If synthesizer exceeds budget → emit condensed result from partial aggregation
- **Never** return a raw LLM timeout to the user when partial results exist

**Rationale:** CP-17 demonstrates the failure mode of an undifferentiated timeout budget — the planner monopolizes the entire envelope, leaving no room for sub-agents or synthesis. Phase budgets localize failures and enable graceful degradation at each boundary.

### Decision 5: Dual-mode orchestration

A single configuration value controls whether gateway decisions are binding or advisory:

```python
# In settings / config
orchestration_mode: Literal["enforced", "autonomous"] = "enforced"
```

**Enforced mode** (production default):

- When `strategy ∈ {HYBRID, DECOMPOSE}`, the runtime enters the expansion controller
- The LLM generates plan content only; it does not decide whether to expand
- Strategy mismatch rate should be near-zero

**Autonomous mode** (research mode):

- When `strategy ∈ {HYBRID, DECOMPOSE}`, the runtime sets `ctx.expansion_strategy` as advice
- The LLM retains full agency over whether to expand (current behavior)
- Strategy mismatch rate is a measured research metric

**Implementation shape:** Single branching point in `executor.py` (~line 745):

```python
if gw.decomposition.strategy in (HYBRID, DECOMPOSE):
    if settings.orchestration_mode == "enforced":
        return await expansion_controller.execute(ctx, gw)
    else:  # "autonomous"
        ctx.expansion_strategy = gw.decomposition.strategy.value
```

**Research benefit:** Enables A/B comparison of enforced vs autonomous orchestration using the same evaluation harness. The strategy mismatch rate becomes a measurable delta between modes rather than a bug to eliminate.

**Trade-off:** Preserves the autonomous orchestration research avenue that pure enforcement would close. The cost is one branching point and two code paths to maintain — not a complexity thread through the application. Both paths converge at the synthesis step; the difference is only in whether expansion is mandatory or optional.

---

## Expansion Controller Architecture

### Target State Machine

```
                    ┌─────────────────────────────────┐
                    │         Gateway Output           │
                    │  strategy ∈ {HYBRID, DECOMPOSE}  │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │    orchestration_mode check      │
                    └──┬───────────────────────────┬──┘
                       │                           │
               enforced│                 autonomous│
                       │                           │
          ┌────────────▼────────────┐    ┌────────▼────────────┐
          │   ExpansionController   │    │  Current behavior   │
          │                         │    │  (LLM decides)      │
          └────────────┬────────────┘    └─────────────────────┘
                       │
          ┌────────────▼────────────┐
          │     LLM Planner Call    │
          │   (structured output)   │──── timeout ──┐
          └────────────┬────────────┘               │
                       │                            │
          ┌────────────▼────────────┐    ┌──────────▼──────────┐
          │    Plan Validation      │    │  Deterministic      │
          │  (schema conformance)   │    │  Fallback Planner   │
          └──┬──────────────────┬──┘    └──────────┬──────────┘
             │                  │                   │
        valid│          invalid │                   │
             │                  └───────────────────┘
             │                            │
          ┌──▼────────────────────────────▼────────┐
          │         Executor Dispatch              │
          │   spawn sub-agents in parallel         │
          │   per-worker + global timeout          │
          └────────────────────┬───────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Partial Aggregation │
                    │  (handle failures)   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │     Synthesis        │
                    │  (LLM composes       │
                    │   final response)    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Final Response    │
                    └─────────────────────┘
```

### Component Responsibilities

| Component | Owns | Does NOT own |
|-----------|------|-------------|
| Gateway (Stage 5) | Whether strategy is HYBRID/DECOMPOSE/SINGLE | How expansion executes |
| Expansion Controller | Enforcing expansion, managing phases, timeouts | Plan content generation |
| LLM Planner | Generating structured subtask plan | Whether expansion happens |
| Fallback Planner | Generating plan from prompt structure when LLM fails | Complex open-ended decomposition |
| Executor | Spawning sub-agents, collecting results | Choosing whether to spawn |
| Synthesizer (LLM) | Composing final response from sub-agent results | Nothing else |

### Code Location

New module: `src/personal_agent/orchestrator/expansion_controller.py`

Existing module changes:

| File | Change |
|------|--------|
| `orchestrator/executor.py` | Add mode branch at ~line 745; remove prompt-hint injection (lines 1084–1096); remove inline expansion hook (lines 1235–1290) |
| `orchestrator/expansion.py` | Unchanged — `execute_hybrid()` and `parse_decomposition_plan()` move into the controller's internal flow |
| `config/settings.py` | Add `orchestration_mode: Literal["enforced", "autonomous"]` |
| `orchestrator/types.py` | Add `ExpansionPlan`, `PlanTask` types |

---

## Telemetry

### New Events

The expansion controller must emit typed events at each phase boundary so every failure is localizable:

| Event | Fields | When |
|-------|--------|------|
| `planner_started` | `strategy`, `task_type` | Controller enters planner phase |
| `planner_completed` | `duration_ms`, `plan_task_count`, `parse_success`, `fallback_used` | Plan produced (by LLM or fallback) |
| `planner_failed` | `reason` (schema_validation_failed, timeout, empty_plan, malformed_json) | LLM planner output rejected |
| `fallback_planner_used` | `reason` (planner_timeout, schema_failure, empty_plan) | Deterministic fallback engaged |
| `expansion_dispatch_started` | `task_count` | Sub-agents about to spawn |
| `subagent_completed` | `task_name`, `duration_ms`, `status` (success, partial, timeout, failed) | Each sub-agent finishes |
| `synthesis_started` | `completed_subtasks`, `failed_subtasks` | Synthesis phase begins |
| `graceful_degradation_triggered` | `phase` (planner, executor, synthesis), `reason` | Any phase degrades |

### New Metrics

| Metric | Definition | Purpose |
|--------|-----------|---------|
| **Strategy mismatch rate** | % of HYBRID/DECOMPOSE requests with no expansion dispatch | Primary canary — should be near-zero in enforced mode |
| Planner fallback rate | % of expansion requests using deterministic fallback | Measures LLM planner reliability |
| Partial synthesis rate | % of expansions with at least one failed sub-agent | Measures sub-agent reliability |
| User-visible timeout rate | % of requests returning raw timeout to user | Should be zero with phase budgets |

### Assertion Layers

The evaluation harness should assert at four layers:

| Layer | Asserts | Example |
|-------|---------|---------|
| 1. Gateway correctness | Intent, complexity, strategy | `expansion_strategy == HYBRID` |
| 2. Workflow correctness | Planner invoked, plan valid, expansion started/completed | `planner_completed == true`, `expansion_dispatch_started == true` |
| 3. Answer correctness | Coverage, entities mentioned, recommendation produced | Response covers all comparison axes |
| 4. Efficiency | Latency budget compliance, timeout incidence | `user_visible_timeout == false` |

---

## Risks and Mitigations

### Risk 1: Over-enforcement creates unnecessary expansion

Forcing expansion on every HYBRID/DECOMPOSE request may produce worse results for simple queries that the gateway over-classified.

**Mitigation:**
- Cap task count (2–3 for HYBRID, 3–5 for DECOMPOSE)
- Allow `HYBRID` light decomposition (sub-agents can be minimal)
- Track cost/latency delta between enforced and autonomous modes
- If over-classification is significant, fix the gateway classifier — don't weaken enforcement

### Risk 2: Structured plan output fails frequently

The LLM may struggle to produce conformant JSON plans, making the fallback planner the de facto path.

**Mitigation:**
- Deterministic fallback planner as first-class path (not an error state)
- Schema validator with clear failure reasons for debugging
- Track planner schema failure rate — if consistently high, simplify the schema
- Planner retry once with constrained prompt before invoking fallback

### Risk 3: Dual-mode adds maintenance burden

Two code paths means two sets of behavior to test and reason about.

**Mitigation:**
- Single branching point (not distributed throughout codebase)
- Both paths converge at synthesis
- Autonomous mode is the *current* behavior — minimal new code
- If A/B comparison concludes enforced is clearly superior, remove autonomous mode in a future ADR

### Risk 4: Fallback planner scope creep

Temptation to make the deterministic fallback "smart enough" to handle all prompts.

**Mitigation:**
- Scope explicitly limited to enumerated comparisons (this ADR, Decision 3)
- Generic prompts get a simple 2-task split — deliberately minimal
- Log fallback planner usage to detect scope pressure
- If demand for general decomposition is high, invest in LLM planner quality instead

---

## Impact on Existing Components

### No changes required

- Pre-LLM Gateway (Stages 1–7) — the gateway is already correct
- Seshat memory system — unrelated
- Brainstem / homeostasis — unrelated
- Captain's Log / insights engine — unrelated
- MCP tool infrastructure — unrelated

### Changes required

| Component | Nature of change |
|-----------|-----------------|
| `orchestrator/executor.py` | Mode branch, remove inline expansion logic |
| `orchestrator/expansion.py` | Functions move into expansion controller's internal flow |
| `config/settings.py` | Add `orchestration_mode` setting |
| `orchestrator/types.py` | Add `ExpansionPlan`, `PlanTask` dataclasses |
| New: `orchestrator/expansion_controller.py` | Core component (~200–300 lines) |
| New: `orchestrator/fallback_planner.py` | Deterministic planner (~100–150 lines) |
| Evaluation harness | Revised assertions for CP-16, CP-17 (layer 2 workflow assertions) |

---

## What This ADR Does NOT Cover

These are related but separate decisions, each requiring their own ADR or implementation spec:

1. **Recall controller for CP-19** — Implicit memory recall (the "going back to the beginning" problem) is a classifier + retrieval gap, not an expansion gap. Separate remediation.
2. **Adversarial evaluation variants** — Paraphrased prompts for CP-16/17/19 to test lexical vs orchestration brittleness. Implementation detail, not architecture.
3. **SearXNG as expansion confound** — The addition of web search tools may affect the LLM's propensity to expand. Requires evaluation, not design.
4. **Temperature/sampling non-determinism** — Contributing factor to cross-run variance. Orthogonal to workflow enforcement.

---

## Implementation Priority

This component slots into Slice 3 implementation. Suggested sequencing:

| Order | Work | Rationale |
|-------|------|-----------|
| 1 | Add `strategy_mismatch_rate` metric | Instrument before changing control flow — baseline measurement |
| 2 | Add planner/fallback/expansion telemetry events | Make failure localization precise before changing code paths |
| 3 | Implement `ExpansionController` + fallback planner | Core architectural change |
| 4 | Add `orchestration_mode` config + mode branch | Dual-mode support |
| 5 | Revise CP-16/CP-17 evaluation assertions | Validate the fix with layer-2 workflow assertions |
| 6 | A/B compare enforced vs autonomous modes | Research data collection |

---

## References

- Evaluation Orchestration Analysis: `docs/research/evaluation-orchestration-analysis.md`
- Second Opinion Response (GPT-5.4): `docs/research/evaluation-run-04-second-opinion-response.md`
- Second Opinion Remediation Plan: `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md`
- Cognitive Architecture Redesign v2: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`
- Anthropic — Building Effective Agents: https://www.anthropic.com/research/building-effective-agents
- Anthropic — Effective Harnesses for Long-Running Agents: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
