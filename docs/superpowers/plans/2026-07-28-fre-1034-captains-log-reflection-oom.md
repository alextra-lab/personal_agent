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
