# FRE-480 — Tool-using discovery sub-agent (`_run_tooled_loop`) via shared dispatch boundary

**Ticket:** FRE-480 (Approved, Tier-1:Opus) · parent FRE-476 · **Implements ADR-0086 D3+D4**
**ADR:** `docs/architecture_decisions/ADR-0086-hybrid-decompose-routing-for-artifact-builds.md`
**Blocked-by (shipped):** FRE-479 (Phase 1 gateway routing — `artifact_decomposition_enabled` flag exists, off by default)
**Blocks:** FRE-481 (Phase 3 — telemetry, joinability, A/B, rollout gate)

---

## 1. Problem (one paragraph)

`_run_tooled_loop` (`orchestrator/sub_agent.py:170-223`) is a **stub**: it makes one LLM call and returns the text (`sub_agent.py:212` `TODO: Parse tool calls…`). `TOOLED_SEQUENTIAL` discovery sub-agents therefore execute **no tools**. Two further gaps make the stub unreachable anyway: (a) the planner JSON validator (`expansion_controller.py:_validate_plan_json`) never parses `mode`/`tools`, so every `PlanTask` defaults to `PARALLEL_INFERENCE` with empty tools; (b) the planner prompt never asks for tool-using discovery slices. This phase makes the loop real, routes its tool calls through the **same** dispatch+governance path the primary executor uses ("one dispatch path, two callers"), and teaches the planner to emit read-only discovery slices — all inert while `artifact_decomposition_enabled=False`.

## 2. Scope (what changes)

| File | Change |
|---|---|
| `orchestrator/tool_dispatch.py` **(new)** | Shared per-call dispatcher `dispatch_tool_call(...)` — the existing `_dispatch_tool_call` body, parameterized on primitives (`trace_id`, `session_id`, `loaded_skills`) instead of `ExecutionContext`, with `gate_result`/`loop_policy`/`args_hash` optional. Identical return-dict contract. |
| `orchestrator/executor.py` | Delete the in-file `_dispatch_tool_call`; import `dispatch_tool_call` from `tool_dispatch`; update the `asyncio.gather` call site (`~3415`) to pass primitives. No behavioural change to the primary loop. |
| `orchestrator/sub_agent.py` | Real `_run_tooled_loop`: resolve read-only tool defs → bounded loop (`settings.sub_agent_max_tool_iterations`) → parse `tool_calls` → reject mutating tools → `dispatch_tool_call` → append results → re-prompt → forced final synthesis at ceiling. Return `(content, tools_used)`. `run_sub_agent` wires `tools_used` + digest summary. |
| `orchestrator/expansion_controller.py` | `_validate_plan_json` parses `mode` + `tools` (read-only enforced, flag-gated) → `PlanTask`. `_PLANNER_SYSTEM_PROMPT` extended (flag-gated) to emit `tooled_sequential` discovery slices with read-only `tools`. |
| `config/settings.py` | New `sub_agent_max_tool_iterations: int` (default 5, ge=1, le=15) + `sub_agent_summary_max_chars: int` (default 8000) — generous, no premature clamp (§3h). |

**Not in scope:** generation sectioning (FRE-478), telemetry/joinability/A-B (FRE-481), the D1/D2 gateway matrix (FRE-479, shipped).

## 3. Key design decisions

### 3a. Shared dispatch boundary — new module, no circular import
`executor` imports `expansion_controller` **lazily** (`executor.py:1722`); `expansion_controller` imports `sub_agent` at top. So `sub_agent` must **not** top-level-import `executor`. Solution: house the shared dispatcher in a new leaf module `orchestrator/tool_dispatch.py` that imports only `tools` (`ToolExecutionLayer`, `get_default_registry`), `loop_gate` (`GateResult`, `ToolLoopPolicy`, `stable_hash`), `skills.find_skills_for_tool`, `config.settings`, `structlog`. Both `executor` and `sub_agent` import it. This is the literal "one dispatch path, two callers" the ADR mandates.

`dispatch_tool_call` signature (keyword-only):
```python
async def dispatch_tool_call(
    *, tool_call_id: str, tool_name: str, arguments: dict[str, Any],
    tool_layer: ToolExecutionLayer, trace_ctx: TraceContext,
    trace_id: str, session_id: str | None, loaded_skills: set[str],
    args_hash: str = "", gate_result: GateResult | None = None,
    loop_policy: ToolLoopPolicy | None = None,
) -> dict[str, Any]: ...
```
Return dict is byte-identical to today's (`tool_call_id, tool_name, content, success, latency_ms, output_hash, gate_result, args_hash, loop_policy, tool_layer_output, tool_layer_error, terminal, terminal_reason, terminal_next_step`). Primary passes its real gate_result/loop_policy/args_hash + `ctx.trace_id/ctx.session_id/ctx.loaded_skills`. Sub-agent passes `gate_result=None, loop_policy=None` and its own primitives. `execute_tool` (which performs ADR-0063 permission + action-boundary governance and ADR-0074 trace threading) is unchanged — both callers inherit it.

### 3b. Read-only enforcement (mutating-tool rejection) — **OPEN DECISION #1** — *revised per codex*
A category denylist `{system_write, artifact_write}` is **unsound**: codex confirmed `run_python` is also `system_dangerous` (tools.yaml:432) and would slip through, and network-mutating tools are categorized `network`. Category alone cannot prove a tool is read-only. **Decision: name allowlist.** Define `_DISCOVERY_TOOL_ALLOWLIST = frozenset({"bash","read","read_skill","web_search","recall_personal_history"})` (the read-only discovery surface the ADR names). The sub-agent loop dispatches a tool **only if** its name is in `spec.tools` AND in the allowlist; anything else → a `rejected` tool-result message, never dispatched. `bash` stays in (the ADR's primary discovery tool) but is acknowledged as an argument-level risk (shell separators/redirects can mutate) — it runs through the **same** `execute_tool` action-boundary governance as the primary, no weaker path.

**Owner decision (2026-06-05): include `bash`.** Owner note — terminology: `bash` is the tool *name*, `system_dangerous` is its *category*; the allowlist gates by name. The static allowlist is explicitly a **placeholder**: the intended end-state is a **dynamic, HITL-gated allow** where a human-in-the-loop approval boundary (via the Agent) authorizes each dangerous-category call per-invocation, rather than a hardcoded set. That HITL gate is **not yet built**; allow `bash` statically for now and let this evolve when that feature lands. → Follow-up ticket: (1) HITL dynamic allow-gate for discovery sub-agents (supersedes the static allowlist); (2) interim `discovery_safe` self-describing tool-metadata flag so the surface isn't a hardcoded list. Both Needs Approval.

### 3c. Flag inertness — **OPEN DECISION #2** — *codex condition folded in*
`HYBRID` is already reachable today for complex ANALYSIS/PLANNING turns, independent of `artifact_decomposition_enabled` (executor.py:1715 reaches `ExpansionController.execute`). If the planner unconditionally emitted `tooled_sequential` slices, those existing HYBRID turns would start running tool loops **while the flag is off** — violating ADR-0086's "neither changes prod behavior until Phase 3." Gate **both**: (a) the planner-prompt augmentation, and (b) `_validate_plan_json`'s acceptance of `mode`/`tools`, on `settings.artifact_decomposition_enabled`. Flag off ⇒ planner prompt unchanged + `_validate_plan_json` emits **neither** a non-default `mode` **nor** `tools` ⇒ `run_sub_agent` only enters tooled mode when **both** are set (sub_agent.py:89), so the guard holds. **Codex condition (explicit):** the new prompt text must be built **per-call** (local string), never by mutating the module-level `_PLANNER_SYSTEM_PROMPT` constant — otherwise flag-on once leaks into all later calls.

### 3d. Ceiling behaviour
On hitting `sub_agent_max_tool_iterations`, do one final `respond` with `tool_choice="none"` (no tools) so the model **synthesizes a digest** from gathered results instead of returning empty — mirrors the primary's `force_synthesis_from_limit`.

### 3e. tool_calls wire-format normalization + sub-agent-owned assembly — *new, per codex*
Codex flagged a real shape mismatch: `LLMResponse.tool_calls` items are `{id, name, arguments}` (types.py `ToolCall`), but `dispatch_tool_call` (extracted from `step_tool_execution`) and the assistant-message transcript expect OpenAI wire format `{id, type, function:{name, arguments}}` (executor bridges via `_build_assistant_tool_calls`, executor.py:456). The sub-agent loop therefore:
- reads `tc["name"]` / `tc["arguments"]` directly off the `LLMResponse.tool_calls` items (no `function` nesting) when calling `dispatch_tool_call`;
- builds its **own** assistant message with OpenAI-format `tool_calls` (reuse a normalizer; may import `_build_assistant_tool_calls` from `tool_dispatch` after it too is moved there, or build inline) + `role:"tool"` result messages, appending to its private `messages`;
- **never** routes results through executor Phase 3 (which reads `gate_result.decision`, executor.py:3484). `dispatch_tool_call` is called with `gate_result=None`; the sub-agent ignores the gate fields in the return dict and reads only `content`/`success`/`tool_name`. This is why the shared callable's gate params are optional.

### 3f. No loop_gate FSM in the sub-agent — *codex point 4, documented*
The primary has a sequential `loop_gate` (begin_turn / check_before / record_output, executor.py:3319+) blocking identity/output/consecutive cycles. The sub-agent has **only** the iteration ceiling. This permits up to `sub_agent_max_tool_iterations` identical reads with no identical-output cycle-break — **accepted** because (a) the ceiling is small (≤5), (b) the surface is read-only/idempotent, (c) a full per-sub-agent FSM is disproportionate surface for Phase 2. Revisit if A/B (FRE-481) shows discovery loops thrash.

### 3h. Digest = NO premature clamping (owner steer, 2026-06-05)
Owner direction: **do not clamp/compress the sub-agent's content in round 1** — we need experience with what the tooled loop actually produces before deciding how to digest it. So this phase:
- Adds **no** digest/summarization transform and **no** "return only load-bearing facts" instruction to the loop. The sub-agent returns its model's final answer as-is.
- Preserves `SubAgentResult.full_output` **complete and uncapped** — this is the observability surface (ES `agent-captains-captures-*`) we'll read to learn the output shape.
- Relaxes the blunt `summary = response_content[:2000]` truncation for the **tooled path** to a generous, configurable bound (`settings.sub_agent_summary_max_chars`, default high, e.g. 8000) so the parent synthesis sees the real discovery output rather than a blind cut. PARALLEL_INFERENCE path keeps current behavior to avoid unrelated regression.
- Consequence acknowledged: this **relaxes** ADR-0086 D4's deterministic parent-tail bound for round 1 — intentional. We gain experience first; FRE-481's before/after A/B then drives where to tighten (digest sizing, structured-fact extraction). Noted explicitly so the cost A/B isn't read as a regression.

### 3g. Response-shape compatibility (str vs LLMResponse)
Real `respond` returns `LLMResponse` (`{"content","tool_calls",...}`). Existing `PARALLEL_INFERENCE` tests mock `respond` → plain `str`. The tooled loop handles dict responses; the non-tooled path keeps `str(response)`. Loop defends both: `content = resp["content"] if isinstance(resp, Mapping) else str(resp)`; `tool_calls = resp.get("tool_calls") if isinstance(resp, Mapping) else []`.

## 4. TDD steps (failing test first each time)

1. **settings** — `tests/personal_agent/config/test_settings.py`: assert `settings.sub_agent_max_tool_iterations == 5` and bounds. Implement field.
2. **shared dispatcher exists + parity** — `tests/personal_agent/orchestrator/test_tool_dispatch.py`: a fake `tool_layer.execute_tool` returns a `ToolResult`; assert `dispatch_tool_call(...)` returns the contract dict with `success=True`. Implement `tool_dispatch.py` by moving the body.
3. **primary still uses it** — patch `personal_agent.orchestrator.tool_dispatch.dispatch_tool_call` and run a minimal `step_tool_execution` with one tool call; assert the patched callable was invoked (guards "primary caller"). Update executor call site.
4. **sub-agent runs ≥1 tool + returns content (AC#1)** — `test_sub_agent.py::test_tooled_loop_executes_tool`: mock `respond` → [LLMResponse w/ one `read` tool_call, then LLMResponse w/ final content]; patch `dispatch_tool_call` to a fake success; assert `result.tools_used == ["read"]`, `result.success`, `result.full_output` == full final content (uncapped), `result.summary` carries it (no compression). Implement `_run_tooled_loop`.
5. **sub-agent shares the path (AC#2)** — assert `_run_tooled_loop` invokes the **same** `tool_dispatch.dispatch_tool_call` symbol (patch + assert called).
6. **mutating tool rejected (AC#3)** — spec grants `["read","write"]`; model emits a `write` tool_call; assert it is **not** dispatched (a tool-result message with an error/`rejected` status is appended) and `write` never reaches `dispatch_tool_call`.
7. **iteration ceiling** — model emits tool_calls every round; assert ≤ `sub_agent_max_tool_iterations` dispatch rounds and a final synthesis call with `tool_choice="none"`.
8. **planner parses mode/tools (flag on)** — `test_expansion_controller.py`: `_validate_plan_json` with flag on parses `mode:"tooled_sequential"` + `tools:["bash","read"]` → `PlanTask.mode==TOOLED_SEQUENTIAL`. With flag off → downgraded to `PARALLEL_INFERENCE`, tools dropped.
9. **planner prompt flag-gated** — assert the tooled-discovery instruction is present in the planner prompt only when flag on.
10. **no PARALLEL regression** — existing `test_sub_agent.py` cases (str-returning mock) stay green.

## 5. Quality gates (all before PR)
```
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py
make test-file FILE=tests/personal_agent/orchestrator/test_tool_dispatch.py
make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor.py   # primary parity
make test            # full suite
make mypy && make ruff-check && make ruff-format
pre-commit run --all-files
```
Expected: all pass; mypy clean (watch the `_dispatch_tool_call` move for new errors).

## 6. Follow-up tickets to file (Needs Approval, project "Turn Cost & Latency Optimization")
- Any digest-sizing / digest-quality eval surfaced during impl (feeds FRE-481 §2 side-by-side).
- (If owner picks name-allowlist in 3b) a `discovery_safe` tool-metadata flag so the surface is self-describing rather than a hardcoded list.

## 7. Halt conditions honoured
One ADR phase = one PR (D3+D4 only). No historical-row changes. Stop at PR — master merges/deploys/closes.
