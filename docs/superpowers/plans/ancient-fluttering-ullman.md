# FRE-376 Phase 2 — Equalize Model Client Telemetry (`ModelClientTelemetry` mixin)

**Linear:** [FRE-376](https://linear.app/frenchforest/issue/FRE-376) (reopened to `In Progress` 2026-05-22)
**ADR:** [ADR-0074](../architecture_decisions/ADR-0074-end-to-end-traceability.md) — Phase 2, Invariant **I2**
**Branch:** `fre-376-phase-2-model-client-telemetry-parity`

## Context

ADR-0074 Phase 1 shipped (PR #69 — cost identity + per-message attribution). Phase 4a/4b shipped (PRs #70/71 — `TraceContext` non-optional). Phase 2 (this plan) and Phases 3 + 5 are still open; the ticket was prematurely marked Done and has been reopened.

**Phase 2 goal (I2):** `LocalLLMClient` and `LiteLLMClient` emit *identical* `model_call_started` / `model_call_completed` event shapes. A request handler that switches between local and cloud cannot tell which path served the call from telemetry alone.

**Current divergence (verified):**

| Aspect | `LocalLLMClient` (`client.py`) | `LiteLLMClient` (`litellm_client.py`) |
|--------|-------------------------------|---------------------------------------|
| Start event name | `model_call_started` | `litellm_request_start` |
| Complete event name | `model_call_completed` | `litellm_request_complete` |
| Model field | `model_id` | `model` |
| Token fields | `prompt_tokens` / `completion_tokens` | `prompt_tokens` / `completion_tokens` (+ `tokens` double-write) |
| `session_id` | **absent** | **absent** |
| `parent_span_id` | **absent** | **absent** |
| `span_id` | present | **absent** |

Downstream consumers of `model_call_completed` (`captains_log/reflection.py:566`, `tests/test_telemetry/test_metrics.py`) currently miss every cloud call.

## Canonical contract

Defined by ADR-0074 §I2 and frozen here as the single source of truth:

| Field | Type | Notes |
|-------|------|-------|
| `event` | `"model_call_started"` \| `"model_call_completed"` | log message |
| `model` | `str` | canonical identifier (e.g. `anthropic/claude-sonnet-4-6`, local model id) |
| `role` | `str` | from `ModelRole` enum (`primary` / `extractor` / etc.) — `.value` |
| `endpoint` | `str` | URL or provider tag (`anthropic`, `openrouter`, `http://localhost:8000/v1/chat/completions`) |
| `trace_id` | `str` (UUID) | `trace_ctx.trace_id` |
| `session_id` | `str \| None` | `trace_ctx.session_id` (None only for system contexts) |
| `span_id` | `str` (UUID) | newly minted for this model call |
| `parent_span_id` | `str \| None` | `trace_ctx.parent_span_id` at call site |
| `latency_ms` | `int` | completion-only |
| `input_tokens` | `int \| None` | completion-only; canonical name |
| `output_tokens` | `int \| None` | completion-only; canonical name |
| `total_tokens` | `int \| None` | completion-only |
| `cache_read_tokens` | `int \| None` | completion-only; provider-specific |

Back-compat double-writes (kept for one release cycle, removed in Phase 3 cleanup):
- `model_id` (alias of `model`)
- `prompt_tokens` / `completion_tokens` (aliases of `input_tokens` / `output_tokens`)
- `litellm_request_start` / `litellm_request_complete` emitted alongside canonical events
- `elapsed_s` / `tokens` (existing back-compat already in `litellm_client.py`)

## Files to create / modify

### Create: `src/personal_agent/llm_client/telemetry.py`

Module-level helpers (not a class mixin — both clients are concrete and unrelated by inheritance; a free-function module is the lighter, mypy-friendlier shape). Exports:

- `emit_model_call_started(*, log, role, model, endpoint, trace_ctx, span_id, extra=None) -> None`
- `emit_model_call_completed(*, log, role, model, endpoint, trace_ctx, span_id, latency_ms, input_tokens, output_tokens, total_tokens=None, cache_read_tokens=None, extra=None) -> None`
- `emit_legacy_litellm_start(...)` / `emit_legacy_litellm_complete(...)` — write the deprecated `litellm_request_*` event names, called alongside canonical emits from `LiteLLMClient` only; keep all current fields (budget_role, reservation_amount, cost_usd, etc.) so downstream consumers don't lose anything.

Both canonical helpers:
- Use the shared `MODEL_CALL_STARTED` / `MODEL_CALL_COMPLETED` constants from `telemetry/events.py`.
- Always pass back-compat aliases (`model_id`, `prompt_tokens`, `completion_tokens`) so today's queries keep working.
- Accept `extra: dict[str, Any] | None` so each client can add provider-specific fields (e.g. `api_type`, `fallback_used`, `tool_calls`, `cost_usd`) without polluting the canonical signature.

### Modify: `src/personal_agent/llm_client/client.py`

- Replace the inline `log.info(MODEL_CALL_STARTED, ...)` at ~line 317 with `emit_model_call_started(...)`.
- Replace the inline `log.info(MODEL_CALL_COMPLETED, ...)` at ~line 489 with `emit_model_call_completed(...)`.
- Pass `extra={"api_type": current_api_type, "fallback_used": tried_fallback}` on the completion call.

### Modify: `src/personal_agent/llm_client/litellm_client.py`

- Add `span_ctx, span_id = trace_ctx.new_span()` near the start of `_completion()` (mirrors `client.py`).
- At ~line 285 (current `litellm_request_start`): call `emit_model_call_started(...)` AND `emit_legacy_litellm_start(...)`.
- At ~line 433 (current `litellm_request_complete`): call `emit_model_call_completed(...)` AND `emit_legacy_litellm_complete(...)`. Pass `extra={"endpoint": self.provider, "tool_calls": len(tool_calls), "cost_usd": round(cost, 6) if cost else None, "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"), "cache_write_tokens": usage.get("cache_creation_input_tokens")}`.
- Keep `litellm_request_budget_denied` and `litellm_request_failed` untouched — they are error events, not the canonical pair.

### Update: `tests/test_llm_client/test_telemetry_parity.py` (new file)

Integration-style test that asserts shape parity. Uses the existing structlog test capture pattern (search `caplog`/`capture_logs` already used in `tests/test_telemetry/`) to:

1. Drive `LocalLLMClient._call_chat_completions(...)` against a mocked httpx transport returning a canned streaming response.
2. Drive `LiteLLMClient._completion(...)` against a mocked `litellm.acompletion` returning a canned `ModelResponse`.
3. Assert: both flows emit exactly one `model_call_started` and one `model_call_completed`.
4. Assert: `set(event["model_call_started"].keys()) >= CANONICAL_START_FIELDS` for both.
5. Assert: same for `model_call_completed` against `CANONICAL_COMPLETE_FIELDS`.
6. Assert: `model`, `role`, `trace_id`, `session_id`, `span_id`, `parent_span_id` are all populated (not None for `session_id` when a real session is on the context).

`CANONICAL_START_FIELDS` / `CANONICAL_COMPLETE_FIELDS` constants live in `telemetry.py` so the test is the contract enforcer.

### Update: `src/personal_agent/telemetry/events.py` (small additions)

Add two module-level frozenset constants for the parity test to import:
```python
CANONICAL_MODEL_CALL_STARTED_FIELDS = frozenset({"model", "role", "endpoint", "trace_id", "session_id", "span_id", "parent_span_id"})
CANONICAL_MODEL_CALL_COMPLETED_FIELDS = CANONICAL_MODEL_CALL_STARTED_FIELDS | frozenset({"latency_ms", "input_tokens", "output_tokens", "total_tokens"})
```

## Acceptance Criteria

| AC | Verification when | How |
|----|------------------|-----|
| AC-1 | Pre-merge | `make mypy` passes |
| AC-2 | Pre-merge | `make ruff-check` + `make ruff-format` clean |
| AC-3 | Pre-merge | `make test` passes (incl. new `test_telemetry_parity.py`) |
| AC-4 | Pre-merge | `tests/test_llm_client/test_telemetry_parity.py::test_local_and_litellm_emit_identical_shapes` green |
| AC-5 | Pre-merge | Existing `tests/test_telemetry/test_metrics.py` still passes (back-compat preserved) |
| AC-6 | Post-deploy | Probe ES: both `endpoint="anthropic*"` and `endpoint LIKE "http://%"` rows present in `seshat-logs-*/_search?q=event:model_call_completed&size=2` within 10 min of seshat-gateway restart |
| AC-7 | Post-deploy | `captains_log/reflection.py:566` now picks up litellm calls — sample one reflection run and confirm `model_calls` list is non-empty for sessions that used cloud models |
| AC-8 | Post-deploy | `litellm_request_complete` still emits (back-compat) — sample `seshat-logs-*/_search?q=event:litellm_request_complete&size=1` returns ≥1 hit |
| AC-9 | Future-gate | Open a follow-up note in Phase 3 plan to drop the legacy `litellm_request_*` emits and back-compat field aliases after one release cycle |

## Verification

```bash
# Code quality
make mypy
make ruff-check
make ruff-format

# Unit tests
make test
make test-file FILE=tests/test_llm_client/test_telemetry_parity.py

# Local round-trip — start gateway, send one request, scrape logs
make rebuild SERVICE=seshat-gateway
uv run agent "hello" --new
make logs SERVICE=seshat-gateway | grep -E "model_call_(started|completed)" | head -4
# Expect: two rows (one started, one completed) — both with model=, session_id=, span_id=, parent_span_id=

# Production probe (after deploy)
curl -s "https://es.example.com/seshat-logs-*/_search?size=2&q=event:model_call_completed&sort=@timestamp:desc" \
  | jq '.hits.hits[]._source | {model, role, session_id, trace_id, span_id, input_tokens, output_tokens, endpoint}'
# Expect: rows from BOTH local and cloud endpoints, all identity fields populated
```

## Out of scope (deferred to later phases)

- **Phase 3:** AST-level audit of every other `log.*`/`bus.publish`/Cypher emit site; remove back-compat aliases added here.
- **Phase 5:** Joinability probe in CI; pre-commit lint `check_identity_threaded.py`.
- Backfilling historical events with identity. ADR-0074 explicitly OOS.

## Shipping

Single PR — backend-only, behind no flag, telemetry-additive. Once green:
1. Squash-merge to `main`.
2. `ENV=cloud make deploy` (no rebuild needed for telemetry change — actually requires `make build SERVICE=seshat-gateway` since `litellm_client.py` is in the image).
3. Run post-deploy ACs (6, 7, 8) before closing Phase 2 in the Linear comment.
4. Update `docs/plans/MASTER_PLAN.md` (per memory: update after every shipped issue/phase).
5. Keep FRE-376 in **In Progress** until Phase 3 + 5 ship (per memory: multi-phase tickets stay In Progress until the last phase ships).
