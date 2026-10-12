# FRE-1588 — `model_call_completed` records the engine that served the call

> Backing design: ADR-0154 Amendment 1, section 1.4 (ticket FRE-1587). Tier: Standard (touches `src/` logic and an ES template). Codex plan-review: required.

## Goal

Every `model_call_completed` event carries `system_fingerprint` and `response_model`. Each holds what the response reported. Each is `null` when the response carried nothing. Neither is ever filled from configuration.

## Findings that shape the design (measured, litellm 1.98.0)

1. **The event's `model` key already exists and holds the requested name.** Cost rows join on it (ADR-0121 AC-8). It cannot hold the reported name. The reported name goes in a new key, `response_model`.
2. **On the streamed local path, litellm replaces the chunk `model` with the requested model.** Probe: the wire chunks carried `model="ENGINE-REPORTED-MODEL"`. The chunks that reached `_aggregate_streaming_chunks` carried `model="unsloth/qwen3.6-35-A3B"` (the requested id). Cause: `streaming_handler.py` sets `model_response.model = self.model`. The response model is not observable on this path.
3. **`system_fingerprint` survives streaming.** The probe chunks kept `b9999-PROBE`. llama.cpp puts its build there on every chunk.
4. **On the non-streamed cloud path, `ModelResponse.model` is the reported name.** litellm sets it from `response_object["model"]` (Anthropic: `completion_response["model"]`).
5. **`_aggregate_streaming_chunks` drops both fields today.** It is the seam that carries `system_fingerprint` into `LLMResponse.raw`, which the local emit reads.

## Decision

| Path | `system_fingerprint` | `response_model` |
|---|---|---|
| Cloud (non-streamed) | `response.system_fingerprint` when a `str`, else `null` | `response.model` when a `str`, else `null` |
| Local (streamed) | last non-empty chunk value, else `null` | always `null` — litellm hides it. Recording the echo would write configuration into a field that claims to be the response. |

AC-2 (live, local engine named) needs only `system_fingerprint`. The `response_model` limit on the local path is stated in the PR and the ticket handoff.

## Tasks

### T1 — Aggregator carries `system_fingerprint`

- Test first, in `tests/test_llm_client/test_streaming_aggregation.py`:
  - `test_system_fingerprint_is_carried_from_the_last_non_empty_chunk`: three chunks, fingerprint `b9999-old` on the first, `b9999` on the last two (one of them usage-only). Assert `result["system_fingerprint"] == "b9999"`.
  - `test_system_fingerprint_absent_when_no_chunk_reports_one`: assert `"system_fingerprint" not in result`.
- Edit `src/personal_agent/llm_client/adapters.py`: track `system_fingerprint: str | None`; update from `chunk.get("system_fingerprint")` when a non-empty `str`; add the key to the returned dict only when set (existing exact-shape tests stay valid).
- Run: `make test-file FILE=tests/test_llm_client/test_streaming_aggregation.py`.

### T2 — Emitter carries both fields

- Test first, in `tests/personal_agent/llm_client/test_litellm_emit_payload.py` (cloud path, AC-1):
  - Give `_call_respond` an optional `response: MagicMock | None = None` argument. Default stays `_make_mock_response()`.
  - `test_model_call_completed_records_what_the_response_reported`: response with `system_fingerprint="b9999"`, `model="m-test"`. Assert both on the event as `system_fingerprint` and `response_model`.
  - `test_model_call_completed_records_null_when_response_reports_nothing`: response with both attributes set to `None`. Assert both are `None` and that the keys are present.
  - `test_model_call_completed_never_records_a_non_string`: leave the `MagicMock` auto-attributes in place. Assert both are `None`.
- Edit `src/personal_agent/llm_client/telemetry.py`: `emit_model_call_completed` gains `system_fingerprint: str | None = None` and `response_model: str | None = None`. Both are always in the payload. Docstring states the contract.
- Edit `src/personal_agent/llm_client/litellm_client.py`:
  - Add module function `_reported_str(value: object) -> str | None` (returns `value` only when it is a non-empty `str`).
  - Cloud emit (about line 1621): pass `_reported_str(getattr(response, "system_fingerprint", None))` and `_reported_str(getattr(response, "model", None))`.
  - Local emit (about line 2002): pass `_reported_str(llm_response["raw"].get("system_fingerprint"))` and `response_model=None`. A comment cites finding 2.
- Run: `make test-file FILE=tests/personal_agent/llm_client/test_litellm_emit_payload.py`.

### T3 — Local path through real litellm dispatch

- Test first, in `tests/personal_agent/llm_client/test_local_via_litellm.py`, using `_dispatch` and `Captured.completed_event()`:
  - `test_completed_event_names_the_engine_build`: SSE chunks carry `system_fingerprint="b9999-test"`. Assert `completed_event()["system_fingerprint"] == "b9999-test"`.
  - `test_completed_event_response_model_is_null_when_litellm_hides_it`: chunks carry `model="ENGINE-REPORTED-MODEL"`, which differs from the requested id. Assert `completed_event()["response_model"] is None`. This is the seeded negative: the wire carried a model, and the event must not record the requested one.
  - `test_completed_event_fingerprint_null_when_stream_has_none`: default stream. Assert `system_fingerprint is None`.
- Run: `make test-file FILE=tests/personal_agent/llm_client/test_local_via_litellm.py`.

### T4 — ES template and vocabulary

- Test first, in `tests/scripts/test_es_templates.py`: `test_logs_pins_engine_identity_fields_as_keyword` — `system_fingerprint` and `response_model` are explicit `keyword` in `index-template.json`.
- Test first, in `tests/test_telemetry/test_vocabulary.py`: a payload with both keys passes `validate_document` (no near-miss against a governed name).
- Edit `docker/elasticsearch/index-template.json`: add both keys as `{ "type": "keyword" }` beside `"model"` (about line 164). Additive, no type change.
- Run: `make test-file FILE=tests/scripts/test_es_templates.py` and `make test-file FILE=tests/test_telemetry/test_vocabulary.py`.

### T5 — Docs

- `src/personal_agent/llm_client/AGENTS.md` (near line 153): two sentences naming the fields and the local-path limit.

### T6 — Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Commit. Then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof |
|---|---|
| AC-1 | T2 tests (reported, null, non-string). T3 covers the local path. |
| AC-2 | Live, after deploy. See the handoff runbook. |

## Out of scope

Capturing the raw chunk `model` on the local path (needs an HTTP-layer hook in the guarded client; a separate decision).
