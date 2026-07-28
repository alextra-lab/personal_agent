# FRE-1034 — Captain's Log reflection OOM / event-loop stall

## Problem (from ticket, verified against source)

`generate_reflection_entry` (src/personal_agent/captains_log/reflection.py:285) calls
`get_trace_events(trace_id)` (src/personal_agent/telemetry/metrics.py:439) synchronously on
every turn. `get_trace_events` calls `_read_log_entries()` (metrics.py:137) with NO filter,
which:

1. Opens the live 83MB/262k-line `current.jsonl` PLUS every rotated backup, `json.loads`
   every line, and accumulates ALL of them into one Python list — THEN filters that list down
   to the one matching `trace_id`. Verified: metrics.py:448 → `entries = _read_log_entries()`
   with zero arguments, followed by a list-comprehension filter at metrics.py:451.
2. Runs as plain synchronous file I/O + JSON parsing inside `generate_reflection_entry`,
   which itself runs as a same-event-loop asyncio task (`run_in_background` at
   orchestrator/executor.py:2720 — confirmed this is `asyncio.create_task`, not a thread/process).
   No `asyncio.to_thread` wraps the `get_trace_events` call (contrast: the DSPy call 64 lines
   below, at reflection.py:349, already correctly uses `asyncio.to_thread`).

This causes the four measured OOM kills (container materializes ~400-600MB transiently per
turn). **Correction from codex plan-review**: this is not a "cliff" at the 100MB rotation
threshold — rotation just renames the current file to `.1` and starts a fresh one
(`logger.py:126-145`); retained corpus size doesn't double or spike at any single crossing.
The real exposure is continuous, unbounded growth as backups accumulate: `_read_log_entries`
walks current + up to 5 backups (metrics.py:155-165), so the ceiling is roughly current +
5×100MiB ≈ 600MB — already close to the peaks observed today (933-965MiB container peak) and
getting worse every day the corpus grows, not at a specific future threshold.

## Chosen approach: Accepted fallback (line-prefilter + thread offload)

The ticket names two options:
- **Preferred**: delete the file-reading path entirely, query Elasticsearch (`agent-logs-*`,
  `trace_id` is already an indexed `keyword` field — confirmed in
  `docker/elasticsearch/index-template.json:98`).
- **Accepted fallback**: filter the raw line for the literal `trace_id` string before calling
  `json.loads`, and thread-offload the whole call.

**Going with the fallback**, not the ES rewrite, because:
- `get_trace_events` has 4 callers (reflection.py hot path; `metrics.py:324`
  `get_request_latency_breakdown`, a CLI-only helper; `ui/cli.py:132`, the CLI itself;
  `tests/evaluation/system_evaluation.py`). An ES rewrite means an async signature change
  propagated to a sync CLI command plus a new ES-availability/pagination design — a much larger
  surface for a single-ticket fix.
- The fallback resolves ALL three acceptance criteria with a small, mechanical diff: memory
  becomes O(matching lines) instead of O(corpus), the rotation cliff disappears (corpus is
  never materialized regardless of backup count), and the event loop no longer stalls.
  It doesn't make reflection depend on Elasticsearch being reachable.
- Matches project convention (CLAUDE.md simplicity-first / surgical-changes): the ES migration
  is more invasive than the defect requires and is not what most of the acceptance criteria
  test for.

## Codex plan-review findings (applied below)

A codex:rescue plan review caught one real correctness bug and several proof-strength gaps.
Full findings: session `019fa967-bd85-7d82-84b6-ac41cda8ecbf`. Summary of what changed as a
result:

1. **Substring-prefilter correctness — was NOT SOUND.** The original plan claimed the raw
   line always contains the literal `trace_id` string as a substring ("pure superset test").
   That's false for any `trace_id` needing JSON escaping (e.g. a literal `"` becomes `\"` in
   the serialized line) — the prefilter would then skip a valid matching line before
   `json.loads` ever runs, and the post-parse equality check never gets a chance to catch it.
   Production trace IDs are UUIDs generated at `gateway/chat_api.py:375` and never contain
   JSON metacharacters in practice, but `get_trace_events` is a public function accepting an
   unconstrained `str`, so the fix must not rely on that being true. **Fix:** search for the
   JSON-*encoded* form of the trace_id (`json.dumps(trace_id)[1:-1]`), which is exactly the
   substring that appears in the raw line for ANY input string, escaped or not.
2. **Rotation-cliff framing — was NOT SUPPORTED.** There is no cliff at the first 100MiB
   rollover: rotation just renames the current file to `.1` and starts a new one
   (`logger.py:126-145`); `_read_log_entries` reads current + up to 5 backups
   (`metrics.py:155-165`), so retained-corpus risk grows continuously as backups accumulate,
   capped at roughly current + 5×100MiB ≈ 600MB — it does not double or spike at any single
   threshold crossing. The exposure already exists today (83MB); it gets worse continuously,
   not at a cliff. Language corrected below and in the PR/ticket comment.
3. **`asyncio.to_thread` kwarg necessity — was overstated.** The kwarg form
   (`to_thread(get_trace_events, trace_id=trace_id)`) is compatible with the existing
   `fake_to_thread(fn, **kwargs)` mock in
   `tests/test_captains_log/test_reflection_prompt_manifest_field.py:107`, but it's not the
   *only* compatible form — a `lambda: get_trace_events(trace_id)` closure would also work.
   Keeping the kwarg form anyway, for consistency with the existing DSPy offload at
   `reflection.py:349-367`, which already uses the same pattern.
4. **Fallback-vs-ES tradeoff — was PARTIALLY SOUND.** Missed that
   `tests/evaluation/system_evaluation.py:135` calls `get_trace_events` synchronously from
   inside an *async* `run_scenario` (`system_evaluation.py:112`) — offloading only the
   reflection call site does not fix that caller's event-loop blocking. This is a real gap,
   but out of scope for this ticket: the eval script is a research/offline tool, not a
   concurrent-request path, and the ticket's own acceptance criteria are scoped to the
   *reflection* path's live-turn behavior. Noting it explicitly here and in the PR/ticket
   comment as a known, deliberately-unaddressed adjacent gap rather than silently ignoring it
   or scope-creeping a fix into this PR.
5. **Acceptance-criteria proof plan — was NOT SOUND.** A `json.loads` call-count test proves
   only that non-candidate lines are skipped, not that peak memory is bounded; a
   `to_thread`-was-called spy proves wiring, not that the event loop stays responsive; the
   substring-collision test alone doesn't prove output equivalence across rotated files,
   malformed lines, or JSON-escaped IDs. All three proofs strengthened below (§ Acceptance-
   criteria proof plan, revised).

## Implementation

### 1. `src/personal_agent/telemetry/metrics.py`

Add `line_filter: str | None = None` param to `_read_log_entries`. Immediately after the
existing `if not line: continue` check (metrics.py:176-177), add:

```python
if line_filter is not None and line_filter not in line:
    continue
```

...BEFORE the `json.loads` try block. The post-parse exact-match filter in `get_trace_events`
(`entry.get("trace_id") == trace_id`) remains the correctness guarantee against false
*positives* (a substring match that isn't the real trace_id); the prefilter's own job is to
never produce a false *negative* (skip a line that would have matched) — which requires
searching for the JSON-*encoded* form of the target string, not the raw Python string (see
finding 1 above).

Update `get_trace_events` (metrics.py:448) to compute the JSON-escaped search key and pass it
as `line_filter`:

```python
def get_trace_events(trace_id: str) -> list[dict[str, Any]]:
    escaped_trace_id = json.dumps(trace_id)[1:-1]  # matches the raw JSON substring exactly
    entries = _read_log_entries(line_filter=escaped_trace_id)
    trace_entries = [entry for entry in entries if entry.get("trace_id") == trace_id]
    ...
```

`json.dumps(trace_id)[1:-1]` strips only the outer quotes `json.dumps` adds, leaving exactly
the escaped-body substring that will appear inside the raw JSONL line for that value,
regardless of what characters `trace_id` contains.

No other caller of `_read_log_entries` passes `line_filter` — `get_recent_event_count`,
`query_events`, `get_recent_cpu_load` etc. are unaffected (default `None` preserves current
behavior for those, which are not on the per-turn hot path this ticket addresses).

### 2. `src/personal_agent/captains_log/reflection.py`

Line 285: change

```python
trace_events = get_trace_events(trace_id)
```

to

```python
trace_events = await asyncio.to_thread(get_trace_events, trace_id=trace_id)
```

Using the `trace_id=trace_id` kwarg form (not positional) so this is compatible with
`tests/test_captains_log/test_reflection_prompt_manifest_field.py`, which patches
`asyncio.to_thread` module-wide with a fake that only forwards `**kwargs` to `fn`
(`async def fake_to_thread(fn, **kwargs): return fn(**kwargs)`) — a positional call would
break under that test's mock shape even though it works with the real `asyncio.to_thread`.

`asyncio` is already imported in reflection.py (line 12).

## Acceptance-criteria proof plan (revised per codex findings)

1. **Peak memory stays flat.** A `json.loads` call-count test only proves non-candidates skip
   parsing, not that memory is bounded (candidates are still appended to a list at
   metrics.py:219, so a corpus with the substring appearing incidentally in many unrelated
   lines would still grow). Use `tracemalloc` instead: write a log file with a large corpus
   (~20k lines) of non-matching, otherwise-valid entries plus a small fixed number (~3) of
   real matches, measure peak traced memory across `_read_log_entries(line_filter=...)`,
   and assert it stays within a small constant bound (e.g. under ~200KB) — i.e. does NOT scale
   with corpus size. Compare against calling `_read_log_entries()` with no filter on the same
   file to show the unfiltered path's peak is order(s) of magnitude larger.
2. **Event loop no longer blocks.** A spy that merely asserts `asyncio.to_thread` was *called*
   doesn't prove scheduling behavior (a fake could invoke the function inline and still
   satisfy that assertion — see the existing `fake_to_thread` in
   `test_reflection_prompt_manifest_field.py:107`, which does exactly that). Use the REAL
   `asyncio.to_thread` (do not patch it away) with a deliberately blocking
   `get_trace_events` stub (`time.sleep(0.2)`), run it concurrently via `asyncio.gather` with
   a heartbeat coroutine that records `time.monotonic()` every 10ms, and assert the heartbeat
   never misses more than one or two ticks — i.e. the event loop kept running while the
   "file read" blocked in a separate thread.
3. **Same trace events returned.** Broaden beyond the substring-collision case: cover (a) a
   trace_id containing a JSON metacharacter (e.g. `trace"1`) to prove the escaping fix from
   finding 1 above, (b) a trace_id substring appearing only in an unrelated field
   (`"message"`) of a different entry, to prove no false-positive leak, (c) malformed
   non-JSON noise lines mixed in, to prove no crash and no incidental match, and (d) matches
   split across a rotated backup file (`current.jsonl.1`) and the live file, to prove rotation
   handling is unaffected. Existing tests (`test_get_trace_events`,
   `test_get_trace_events_empty_trace`) continue to cover the ordinary-ASCII-ID and
   no-match cases.

## Files touched

- `src/personal_agent/telemetry/metrics.py` — `line_filter` param + JSON-escaped wiring in
  `get_trace_events`.
- `src/personal_agent/captains_log/reflection.py` — thread-offload the call site.
- `tests/test_telemetry/test_metrics.py` — new tests (tracemalloc bound, escaped-id
  correctness, unrelated-field false-positive guard, malformed-line robustness, rotated-file
  coverage).
- New: `tests/test_captains_log/test_reflection_thread_offload.py` — proves the thread-offload
  with real `asyncio.to_thread` + a heartbeat, not just a call-spy.

## TDD steps

1. Add failing tests to `tests/test_telemetry/test_metrics.py`:
   - `test_read_log_entries_line_filter_bounds_memory_regardless_of_corpus_size` (tracemalloc)
   - `test_get_trace_events_handles_json_escaped_trace_id`
   - `test_get_trace_events_substring_in_unrelated_field_does_not_leak`
   - `test_get_trace_events_skips_malformed_lines_without_crashing`
   - `test_get_trace_events_finds_matches_across_rotated_files`
2. Confirm all fail (`_read_log_entries` has no `line_filter` param yet; `get_trace_events`
   doesn't escape).
3. Implement metrics.py changes (§1 above).
4. Confirm all new tests pass; run
   `make test-file FILE=tests/test_telemetry/test_metrics.py` clean (including the pre-existing
   tests in that file — no regression).
5. Add failing test `tests/test_captains_log/test_reflection_thread_offload.py` using real
   `asyncio.to_thread` + heartbeat (per revised §2 above).
6. Confirm it fails (no `asyncio.to_thread` wrap yet — the blocking stub stalls the heartbeat).
7. Implement reflection.py change (§2 above).
8. Confirm it passes; run
   `make test-file FILE=tests/test_captains_log/test_reflection_thread_offload.py` and
   `make test-file FILE=tests/test_captains_log/test_reflection_prompt_manifest_field.py`
   (regression guard on the existing to_thread mock shape) clean.
9. `make test` (full suite, once).
10. `make mypy`, `make ruff-check`, `make ruff-format`.
11. `code-review` skill at `medium` effort (hot concurrency/memory path, small diff).
12. `security-review` skill (diff touches file-read logic) — expect no findings, but run it
    per Step 8 of the build skill since the diff touches file I/O.

## Post-deploy runbook (for master / ticket comment)

- Sample container cgroup memory (`/sys/fs/cgroup/memory.current` or `docker stats`) during a
  live turn; compare against today's recorded baseline (idle 355MiB, peaks 933/965MiB on two
  turns) — expect peak to stay near idle.
- Confirm no synchronous file-parse on the main thread during a turn (stack sample, or just
  confirm the shipped code has the `asyncio.to_thread` wrap).
- Re-run reflection for one known trace_id before/after if possible, or rely on the
  substring-superset test as the correctness proof (no live A/B needed — this is a pure
  refactor of internal filtering, not new behavior).
- Master should reconsider the 768MiB → 2GiB container-limit bump (applied live today as an
  unblock) once this lands and peak memory is re-measured live.

## Extension after PR #731 bounce: Elasticsearch hot path for the reflection caller

Master bounced PR #731 not as a rejection but an extension: keep everything above (the
`line_filter` prefilter stays exactly as shipped, still serving the CLI/eval callers), and
additionally give the ONE hot per-turn caller (`reflection.py`'s `generate_reflection_entry`)
an Elasticsearch-backed path instead of the thread-offloaded file scan.

**Why the fallback-only argument didn't hold.** The original plan justified skipping ES
because `get_trace_events` has four callers and an async signature change would propagate to
a sync CLI. Master checked: only ONE of the four runs per turn (`reflection.py:285` at the
time). `ui/cli.py:132` and `get_request_latency_breakdown` (called only from `cli.py:194`) are
manual CLI commands; `tests/evaluation/system_evaluation.py` is an offline eval script. None
runs per turn — so ES never actually required touching the shared sync function. The fix:
add a NEW async function alongside it, used only by the hot caller. No propagation, no
refactor of the sync path.

**The measurement that decided it.** Master measured concurrency against the live corpus.
Threaded file scan (the fallback-only fix, even off the event loop): 1 concurrent → 0.113s, 2
→ 0.352s, 4 → 0.706s, 8 → 1.544s — linear, because `line_filter not in line` is a pure-Python
string operation that holds the GIL, so threads serialize. Elasticsearch over the same range:
1 → 0.037s, 2 → 0.036s, 4 → 0.124s, 8 → 0.190s — network I/O releases the GIL, so queries
genuinely run in parallel. At 8 concurrent turns that's 1.544s vs 0.190s, and the gap widens
with both corpus size and concurrency.

**What was added (line_filter and its tests untouched):**

1. `TelemetryQueries.get_trace_events(trace_id, size=2000)` (new method,
   `src/personal_agent/telemetry/queries.py`) — a plain `term` query on the `trace_id`
   keyword field (exact match, no `.keyword` suffix, no analysis — confirmed live: 22-27ms
   steady state for a real trace). Translates ES's `event_type`/`@timestamp` field names back
   to `event`/`timestamp` (`_translate_es_source_to_log_entry`) so the result is
   interchangeable with the file-based path's output for every downstream consumer
   (`_summarize_telemetry`, `_extract_failure_excerpt`, `build_prompt_manifest`). Verified live
   against `es_handler.py`/`es_logger.py`'s actual write shape — ES documents have
   `event_type`/`@timestamp`/`message`, never `event`/`timestamp`; every other custom field
   (`duration_ms`, `tool`, `status`, etc.) passes through unchanged since both paths originate
   from the same structlog event dict. Raises on any ES error — no fallback logic inside it;
   the caller decides.
2. `_fetch_trace_events(trace_id)` (new function, `src/personal_agent/captains_log/reflection.py`)
   — waits `ES_REFRESH_WAIT_SECONDS = 5.5` (see refresh-window handling below), then `await`s
   the ES method directly (NOT wrapped in `asyncio.to_thread` — that would reintroduce the
   GIL-bound serialization this change exists to remove), and falls back to the existing
   thread-offloaded file path (unchanged) only if the ES call raises — a deliberate, logged
   fallback (`es_trace_fetch_failed_falling_back_to_file`), not the normal path. Explicitly
   disconnects its `TelemetryQueries` client in a `finally` block (a code-review pass flagged
   that a per-call client left open would itself be a small resource leak on this exact
   per-turn hot path — directly on-theme for an OOM ticket, so fixed rather than left
   for later).
3. `generate_reflection_entry`'s call site now reads `trace_events = await _fetch_trace_events(trace_id)`.

**Refresh-window handling.** `agent-logs-*` has `index.refresh_interval=5s` (not the 1s
default — confirmed on both the live index via `_settings` and
`docker/elasticsearch/index-template.json:9`). Reflection fires ~1.5s after task completion,
inside that window, so an immediate query would silently return fewer events than the trace
actually has — missing exactly the newest events of the trace being reflected on. Since
reflection is fire-and-forget (`run_in_background` → `asyncio.create_task`, nothing awaits it,
confirmed no timeout wrapper at the `orchestrator/executor.py:2720` call site), the cheapest
correct fix is a fixed wait before querying — not forcing an index refresh on read (which
master explicitly ruled out). `ES_REFRESH_WAIT_SECONDS = 5.5` is asserted-by-test to exceed
whatever `index.refresh_interval` the template actually specifies
(`TestEsRefreshWaitExceedsConfiguredInterval`), so a future change to the refresh interval
fails that test rather than silently regressing.

**Live proof against a freshly created trace** (per master's explicit ask — this must be a
trace young enough that the refresh window is actually being tested, not one already stale
enough to have refreshed regardless): wrote 6 synthetic events tagged
`trace_id=fre-1034-verify-b73c1e96-f43b-4374-b275-9a326838f76b` directly into the live
`agent-logs-*` index (bypassing the app; same index, same mapping, same real
`refresh_interval=5s`), then queried at two offsets from the write:
- t=1.50s (the OLD/naive timing, matching reflection's actual ~1.5s-after-completion firing
  point): **0/6 docs visible** — confirms the bug empirically, not just from the template config.
- t=5.51s (the NEW wait): **6/6 docs visible** — confirms `ES_REFRESH_WAIT_SECONDS=5.5` is
  sufficient.

**Live concurrency proof**, same methodology as master's own measurement, against a real
existing trace (`1752d25f-fbdd-4fbf-91a8-604e34733df5`, 1 event): 1 concurrent → 0.016s, 2 →
0.017s, 4 → 0.059s, 8 → 0.147s — consistent with master's numbers (sub-linear growth, and even
at 8x concurrency roughly an order of magnitude faster than the threaded scan's 1.544s).
Deterministic, mocked equivalents of both the freshness proof and the concurrency proof are
also in the test suite (see below) so CI enforces the same properties without depending on
live infrastructure or real sleep durations.

**Elasticsearch-unreachable behavior (explicit decision, per master's ask).** If the ES call
raises for any reason (connection refused, timeout, auth failure, etc.), `_fetch_trace_events`
logs a warning and falls back to the existing thread-offloaded file-based path — the exact
same code that shipped in the fallback-only PR, unchanged. This is a deliberate, narrow
fallback (only triggered by an ES exception, not on the normal path) and keeps reflection
functional even when Elasticsearch is down, at the cost of reverting to the file scan's
per-call concurrency characteristics for that one degraded call.

### Updated test plan

- `tests/test_telemetry/test_queries.py` — `TelemetryQueries.get_trace_events`: exact-term
  query shape (`test_get_trace_events_builds_exact_term_query`), field-name translation
  (`test_get_trace_events_translates_es_field_names`), error propagation with no fallback
  baked in (`test_get_trace_events_propagates_es_errors`), and a deterministic concurrency
  proof using a mocked ES client with a fixed simulated network delay
  (`test_get_trace_events_concurrent_calls_do_not_scale_linearly` — asserts 8 concurrent calls
  stay within 3x a single call's duration, reproducing the live measurement's shape without
  depending on a live cluster).
- `tests/test_captains_log/test_reflection_thread_offload.py` — restructured:
  `TestFetchTraceEventsEsHotPath` proves ES-success never touches the file path
  (`test_uses_es_result_and_never_touches_the_file_path`), ES-failure falls back correctly
  (`test_falls_back_to_file_path_when_es_unreachable`), and the wait is wired correctly
  (`test_waits_out_the_refresh_window_before_querying`, a spy on `asyncio.sleep` — deterministic,
  doesn't actually sleep). `TestEsRefreshWaitExceedsConfiguredInterval` is the self-updating
  consistency check against the real template value. `TestReflectionTraceEventsThreadOffload`
  keeps the original real-`asyncio.to_thread`-plus-heartbeat proof, now forcing the ES attempt
  to fail first so it exercises the file-fallback branch specifically.
- `tests/test_captains_log/test_reflection_prompt_manifest_field.py` — the one pre-existing
  test that patched the file-based `get_trace_events` directly now patches
  `reflection._fetch_trace_events` instead (the new call site), since HOW trace events are
  fetched is no longer that test's concern — it's about manifest-building from whatever trace
  events come back.

### Self-review (second pass, after the ES extension)

`feature-dev:code-reviewer` (medium effort) — no confirmed findings against the escaping/
translation logic, the fixed wait's justification, the fallback safety, or test quality;
flagged (below its own reporting bar) that the new per-call `TelemetryQueries()` client was
never closed — fixed by adding an explicit `disconnect()` in a `finally` block rather than
left as known debt, since it's directly on-theme for an OOM ticket. `security-review` — no
findings ≥8 confidence in any category (no query injection: `trace_id` is a JSON value in a
parameterized client call, never parsed as query syntax; no information disclosure: same
exact-match scope as the file path; no unsafe deserialization: standard `elasticsearch-py`
JSON handling).
