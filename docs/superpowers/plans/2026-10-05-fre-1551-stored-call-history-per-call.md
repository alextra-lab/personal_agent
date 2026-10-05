# FRE-1551 — The stored call history lists calls, not distinct tool names

Ticket: FRE-1551 (bugfix, Approved). Related: FRE-1543 (stored summary), FRE-1547 (tool events).
Backing design: ADR-0123 T4 (folded per-turn summary).

## Cause

* `build_turn_summary` (`src/personal_agent/transport/turn_summary.py`) keeps a tool name once.
* `buildTurnSummary` (`seshat-pwa/src/lib/phase-summary.ts`) does the same on the live side.
* The live panel (`ToolIndicator`) shows one row per call. After a reload the panel reads the
  stored summary, so 5 calls reload as 2 rows.

## Design

`tools` changes from `list[str]` to a list of per-call entries `{name, status}`, in
`TOOL_CALL_START` order.

* `status` is `completed`, `failed` or `unfinished`.
* The wire has no call id today, and parallel same-name calls can end in a different order
  than they start (`asyncio.gather` in `step_tool_execution`). Pairing by name alone gives a
  row the outcome of its sibling (codex plan-review, High). So `TOOL_CALL_START` and
  `TOOL_CALL_END` carry the existing `tool_call_id` (`ToolStartEvent` / `ToolEndEvent` get an
  optional `tool_call_id`; `_dispatch_announced` passes it). An end closes the open row with
  the same id. An event without an id (older emitters) falls back to the first open row of
  that name, the rule `useAgentStream.ts` uses today.
* An end with `result == "failed"` marks the row `failed` (the executor sets that result on a
  failed dispatch, FRE-1547). Any other end marks it `completed`. A row with no end is
  `unfinished` (cancelled or errored mid-call).
* Rows already stored hold plain strings. `parseTurnSummary` accepts a string as a row with
  status `unknown` (the old record kept no outcome and dropped repeated calls). The server
  never writes `unknown`.

One field, not two: the stored `tools` becomes the call list. No second copy of the names.

## Steps

1. Backend test first, `tests/personal_agent/transport/test_turn_summary.py`:
   * Update `test_two_round_turn_holds_phases_in_order_with_server_durations`: three starts
     (`web_search`, `web_search`, `read_url`) store 3 rows in order.
   * Add: parallel pair of one tool plus one other, with ends in a different order; each row
     has its status (`completed`, `failed`, `unfinished`).
   * Add: two same-name calls that end in reverse order; each row gets its own outcome (needs ids).
   * Add: adapter test, the envelope carries `tool_call_id`; executor test with distinct ids.
   * Run: `make test-file FILE=tests/personal_agent/transport/test_turn_summary.py`. Expect FAIL.
2. Implement the call id: `transport/events.py`, `agui/adapter.py`, `executor.py:_dispatch_announced`.
   Then in `turn_summary.py`: add `TurnSummaryTool` (frozen), `ToolStatus` literal, change
   `TurnSummaryRecord.tools`, fold `TOOL_CALL_START` / `TOOL_CALL_END` as above, update the
   module docstring. Re-run step 1. Expect PASS.
3. Update the tests that assert the old shape:
   `tests/personal_agent/events/test_request_completed_consumer_integration.py` (lines 132, 184),
   `tests/personal_agent/orchestrator/test_fre1547_tool_lifecycle_events.py` (summary assertion;
   add a same-name parallel case that drives the real `step_tool_execution`),
   `tests/test_service/test_chat_stream_request_completed.py` (empty list, no change expected).
4. PWA test first: `phase-summary` unit tests, `TurnSummaryPanel.test.tsx`,
   `StreamingChat.reload.test.tsx`:
   * `buildTurnSummary` with `[web_search, web_search, fetch_url]` returns 3 entries.
   * `parseTurnSummary` of the server shape returns the same 3 entries; a legacy string still
     parses; a malformed entry is dropped.
   * AC-2: the panel rendered from the live summary and from the parsed stored summary show the
     same number of tool rows (3), and the header says `3 tools`.
   * Run: `cd seshat-pwa && npx vitest run src/__tests__/TurnSummaryPanel.test.tsx src/__tests__/useAgentStream.summary.test.tsx src/__tests__/StreamingChat.reload.test.tsx`. Expect FAIL.
5. Implement PWA: `ToolSummaryEntry` in `types.ts`; `buildTurnSummary` maps each `ToolCall`
   (one entry per call; `running` → `unfinished`, completed with result `failed` → `failed`);
   `parseTurnSummary` parses entries; `TurnSummaryPanel` renders one chip per entry, keyed by
   index, with `data-status`, failed in the error colour. Fix the existing fixtures that pass
   `tools: ['x']`. Re-run step 4. Expect PASS.
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `uv run ruff format tests` ·
   `pre-commit run --all-files` · `cd seshat-pwa && npm run lint && npx vitest run && npx tsc --noEmit`.
7. Commit, self-review (`feature-dev:code-reviewer` on `git diff origin/main...HEAD`), PR,
   handoff comment on FRE-1551.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | Backend test: 2 same-name + 1 other TOOL_CALL_START stored as 3 rows, in order, each with a status. |
| AC-2 | PWA test: panel from the live summary and from the stored summary shows the same 3 rows. |
| AC-3 (live) | After deploy, master compares the stored row count with the `TOOL_CALL_START` count in `session_events`. |

## Diff class

Not a production write path change beyond the existing `turn_summary` JSON on the assistant
message. No schema change (JSON column content only). No cost or governance code. Self-serve.
