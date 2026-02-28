# Router Refactor: Analysis and Plan

## A) CODEBASE ANALYSIS (short)

### Current call chain and files

1. **Orchestrator execution loop**
   - `step_init` (executor.py ~801): loads session history, appends user message, applies context window, queries memory → transitions to LLM_CALL.
   - `step_llm_call` (executor.py ~971): determines `model_role` via `_determine_initial_model_role` (ROUTER for CHAT); builds `system_prompt` from `get_router_prompt()` for ROUTER; injects memory into system_prompt for all roles (1061–1076); injects tool prompts when tools present (1074–1093); sets `request_messages = ctx.messages` (1104) then applies no_think and `_validate_and_fix_conversation_roles` for all roles; calls `LocalLLMClient.respond()` with those messages; on ROUTER, parses via `_parse_routing_decision` and either delegates or HANDLE.
   - Routing parse: `_parse_routing_decision` (executor.py ~397) expects `routing_decision` (HANDLE/DELEGATE), `confidence`, `reasoning_depth`, `reason`, optional `target_model`; defaults missing `target_model` to STANDARD (491–499); on HANDLE extracts `response` (511–514).
   - Tool flow: tools only when not ROUTER and not synthesizing (1036–1059); tool definitions and TOOL_USE_SYSTEM_PROMPT only for non-router (1074–1093).

2. **Router prompts**
   - System: `orchestrator/prompts.py` — `get_router_prompt()` returns `ROUTER_SYSTEM_PROMPT_BASIC` or `ROUTER_SYSTEM_PROMPT_WITH_FORMAT` (615–627). Both are long, include HANDLE path, format detection, many examples.
   - User: `ROUTER_USER_TEMPLATE` and `format_router_user_prompt()` (478–488, 630–649) exist but are **not used** in executor; router receives full `ctx.messages` (hydrated conversation).

3. **LocalLLMClient.respond payload**
   - `llm_client/client.py` respond() (101+): gets model config by role, builds `request_messages = messages.copy()` and inserts system_prompt at 0 (224–226); passes `response_format` through to `build_chat_completions_request`.
   - `llm_client/adapters.py` `build_chat_completions_request()` (346+): adds `response_format` to payload as-is (417–418). No special handling for router.

### Concrete reasons routing is slow/flaky in this repo

| Cause | Location |
|-------|----------|
| Full hydrated conversation sent to router | executor.py ~1104: `request_messages = ctx.messages` for all roles; no branch for ROUTER to use only latest user message. |
| Context-window truncation and role fixing applied to router input | executor.py ~1125: `_validate_and_fix_conversation_roles(request_messages)` applied to same messages used for router, so router sees long history. |
| Memory injected into router prompt | executor.py 1061–1076: `memory_section` appended to `system_prompt` whenever `ctx.memory_context` is non-empty; no `model_role == ROUTER` guard. |
| Overly wide router JSON schema | executor.py 66–95: `_router_response_format()` includes `routing_decision`, `response`, `reasoning_depth`, `detected_format`, `format_confidence`, `format_keywords_matched`, `recommended_params`; `additionalProperties: True`. |
| HANDLE path in prompt and parse | prompts.py 51–69, 232, 447–459: HANDLE branch and examples; executor 511–514, 1286–1298: HANDLE handling and use of router `response`. |
| No deterministic pre-router gate | Router is always called for CHAT; no fast heuristic to skip LLM. |
| No router-specific timeout | client.py 94: ROUTER uses same 30s default as other roles; no short timeout for router-only. |
| Silent default of target_model to STANDARD | executor.py 491–499: when router omits `target_model`, parser sets STANDARD without failing; encourages flaky schema. |

---

## B) PLAN (minimal, pragmatic)

1. **Settings**
   - Add `routing_policy` (heuristic_then_llm | heuristic_only | llm_only), `router_role` (ROUTER | STANDARD), `enable_reasoning_role`, `routing_heuristic_threshold` (float), `router_timeout_seconds` (e.g. 5).
   - Add `resolve_role(requested_role)` helper: if single-model mode (router_role == STANDARD), map ROUTER→STANDARD; if not enable_reasoning_role, map REASONING→STANDARD.

2. **Pre-router heuristic gate**
   - New function `_heuristic_routing(user_message: str) -> HeuristicRoutingPlan` (target_model, confidence, reason, used_heuristics).
   - Rules: CODING (code fences, stack traces, def/class/import, debug/refactor/implement, file diffs, CI errors); STANDARD (explicit tool intent: search web, look up, list files, read file, check disk, open URL, latest news); REASONING (prove/derive/rigorously, deep reasoning, research synthesis); else STANDARD.
   - In step_llm_call, when model_role == ROUTER and routing_policy != llm_only: run heuristic; if confidence >= threshold, skip router LLM, set ctx.selected_model_role from resolve_role(heuristic.target_model), log and return LLM_CALL.

3. **Router-only messages**
   - When model_role == ROUTER: set `request_messages = [{"role": "user", "content": ctx.user_message}]`. Do not use ctx.messages, no context-window or role fixing for this path.

4. **No memory for router**
   - Only append memory_section when `model_role != ModelRole.ROUTER`.

5. **Tool prompt only for non-router**
   - Keep existing condition: tool awareness + TOOL_USE_SYSTEM_PROMPT only when `tools` is truthy; tools are already None for ROUTER, so no change needed except to make the memory block explicitly router-excluded (above).

6. **Short router prompt (DELEGATE-only)**
   - Replace router system prompt with ~10–15 lines: you classify the user query; output ONLY JSON with target_model (STANDARD|REASONING|CODING), confidence, reason; no HANDLE, no format detection, no commentary.

7. **Minimal router schema**
   - `_router_response_format()`: one object with `target_model` (enum STANDARD|REASONING|CODING), `confidence` (number), `reason` (string); required: target_model, confidence; additionalProperties: false.

8. **Router parse delegate-only**
   - Remove HANDLE from schema and prompt. Parser expects only DELEGATE; if routing_decision present and HANDLE, treat as DELEGATE to STANDARD or ignore response field. Better: schema no longer has routing_decision; parser expects only target_model, confidence, reason. So we change parser to accept this minimal shape and produce RoutingResult with decision=DELEGATE always.
   - Missing target_model: do not default to STANDARD; treat as parse failure → run heuristic fallback (or one retry with “ONLY JSON” instruction). Fallback: use heuristic routing result for this turn.

9. **Router timeout and fallback**
   - Pass router_timeout_seconds into respond() for ROUTER role (client already supports timeout_s per call). On timeout or parse failure after optional retry: set ctx.selected_model_role from heuristic (or STANDARD if heuristic not run).

10. **Single-model mode**
   - resolve_role(): when router_role == STANDARD, ROUTER → STANDARD; when enable_reasoning_role is False, REASONING → STANDARD. Use resolved role when setting ctx.selected_model_role after heuristic or after router parse.

**Testing**
   - Unit: heuristic gate (CODING/STANDARD/REASONING/else); router request_messages when ROUTER (single user message); memory not in router prompt; schema has additionalProperties false and required target_model; parse missing target_model → fallback (no silent STANDARD); resolve_role mapping.
   - Integration: CHAT with heuristic_only → no router call; CHAT with heuristic_then_llm and high-confidence heuristic → no router call; CHAT with low confidence → router called with 1 user message; router timeout → heuristic fallback.

**Rollback**
   - Feature-flag via routing_policy: llm_only restores “always call router”; heuristic_only disables router LLM. Keep existing telemetry event names.

---

## Implementation order

1. Config (settings + resolve_role).
2. Heuristic gate + types.
3. Router prompt and schema (minimal, delegate-only).
4. Parser: delegate-only, missing target_model = failure, heuristic fallback.
5. step_llm_call: router-only messages, no memory for router, pre-router gate, router timeout, telemetry.
6. Tests and telemetry.
