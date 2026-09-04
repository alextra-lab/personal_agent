# FRE-1379 — a timed-out sub-agent reports nothing, and the inner budget cannot bound a stream

Ticket: https://linear.app/frenchforest/issue/FRE-1379
Builds on: FRE-1374 (`orchestrator/sub_agent.py`, `config/settings.py`)

## Scope (per the ticket, Finding 2 first)

1. A killed sub-agent must report what it managed: tokens generated, elapsed
   generation time (distinct from wall time), and partial content.
2. `worker_timeout_seconds` (60s) must either actually bound a streaming
   generation, or be removed. Currently it becomes an httpx **read** timeout
   (gap-between-bytes), which a steadily-streaming response never trips —
   verified in the live incident: both failures reported "Timeout after 85.0s",
   never 60.0s.
3. Characterise the observed throughput variance (12.7 vs 65 tok/s in the
   incident) across at least one real fan-out, once the instrumentation from
   (1) exists to see it on a killed worker too.

**Decision on (2):** retain `worker_timeout_seconds`, and make it a genuine
wall-clock bound on the local streaming path, in addition to (not instead of)
the existing read-timeout. Rationale: the read-timeout is real defense against
a stalled connection (no bytes at all); it just isn't the whole story. Deleting
the setting would remove that defense too. One value, two enforcement points,
both honest about what they catch.

## Design

### New: `GenerationProgress` (`llm_client/types.py`)

```python
@dataclass
class GenerationProgress:
    """Mutable sink for partial-streaming state (FRE-1379).

    A caller wrapping ``respond()`` in its own outer timeout loses the return
    value when that outer timeout cancels the call. Passing a
    ``GenerationProgress`` in lets the caller recover whatever was generated
    up to the point of cancellation — the streaming client updates it as a
    side effect, not via the (lost) return value.
    """
    content: str = ""
    generation_started_monotonic: float | None = None
```

No behaviour, just a mutable box `_respond_local` writes into and `sub_agent.py`
reads from after the call is cancelled out from under it.

### `LiteLLMClient.respond()` / `_respond_local()` (`llm_client/litellm_client.py`)

**Revised after codex plan-review** (original draft wrapped only the chunk-collection
loop in `asyncio.wait_for`, missing stream *creation* time, and never closed the
stream on cancellation — both flagged; see review notes below).

- Add `progress_sink: GenerationProgress | None = None` to both signatures.
  Cloud `respond()` accepts and ignores it (no streaming there — a single
  blocking `acompletion()` has no partial state to report).
- In `_respond_local`, wrap **both** `stream = await litellm.acompletion(...)`
  and the chunk-collection loop in one `async with asyncio.timeout(effective_timeout_s):`
  block (project pins Python 3.12; `asyncio.timeout` correctly distinguishes its
  own expiry — surfaced as a plain `TimeoutError` on exit — from an externally
  triggered cancellation, which still propagates as `CancelledError`, unlike
  `asyncio.wait_for`). `effective_timeout_s` is the same value already used for
  the httpx read timeout — no new setting, the one budget is now enforced two
  ways, and now covers the full post-slot-acquisition generation, not just
  consumption.
- Per chunk, update `progress_sink` (first-chunk timestamp into
  `generation_started_monotonic`; append any `delta.content` text).
- **Stream cleanup**: keep a reference to `stream` (initialised `None` before
  the `async with`) and, in a `finally` around the collection loop, best-effort
  `await stream.aclose()` (litellm 1.98.0's `CustomStreamWrapper` exposes this;
  guard with `getattr(..., "aclose", None)` + `contextlib.suppress(Exception)`
  so a missing/failing close never masks the real timeout/cancellation
  propagating through the same `finally`). Without this, a cancelled stream
  leaks into the cached guarded `httpx.AsyncClient` connection pool
  (`_guarded_httpx_clients`, process-lifetime) in an unclosed state.
- Catch `TimeoutError` from the `asyncio.timeout()` block specifically (before
  the general `except Exception`), build `LLMTimeout(f"Local generation
  exceeded its {effective_timeout_s:.1f}s wall-clock budget")` chained
  `from exc`, **and emit the same `MODEL_CALL_ERROR` log line the generic
  handler already emits** (codex flagged the risk of this branch silently
  skipping telemetry — it must not). This is what makes AC-3 observable: the
  inner budget now terminates a steadily-streaming call at its stated value,
  distinct from the httpx read-timeout's own (different) message.
- `except asyncio.CancelledError: raise` stays above this, unchanged — an
  externally triggered cancel (the outer hard-deadline `wait_for` in
  `sub_agent.py`, or the global dispatch cancel) is not our own timeout and
  must keep propagating as `CancelledError`, which `asyncio.timeout()`
  preserves correctly per the note above.

**Codex review notes (2026-09-04):**
- Confirmed the same-value read-timeout + wall-clock-timeout pairing is sound,
  not redundant — they bound different failure shapes.
- Flagged the collection-only `wait_for` boundary as incomplete (stream
  creation wasn't covered) → fixed by wrapping `acompletion()` too.
- Flagged missing `stream.aclose()` on cancellation as a real connection-pool
  leak risk against the process-wide cached client → fixed with the `finally`
  above; a test asserting the cached pool still serves a subsequent request
  after a cancelled stream is added (see Tests).
- Confirmed the `GenerationProgress` mutable-sink pattern is sound under a
  strict one-sink-per-call, single-writer contract (already the plan's
  design) — a callback or caller-side iteration would be more invasive for no
  benefit here.
- Confirmed deferring AC-4 (no live fan-out from this build session) is
  correct given no running service and no standing live-turn authorization.

### `SubAgentSpec`/`SubAgentResult` (`orchestrator/sub_agent_types.py`)

Add two fields to `SubAgentResult` (additive, no existing field's meaning
changes):
```python
tokens_generated: int = 0
elapsed_generation_ms: float | None = None
```
`token_count` is left as-is (pre-existing word-count approximation of the
*final* response) — `tokens_generated` is the same approximation applied to
`progress.content`, valid on both success and killed paths, so a fan-out's
survivors and its casualties are comparable on the same field for the first
time. **Naming, per codex review:** neither field is a real tokenizer count —
both are word-count estimates. `tokens_generated`'s docstring says so
explicitly ("word-count estimate, same approximation convention as
`token_count`") rather than implying exactness; adding a real tokenizer is
out of scope for this ticket. `elapsed_generation_ms` is measured from
first-chunk-received to result-build time — codex noted this can include a
few ms of post-cancellation cleanup on the killed path; not worth
correcting given it is negligible against 60-85s budgets, but documented in
the field's docstring so a future reader isn't misled into treating it as
exact.

### `orchestrator/sub_agent.py`

- Hoist the existing inline `summary_cap = 2000` to a module constant
  `_SUMMARY_CAP_CHARS` (needed in both the success and the new killed-result
  path).
- Create `progress = GenerationProgress()` before the `try`, pass
  `progress_sink=progress` into `llm_client.respond(...)`.
- New helper `_killed_result(task_id, spec, duration_ms, progress, error, cost_usd=0.0) -> SubAgentResult`
  building a result from whatever `progress` captured: `full_output`/`summary`
  from `progress.content` (this alone fixes the "digest_chars=0,
  full_output_chars=0" black hole — no new field needed for "partial content",
  the existing ones just get populated now), `tokens_generated` from
  `len(progress.content.split())`, `elapsed_generation_ms` from
  `progress.generation_started_monotonic`.
- `except asyncio.TimeoutError` (outer hard-deadline) and a new
  `except LLMTimeout` (inner generation-budget, raised by `_respond_local`)
  both call `_killed_result`, with distinct `error` text so a reader can tell
  which budget fired without cross-referencing durations.
- `except asyncio.CancelledError` (global dispatch timeout) also switches to
  `_killed_result` for the same reason — it is the same "killed with partial
  progress available" shape, just a different trigger.
- Add `tokens_generated`/`elapsed_generation_ms` to the success-path
  `SubAgentResult` too (`tokens_generated = token_count`,
  `elapsed_generation_ms` from `progress.generation_started_monotonic` when
  set), so the field exists uniformly across every terminal state — a
  fan-out's tok/s is computable the same way for every worker in it.
- Thread both new fields onto `SubAgentCapture` (`captains_log/capture.py`)
  and the `sub_agent_complete` log line, mirroring how every other result
  field already flows there.

## Tests (TDD, new failing-first)

`tests/personal_agent/orchestrator/test_sub_agent.py`:
- AC-1: a stub `llm_client.respond` that accepts `progress_sink`, streams
  slowly (advances `progress_sink.content` and sets
  `generation_started_monotonic` via `asyncio.sleep` steps) past
  `hard_deadline`, never returns. Assert the result carries non-empty
  `full_output`/`summary`, `tokens_generated > 0`, `elapsed_generation_ms` is
  not None.
- The existing `TestSubAgentCaptureEmitted` cases get the same assertions
  added where relevant (capture record carries the new fields).
- Success-path test: `tokens_generated`/`elapsed_generation_ms` populated when
  `progress_sink` was used by the stub, absent (0 / None) when the stub
  ignores it (cloud-shaped mock, back-compat).

`tests/personal_agent/llm_client/test_local_via_litellm.py` (extends the
existing real-dispatch harness — real `litellm.acompletion`, only the
transport is faked):
- New helper: an async-generator SSE body with `await asyncio.sleep()`
  between chunks (httpx.Response accepts an `AsyncIterable[bytes]` for
  `content=` and reads it lazily on iteration, so the delay is real wall-clock
  time inside the test, not merely simulated).
- AC-2/AC-3: dispatch through the real `LiteLLMClient` local path with a
  slow-streaming body whose total time exceeds `timeout_s` but not the test's
  own patience; assert `LLMTimeout` is raised at approximately `timeout_s`
  (not later), and that `progress_sink.content`/`generation_started_monotonic`
  were populated before the raise.
- Confirm `test_role_timeout_is_the_read_budget_not_the_connect_budget` and
  `test_call_site_timeout_overrides_the_declared_default` still pass unchanged
  — the httpx `Timeout(read=...)` construction is untouched.
- **New, per codex review:** after a slow-streaming call is cancelled by the
  wall-clock budget, dispatch a second, ordinary (fast, non-streaming-slow)
  call through the same test-session guarded client cache and assert it still
  succeeds — proof the cancelled stream's `aclose()` did not leave the cached
  `httpx.AsyncClient` connection pool in a state that wedges the next request.

## Finding 3 (added at dispatch, master comment 2026-09-04 10:07) — max_tokens has the same shape

The catalog (`config/models.yaml`, `qwen3.8-flash-next-instruct.max_tokens: 2048`, deliberately
sized "safe because thinking is hard-disabled") never reaches the wire. `expansion_controller.py:408`
builds every `SubAgentSpec` with `max_tokens=settings.sub_agent_max_tokens` (4096), and `sub_agent.py`
always passes that non-`None` value into `llm_client.respond(max_tokens=...)`, which unconditionally
beats the client's own catalog-derived default (`factory.py:114` already sets
`self.max_tokens = model_def.max_tokens` = 2048 correctly — it just never gets used because the
call site always overrides it). Same shape as Finding 1: a value that reads as the declared ceiling
and is not.

**Fix — catalog wins, matching ADR-0121:**
- `SubAgentSpec.max_tokens`: `int = 4096` → `int | None = None` (mirrors `hard_deadline_seconds`'s
  existing "`None` defers to the deployment" convention on the same dataclass).
- `expansion_controller.py`: delete the `max_tokens=settings.sub_agent_max_tokens` line from the
  `SubAgentSpec(...)` construction — omitted means the dataclass default (`None`) applies, which
  flows through `respond()`'s existing `if max_tokens is not None else self.max_tokens` fallback to
  the catalog value. No client/respond() change needed — that plumbing already exists.
- `settings.sub_agent_max_tokens` itself is **not** deleted: `orchestrator/expansion.py`'s
  `parse_decomposition_plan` (the `autonomous`-mode legacy decomposition path, gated behind
  `settings.orchestration_mode == "autonomous"` — default is `"enforced"`, so this path is inactive
  in production) is a separate, legitimate consumer with its own reason to declare a default. Only
  the enforced-mode HYBRID path's silent override is the defect; update the Field's `description` to
  say so explicitly rather than leaving the scope ambiguous for the next reader.
- Tests: `test_sub_agent_types.py`'s default-value assertion (`spec.max_tokens == 4096`) becomes
  `spec.max_tokens is None`; the three `mock_settings.sub_agent_max_tokens = 4096` lines in
  `test_expansion_controller.py` are dead once the call site stops reading that attribute — removed.

## AC-4 — resolved by master's live benchmark (comment 2026-09-04 08:53), not re-measured here

Master already ran the real fan-out measurement this AC asks for: llama.cpp build 10770, MTP depth 1,
3 slots, `--kv-unified`, 27 requests against the live `slm_server`, unique prompt markers (no
cross-request cache riding). Per-request median tok/s: 37.08 (concurrency 1), 19.95 (concurrency 2),
14.42 (concurrency 3) — division is near-exact, confirming memory-bandwidth-bound (11-12% GPU
utilisation), not compute-bound. Within-batch spread is tight (14.06/14.40/14.16 at concurrency 3),
which reframes the ticket's own data: worker A's 12.7 tok/s matches the concurrency-3 rate within
noise (normal); worker D's 65 tok/s exceeds even the single-stream ceiling of 37-41 tok/s (the actual
anomaly, more likely an artefact of how `output_tokens` is counted under speculative decoding than a
real rate — not resolved in this ticket).

Combined with Finding 3, the timeout mechanism is now fully explained rather than merely observed:
at 14.42 tok/s (measured concurrency-3 rate) an 85s deadline permits ~1,224 generated tokens, while
the shadowing `sub_agent_max_tokens=4096` told sub-agents they had almost 4x that budget. Workers C
and D died because their tasks called for more than ~1,200 tokens against a deadline sized for
single-stream throughput while running at a third of it — deterministic, not intermittent. This
AC-4 report goes into the ticket/handoff citing master's benchmark session directly; no new live
fan-out is run from this build session to reproduce it.

**Coordination constraint (master, same 10:07 comment):** `slm_server` is one shared GPU; 4
concurrent requests wedge it (all 504, backend unresponsive, manual restart required). This plan's
own tests never need it — AC-1/AC-2/AC-3 dispatch through the real `LiteLLMClient`/`litellm.acompletion`
code path but with the transport faked (`httpx.AsyncHTTPTransport.handle_async_request` patched, the
same pattern `test_local_via_litellm.py` already uses), so no real network call reaches the GPU. If a
genuine single-stream live probe becomes useful during implementation, that alone needs no
permission; anything at concurrency ≥ 2 against the real backend goes through master to the owner
first, per the constraint — not needed by the current plan.

## Non-goals

- Not touching `worker_hard_deadline_seconds` (85s) — ticket says don't tune it.
- Not touching the cloud (`respond()`, non-local) path — the incident and
  every finding is about the local streaming deployment.
- Not touching `client.py`'s `LocalLLMClient` — dead since ADR-0141 moved
  local dispatch onto `LiteLLMClient._respond_local`; not on any live call
  path from the factory.
