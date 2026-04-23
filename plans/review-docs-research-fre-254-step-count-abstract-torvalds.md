# Plan: Apply In-Code Corrections from FRE-254 Step-Count Investigation

> Source research: `docs/research/FRE-254-step-count-investigation.md`
> Out of scope: llama.cpp update + Qwen3.6 `parallel_tool_calls: true` enablement (Rank 1) — requires SLM-server access the user does not currently have.

---

## 1. Context

Interaction latency is dominated by step count, not model speed (p50 39.7 s, p90 122.5 s; 57-step interactions hit ~122 s). The FRE-254 investigation identified two independent root causes:

- **Local path** — llama.cpp Qwen3.x chat-template bug suppresses parallel tool calls. *Not actionable here* (no SLM-server access).
- **Cloud path + shared** — orchestrator's serial `for tool_call in tool_calls:` at `executor.py:1776` executes tool calls one-by-one even when Sonnet emits them in parallel; no Anthropic prompt caching; no TaskType-based tool filtering; verbose tool descriptions; iteration budget calibration.

This plan applies every correction we can make in our own codebase — Ranks 0, 2, 3, 4, 5, 6 from the investigation — in **one PR**, plus stale-doc cleanup and Linear consolidation. The end-to-end goal is to move p50 toward ≤ 15 s and p90 toward ≤ 40 s on the cloud path today, and unlock the local-path improvement once the SLM server is updated separately.

---

## 2. Code Corrections (single PR, ordered for review)

### Rank 5 — Prune verbose tool descriptions and stale prompt strings (mechanical)

**Files:**
- `src/personal_agent/tools/self_telemetry.py` (description field, ~lines 104–129) — strip the inline query-type matrix and per-time-format examples; keep a 1-line description per query type. Target: ~150 fewer tokens.
- `src/personal_agent/tools/sysdiag.py` (`_build_description()`, ~lines 79–114) — collapse to ≤ 4 usage patterns; drop the macOS-vs-Linux duplication. Target: ~100 fewer tokens.
- `src/personal_agent/tools/web.py` (description, ~lines 22–35) — replace per-category descriptions with a single "categories: general, it, science, news, weather" line; remove engine-selection prose. Target: ~80 fewer tokens.
- `src/personal_agent/orchestrator/prompts.py` (`_TOOL_RULES`, ~line 49) — remove the stale `mcp_perplexity_ask` reference (the MCP Perplexity tool is disabled; the native `perplexity_query` is the only Perplexity tool now).

**Keep** the `PARALLEL CALLS:` instruction in `_TOOL_RULES` — it is honoured by Sonnet (proven in trace `675cdbb7`) and benefits the cloud path. Once Rank 0 (asyncio.gather) lands, Sonnet's parallel emission converts directly into wall-clock reduction.

**Test:** Adjust the prompts assertions in `tests/personal_agent/orchestrator/test_prompts.py` (or wherever `_TOOL_RULES` content is asserted) for the removed `mcp_perplexity_ask` line. Run `make test-k K=tool_descriptions` plus a focused `make test-file FILE=tests/personal_agent/tools/test_self_telemetry.py` if a description assertion exists.

---

### Rank 4 — Soft step-budget hint in `_TOOL_RULES` (trivial)

**File:** `src/personal_agent/orchestrator/prompts.py`, `_TOOL_RULES` block

**Add:** A new bullet to `_TOOL_RULES`:

> "Step budget: Complete most requests in ≤ 6 tool calls. Prefer synthesizing with gathered data over additional lookups. If you have enough information to answer, synthesize immediately."

**Why ≤ 6 (not ≤ 4):** The investigation flagged premature-synthesis risk on analysis tasks if too tight. Rank 6 below provides a per-TaskType override (≤ 10) for `analysis` / `tool_use` so the hint stays advisory.

**ToolLoopGate alignment:** The investigation's §7 recommends tightening `loop_max_consecutive` from 3 → 2 when the soft budget is 6, so a single tool can't burn half the budget. Apply this in `src/personal_agent/orchestrator/loop_gate.py` `ToolLoopPolicy` defaults.

**Test:** Update `tests/personal_agent/orchestrator/test_prompts.py` assertions to include the new line. Update any `loop_gate` policy default tests.

---

### Rank 0 — Parallelise tool execution with `asyncio.gather` (the headline change)

**File:** `src/personal_agent/orchestrator/executor.py`, `step_tool_execution()` (around line 1776)

**Change shape — three-phase pattern that preserves ToolLoopGate semantics:**

1. **Phase 1 (sequential gate-check):** Iterate `tool_calls` in order; for each, call `loop_gate.check_before(tool_name, args)`. Split into `allowed_calls` and `blocked_results` (where `blocked_results` carry the gate-result hint already used at executor.py:1833–1838). This must be sequential because the FSM mutates `consecutive_count` / `signature_count` on every check, and concurrent checks would let two callers both pass the threshold.
2. **Phase 2 (parallel dispatch):** Factor the existing per-tool body (argument parsing, dispatch via `_tool_registry.dispatch()`, result formatting) into a private `_execute_single_tool(tool_call) -> ToolResult` coroutine. Call `asyncio.gather(*[_execute_single_tool(tc) for tc in allowed_calls], return_exceptions=True)`. Per-coroutine try/except so one failure does not poison siblings; exceptions become per-call error results, not the whole batch.
3. **Phase 3 (sequential outcome record + result assembly):** Iterate `(tc, dispatched_result)` pairs in original order; call `loop_gate.record_output(tool_name, args, result)` once per call. Then merge `blocked_results + dispatched_results` back into a list ordered to match `tool_calls` (so each tool result lines up with its `tool_call_id`).

**Why this is gate-safe:** asyncio is single-threaded, but coroutine interleaving at `await` points can still violate gate invariants if check/record happen concurrently. Phases 1 and 3 are sequential; only the I/O-bound dispatch in Phase 2 runs concurrently — that's where the real wall-clock win is (network, Elasticsearch, Neo4j, Perplexity, web_search are all I/O-bound).

**Logging:** Emit one structured `tools_dispatched_parallel` log per turn with `count`, `blocked_count`, `max_latency_ms`, `total_serial_equivalent_ms` so we can measure the win in Kibana.

**Tests:**
- `tests/personal_agent/orchestrator/test_executor_parallel_tools.py` (new): assert that with 3 mocked tool-call awaits each sleeping 100 ms, total wall-clock is ≤ 150 ms (vs ~300 ms serial); assert tool_call_id ordering is preserved; assert one tool raising does not cancel siblings; assert `loop_gate.check_before` is called sequentially with the original order; assert blocked calls return the gate hint and skip dispatch.
- Existing `step_tool_execution` tests: verify they still pass with the new pattern (no semantic change for single-tool turns).

---

### Rank 2 — Anthropic prompt caching in `LiteLLMClient` (cloud path)

**File:** `src/personal_agent/llm_client/litellm_client.py`, `respond()` (litellm_kwargs construction, ~lines 170–188)

**Change:**

```python
# After litellm_kwargs is built, before await litellm.acompletion(...):
if self._is_anthropic_provider():
    litellm_kwargs.setdefault("extra_headers", {})[
        "anthropic-beta"
    ] = "prompt-caching-2024-07-31"
    # Mark system message as a cache breakpoint
    _apply_anthropic_cache_control(litellm_kwargs["messages"], litellm_kwargs.get("tools"))
```

`_apply_anthropic_cache_control()` (new helper, same module):
- If `messages[0].role == "system"`, attach `cache_control={"type": "ephemeral"}` to the system-message content (LiteLLM forwards Anthropic's content-block format when present; confirm via LiteLLM's Anthropic adapter docs and a small spike in the dev container).
- If `tools` is non-empty, attach `cache_control={"type": "ephemeral"}` to the **last** tool definition — Anthropic caches the whole prefix up to the marked block.

**Provider detection:** Add `_is_anthropic_provider()` returning `True` when `self.model` (e.g. `"anthropic/claude-sonnet-4-6"`) starts with `"anthropic/"` or when the resolved profile maps to an Anthropic provider. Reuse existing `provider` field on the model entry if present.

**Test:**
- `tests/personal_agent/llm_client/test_litellm_client_caching.py` (new): mock `litellm.acompletion`; assert that for an Anthropic model, the first call has `extra_headers["anthropic-beta"] == "prompt-caching-2024-07-31"` and a `cache_control` marker on the system message and last tool; assert that for a non-Anthropic model, no headers/markers are added.
- Integration smoke (manual, not in unit run): one Sonnet call against the live API verifying `usage.cache_read_input_tokens` is non-zero on the second turn.

**Risk mitigation:** Caching is Anthropic-only; gate strictly on provider detection. If LiteLLM's pass-through of `cache_control` regresses for tool definitions, fall back to caching only the system message (still ~280 tokens of saving per turn).

---

### Rank 3 — Wire `GovernanceContext.allowed_tool_categories` into tool dispatch

**Background:** FRE-252 (Done) already computes `GovernanceContext.allowed_tool_categories` as the intersection of per-TaskType policy (`config/governance/tools.yaml` `task_type_policies`) and per-Mode policy. Verified: `governance.py:50–94` populates this field correctly. The remaining gap is that `executor.py:1331` calls `_tool_registry.get_tool_definitions_for_llm(mode=ctx.mode)` — only mode is passed; categories are dropped.

**Files:**
- `src/personal_agent/tools/registry.py`, `get_tool_definitions_for_llm()` (~lines 99–142): add an `allowed_categories: Sequence[str] | None = None` keyword parameter. When non-`None`, intersect the per-tool `category` field against the allowlist after the existing mode filter. When `None`, behaviour is unchanged.
- `src/personal_agent/orchestrator/executor.py`, `step_llm_call()` (~line 1330): pass `allowed_categories=ctx.gateway_output.governance.allowed_tool_categories` (or whichever attribute path holds the GovernanceContext on the executor's `ctx`) to the registry call.
- **Edge case — empty list vs None:** `conversational` resolves to `allowed_categories: []` (no tools). The registry must distinguish "no filter" (`None`) from "explicitly empty" (`[]` → return zero tool definitions). When zero tools are returned, `step_llm_call()` should also drop `tools` from the LLM payload entirely (otherwise some providers reject `tools=[]`). Add this guard right after the registry call.

**Tests:**
- `tests/personal_agent/tools/test_registry_category_filter.py` (new): assert `allowed_categories=None` returns the full mode-filtered set; `allowed_categories=["read_only"]` returns only read-only tools; `allowed_categories=[]` returns `[]`.
- `tests/personal_agent/orchestrator/test_executor_tool_filter.py` (new): assert that for a `conversational` `TaskType`, the LLM call kwargs do not include `tools`; for `tool_use`, all NORMAL-mode tools appear.

---

### Rank 6 — Per-TaskType `max_tool_iterations` override

**Files:**
- `src/personal_agent/config/settings.py` (around line 114): keep `orchestrator_max_tool_iterations: int = 25` as the global ceiling; add a sibling `orchestrator_max_tool_iterations_by_task_type: dict[str, int] = {"conversational": 6, "memory_recall": 8, "analysis": 25, "planning": 25, "tool_use": 25, "delegation": 25, "self_improve": 25}` (or default to `12` for unspecified types). Pydantic-settings supports dict fields; verify env-var format.
- `src/personal_agent/orchestrator/executor.py`, around the warning (~lines 1306–1308) and forced-synthesis check (~line 1696): replace `settings.orchestrator_max_tool_iterations` with a helper `_resolve_max_iterations(ctx) -> int` that looks up `ctx.gateway_output.intent.task_type` in the per-type map, falling back to the global ceiling.

**Why after Rank 3:** Lower limits only make sense once tool filtering ensures the model isn't drowning in irrelevant tools. With both in place, conversational turns will bypass tools entirely and never approach the limit; analysis turns retain 25.

**Tests:**
- `tests/personal_agent/orchestrator/test_executor_iteration_limits.py` (new): assert that the warning fires at the per-TaskType `max - 2` boundary; assert that forced synthesis triggers at the per-type cap.

---

## 3. Stale Research Doc Cleanup

**Delete** `docs/research/parallel-tool-calls-model-comparison.md`. Its "Root Cause" section attributes parallel-call suppression to a Qwen capability gap; the FRE-254 investigation establishes that root cause is the llama.cpp Qwen3.x template bug. Keeping a doc with an incorrect root-cause attribution is more harmful than the historical-trace value of the file. Anyone needing the full story has FRE-254's investigation report.

---

## 4. Linear Consolidation

### Update FRE-232 — re-scope to post-llama.cpp validation

Update the description with the corrected finding (model is capable; llama.cpp Qwen3.x chat-template bug is the root cause; investigation lives at `docs/research/FRE-254-step-count-investigation.md` §2.2). Re-scope the acceptance criteria:

> **Acceptance:** Once llama.cpp on the SLM server is updated to a build including QwenLM/#1831 Qwen3.x template fixes, run a 3-tool parallel test (e.g. `infra_health` + `self_telemetry_query` + `search_memory` in one prompt) against Qwen3.6-35B-A3B and confirm `tool_count=3` in a single `step_executed` event. If confirmed, enable `parallel_tool_calls: true` in `extra_body` for `LocalLLMClient.respond()` calls and verify ToolLoopGate's `loop_max_consecutive` blocks any runaway loops (ggml-org/#22043).

Keep `Approved` status; add link to FRE-254. Do **not** spawn a separate testing ticket — this is the testing ticket.

### Add cross-link comment on FRE-223

FRE-223 ("tool use broken with Sonnet") is a separate bug (schema/contract mismatch) but the Rank 0 (asyncio.gather) and Rank 2 (prompt caching) work touches the Sonnet path. Add one comment on FRE-223:

> Cross-ref: FRE-254 follow-up PR adds Anthropic prompt caching headers and parallelises tool execution in `executor.py`. If the underlying tool-use breakage is contract/schema-related (path 1 in this issue), those changes won't fix it; if it surfaces as serial-execution timeouts, the asyncio.gather change may mask it. Re-test FRE-223 reproduction after the FRE-254 PR merges.

### Issue tracking for the PR itself

Create one parent Linear issue ("Apply FRE-254 in-code corrections") with the description summarising Ranks 0/2/3/4/5/6 and a link to this plan. Status `Needs Approval`, label `PersonalAgent`, label `Tier-2:Sonnet`. Move to `Approved` per workflow before implementation begins.

---

## 5. ToolLoopGate Interaction Summary

The gate is designed for the serial loop today and is **not coroutine-safe** under `asyncio.gather` (per-tool `ToolFSM` mutation has no locks). Rank 0's three-phase pattern keeps gate calls sequential (Phase 1 + Phase 3) so no locking is needed. Two further alignments:

- Tighten `loop_max_consecutive` from 3 → 2 in `ToolLoopPolicy` defaults to fit the new ≤ 6 step budget (Rank 4).
- The ggml-org/#22043 infinite-loop risk that motivated keeping `parallel_tool_calls: false` on local is *still* mitigated by the gate once we eventually flip the flag (FRE-232 follow-up).

---

## 6. Verification (VPS / cloud-sim stack)

We are working directly on the VPS. The agent runs as the `cloud-sim-seshat-gateway` container built from `docker-compose.cloud.yml`. All validation flows through container rebuild → container logs → Kibana, not `make dev`.

**Step 1 — Static checks (host, no container needed):**

```bash
uv run mypy src/
uv run ruff check src/
uv run ruff format src/
make test                # unit suite — pre-commit hook serialises pytest
```

**Step 2 — Rebuild and restart the gateway container:**

```bash
docker compose -f docker-compose.cloud.yml build seshat-gateway
docker compose -f docker-compose.cloud.yml up -d seshat-gateway
docker compose -f docker-compose.cloud.yml logs -f --tail=200 seshat-gateway
```

Watch the boot logs for `governance_evaluated allowed_tool_categories=...` (Rank 3 wiring), `parallel_tool_calls` startup banners (none expected — local path unchanged), and any tracebacks from the new `_apply_anthropic_cache_control` / `_execute_single_tool` paths.

**Step 3 — Drive traffic through the live stack:**

The PWA + Caddy + Cloudflare tunnel front the gateway. Send the verification prompts via the chat UI (or `curl` against the gateway's `/chat` endpoint inside the cloud-sim network — see `docker-compose.cloud.yml` for the gateway port) using the Sonnet profile:

1. **Parallel-execution win (Rank 0):** "Run infra_health AND check my last 5 errors AND search memory for SkyExpress. Batch the tool calls." Single turn. In Kibana, find the `tools_dispatched_parallel` event for this trace; assert `count == 3` and `total_serial_equivalent_ms / max_latency_ms > 1.5`.
2. **Prompt caching (Rank 2):** Send a second message in the same session. In the same trace's `litellm_request_complete` log, assert `usage.cache_read_input_tokens > 0`. (Cache write happens on the first turn; cache hit on the second.)
3. **TaskType filtering (Rank 3):** Start a fresh session and send "Hi, how's it going?" — `conversational` intent. In the `step_llm_call` log, assert `tool_count == 0` (or `tools` field absent) and `prompt_tokens` is ~3,500 lower than a NORMAL-mode tool-eligible request.
4. **Iteration ceiling (Rank 6):** Send a deliberate analysis prompt that fans out to many sequential lookups; verify the warning fires at the per-TaskType `max - 2` (25 - 2 = 23 for analysis).

**Step 4 — Local-path smoke (Qwen via llama.cpp on the SLM host):**

No parallel-emission behaviour change expected today — llama.cpp template bug still suppresses it. Confirm the Rank 4 + Rank 5 + Rank 6 changes do not regress local Qwen by replaying one routine prompt against the local profile and checking the `step_executed` event count is at-or-below the pre-change baseline.

**Step 5 — Rollback path:**

If a regression appears in container logs, `docker compose -f docker-compose.cloud.yml up -d --no-build seshat-gateway` after `git revert <commit>` restores the prior image tag from the local docker daemon (the previous build is still cached). Capture the failing trace ID before reverting so the regression is debuggable from Kibana.

---

## 7. Critical File Index

| Path | Why |
|------|-----|
| `src/personal_agent/orchestrator/executor.py` | `step_tool_execution()` — Rank 0 refactor; `step_llm_call()` — Rank 3 wiring + Rank 6 iteration helper |
| `src/personal_agent/orchestrator/prompts.py` | `_TOOL_RULES` — Rank 4 budget hint + Rank 5 stale-line removal |
| `src/personal_agent/orchestrator/loop_gate.py` | `ToolLoopPolicy` — tighten `loop_max_consecutive` to 2 |
| `src/personal_agent/llm_client/litellm_client.py` | `respond()` — Rank 2 Anthropic cache headers + cache_control markers |
| `src/personal_agent/tools/registry.py` | `get_tool_definitions_for_llm()` — Rank 3 `allowed_categories` parameter |
| `src/personal_agent/tools/self_telemetry.py` | Rank 5 description trim |
| `src/personal_agent/tools/sysdiag.py` | Rank 5 description trim |
| `src/personal_agent/tools/web.py` | Rank 5 description trim |
| `src/personal_agent/config/settings.py` | Rank 6 per-TaskType iteration map |
| `src/personal_agent/request_gateway/governance.py` | Reference only — already produces `allowed_tool_categories` (FRE-252) |
| `config/governance/tools.yaml` | Reference only — `task_type_policies` already in place |
| `docs/research/parallel-tool-calls-model-comparison.md` | DELETE — superseded by FRE-254 investigation |
| `docs/research/FRE-254-step-count-investigation.md` | Source of truth for this plan |

---

## 8. Out of Scope

- Updating llama.cpp on the SLM server (Rank 1) — user has no current access. Tracked by re-scoped FRE-232.
- Enabling `parallel_tool_calls: true` in `LocalLLMClient` — depends on llama.cpp update + FRE-232 verification.
- Fixing FRE-223 (Sonnet tool-use schema bug) — separate triage; cross-link comment added so it gets re-tested after this PR.
