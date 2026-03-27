# Evaluation Run-04 — Second-Opinion Brief

> **Purpose:** This document is a structured brief for an external model review. It contains full context on the remaining test failures after four evaluation runs of the Personal Agent system — including failing assertions, architectural detail, design philosophy, and open questions. The goal is to brainstorm root causes and potential fixes.
>
> **Date:** 2026-03-27
> **Run name:** `run-04-fixes-and-searchxng`

---

## 1. System Overview

**Personal Agent** is a biologically-inspired cognitive architecture research project. It is a Python/FastAPI service running locally on Apple Silicon, wrapping a small local LLM (Qwen3.5-35B-A3B, MLX-quantized) with a deterministic pre-processing pipeline, memory systems, and a sub-agent expansion layer.

### Cognitive Architecture (abbreviated)

```
Client → Pre-LLM Gateway (7 deterministic stages)
              ↓
         Primary Agent (Qwen3.5-35B)
              ↓
    ┌─────────┬────────────┬──────────────┐
    │  Tools  │   Memory   │  Sub-Agents  │
    │ (MCP +  │  (Seshat   │  (ephemeral, │
    │ native) │  + Neo4j)  │  task-scoped)│
    └─────────┴────────────┴──────────────┘
              ↓
         Brainstem (homeostasis, scheduling)
```

### Pre-LLM Gateway — 7 Stages (all deterministic, no LLM calls)

| # | Stage | Purpose | Key Output |
|---|-------|---------|------------|
| 1 | Security | Rate limit, sanitize, PII detect | ALLOW / REJECT |
| 2 | Session | Load/create session, hydrate history | `SessionContext` |
| 3 | Governance | Check brainstem mode, resource constraints | `GovernanceContext` |
| 4 | Intent Classification | Regex + heuristics → task type | `IntentResult` |
| 5 | Decomposition Assessment | Decide SINGLE/HYBRID/DECOMPOSE/DELEGATE | `DecompositionResult` |
| 6 | Context Assembly | Messages, memory, skills, tools | `AssembledContext` |
| 7 | Context Budget | Trim to token window | `BudgetedContext` |

### Intent Types (Stage 4)

Classified via ordered regex patterns (first match wins):

```
MEMORY_RECALL    — "what have I", "do you remember", "recall our", etc.
SELF_IMPROVE     — "improve your own architecture", "captain's log"
DELEGATION       — coding tasks, "use Claude Code", "implement/write/build X"
PLANNING         — "plan/outline/roadmap/decompose", "break this into"
ANALYSIS         — "analyze/research/investigate/evaluate", "trade-offs"
TOOL_USE         — "search/find", "list/show/display", "read/open file"
CONVERSATIONAL   — default (no special pattern matched)
```

Complexity is estimated heuristically from word count, question count, action verb count, and task-type bias:
- `SIMPLE` — short, single question/action
- `MODERATE` — 40+ words or multiple questions
- `COMPLEX` — 3+ questions or 3+ action verbs in ANALYSIS/PLANNING context

### Decomposition Strategy Matrix (Stage 5)

```
CONVERSATIONAL / MEMORY_RECALL / SELF_IMPROVE / TOOL_USE  →  SINGLE
DELEGATION                                                  →  DELEGATE
ANALYSIS + SIMPLE                                           →  SINGLE
ANALYSIS + MODERATE                                         →  HYBRID
ANALYSIS + COMPLEX                                          →  DECOMPOSE
PLANNING + SIMPLE                                           →  SINGLE
PLANNING + MODERATE+                                        →  HYBRID

Override: if expansion_budget <= 0 OR expansion_permitted == False → SINGLE
```

### Expansion Architecture

When strategy is HYBRID or DECOMPOSE:

1. `step_init()` sets `ctx.expansion_strategy = "hybrid"|"decompose"` and logs `step_init_expansion_flagged`
2. Primary agent generates a numbered decomposition plan
3. `parse_decomposition_plan()` extracts numbered items → `list[SubAgentSpec]` (max 3)
4. `execute_hybrid(specs, trace_id)` is called:
   - Emits `hybrid_expansion_start` event with `sub_agent_count`, `max_concurrent`
   - Runs sub-agents concurrently under an `asyncio.Semaphore`
   - Emits `hybrid_expansion_complete` event with `total`, `successes`, `failures`
5. Sub-agent results (`SubAgentResult.summary`, ≤500 tokens each) are injected into primary agent context for synthesis

**Key principle:** The Gateway decides *whether* to expand (HYBRID/DECOMPOSE flag). The primary agent decides *how* (decomposition plan content). This separation means: correct gateway classification does **not** guarantee expansion will execute.

### Governance Modes That Disable Expansion

```python
_EXPANSION_DISABLED_MODES = frozenset({
    Mode.ALERT, Mode.DEGRADED, Mode.LOCKDOWN, Mode.RECOVERY
})
```

Normal operation uses `Mode.NORMAL` with `expansion_budget = 3`.

---

## 2. Evaluation Harness — Overview

The harness runs 25 "critical paths" (CP-01 through CP-25) across 7 categories. Each path is a multi-turn conversation session. Assertions check:

- Telemetry events emitted to Elasticsearch (event presence/absence, field values)
- Intent classification correctness (`task_type`, `confidence`)
- Decomposition strategy correctness (`strategy`, `complexity`)
- Tool call execution (`tool_call_completed` event presence)
- Expansion lifecycle events (`hybrid_expansion_start`, `hybrid_expansion_complete`)

Human-eval quality criteria (qualitative, not automated) are listed per path but not counted in the pass rate.

### Overall Progression Across 4 Runs

| Run | Context | Paths | Assertions | Pass Rate | Avg Latency |
|-----|---------|-------|------------|-----------|-------------|
| Baseline | First instrumented run | 22/25 | 118/127 | 92.9% | 33.6s |
| Run-02 | Subagent fix attempt (introduced regression) | 7/25 | 78/127 | 61.4% | 23.4s |
| Run-03 | Three targeted fixes | 20/25 | 111/127 | 87.4% | 37.3s |
| **Run-04** | **Further fixes + SearXNG integration** | **22/25** | **119/127** | **93.7%** | **33.6s** |

Run-04 is the **best assertion pass rate across all runs** and returns to baseline latency.

### What Run-04 Fixed (vs Baseline)

- **CP-10 (DECOMPOSE Strategy — Complex Multi-Part Analysis)**: Now passing. The complex decompose path correctly fires expansion for a 3-system, multi-dimension comparison prompt.
- **CP-11 (Complexity Escalation Across Turns)**: Now passing. Per-turn complexity re-assessment works correctly — simple turn → SINGLE, moderate turn → HYBRID, follow-up → SINGLE.
- **CP-09 (HYBRID Strategy — Moderate Analysis)**: Was passing at baseline, failed in run-03, restored in run-04.

### Remaining Failures (3 paths, 8 assertions)

| Path | Category | Assertions Failed | Consistent? |
|------|----------|-------------------|-------------|
| CP-16 | Expansion & Sub-Agents | 4/9 | Intermittent — passed baseline + run-03, failed run-04 |
| CP-17 | Expansion & Sub-Agents | 3/6 | Persistent since run-03 (passed only at baseline) |
| CP-19 | Context Management | 1/2 | **Persistent across all 4 runs** |

---

## 3. Failing Path Deep-Dives

---

### CP-16: HYBRID Synthesis Quality

**Category:** Expansion & Sub-Agents
**Status across runs:** ✅ Baseline → ✅ Run-03 → ❌ Run-04

#### Test Objective

This path verifies that when a MODERATE ANALYSIS request arrives, the system:
1. Correctly classifies it as `analysis` / `moderate` / `hybrid`
2. **Actually executes sub-agent expansion** (not just classifies toward it)
3. Sub-agents complete successfully
4. The final response is a coherent synthesis (not three stitched answers)

The distinction being tested: *classification correctness alone is not enough* — the expansion must execute and emit observable lifecycle events.

#### The Prompt

**Turn 1:** `"Research microservices communication patterns and evaluate the trade-offs between synchronous HTTP, asynchronous messaging, and gRPC."`

**Turn 2:** `"Which pattern would you recommend for a system with both low-latency and high-throughput requirements?"`

#### Failing Assertions (Turn 1)

| Assertion | Expected | Actual |
|-----------|----------|--------|
| `intent_classified.task_type` | `analysis` | `analysis` ✅ |
| `decomposition_assessed.complexity` | `moderate` | `moderate` ✅ |
| `decomposition_assessed.strategy` | `hybrid` | `hybrid` ✅ |
| Event `hybrid_expansion_start` present | present | **NOT FOUND** ❌ |
| `hybrid_expansion_start.sub_agent_count >= 1` | ≥ 1 | **N/A (event absent)** ❌ |
| Event `hybrid_expansion_complete` present | present | **NOT FOUND** ❌ |
| `hybrid_expansion_complete.successes >= 1` | ≥ 1 | **N/A (event absent)** ❌ |

#### Actual Response (Run-04, Turn 1)

The agent produced a complete, detailed analysis of all three communication patterns with pros/cons tables — **without using sub-agents**. The response took **28,987ms** (vs 162,736ms at baseline when sub-agents ran). The quality is acceptable as a single-pass response, but the expansion mechanism did not engage.

#### Baseline Comparison

At baseline, this same prompt produced:
- `hybrid_expansion_start` with `sub_agent_count: 3`
- `hybrid_expansion_complete` with `successes: 3`
- Response time: **162,736ms** (sub-agents ran in parallel, synthesis happened)

#### Key Observation

Gateway is doing its job (classifying HYBRID correctly). The expansion mechanism is not executing even though it should be. The agent is answering the question well directly — so the LLM is "choosing" to answer rather than decompose.

---

### CP-17: Sub-Agent Concurrency

**Category:** Expansion & Sub-Agents
**Status across runs:** ✅ Baseline → ❌ Run-03 → ❌ Run-04 (persistent since run-03)

#### Test Objective

This path verifies that a COMPLEX ANALYSIS request with explicitly named sub-domains:
1. Triggers `DECOMPOSE` strategy (not just `HYBRID`)
2. Actually spawns multiple sub-agents **concurrently**
3. Sub-agents complete (successes ≥ 2 of 3)
4. The final response covers all three sub-domains with appropriate depth

The DECOMPOSE path is conceptually stronger than HYBRID: the primary agent is instructed to fully decompose the problem before solving it, not just parallelize sub-analyses.

#### The Prompt

**Turn 1:** `"Compare the performance characteristics of Redis, Memcached, and Hazelcast for distributed caching. Analyze their memory management approaches and evaluate operational complexity. Recommend which fits our workload of ten thousand requests per second."`

_(Single-turn path — no Turn 2)_

#### Failing Assertions

| Assertion | Expected | Actual |
|-----------|----------|--------|
| `intent_classified.task_type` | `analysis` | `analysis` ✅ |
| `decomposition_assessed.complexity` | `complex` | `complex` ✅ |
| `decomposition_assessed.strategy` | `decompose` | `decompose` ✅ |
| Event `hybrid_expansion_start` present | present | **NOT FOUND** ❌ |
| `hybrid_expansion_start.sub_agent_count >= 2` | ≥ 2 | **N/A (event absent)** ❌ |
| `hybrid_expansion_complete.successes >= 2` | ≥ 2 | **N/A (event absent)** ❌ |

#### Actual Response (Run-04)

```
Error: The request took too long to process. Please try again with a simpler
request. (Debug: LLMTimeout: Request to http:[path] timed out after 180.0s)
```

Response time: **187,101ms** — the request timed out entirely. This is the only path in run-04 that results in a timeout error rather than a degraded-but-successful single-pass answer.

#### Baseline Comparison

At baseline:
- `hybrid_expansion_start` with `sub_agent_count: 3` (or ≥ 2)
- `hybrid_expansion_complete` with `successes: 3`
- Response time: **272,428ms** — long but successful

#### Key Observations

1. This path times out while CP-16 (similar mechanism) quietly falls back to single-pass. Different failure modes for what should be the same code path.
2. The timeout at 187s suggests the primary agent attempted something expensive (maybe tried to answer the full DECOMPOSE question in a single pass at the 35B model context limit) rather than spawning sub-agents.
3. At baseline (272s) this path succeeded with sub-agents, suggesting the longer wall-clock time there was sub-agents running in parallel, not a single blocked thread.

---

### CP-19: Long Conversation Trimming

**Category:** Context Management
**Status across runs:** ❌ Baseline → ❌ Run-03 → ❌ Run-04 (**never passed**)

#### Test Objective

This path tests two distinct properties of a 10-turn conversation:

1. **Context window management:** As the conversation grows, the system should trim intelligently — preserving foundational facts from early turns while allowing mid-conversation details to be de-prioritized.
2. **Intent classification in late turns:** When a user asks a "going back to the beginning" retrospective question after 9 turns of other content, the system should recognize this as `memory_recall`, not `conversational`.

The path is specifically designed to stress-test whether the gateway's regex-based classifier handles **implicit** retrospective reference — phrasing that doesn't use canonical recall phrases ("do you remember", "what did I say") but clearly requests retrieval of prior context.

#### The Conversation (10 turns)

| Turn | Message | Category |
|------|---------|----------|
| 1 | "Let's talk about our system architecture. We use a microservices pattern with FastAPI..." | Foundation |
| 2 | "Our primary database is PostgreSQL for transactional data." | Foundation |
| 3 | "We also use Elasticsearch for logging and Neo4j for our knowledge graph." | Foundation |
| 4 | "The deployment is on Docker Compose locally and Kubernetes in production." | Foundation |
| 5 | "We've been having issues with service discovery between containers." | Problem |
| 6 | "I tried using Consul but it added too much operational overhead." | Continuation |
| 7 | "We're now evaluating DNS-based service discovery versus Envoy sidecar proxies." | Continuation |
| 8 | "The team is leaning toward Envoy because it also handles load balancing." | Continuation |
| 9 | "But I'm worried about the memory overhead of running Envoy sidecars on every service." | Continuation |
| 10 | **"Going back to the beginning — what was our primary database again?"** | **Recall** |

#### Failing Assertion (Turn 10)

| Assertion | Expected | Actual |
|-----------|----------|--------|
| `intent_classified.task_type` | `memory_recall` | `conversational` ❌ |

#### Actual Response (Run-04, Turn 10)

```
I don't have information about what primary database we were using. This conversation
focused on service discovery, Envoy sidecar proxies, and load balancing options.

Was there a database discussed in an earlier conversation or project documentation
that I should reference? If you can share more context about your stack, I can help
identify the right database approach for your architecture.
```

This response reveals **two compounded failures:**
1. The classifier misidentifies intent as `conversational`
2. The agent claims not to know the database — yet PostgreSQL was established in Turn 2 of the same session, and is visible in the conversation history (the history is not trimmed at turn 10, with only 10 turns)

This means the content failure (wrong answer about PostgreSQL) is **not a trimming artifact** — the information is present in context. The agent is failing to synthesize backward from recent Envoy discussion to retrieve an earlier-established fact.

#### Why This Matters Architecturally

The intent classifier runs on the raw user message text only — it does not have access to conversation history. So `"Going back to the beginning — what was our primary database again?"` must be classified purely on its lexical content. The current `MEMORY_RECALL_PATTERNS` regex targets explicit recall phrases. This message uses:
- Temporal framing ("going back to the beginning")
- Episodic framing ("what was our...")
- A question about a specific established fact

None of these match the existing regex patterns for `MEMORY_RECALL`.

---

## 4. Cross-Cutting Observations

### The Gateway → Agent Gap (CP-16 and CP-17)

The architecture explicitly separates "whether to expand" (gateway) from "how to expand" (primary agent). This is philosophically sound — the 35B model is better positioned than a regex to design sub-task decomposition plans. But it creates a **silent failure mode**: the gateway can correctly flag HYBRID/DECOMPOSE, yet the primary agent can choose to answer directly without spawning sub-agents, and this choice is invisible in telemetry until the `hybrid_expansion_start` event is absent.

The current system has no mechanism to:
- Detect that the agent chose not to expand when it was supposed to
- Enforce expansion (e.g., a structured prompt or tool-call requirement)
- Alert on "strategy mismatch" between gateway output and actual execution path

### Expansion Flakiness Pattern

Across all 4 runs, the same prompts sometimes trigger expansion and sometimes don't:

| Run | CP-09 (HYBRID) | CP-10 (DECOMPOSE) | CP-16 (HYBRID) | CP-17 (DECOMPOSE) |
|-----|---------------|-------------------|----------------|-------------------|
| Baseline | ✅ expanded | ❌ no expansion | ✅ expanded | ✅ expanded |
| Run-02 | ❌ | ❌ | ❌ | ❌ |
| Run-03 | ❌ | ❌ | ✅ expanded | ❌ no expansion |
| Run-04 | ✅ expanded | ✅ expanded | ❌ no expansion | ❌ timeout |

The pattern is non-deterministic. Same prompt, same model, same gateway output — different expansion behavior. This strongly suggests the decision to expand lives in LLM sampling, not in deterministic code.

### CP-19: A Classifier Gap, Not a Memory Gap

The content failure (agent doesn't recall PostgreSQL) is a consequence of the classification failure. If the agent were prompted for `memory_recall`, the gateway would assemble a different context — potentially including session history highlights or Seshat memory results. Under `conversational`, the agent receives no special recall scaffolding.

This means fixing the classifier likely fixes the content response too, without any changes to the memory system.

---

## 5. Open Questions for Review

### Q1 — CP-16 / CP-17: Where should expansion enforcement live?

The gateway outputs a `DecompositionStrategy` enum. The primary agent currently reads this as context in its system prompt ("you have been asked to expand this task into sub-problems"). Should expansion be:

- **(A) Enforced via system prompt instruction** — stronger directive language, possibly including a required structured output format before proceeding
- **(B) Enforced via a tool call** — require the model to call a `spawn_sub_agents(tasks: list[str])` tool rather than choosing to decompose or not, making expansion an explicit, observable action
- **(C) Enforced via executor gate** — if `ctx.expansion_strategy` is set and `hybrid_expansion_start` is not observed within N seconds, abort and retry with a stronger prompt
- **(D) Accepted as non-deterministic** — tolerate expansion flakiness at the agent decision layer, rely on output quality (human eval) rather than telemetry events

What are the trade-offs? Are there precedents from other agent frameworks for enforcing structured sub-task decomposition?

### Q2 — CP-17: Timeout under DECOMPOSE

CP-17 times out at 187s (180s LLM timeout). CP-16 (similar mechanism, HYBRID) answers in 28s via single-pass fallback. Why might DECOMPOSE cause a timeout where HYBRID produces a quiet fallback?

Hypotheses:
- DECOMPOSE prompt instructs deeper pre-planning before answering, causing the model to generate a very long response that exceeds the timeout
- DECOMPOSE uses a different execution path with different timeout handling
- The three-system comparison prompt (Redis/Memcached/Hazelcast) with explicit performance/memory/complexity dimensions creates a qualitatively different context window than the microservices pattern prompt

What architectural patterns prevent LLM timeouts from surfacing as user-visible errors vs. graceful degradation?

### Q3 — CP-19: Intent classification of implicit recall

The phrase `"Going back to the beginning — what was our primary database again?"` fails to match `MEMORY_RECALL` patterns. The current patterns are anchored on explicit recall phrases ("do you remember", "what did I say", "recall our").

The classifier runs on the raw message only — no conversation history access. Options:

- **(A) Extend the regex** — add patterns for implicit backward-referencing ("going back", "what was our X again", "earlier you said", "what did we decide on X")
- **(B) Hybrid classifier** — for messages containing a question about a specific noun + "again" or temporal framing, boost toward `memory_recall`
- **(C) Classifier uses conversation history** — allow Stage 4 to peek at the last N messages as context when classifying ambiguous turns (breaks the "pure regex" principle but improves accuracy)
- **(D) Post-hoc reclassification** — after the agent produces a response that says "I don't know" / requests more context, reclassify and retry with memory recall scaffolding

What is the right trade-off between deterministic/fast regex classification and accuracy for implicit recall patterns? Should the classifier be allowed to use context?

### Q4 — Broader: Telemetry as a proxy for quality

The harness uses event presence (`hybrid_expansion_start` found) as a proxy for "expansion happened." But CP-16 run-04 shows that a HYBRID-classified request can produce a quality response without expanding (28s, detailed analysis).

Is `hybrid_expansion_start` presence the right assertion? Should we instead assert on:
- Response length / depth (proxy for quality)
- Coverage of all N named items in the prompt
- Explicit assertion that sub-agent results appear in the ES trace

Or is telemetry-based assertion the right layer and the issue is purely that expansion should be mandatory when the gateway says HYBRID?

---

## 6. Summary Table of Failures

| Path | Failing Assertions | Root Cause Hypothesis | Consistency |
|------|-------------------|-----------------------|-------------|
| CP-16 | `hybrid_expansion_start` absent, `hybrid_expansion_complete` absent | LLM chose to answer directly despite HYBRID flag; gateway-agent gap | Intermittent (2/4 runs fail) |
| CP-17 | `hybrid_expansion_start` absent, `hybrid_expansion_complete` absent + LLM timeout | DECOMPOSE prompt triggers expensive single-pass attempt rather than sub-agent dispatch; timeout instead of graceful fallback | Persistent (failed 2/3 non-broken runs) |
| CP-19 | `intent_classified.task_type` expected `memory_recall`, got `conversational` | Implicit backward-reference phrasing not matched by current MEMORY_RECALL regex patterns | Persistent (failed all 4 runs) |

---

*Document prepared for external model review. All implementation details reflect the codebase as of 2026-03-27.*
