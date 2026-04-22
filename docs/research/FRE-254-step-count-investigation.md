# FRE-254 — Step-Count Investigation: Interaction Latency Reduction

> Investigator: Sub-agent research pass  
> Date: 2026-04-22  
> Related issue: FRE-254  
> Context: p50 39.7s / p90 122.5s latency; 57-step interactions hit ~122s; target ≤15s p50 / ≤40s p90

---

> **Update 2026-04-22 (post-initial-draft):** Initial findings incorrectly attributed the single-tool-call-per-turn behaviour to a Qwen3.6 model capability gap. Corrected after user feedback: the model is **Qwen3.6-35B-A3B** (not Qwen3.5/Qwen3-35B-A3B), which natively supports parallel tool calls. The root cause is a **llama.cpp chat template bug** specific to Qwen3.x. Additionally, a separate investigation of the cloud path (Claude Sonnet 4.6) reveals a second shared root cause: the orchestrator's **serial tool execution loop** (`for tool_call in tool_calls:` at executor.py:1776) serialises all tool calls regardless of whether the model emits them in parallel. Both paths are affected.

## 1. Executive Summary

- **Local path root cause: llama.cpp chat template bug.** Qwen3.6-35B-A3B natively supports parallel tool calls (confirmed: vLLM/SGLang serve it with `--tool-call-parser qwen3_coder`; multiple tool calls in one turn is expected behaviour). The single-tool-call-per-turn observation is caused by llama.cpp's Qwen3.x grammar enforcement, which has known bugs with parallel call emission. Fix: update llama.cpp (QwenLM issue #1831 fixes 21 template bugs including parallel call interleaving; the `parallel_tool_calls: true` infinite loop bug in older llama.cpp is mitigated by the ToolLoopGate).
- **Cloud path root cause: orchestrator serial tool execution.** Claude Sonnet 4.6 correctly emits N tool calls in a single response, but `step_tool_execution()` at executor.py:1776 iterates with a `for` loop — no `asyncio.gather`. Three parallel tool calls emitted by Sonnet still run one-by-one. This is a shared bottleneck affecting both local and cloud paths and is the highest-priority code change.
- **Second shared problem: no prompt caching on the cloud path.** The full ~4,200-token tool payload + system prompt is re-sent to Anthropic on every turn. Anthropic supports prompt caching (`cache_control` headers via LiteLLM `extra_headers`). Adding it to `LiteLLMClient` would eliminate redundant input token costs and reduce Anthropic-side TTFT on cached prefixes.
- **The iteration ceiling is set at 25** (`orchestrator_max_tool_iterations`), already raised from 6. Complex analysis tasks can legitimately consume 15+ sequential calls, making the 57-step ceiling plausible for deep telemetry/analysis flows.
- **The total tool-description token budget is ~4,200–4,600 tokens** across 14 native tools in NORMAL mode. The `self_telemetry_query` description alone is ~400 tokens. Several descriptions are verbose and can be pruned.
- **TaskType-based tool filtering is wired in governance but not applied at dispatch.** For `conversational` intents (allowed_categories=[]), the full tool payload is still sent. Connecting the gateway's `allowed_tool_categories` to `get_tool_definitions_for_llm()` eliminates ~3,000–4,000 tokens on simple turns.
- **The ToolLoopGate (ADR-0062) is operational** and blocks identical-signature and consecutive-call loops, but does not limit total step count. It is a necessary safety net when `parallel_tool_calls: true` is enabled in llama.cpp.

---

## 2. Current State by Investigation Area

### 2.1 Prompt Compaction

**System prompt assembly location:** `src/personal_agent/orchestrator/executor.py:step_llm_call()` (lines 1232–1440)

The system prompt is **dynamically assembled per request** from up to four components:

| Component | Approximate tokens | Condition |
|-----------|-------------------|-----------|
| Deployment context (Docker/production) | ~110 | `environment == PRODUCTION` only |
| Memory section (broad recall / past conversations) | 50–500 | When `ctx.memory_context` is populated |
| Tool awareness header (`get_tool_awareness_prompt()`) | ~80–120 | When tools are passed |
| Tool use behavioral rules (`TOOL_USE_NATIVE_PROMPT`) | ~280 | When tools are passed |

Assembly order (executor.py:1416–1420):
```python
system_prompt = f"{tool_awareness}\n\n{system_prompt}\n\n{tool_prompt}"
```

**TOOL_USE_NATIVE_PROMPT** (prompts.py:53–60) is ~280 tokens and contains the shared `_TOOL_RULES` block (~230 tokens). The `_TOOL_RULES` block includes the parallel-call instruction that Qwen3 ignores, a web_search routing hint, and synthesis guidance.

**Tool awareness prompt** (`get_tool_awareness_prompt()`, prompts.py:97–180) generates a tool listing section (~80–120 tokens) and is cached for 60 seconds.

**Redundancies identified:**
- `_TOOL_RULES` references both `web_search` and `mcp_perplexity_ask` by name, but `mcp_perplexity_ask` is disabled. The reference is now stale.
- The parallel call instruction ("PARALLEL CALLS: When a task needs multiple independent tool calls...") has zero effect on Qwen3 (confirmed empirically — see `docs/research/parallel-tool-calls-model-comparison.md`).
- The tool awareness section lists all tools by category, then `TOOL_USE_NATIVE_PROMPT` contains tool usage behavioral rules. These two sections overlap in purpose.
- `get_tool_awareness_prompt()` repeats capability summaries (web search, Perplexity, etc.) that are already described in individual tool descriptions passed to the LLM.

**Total static system prompt (without memory, NORMAL mode, local):** approximately 360–420 tokens.

**Concrete reduction opportunities:**
1. Remove the stale `mcp_perplexity_ask` reference in `_TOOL_RULES` (prompts.py:49).
2. Collapse the tool awareness section into a 1-line header (e.g., "You are Seshat v0.1.0. All tools are listed below.") — saves ~80 tokens.
3. Trim the parallel-call instruction since it has no effect on Qwen3 — saves ~25 tokens.
4. Move synthesis guidance from `_TOOL_RULES` into only the forced-synthesis injection path.

### 2.2 Parallel Tool Batching

**Current support:** The orchestrator fully supports parallel tool calls. In `step_tool_execution()` (executor.py:1776), the code iterates over `tool_calls` (plural) and executes them, then extends `ctx.messages` with all results at once. Multiple tool calls per LLM response are natively handled.

**Does the LLM use it?** Not currently on the local path. `docs/research/parallel-tool-calls-model-comparison.md` (2026-04-17) observed Qwen3.6 emitting one tool call per turn. **This was initially misattributed to a model capability gap — it is actually a llama.cpp infrastructure bug.**

| Model | Tools per step (current) | Root cause | Fix |
|-------|--------------------------|-----------|-----|
| Qwen3.6-35B-A3B (local, llama.cpp) | always 1 | llama.cpp chat template bug for Qwen3.x | Update llama.cpp; enable `parallel_tool_calls: true` |
| Claude Sonnet 4.6 (cloud) | up to N (model emits correctly) | — | Already works at model level |

**Qwen3.6 native support confirmed:** Qwen/Qwen3.6-35B-A3B is served with `--tool-call-parser qwen3_coder` on vLLM and SGLang and correctly emits parallel tool calls. The model is capable. The blocking issue is llama.cpp's Jinja2 chat template for Qwen3.x:
- QwenLM issue #1831 ("21-fix chat template for Qwen 3.5") patches parallel tool call interleaving with a `\n\n` delimiter between blocks
- ggml-org/llama.cpp issue #22043: `parallel_tool_calls: true` caused an infinite reasoning loop in older llama.cpp builds — **the ToolLoopGate (ADR-0062) mitigates this** by blocking consecutive identical-signature calls
- ggml-org/llama.cpp issue #20164: tool calling breaks under long context with multiple optional parameters on Qwen3.5-35B

**What enabling parallel calls requires:**
1. Update llama.cpp to a build that includes the Qwen3.x template fixes (post-QwenLM/#1831)
2. Pass `parallel_tool_calls: true` in the `extra_body` of local LLM calls
3. Verify ToolLoopGate catches any runaway parallel call loops

**Critical gap that persists even after the llama.cpp fix:** The orchestrator's `step_tool_execution()` at executor.py:1776 runs tool calls in a `for` loop — no `asyncio.gather`. Even if Qwen3.6 emits 3 tool calls at once, they execute one-by-one in the orchestrator. The latency gain from parallel tool call generation is negated by serial execution. This affects **both the local and cloud paths** (see §2.6).

**What would fully realise the latency win:** (a) llama.cpp fix so the model emits N calls → AND (b) orchestrator executes them with `asyncio.gather` → AND (c) the tools themselves are I/O-bound (network, ES, Neo4j queries can all run concurrently).

### 2.3 Reasoning Step Elision

**`orchestrator_max_tool_iterations`:** Set to **25** (settings.py:114–123). The field docstring notes this was raised from 6 to 25 because "compound telemetry/analysis tasks can need 15+ sequential calls."

**Budget warning injection:** When `tool_iteration_count >= max - 2` (i.e., ≥ 23), a warning is injected into messages telling the model to prioritize synthesis (executor.py:1305–1323).

**Forced synthesis:** When `tool_iteration_count > max` (i.e., > 25), `ctx.force_synthesis_from_limit = True` is set and the model is re-called with tools disabled (executor.py:1696–1722, 1288–1302).

**Loop detection (ToolLoopGate — ADR-0062):** Implemented in `src/personal_agent/orchestrator/loop_gate.py`. Per-tool FSMs track:
- `loop_max_per_signature` (default 1): blocks same `(tool, args)` pair after N executions
- `loop_max_consecutive` (default 3): warns at N consecutive, blocks at N+1
- `loop_output_sensitive`: if True, skips output-identity blocking

When blocked, a gate result hint is returned to the model instead of executing the tool (executor.py:1833–1838). This effectively forces the model to synthesize from existing results.

**Circular reasoning detection:** There is no mechanism to detect when the model re-reads context it already has without calling a tool (i.e., pure reasoning steps that don't produce new information). The state machine only increments `tool_iteration_count` in `step_tool_execution`, so pure LLM turns between tool calls do not consume the budget counter directly — but they do consume one LLM inference round-trip each.

**Observation:** A 57-step interaction likely includes: (a) up to 25 tool iterations (sequential, one call per turn), each counting LLM call + tool execution as separate steps in `ctx.steps`, plus (b) intermediate assistant messages. The `ctx.steps` list appends both `llm_call` and `tool_call` step types, so 25 tool iterations = 25 LLM calls + 25 tool calls = 50 steps, plus init/synthesis = ~53+ entries. This explains 57-step interactions naturally.

### 2.4 Soft Step-Budget Cap

**Current state:** No explicit per-request step-budget hint is injected at conversation start. The existing mechanism only fires near the iteration limit (`max - 2`), which means the model has no incentive to be concise until step 23 of 25.

**Where to inject:** The system prompt assembly block in `step_llm_call()` (executor.py:1397–1440) or the `TOOL_USE_NATIVE_PROMPT` constant (prompts.py:53–60).

**Draft text for a soft step-budget hint** to add to `TOOL_USE_NATIVE_PROMPT`:

```
Step budget: Complete most requests in ≤ 6 tool calls. Prefer batching and synthesis over repeated narrow lookups. If you have enough information to answer, synthesize immediately.
```

This adds ~25 tokens but provides an early incentive to be efficient.

**Risk/quality tradeoff:** A tight budget (≤ 4) risks premature synthesis on genuinely complex tasks. A loose budget (≤ 10) keeps the current ceiling's protective effect. Recommended starting value: ≤ 6 for NORMAL mode tasks; ≤ 10 for analysis/tool_use TaskTypes. The hint is advisory for the model, not enforced — it works by calibrating model expectations rather than hard-stopping execution.

**Interaction with ToolLoopGate:** The gate blocks individual tool loops but does not limit total step count. The budget hint addresses total turn count, which the gate does not. These are complementary mechanisms.

### 2.5 Tool Description Pruning

**Native tools registered (NORMAL mode):** 14 tools  
Source: `register_mvp_tools()` in `src/personal_agent/tools/__init__.py`

| Tool | Category | Description length (approx tokens) | Notes |
|------|----------|-------------------------------------|-------|
| `read_file` | read_only | ~20 | Minimal — good |
| `list_directory` | read_only | ~90 | Includes per-param guidance; "NOT 'directory'" warning adds noise |
| `system_metrics_snapshot` | read_only | ~15 | Minimal — good |
| `search_memory` | memory | ~110 | Moderate; includes entity type examples |
| `self_telemetry_query` | read_only | ~400 | Very verbose; inline query type matrix, time formats, scoping rules |
| `web_search` | network | ~230 | Verbose; lists all categories + engine options; guidance on when to use |
| `query_elasticsearch` | network | ~130 | Includes 4-action enum and example queries |
| `perplexity_query` | network | ~130 | Includes 3-mode descriptions + routing guidance |
| `fetch_url` | network | ~80 | Clear and concise |
| `get_library_docs` | network | ~80 | Clear and concise |
| `run_sysdiag` | read_only | ~260 | Platform-specific usage examples (dynamic); verbose |
| `infra_health` | read_only | ~80 | Minimal — good |
| `create_linear_issue` | network | ~70 | Concise |
| `find_linear_issues` | network | ~60 | Concise |
| `list_linear_projects` | network | ~30 | Minimal |
| `create_linear_project` | network | ~30 | Minimal |

**Total approximate token cost of all tool descriptions (NORMAL mode, 14 tools): ~1,735 tokens**

Adding JSON schema (parameter names + types + descriptions) roughly doubles this to **~3,500–4,200 tokens** for the full tool definitions array sent to the LLM.

**Most verbose tools:**
1. `self_telemetry_query` — ~400 tokens for description alone. Contains an inline documentation table (query types, time formats, scoping), plus internal-use code examples that the LLM will never use. The code blocks in the docstring are NOT sent to the LLM but the `description` field (lines 104–129) is large.
2. `run_sysdiag` — ~260 tokens (platform-dynamic). The allowed-command list plus 8+ usage examples is useful but lengthy.
3. `web_search` — ~230 tokens. Lists all 8 SearXNG categories with descriptions, plus engine guidance, plus routing guidance vs. Perplexity.

**Tools infrequently needed that inflate all-mode descriptions:**
- `create_linear_issue`, `create_linear_project`: NORMAL mode only; appropriate.
- `search_memory`: Only useful for memory recall tasks. Currently sent to all modes where memory is registered. Since `allowed_modes` is `["memory"]` (its category), it depends on mode policy intersection.
- `system_metrics_snapshot`: Minimal tokens; keep.

**Pruning recommendations:**

| Tool | Action | Token saving |
|------|--------|-------------|
| `self_telemetry_query` | Remove inline code examples from description; compress time-window docs to 1 line | ~150 tokens |
| `run_sysdiag` | Trim to 3–4 key usage patterns; remove macOS/Linux variants from description (model doesn't need both) | ~100 tokens |
| `web_search` | Remove the per-category descriptions; keep just "categories: general, it, science, news, weather" | ~80 tokens |
| `list_directory` | Remove the "NOT 'directory'" warning — it's a workaround for a model that may no longer misbehave | ~10 tokens |

**Total estimated saving from description pruning: ~340 tokens**

**TaskType-based tool filtering (already partially implemented via FRE-252):**

The governance stage (`src/personal_agent/request_gateway/governance.py`) already computes allowed tool categories per TaskType via `task_type_policies` in `config/governance/tools.yaml`. The orchestrator calls `_tool_registry.get_tool_definitions_for_llm(mode=ctx.mode)` — but this filters by mode only, not by the TaskType-intersected allowed categories from the gateway.

This means for a `conversational` intent (allowed_categories: []) the model still receives all native tool definitions. Wiring `GatewayOutput.governance.allowed_tool_categories` into `get_tool_definitions_for_llm()` would enable per-TaskType tool filtering, potentially removing 8–12 tools from the LLM payload on simple conversational turns.

**Estimated token saving from TaskType filtering on conversational intents: ~3,000–3,500 tokens** (nearly the full tool payload).

### 2.6 Cloud Path — Claude Sonnet 4.6 Latency Analysis

**Cloud profile:** `config/profiles/cloud.yaml` — primary: `claude_sonnet`, sub_agent: `claude_haiku`. Sonnet is served via `LiteLLMClient` → `litellm.acompletion()` → Anthropic API.

**Why Sonnet still has high latency despite supporting parallel tool calls:**

| Factor | Impact | Source |
|--------|--------|--------|
| Serial tool execution in orchestrator | HIGH — same bottleneck as local path | executor.py:1776 `for tool_call in tool_calls:` |
| Network RTT × turns | HIGH — each LLM turn adds ~200–600ms Anthropic API round-trip | `litellm_request_start` log events |
| No prompt caching | MEDIUM — full system prompt + 4,200 token tool payload re-sent per turn | `LiteLLMClient` has no `cache_control` headers |
| Anthropic TTFT on long prompts | MEDIUM — uncached 4,200-token tool payload increases time-to-first-token | Anthropic pricing: cache miss billed at full input rate |
| Thinking tokens (if enabled) | MEDIUM — `thinking_budget_tokens` not set for Sonnet; extended thinking could add latency | `config/models.yaml` sonnet entry |

**Serial execution is the dominant issue on the cloud path:** Sonnet correctly emits 3 tool calls in one response (observed in `docs/research/parallel-tool-calls-model-comparison.md`). The orchestrator executes them sequentially — no `asyncio.gather`. For 3 I/O-bound tool calls (e.g., ES query + Neo4j query + web search), sequential execution adds ~2–5 seconds vs. ~0.5–1s concurrent. At 15+ tool calls per session, this compounds significantly.

**No prompt caching on the cloud path:** `LiteLLMClient.respond()` builds `litellm_kwargs` without `extra_headers`. Anthropic's prompt caching requires `{"anthropic-beta": "prompt-caching-2024-07-31"}` plus `cache_control` markers on system message and tool definitions. Currently missing — every turn pays full input token cost for the static system prompt and tool list. Anthropic charges ~$0.30/MTok for cache writes vs. ~$3.00/MTok for cache misses. At 14 tools × ~300 tokens each, every uncached turn wastes ~4,200 tokens × ($3.00/MTok - $0.30/MTok) ≈ $0.0113 per turn in avoidable input cost.

**Anthropic network RTT:** Each LLM turn to the Anthropic API adds ~200–600ms network round-trip from the VPS. At 15 turns per session, this alone adds 3–9 seconds. Not controllable directly, but reducing turn count (via parallel execution + earlier synthesis) directly reduces accumulated RTT.

**Mitigation available now:** The ToolLoopGate (ADR-0062) already prevents repetitive tool loops on the cloud path, reducing worst-case turn counts.

---

## 3. Recommendations (Ranked by Expected Impact)

### Rank 0 — Parallelise tool execution with `asyncio.gather` (CRITICAL impact, MEDIUM effort, affects BOTH paths)

**File:** `src/personal_agent/orchestrator/executor.py`, `step_tool_execution()` (line 1776)  
**Change:** Replace the sequential `for tool_call in tool_calls:` loop with `asyncio.gather(*[_execute_single_tool(tc) for tc in tool_calls])`. Factor out the per-tool logic (argument parsing, loop gate check, tool dispatch, result building) into a private `_execute_single_tool()` coroutine. Tool results can be gathered concurrently because native tools are I/O-bound (network, ES, Neo4j) and independent.

**Expected improvement:** For N concurrent tool calls in one LLM turn: N × tool_latency → max(tool_latencies). For 3 independent lookups averaging 500ms each: 1,500ms → 500ms. Applied to the cloud path (Sonnet already emits N parallel calls), this improvement is available today without any model changes. Applied to the local path once llama.cpp is fixed, the full stack benefit is realised.

**Risk:** Medium. The ToolLoopGate checks (`check_before`, `record_output`) are per-tool-call and update shared FSM state — must ensure the gate remains thread-safe / coroutine-safe (asyncio is single-threaded, so no true concurrency; gather is fine). Tool result ordering must be preserved (matched to tool_call_id). JSON parsing errors per tool should not cancel sibling coroutines (`return_exceptions=True` or individual try/except per coroutine). History sanitiser runs before dispatch so no conflict there.

---

### Rank 1 — Update llama.cpp and enable parallel tool calls for Qwen3.6 (CRITICAL for local path, HIGH effort)

**Files:** SLM server (separate repo) + `src/personal_agent/llm_client/client.py` (extra_body)  
**Change:**
1. Update llama.cpp to a build that includes QwenLM/#1831 Qwen3.x chat template fixes (parallel call interleaving fixed with `\n\n` delimiter).
2. Pass `extra_body={"parallel_tool_calls": true}` in local LLM calls (add to `LocalLLMClient.respond()` kwargs when `supports_function_calling=True`).
3. Verify ToolLoopGate catches any runaway parallel loops (ggml-org/#22043 infinite loop risk — ToolLoopGate's `loop_max_consecutive` default of 3 already blocks this).
4. Test with a 3-tool parallel prompt to confirm N tool calls in one response.

**Expected improvement:** For a 3-tool parallel task on local: 3 sequential LLM turns → 1 turn. Combined with Rank 0 (parallel execution), wall-clock reduction is ~60–70% for parallelisable tasks. p50 target of ≤ 15s becomes achievable on local path.

**Risk:** Medium. llama.cpp update may introduce regressions. Validate on a dev build before deploying. The `parallel_tool_calls: true` bug in older llama.cpp is the infinite-loop risk; confirm the build version fixes it before enabling the flag.

---

### Rank 2 — Add prompt caching to `LiteLLMClient` for the cloud path (HIGH impact, LOW effort)

**File:** `src/personal_agent/llm_client/litellm_client.py`, `respond()` method  
**Change:** Add Anthropic prompt caching headers and `cache_control` markers:

```python
# In litellm_kwargs construction:
if self.provider == "anthropic":
    litellm_kwargs["extra_headers"] = {
        "anthropic-beta": "prompt-caching-2024-07-31"
    }
    # Mark system message and tool list as cacheable (prepend cache_control)
    # LiteLLM passes cache_control through to the Anthropic API
```

The system message and tool definitions array are static per-request and ideal cache targets. Anthropic caches prefixes ≥ 1,024 tokens — the 4,200-token tool payload qualifies.

**Expected improvement:** On subsequent turns within a session, the system prompt + tool definitions are served from cache (~$0.30/MTok vs. $3.00/MTok). TTFT (time-to-first-token) drops because Anthropic doesn't re-process the cached prefix. At 15 turns/session × 4,200 tokens: ~14 cache hits × 4,200 tokens × $2.70/MTok saving ≈ $0.16/session saved. Latency benefit: cached TTFT is ~30–50% lower on Anthropic's infrastructure.

**Risk:** Low. Prompt caching is a well-supported Anthropic feature. LiteLLM passes `extra_headers` through transparently. Must verify that `cache_control` placement in tool definitions doesn't alter tool call behaviour.

---

### Rank 3 — Wire TaskType-based tool filtering into the tool payload (HIGH impact, MEDIUM effort)

**File:** `src/personal_agent/orchestrator/executor.py` (step_llm_call, ~line 1330)  
**Change:** Pass `allowed_categories=ctx.gateway_output.governance.allowed_tool_categories` to `get_tool_definitions_for_llm()`. Implement category filtering in `ToolRegistry.get_tool_definitions_for_llm()`.

**Expected improvement:** For `conversational` intents (allowed_categories=[]), tool payload drops from ~4,200 tokens to 0. Eliminates per-turn tool-selection overhead on simple queries.

**Risk:** Low. The governance policy already defines per-TaskType allowed categories. ToolLoopGate and mode-based filtering remain intact.

---

### Rank 4 — Add a soft step-budget hint to the system prompt (MEDIUM impact, LOW effort)

**File:** `src/personal_agent/orchestrator/prompts.py`, `_TOOL_RULES` block (line 39)  
**Change:** Add: `"Step budget: Complete most requests in ≤ 6 tool calls. Prefer synthesizing with gathered data over additional lookups."` Override to ≤ 10 for analysis/tool_use TaskTypes.

**Expected improvement:** Calibrates model expectation at request start; reduces unnecessary exploratory calls before the iteration limit warning fires at step 23.

**Risk:** Medium. Too tight (≤ 4) risks premature synthesis. Start at ≤ 6 and tune.

---

### Rank 5 — Prune verbose tool descriptions (MEDIUM impact, LOW effort)

**Files:**
- `src/personal_agent/tools/self_telemetry.py` (lines 104–129): Remove inline code examples; compress query-type matrix to one-line list. Save ~150 tokens.
- `src/personal_agent/tools/sysdiag.py` (`_build_description()`, lines 79–114): Cut to ≤ 4 usage patterns. Save ~100 tokens.
- `src/personal_agent/tools/web.py` (lines 22–35): Remove per-category descriptions. Save ~80 tokens.
- `src/personal_agent/orchestrator/prompts.py` (line 49): Remove stale `mcp_perplexity_ask` reference. Save ~15 tokens.

**Expected improvement:** ~340 fewer tokens per call (~8% reduction in tool payload).  
**Risk:** Near-zero.

---

### Rank 6 — Lower `orchestrator_max_tool_iterations` per TaskType (MEDIUM impact, LOW effort)

**File:** `src/personal_agent/config/settings.py` (line 114)  
**Change:** Reduce from 25 to 12 for conversational/knowledge TaskTypes; keep 25 for analysis/tool_use. Wire override via gateway output.

**Expected improvement:** Forces earlier synthesis on tasks that overrun. Reduces worst-case p90 ceiling.  
**Risk:** Medium. Needs evaluation data before setting thresholds. Implement after Rank 3 (TaskType tool filtering).

---

## 4. Implementation Roadmap

Ordered by: immediate impact, then path (cloud quick wins before local infrastructure change).

| Step | Change | Paths | Files | Expected impact |
|------|--------|-------|-------|-----------------|
| 1a | Add prompt caching headers to `LiteLLMClient` | Cloud | `llm_client/litellm_client.py` | −30–50% TTFT on Sonnet; cost saving |
| 1b | Prune verbose tool descriptions | Both | `tools/self_telemetry.py`, `tools/sysdiag.py`, `tools/web.py`, `orchestrator/prompts.py` | −340 tokens/call |
| 2 | Inject soft step-budget hint into `_TOOL_RULES` | Both | `orchestrator/prompts.py` | −2 to −5 steps on routine queries |
| 3 | Parallelise tool execution with `asyncio.gather` | Both | `orchestrator/executor.py:1776` | N×tool_latency → max(tool_latencies); biggest win on cloud today |
| 4 | Wire TaskType → `allowed_tool_categories` into tool payload | Both | `orchestrator/executor.py`, `tools/registry.py` | −3,000–4,000 tokens on conversational intents |
| 5 | Update llama.cpp + enable `parallel_tool_calls: true` + test Qwen3.6 | Local | SLM server (separate repo), `llm_client/client.py` | −60–70% steps on parallelisable tasks |
| 6 | Per-TaskType `max_tool_iterations` override | Both | `config/settings.py`, `orchestrator/executor.py` | Reduces worst-case p90 ceiling |

Steps 1a, 1b, 2 are mechanical (< 1 hour each, no new logic). Step 3 is the highest-code-complexity change (factor out `_execute_single_tool` coroutine, validate gate thread-safety). Step 5 is infrastructure — requires updating the SLM server separately and testing Qwen3.6 parallel call emission before enabling in production.

---

## 5. Metrics to Track Before/After Each Change

| Change | Metric | Tool |
|--------|--------|------|
| All changes | p50/p90 latency per TaskType | `self_telemetry_query(query_type='performance', window='24h')` |
| Tool description pruning | Prompt token count per call | `ctx.steps` `prompt_tokens` field in TaskCapture |
| Step-budget hint | `ctx.steps` length distribution | Captain's Log `steps` array in JSONL captures |
| TaskType filtering | Tool definitions array size per intent | Add log in `step_llm_call` before LLM call |
| Lower iteration ceiling | `tool_iteration_limit_reached` event count | `self_telemetry_query(query_type='events', event='tool_iteration_limit_reached')` |
| Model swap | `step_executed` tool_count per step | Elasticsearch: `FROM agent-logs-* | WHERE event='step_executed' | STATS avg(tool_count)` |

---

## 6. Key File References

| File | Line(s) | Relevance |
|------|---------|-----------|
| `src/personal_agent/config/settings.py` | 114–123 | `orchestrator_max_tool_iterations = 25` |
| `src/personal_agent/orchestrator/executor.py` | 1776 | **Serial tool execution loop** — `for tool_call in tool_calls:` (target for asyncio.gather; affects both paths) |
| `src/personal_agent/orchestrator/executor.py` | 1288–1323 | Budget warning + forced synthesis injection |
| `src/personal_agent/orchestrator/executor.py` | 1330–1352 | Tool payload assembly (`get_tool_definitions_for_llm`) |
| `src/personal_agent/llm_client/litellm_client.py` | 170–188 | Cloud LLM call kwargs — no `cache_control`/prompt caching headers |
| `src/personal_agent/orchestrator/executor.py` | 1397–1440 | System prompt assembly order |
| `src/personal_agent/orchestrator/executor.py` | 1696–1722 | Iteration limit check → `force_synthesis_from_limit` |
| `src/personal_agent/orchestrator/prompts.py` | 39–50 | `_TOOL_RULES` shared behavioral block |
| `src/personal_agent/orchestrator/prompts.py` | 53–60 | `TOOL_USE_NATIVE_PROMPT` |
| `src/personal_agent/orchestrator/prompts.py` | 97–180 | `get_tool_awareness_prompt()` |
| `src/personal_agent/orchestrator/loop_gate.py` | 54–60 | `ToolLoopPolicy` defaults |
| `src/personal_agent/tools/__init__.py` | 104–123 | `register_mvp_tools()` — 14 native tools |
| `src/personal_agent/tools/registry.py` | 99–142 | `get_tool_definitions_for_llm()` — filters by mode only |
| `src/personal_agent/tools/self_telemetry.py` | 104–129 | Largest single tool description (~400 tokens) |
| `config/governance/tools.yaml` | 1–20 | `task_type_policies` — per-TaskType allowed categories |
| `docs/research/parallel-tool-calls-model-comparison.md` | all | Confirmed: Qwen3 ignores parallel call instruction |

---

## 7. ToolLoopGate Interaction with Step Reduction

The ToolLoopGate (ADR-0062, `loop_gate.py`) is the existing circuit-breaker for individual tool loops. It operates at the tool level (same args → block; same output → block; N consecutive → warn/block) but does not constrain total step count across different tools.

The gate complements step-reduction recommendations rather than conflicting with them:
- Rank 2 (step-budget hint) addresses the total step ceiling across all tools.
- ToolLoopGate addresses repeated calls to the same tool with the same arguments.
- Rank 1 (TaskType filtering) reduces which tools are offered, making the gate's surface area smaller.

If the step-budget hint is added, the gate's consecutive limit (`loop_max_consecutive = 3` default) should be reviewed. For a 6-step budget, allowing 3 consecutive calls to one tool may consume half the budget on a single tool. Consider tightening `loop_max_consecutive = 2` globally when the soft budget is tightened.
