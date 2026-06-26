# FRE-598 — KG-quality pipeline "0 proposals": real root cause = budget gate measures the wrong metric

**Linear:** FRE-598 (Approved, Memory Recall Quality) · **Refs:** ADR-0030 (promotion), ADR-0040 (issue-budget gate), ADR-0060 (KGQ stream), FRE-364 (origin, canceled)

## Finding (premise was stale)

The KG-quality pipeline is **not broken**. Live on the VPS (2026-06-26) it runs end-to-end and has
produced real Linear issues **FRE-423 / 424 / 428 / 430** (auto-created by the ADR-0030 promotion
pipeline). FRE-364's "0 proposals" was measured 2026-05-14, before the first graph-quality Captain's
Log entry (2026-05-27) — a historical state, since resolved.

The **present-day** reason new anomalies stop short of Linear is a different, real defect:

### Root cause: the ADR-0040 budget gate counts the wrong thing

`PromotionPipeline.run()` pauses all promotion when
`count_non_archived_issues(team) > issue_budget_threshold (200)`. The intent is backpressure: *don't
pile more open work onto an already-large open backlog.* But `count_non_archived_issues` counts every
**non-archived** issue, and Linear does **not** archive Done/Canceled issues while their project stays
open. So the count is dominated by finished work that will never archive.

Live breakdown (Linear GraphQL, read-only, 2026-06-26):

| | count | |
|---|---|---|
| non-archived **total** (what the gate counts today) | **263** | > 200 → PAUSED |
| — terminal (Done 128 + Canceled 15 + Duplicate 3) | 146 (56%) | never archives while projects open |
| — **open/active** (Backlog/Approved/Needs-Approval 110 + In Progress 7) | **117** | < 200 → would be OPEN |

The valve is stuck shut on 146 phantom (terminal) issues, blocking **all** auto-promotion — KG-quality
included. Gate is correct to exist; its **metric** is wrong.

## Fix (surgical, one PR)

Count **open work** instead of "non-archived". Add a state-type exclusion to the GraphQL filter; keep
the threshold (200) unchanged.

Verified against the live API — the exact production filter returns the expected count:
`filter.state.type.nin = ["completed","canceled","duplicate"]` → **117** (vs 263 today).

### Steps

1. **`src/personal_agent/captains_log/linear_client.py`** — rename `count_non_archived_issues` →
   `count_open_issues`; add module constant `_TERMINAL_STATE_TYPES = ("completed", "canceled",
   "duplicate")`; inject `"state": {"type": {"nin": list(_TERMINAL_STATE_TYPES)}}` into the filter
   variables. Update docstring to state it counts open (non-terminal) issues. Keep pagination/cap logic.
   → verify: unit test asserts the outgoing filter carries the `state.type.nin` clause.

2. **`src/personal_agent/captains_log/promotion.py:281`** — call `count_open_issues(...)`. Keep the
   `issue_budget_promotion_paused` / `issue_budget_warning` log events and threshold logic unchanged.
   → verify: `test_promotion.py` budget tests still gate at 201>200 / 50<200.

3. **`src/personal_agent/captains_log/feedback.py:470`** — call `count_open_issues(...)`.
   → verify: `test_feedback_loop.py` passes.

4. **Tests** — update mock attribute names (`count_non_archived_issues` → `count_open_issues`) in
   `test_promotion.py`, `test_feedback_loop.py`; in `test_linear_client.py` rename the test block and
   add an assertion that the GraphQL `variables["filter"]` contains
   `{"state": {"type": {"nin": [...]}}}` with the three terminal types.

### TDD order

1. Add failing test in `test_linear_client.py`: call `count_open_issues`, capture `_call` variables,
   assert `filter.state.type.nin == ["completed","canceled","duplicate"]`. Run → fails (no such method).
2. Implement step 1. Run → passes.
3. Update callers (steps 2–3) + remaining tests (step 4). Run module tests green.

### Test commands

```bash
make test-file FILE=tests/test_captains_log/test_linear_client.py
make test-file FILE=tests/test_captains_log/test_promotion.py
make test-file FILE=tests/test_captains_log/test_feedback_loop.py
make mypy && make ruff-check && make ruff-format
```

## Part B — durability fix (owner-requested, same PR)

The ADR-0054 "durable JSONL" leg of the Wave-3 self-improvement streams writes to container-relative
`telemetry/<stream>/` → `/app/telemetry/<stream>`, which is **not** a mounted volume in
`docker-compose.cloud.yml` (only `captains_log` + `feedback_history` are). So the JSONL is written daily
but lives in the ephemeral container layer — lost on `docker compose up`/rebuild. ADR-0054's durable leg
isn't durable in cloud.

Affected ADR-0054 self-improvement stream legs (same family as the bug):

| dir | ADR / stream |
|---|---|
| `telemetry/graph_quality` | ADR-0060 Stream 8 (the one in the ticket) |
| `telemetry/context_quality` | ADR-0059 |
| `telemetry/error_patterns` | ADR-0056 |
| `telemetry/freshness_review` | ADR-0060 Stream 6 |

### Fix — `docker-compose.cloud.yml` (cloud only; local `make dev` writes to the host repo tree)

Follow the existing per-dir named-volume pattern (the `captains_log`/`feedback_history` precedent):

1. Under the `seshat-gateway` service `volumes:` block, add four mounts:
   ```yaml
   # ADR-0054 durable JSONL leg of the self-improvement streams must survive container
   # recreation (FRE-598). Without these the *.jsonl dual-write is ephemeral; data also
   # lands in Redis + ES, but the on-disk durable record is the ADR-0054 source of truth.
   - seshat_graph_quality_cloud:/app/telemetry/graph_quality
   - seshat_context_quality_cloud:/app/telemetry/context_quality
   - seshat_error_patterns_cloud:/app/telemetry/error_patterns
   - seshat_freshness_review_cloud:/app/telemetry/freshness_review
   ```
2. Declare the four volumes under the top-level `volumes:` section (`driver: local`).
   → verify: `docker compose -f docker-compose.cloud.yml config -q` (YAML + volume refs validate).

**Deliberately out of this fix:** `telemetry/{skill_routing_monitor,tool_result_digest,within_session_compression,logs}`
— monitor/cache/log state, not ADR-0054 Captain's-Log dual-writes (`tool_result_digest` is PARKED/OFF).
`telemetry/logs` is intentionally ephemeral (ships to ES; lifecycle-purged). Noted for a possible
follow-up, not bundled.

**Deploy note (master):** this takes effect only on container recreation. The few ephemeral
`GQ-*.jsonl` currently in the container layer are not migrated (already non-durable; also in Redis+ES).

## Doc-drift fixes (codex review)

- `src/personal_agent/captains_log/feedback.py:468` — comment "Daily budget log (non-archived count)"
  → "(open-issue count)".
- `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` — update the two "non-archived count" references to
  reflect open-issue counting.

## Out of scope (file as follow-ups, do NOT bundle)

- **Operational backlog sweep** — even after the metric fix, archiving terminal issues (or closing
  finished projects so Linear archives them) keeps the count honest. Not code.
- **Threshold value (200)** — unchanged. A budget knob; not touched without explicit owner sign-off.
- **Other ephemeral telemetry dirs** (skill_routing_monitor / tool_result_digest /
  within_session_compression) — durability TBD; not ADR-0054 CL legs. → optional follow-up ticket.
