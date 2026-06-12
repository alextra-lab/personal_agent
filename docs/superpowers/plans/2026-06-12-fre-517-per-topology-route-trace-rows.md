# FRE-517 — ADR-0088 seam: per-topology route-trace rows `(trace_id, task_id)` fan-out

**Ticket:** FRE-517 (Approved, Tier-2:Sonnet, project Observability Foundation)
**ADR:** ADR-0088 §D1 (topology first-class), §D2 (seam identity tuple `trace_id, session_id, task_id, topology`), §D6 (durable write)
**Builds on:** FRE-513 (spine + `(trace_id, task_id)` key migration, PR #178) · FRE-452 (ledger) · FRE-514 (REST read surface)

## Goal

Emit one durable route-trace row **per sub-agent segment** keyed by `(trace_id, task_id)`, in
addition to the existing turn-level row (`task_id = NULL`). Generalize the read path so a trace
returns all its rows (turn-level + segments).

## Decisions (owner-confirmed 2026-06-12)

1. **`task_id` representation — widen `SubAgentResult.task_id` to `UUID`.** The
   `route_traces.task_id` column is `UUID`; the current `"sub-{12hex}"` string can't go in it.
   The **domain object** carries a real `uuid.uuid4()`. **Wire/log/ES boundaries keep `str`**
   (`SubAgentProgressEvent.task_id: str`, `SubAgentAuditCapture.task_id: str`, structlog) — we
   pass `str(task_id)` there, matching the existing string-id convention for bus events. The
   `"sub-"` prefix is generated-and-logged only; nothing parses it, so the format change is safe.

2. **Row layout — turn-level row + N segment rows.** The existing turn-level row (`task_id NULL`,
   authoritative `SUM(api_costs)`, `sub_agents` JSONB) is unchanged. Per-sub-agent segment rows are
   **additive**, discriminated by `task_id IS NOT NULL`.

3. **Attribution — per-segment cost from `SubAgentResult`, not `api_costs`.** `api_costs` has **no
   `task_id` column**; sub-agent calls bill under the turn `trace_id`. So a segment row's cost is
   `SubAgentResult.cost_usd` (live == authoritative == self-sourced, so `cost_reconciled=True`).
   Per-sub **token** split is unavailable (only an aggregate `token_count` exists) — and it is
   **already captured** in the turn-level row's `sub_agents` JSONB (`token_count`, `cost_usd` per
   sub). Segment rows therefore set `input_tokens=output_tokens=0` (documented), and the turn-level
   row remains the authoritative roll-up.

4. **Turn-level endpoints stay turn-level.** `list_recent` / `list_by_session_id` gain
   `WHERE task_id IS NULL` so the FRE-514 dashboards/filters keep returning one row per turn (a
   no-op for existing data, where every row is turn-level). Segments are reachable only via the
   per-trace fetch.

## Out of scope (noted, not built)

- Projector-health counter `projector_events_received` (FRE-513 codex Q2) — file as follow-up.
- `delegate` topology has no `SubAgentResult` fan-out today, so it produces only a turn-level row;
  no segment rows until a delegate result-set exists. No special-casing here.

---

## Files & changes

### 1. `src/personal_agent/orchestrator/sub_agent_types.py`
- `SubAgentResult.task_id: str` → `task_id: UUID`. Update docstring. Add `from uuid import UUID`.

### 2. `src/personal_agent/orchestrator/sub_agent.py`
- `import uuid` already present. Line 235: `task_id = f"sub-{uuid.uuid4().hex[:12]}"` →
  `task_id = uuid.uuid4()`. **Immediately bind `task_id_str = str(task_id)` once** (codex fix #3)
  and use `task_id_str` at *every* string boundary so no UUID leaks:
  - `_publish_sub_agent_progress(task_id=task_id_str)` (helper keeps `task_id: str`).
  - `_run_tooled_loop(..., task_id=task_id_str)` (helper keeps `task_id: str`).
  - all `logger.*(task_id=task_id_str)`.
  - FRE-505 `SubAgentAuditCapture(task_id=task_id_str)` (`capture.py:111` is typed `str`; doc id
    `f"{trace_id}:{task_id}"` at `capture.py:168` stays string-safe).
- `SubAgentResult(task_id=task_id, ...)`: pass the `UUID` unchanged (the only typed-UUID boundary).

### 3. `src/personal_agent/observability/route_trace/assembler.py`
- `_sub_agent_records` (turn-level JSONB): `"task_id": getattr(s, "task_id", None)` →
  `"task_id": str(s.task_id)` (UUID isn't JSON-serialisable for `json.dumps` in the ledger).
- **New pure function** `assemble_sub_agent_route_trace(ctx, sub, *, created_at=None) -> RouteTraceRow`:
  builds a sparse segment row — `trace_id`/`session_id` from ctx, `task_id=sub.task_id`,
  `model_role="sub_agent"`, `tools_used=tuple(sub.tools_used)`,
  `cost_live_usd=cost_authoritative_usd=sub.cost_usd`, `cost_reconciled=True`,
  `input_tokens=output_tokens=0`, `final_reply_chars=len(sub.full_output or "")`,
  `error_type="sub_agent_failed" if not sub.success else None`,
  `orchestration_event="primary_handled"` (neutral; the discriminator is `task_id IS NOT NULL`).
  - **Token-split signal (codex fix #5):** `SubAgentResult.token_count` is a word-split *estimate*,
    not a billed input/output split, so leaving `input_tokens=output_tokens=0` is ambiguous to
    readers. The segment's `sub_agents` JSONB carries one **self-descriptor** record making the
    gap explicit: `({"task_id": str(sub.task_id), "token_count": sub.token_count,
    "token_count_is_estimate": True, "token_split_available": False},)`. (For a segment row
    `sub_agents` describes the segment itself — it has no children — documented in the docstring.)
- **Export** `assemble_sub_agent_route_trace` from the route-trace package `__all__`
  (`observability/route_trace/__init__.py`, today only exports `assemble_route_trace` — codex fix #4).

### 4. `src/personal_agent/observability/route_trace/ledger.py`
- `_SELECT_SQL`: `WHERE trace_id = $1 ORDER BY (task_id IS NOT NULL), created_at ASC, task_id ASC`
  (turn-level NULL row first, then segments **chronologically** — codex fix #1: `NULLS FIRST` would
  have sorted segments by UUID lexicographically, not by time).
- `_SELECT_BY_SESSION_SQL`: add `AND task_id IS NULL` (no behavior change for existing data).
- `list_recent`: seed `clauses` with `"task_id IS NULL"` so it's always present.
- `get_by_trace_id(trace_id) -> list[RouteTraceRow]`: `fetch` (not `fetchrow`), return
  `[_row_from_record(r) for r in records]` (empty list when none/unconnected). Update docstring.

### 5. `src/personal_agent/observability/topology/seam.py`
- After the turn-level `ledger.write(row)` in `_write_durable_row`, write one segment row per
  `ctx.sub_agent_results` via `assemble_sub_agent_route_trace`, each through `ledger.write`
  (idempotent on `(trace_id, task_id)`).
- **Segment-write failure isolation (codex fix #2):** the current single `try/except` returns
  `0.0` on *any* failure — if a segment write throws *after* the cost fetch + turn-level write, it
  would corrupt `TurnCompletedEvent.cost_authoritative_usd`. Restructure so `cost` is captured
  first, then segment writes run in a **separate inner `try/except` that cannot change the returned
  `cost`** (each segment wrapped so one bad segment doesn't drop the rest). `CancelledError` still
  propagates (not caught).

### 6. `src/personal_agent/gateway/route_trace_api.py`
- `GET /{trace_id}` now returns `list[dict]` (all rows for the trace). 404 when the list is empty.
  Update return annotation, docstring, and the log line.

### 7. `scripts/eval/fre453_canonical_evalset/harness.py` (line ~484)
- `get_by_trace_id` now returns a list. Poll for the **turn-level** row:
  `rows = await ledger.get_by_trace_id(tid); turn = next((r for r in rows if r.task_id is None), None)`;
  return `turn` when present (preserves the helper's `RouteTraceRow | None` contract).

---

## Tests (TDD — write first, confirm red, then implement)

### `tests/observability/route_trace/test_assembler.py`
- `test_assemble_sub_agent_route_trace_segment_shape` — task_id is the sub's UUID, model_role
  `sub_agent`, cost from `sub.cost_usd`, reconciled True, tokens 0, error_type set on failure.

### `tests/observability/route_trace/test_ledger.py`
- Update `get_by_trace_id` test: now returns a `list`; assert turn-level + segment rows come back,
  turn-level first.
- `test_get_by_trace_id_returns_all_segments` — write 1 turn-level + 2 segment rows, assert 3 rows.
- `test_list_recent_excludes_segments` / `test_list_by_session_excludes_segments` — segment rows
  present in table are not returned by the turn-level endpoints.

### `tests/observability/topology/test_seam.py`
- `test_observe_topology_writes_segment_row_per_sub_agent` — ctx with 2 `sub_agent_results` →
  ledger `write` called 3× (1 turn-level + 2 segments) with the right `task_id`s.

### `tests/personal_agent/gateway/test_route_trace_api.py`
- Update `get_by_trace_id` mock to return a list; `GET /{trace_id}` returns the list; 404 on empty.

### `tests/personal_agent/orchestrator/` (sub-agent)
- `test_sub_agent_result_task_id_is_uuid` — `run_sub_agent` produces a `SubAgentResult` whose
  `task_id` is a `UUID`; progress event still receives a `str`.

### Fixture sweep (codex fix #4 — do in one pass, no `# type: ignore` band-aids)
- Update every `SubAgentResult(task_id="sub-...")` literal to `task_id=uuid4()` across:
  `tests/.../test_sub_agent_types.py`, `test_classifier.py`, `test_assembler.py`,
  `test_expansion_controller.py`, `test_gateway_integration.py` (grep
  `SubAgentResult(` to enumerate). Prefer a shared `_make_sub_agent_result(**overrides)` helper
  where several fixtures repeat.
- Update every `get_by_trace_id` call/mock to the new `list` return (ledger test, gateway test,
  eval harness).

---

## Quality gates (all green before PR)

```bash
make test-file FILE=tests/observability/route_trace/test_assembler.py
make test-file FILE=tests/observability/route_trace/test_ledger.py
make test-file FILE=tests/observability/topology/test_seam.py
make test-file FILE=tests/personal_agent/gateway/test_route_trace_api.py
make test            # full unit suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Follow-up tickets (Needs Approval, project Observability Foundation)
- Projector-health counter `projector_events_received` (FRE-513 codex Q2).

## PR
One phase = one PR. Pre-merge checklist only. Stop at PR — no merge/deploy/MASTER_PLAN.
