# FRE-405 P1 — Prompt Identity Primitive — Implementation Plan

> Tier-1:Opus · ADR-0078 D1/D4 · Spec §2/§4/§5 · Branch + PR (code)
> Status: awaiting owner sign-off · 2026-05-29

## Goal

Stamp a `PromptIdentity` on every `model_call_completed` event, add a real
static/dynamic prefix-hash measurement, and close the `gateway/chat_api.py`
telemetry dark path. This is the primitive P2–P5 hang off.

## Design decisions already settled (Codex-confirmed; ADR/spec corrected in `a41fe3f`)

1. **Keep `compute_prefix_hash(message)` intact.** It guards the head-preservation
   invariant (8 assertions in `test_kv_cache_stability.py` / `test_within_session_compression.py`).
   Do NOT change its signature.
2. **New module owns identity hashing**, distinct from `compute_prefix_hash`.
3. **`static_prefix_hash` measures the literal cacheable prefix** — bytes up to the
   first DYNAMIC component (`memory_section`). Given the broken assembly
   (`executor.py:2218` appends STATIC tool rules *after* DYNAMIC memory), this
   honestly excludes the post-memory tool rules. By design — it makes the erosion
   visible for P2.
4. **100% coverage via fallback derivation**, not a hard-required param threaded
   through every caller (aligns with the "self-describing over harness routing"
   preference). The orchestrator-primary and gateway paths pass a rich identity;
   every other path gets a derived fallback inside the client so no event ever
   emits null prompt fields.

## Files & changes

### 1. NEW `src/personal_agent/llm_client/prompt_identity.py`
- `PromptIdentity` frozen dataclass: `callsite: str`, `component_ids: tuple[str, ...]`,
  `static_prefix_hash: str`, `dynamic_hash: str`.
- `_short_hash(text: str) -> str` → `sha256(text.encode()).hexdigest()[:16]`.
- `derive_prompt_identity(callsite, *, static_prefix, full_prompt, component_ids=()) -> PromptIdentity`.
- Google docstrings; no `Any`; mypy clean.

### 2. `src/personal_agent/telemetry/events.py`
- Add to `CANONICAL_MODEL_CALL_COMPLETED_FIELDS`: `prompt_callsite`,
  `prompt_component_ids`, `prompt_static_prefix_hash`, `prompt_dynamic_hash`.
  This forces the parity test to require both clients to emit them.

### 3. `src/personal_agent/llm_client/telemetry.py`
- `emit_model_call_completed(...)` gains `prompt_identity: PromptIdentity` (required;
  callers always pass one — derived if necessary).
- Flatten into payload: `prompt_callsite`, `prompt_component_ids` (list),
  `prompt_static_prefix_hash`, `prompt_dynamic_hash`.

### 4. `src/personal_agent/llm_client/client.py` (LocalLLMClient)
- `respond(..., prompt_identity: PromptIdentity | None = None)` → thread to `_do_request`.
- In `_do_request`, before the emit at ~:497: if `prompt_identity is None`, derive a
  fallback: `callsite=f"role.{role.value}"`, `static_prefix = full_prompt = (system_prompt or "")`,
  `component_ids=()`. Pass to `emit_model_call_completed(prompt_identity=...)`.

### 5. `src/personal_agent/llm_client/litellm_client.py` (LiteLLMClient)
- Same: `respond(..., prompt_identity=None)` threaded to the emit at ~:455, with the
  same fallback derivation.

### 6. `src/personal_agent/orchestrator/executor.py`
- Capture `inner_system_before_memory = system_prompt` immediately **before** line 2193
  (the `memory_section` append).
- After full assembly, build:
  - `static_prefix = f"{tool_awareness}\n\n{inner_system_before_memory}"` when
    tool_awareness was added (tools present), else `inner_system_before_memory or ""`.
  - `component_ids` = ordered tuple of components actually included this turn
    (e.g. `deployment_context, operator_stanza, skill_index, memory_section,
    tool_awareness, tool_use_rules, decomposition_instructions`), conditioned on presence.
  - `prompt_identity = derive_prompt_identity("orchestrator.primary",
    static_prefix=static_prefix, full_prompt=system_prompt or "", component_ids=...)`.
- Pass `prompt_identity=prompt_identity` to `llm_client.respond(...)` at :2287.

### 7. `src/personal_agent/gateway/chat_api.py` (dark path)
- In `_stream_to_queue`, on the success path (after `get_final_message`), emit
  `model_call_completed` directly via `emit_model_call_completed`:
  - Build a minimal `TraceContext(trace_id=..., session_id=...)` (confirm constructor
    during impl) + fresh `span_id = uuid4().hex`.
  - `prompt_identity = derive_prompt_identity("gateway.chat",
    static_prefix=_SYSTEM_PROMPT, full_prompt=_SYSTEM_PROMPT,
    component_ids=("gateway_persona",))`.
  - `latency_ms` from a start timestamp; `input/output_tokens` from
    `final_message.usage`; `cost_usd` reuse the value computed in
    `_commit_reservation_safe` (extract the per-token math into a tiny helper so it's
    computed once and shared with the emit).
- No re-architecture to route through LiteLLMClient (spec sanctions a direct canonical
  emit when a direct call is architecturally necessary — the streaming + reservation
  logic here is bespoke).

### 8. Token counter — already shipped in P0. No change.

## Tests (TDD — write first)

- NEW `tests/personal_agent/llm_client/test_prompt_identity.py`:
  - `PromptIdentity` is frozen; `_short_hash` returns 16 hex chars.
  - `static_prefix_hash` changes when `static_prefix` changes.
  - **AC core:** two identities with the *same* `static_prefix` but different
    `full_prompt` → equal `static_prefix_hash`, differing `dynamic_hash` (i.e. stable
    when only memory/dynamic tail changes).
- `tests/personal_agent/llm_client/test_telemetry.py` (extend/new): emit payload carries
  the four `prompt_*` fields.
- Existing parity test: now enforces the four fields; verify both clients pass.
- Gateway: extend `tests/personal_agent/gateway/` — mock the anthropic stream +
  capture logs; assert a `model_call_completed` with `prompt_callsite="gateway.chat"`.
- Regression: `compute_prefix_hash` head-preservation tests stay green untouched.

## Acceptance criteria (FRE-405)

| Gate | Criterion | Covered by |
|------|-----------|------------|
| Pre-merge | `PromptIdentity` defined; mypy clean | file 1 |
| Pre-merge | emit signature updated; both clients pass identity | files 3/4/5 |
| Pre-merge | prefix-hash test: changes on STATIC change, stable on memory-only change | tests |
| Pre-merge | gateway emits `prompt_callsite="gateway.chat"` | file 7 + test |
| Pre-merge | `make test` + `make mypy` clean | CI |
| Post-deploy | ES: last 100 `model_call_completed` carry non-null `prompt_callsite` + `prompt_static_prefix_hash` | post-deploy probe |
| Post-deploy | ES: `prompt_callsite="gateway.chat"` events now present | post-deploy probe |

## Post-deploy plan (same session as deploy)
1. Rebuild + deploy `seshat-gateway`.
2. Drive one orchestrator turn + one gateway `/chat` turn.
3. ES query: confirm `prompt_callsite` populated on both paths; confirm `gateway.chat`
   events exist. Record results in MASTER_PLAN.
4. FRE-405 → Done only after post-deploy probe passes; FRE-403 EPIC stays In Progress.

## Out of scope (P1)
- Sub-agent / compressor / entity-extraction rich identities (they get the fallback;
  rich stamping is incremental and not required by P1 AC).
- Composer reordering (optional phase, gated on P2 data).
- Kibana views / drift alarm (P2).
