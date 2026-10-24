# FRE-1433 — In-flight local-call registry

**Backing ADR:** ADR-0144 D4 (mechanics 1–4), AC-6, AC-7. **Tier:** Standard (touches `src/` logic).
**One phase, one PR.** FRE-1434 (retry loop) and FRE-1436 (health) read this registry. They are out of scope.

## Facts checked against the code (the ticket's line numbers are stale)

- The local call site is `LiteLLMClient._respond_local`, `litellm_client.py` ~2163. The slot is
  `request_slot(...)`. The per-call `span_id` is set at ~2172 from `model_call_span`.
- FRE-1602 already put a retry loop inside the slot: `while True: attempts += 1; try: acompletion;
  async for chunk ... except ... finally: _safe_aclose_stream`. The `try` **already starts before**
  `acompletion()`, so the ticket's remark that the `finally` is "too narrow" no longer holds for
  connection failures. The aggregation (`_aggregate_streaming_chunks`) runs **after** the loop.
- `_update_generation_progress` (`:594`) skips `prompt_progress` chunks and folds `delta.content`
  only. It does not change.

## Design

New module `src/personal_agent/llm_client/inflight.py`:

- `InflightCall` — frozen dataclass: `span_id`, `attempt`, `dispatched_monotonic`,
  `last_byte_monotonic`. Methods `age_s(now=None)` and `silence_s(now=None)`.
- `InflightCallRegistry` — `dict[tuple[str, int], InflightCall]`. Methods: `register(span_id, attempt)
  -> key` (stamps `dispatched` = `last_byte` = now), `touch(key)` (replaces the record with a new
  `last_byte`; no-op if the key is gone), `remove(key)` (idempotent), `get(key)`, `snapshot()`.
- `get_inflight_registry()` — process singleton, plus `reset_inflight_registry()` for tests.
- Process-local and lock-free: single event loop, one worker (ADR-0143 D1 invariant). It is **not**
  ADR-0143's turn registry — no trace key, no `started_at`.

Wiring in `_respond_local`, inside the slot and inside the retry loop (one record per attempt):

```python
attempts += 1
stream = None
chunks = []
inflight_key = inflight.register(span_id, attempts)   # directly above the try: nothing can raise between
try:
    stream = await litellm.acompletion(**litellm_kwargs)
    async for chunk in stream:
        inflight.touch(inflight_key)                   # ANY chunk, prefill reports and reasoning included
        dumped = chunk.model_dump()
        ...
finally:
    inflight.remove(inflight_key)                      # first statement: a hung aclose cannot leave a dead record
    await _safe_aclose_stream(stream)
```

Covered exits: success, `CancelledError` (user cancel, wall-clock `asyncio.timeout`), gap-timeout
`httpx` errors, connection failure before the stream opens, transport reset mid-stream (the retry
removes attempt N and registers attempt N+1). An aggregation failure happens after the loop, when the
record is already gone.

Decision: the dispatch stamp is taken immediately before `acompletion()`. `last_byte` starts equal to
the dispatch stamp, so `silence_s` is uniform for a chunkless call and no consumer handles `None`.

## Codex plan-review (2026-10-24) — outcome

| Finding | Disposition |
|---|---|
| High: `(span_id, attempt)` is not unique when the span is non-recording (all-zero id). | **Adopted.** `_respond_local` keys the registry on `uuid4().hex[:16]` when `span_context.is_valid` is false. The logged `span_id` is unchanged. Test: `test_two_calls_with_a_non_recording_span_are_still_distinct`. |
| Medium: removal comes before aggregation, not after it as ADR-0144 words it. | **Kept, flagged.** Aggregation is local CPU work after the origin finished. A record that lived through it would read as a silent call. The ticket's AC-4 asks only that the registry is empty after every exit, including an aggregation failure. |
| Medium: AC-6(a) wants the verdict to stay `up` through a long reasoning phase. | **Out of scope.** The verdict and its reader are FRE-1436. This ticket proves the substrate: the stamp advances. |
| Medium: AC-7 wants the mixed exits concurrently. | **Adopted.** `test_concurrent_calls_ending_six_ways_leave_the_registry_empty`. The cancel is a `CancelledError` raised from the stream, not a `task.cancel()`. The sequential parametrised test keeps the real `task.cancel()`. |
| Prefill reports advance the stamp. | **Confirmed**, with a test for both report shapes. |

## Tasks

1. **Red.** `tests/personal_agent/llm_client/test_inflight_registry.py` — unit tests of the registry
   (distinct keys, same span different attempt, touch advances, touch after remove is a no-op, remove
   idempotent, snapshot immutable). Add `TestInflightRegistry` to
   `tests/personal_agent/llm_client/test_local_via_litellm.py` for AC-1 to AC-5 through the real
   dispatch. Run: `make test-file FILE=tests/personal_agent/llm_client/test_inflight_registry.py`
   → fails (no module). The dispatch tests fail on the empty registry.
2. **Green.** Write `inflight.py`. Wire `_respond_local`. Re-run both files → pass.
3. **Docs.** `src/personal_agent/llm_client/AGENTS.md` structure list gains `inflight.py`.
4. **Gates.** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Commit. `feature-dev:code-reviewer` on `git diff origin/main...HEAD`.

## Acceptance criteria → tests (outcome, not wiring)

| AC | Test (all in `test_local_via_litellm.py::TestInflightRegistry`) | Observed through |
|----|------|------|
| AC-1 | `test_a_call_with_no_chunk_yet_has_an_age_from_dispatch` | A stream that holds its first byte for 0.3 s. A poller reads `snapshot()` mid-call: one record, `age_s() > 0`, `silence_s() > 0`, strictly larger at a second read. |
| AC-2 | `test_reasoning_deltas_advance_the_last_byte_stamp` | A stream of reasoning-only deltas, 0.08 s apart. The poller collects `last_byte_monotonic` across the phase: strictly increasing. Also: with `progress_sink`, `content == ""` (AC-5). |
| AC-2b | `test_prefill_reports_advance_the_last_byte_stamp` | Same, with `prompt_progress` chunks in both shapes. A long prefill is not a stall. |
| AC-3 | `test_two_calls_under_one_trace_at_attempt_one_are_distinct` | Two concurrent `respond()` on one `TraceContext`. Two records, same `attempt == 1`, different `span_id`. |
| AC-4 | `test_the_registry_is_empty_after_every_exit` (parametrised) | success · gap timeout (`httpx.ReadTimeout` mid-stream) · wall-clock timeout · user cancel · `ConnectError` before the stream opens · aggregation failure after it closes · mid-stream reset retried. Each: registry non-empty while live (where observable), empty after. |
| AC-4b | `test_a_retried_attempt_is_a_new_record` | Reset then success: the poller sees attempt 1, then attempt 2 with the same `span_id` and a later dispatch stamp. |
| AC-5 | `test_reasoning_text_never_reaches_generation_progress` | Reasoning-only stream with a `progress_sink`: `content == ""`. The existing FRE-1379 / FRE-1488 tests stay green unchanged. |

**Known limit, stated in the handoff:** AC-4 drives each exit through the real dispatch one at a time.
The harness patches the process-wide transport, so six differently-failing calls cannot run
concurrently in one test. AC-3 is the concurrent proof.

## Non-goals

No health verdict, no retry change, no probe, no telemetry event, no `GenerationProgress` change.
Cloud placement does not register (the ticket scopes local calls).
