# FRE-572 — Backend Monitors: A quality incident + D reset-cadence

**Date:** 2026-06-20  
**Branch:** `fre-572-backend-monitors`  
**Refs:** ADR-0092 §D5/§D7, ADR-0047, ADR-0081, FRE-570 (dependency — merged)

---

## Scope (3 bullets)

- **A incident:** durable JSONL + structlog→ES record when gateway budget compaction drops content, keyed by severity (phase 2 = high, phase 1/3 = low); builds on `telemetry/context_quality.py` JSONL-write pattern without touching the existing `IncidentTracker` (different governance concern).
- **D cadence monitor:** add `turns_since_reset` to the existing `frozen_reset_fired` structlog log; emit a per-reset ES doc to `agent-monitors-cache-reset-cadence-*` carrying `actual_turns`, `l_star`, `deviation_turns`, `reason` — the "dashboard input" for validating ADR-0081 optimiser production cadence.
- **No user surface:** no `turn_status` changes (FRE-570 already wired those). No changes to `IncidentTracker` / Stage 7 governance.

---

## Files

| # | Action | File |
|---|--------|------|
| 1 | Modify | `src/personal_agent/telemetry/context_quality.py` |
| 2 | Modify | `src/personal_agent/request_gateway/pipeline.py` |
| 3 | Modify | `src/personal_agent/orchestrator/executor.py` |
| 4 | Create | `docker/elasticsearch/monitors-cache-reset-cadence-index-template.json` |
| 5 | Modify | `scripts/setup-elasticsearch.sh` |
| 6 | Create | `tests/personal_agent/telemetry/test_budget_compaction_incident.py` |
| 7 | Create | `tests/test_orchestrator/test_cache_reset_cadence_monitor.py` |

---

## Atomic Steps

### Step 1 — TDD: write failing A-incident tests

File: `tests/personal_agent/telemetry/test_budget_compaction_incident.py`

Tests:
- `test_writes_bcomp_jsonl_with_correct_fields` — `record_budget_compaction_incident` writes `BCOMP-{day}.jsonl` with `trace_id`, `session_id`, `phases_fired`, `severity`, `detected_at`.
- `test_phase2_is_high_severity` — `phases_fired=(1,2)` → `severity="high"`.
- `test_phase1_only_is_low_severity` — `phases_fired=(1,)` → `severity="low"`.
- `test_appends_multiple_incidents` — two calls → two JSONL lines.
- `test_schedule_sync_path_writes_jsonl` — `schedule_record_budget_compaction_incident` under no event loop writes the file.

Verify: `make test-file FILE=tests/personal_agent/telemetry/test_budget_compaction_incident.py` → all FAIL.

### Step 2 — Implement A incident in `context_quality.py`

Add after existing `schedule_record_incident`:

```python
@dataclass(frozen=True)
class BudgetCompactionIncident:
    trace_id: str
    session_id: str
    phases_fired: tuple[int, ...]
    severity: Literal["high", "low"]
    detected_at: datetime

async def record_budget_compaction_incident(
    incident: BudgetCompactionIncident,
    *,
    output_dir: Path | None = None,
) -> None:
    """Durable JSONL + structlog for gateway budget compaction (ADR-0092 §D5)."""
    target_dir = output_dir or _default_output_dir()
    ...  # write BCOMP-{day}.jsonl
    log.info("budget_compaction_incident", ...)

def schedule_record_budget_compaction_incident(
    incident: BudgetCompactionIncident,
) -> None:
    """Fire-and-forget wrapper — same pattern as schedule_record_incident."""
    ...
```

Verify: `make test-file FILE=tests/personal_agent/telemetry/test_budget_compaction_incident.py` → all PASS.

### Step 3 — Wire A incident in `pipeline.py`

After the `CompactionAMarkerEvent` publish block (lines ~190-203), call `schedule_record_budget_compaction_incident(BudgetCompactionIncident(...))` in the same best-effort try/except.

Verify: `make test-k test_budget_compaction` → PASS.

### Step 4 — TDD: write failing D-cadence tests

File: `tests/test_orchestrator/test_cache_reset_cadence_monitor.py`

Tests:
- `test_emit_cadence_doc_standard_fields` — `_emit_cadence_monitor_doc(...)` calls `schedule_es_index` with doc containing `actual_turns`, `l_star`, `reason`, `trace_id`, `session_id`.
- `test_emit_cadence_doc_l_star_none_when_inf` — `optimal_run_length=math.inf` → `l_star=None`, `deviation_turns=None`.
- `test_emit_cadence_doc_deviation_rounds` — finite L* gives `deviation_turns = round(actual - l_star, 2)`.
- `test_emit_cadence_doc_id_keyed_by_trace` — `doc_id=f"{trace_id}:D"`.

Verify: `make test-file FILE=tests/test_orchestrator/test_cache_reset_cadence_monitor.py` → all FAIL.

### Step 5 — Implement D cadence monitor in `executor.py`

In `_maybe_frozen_reset`:
1. Add `turns_since_reset=inputs["turns_since_reset"]` to the `frozen_reset_fired` log.
2. Add a new private helper `_emit_cadence_monitor_doc(trace_id, session_id, backend, actual_turns, optimal_run_length, reason)` that calls `schedule_es_index` to `agent-monitors-cache-reset-cadence-{date}`.
3. Call it after the `frozen_reset_fired` log, before the D-marker bus publish.

Verify: `make test-file FILE=tests/test_orchestrator/test_cache_reset_cadence_monitor.py` → all PASS.

### Step 6 — ES template + setup script

Create `docker/elasticsearch/monitors-cache-reset-cadence-index-template.json`:
```json
{
  "index_patterns": ["agent-monitors-cache-reset-cadence-*"],
  "priority": 110,
  "template": {
    "settings": { "number_of_shards": 1, "number_of_replicas": 0 },
    "mappings": {
      "dynamic": false,
      "properties": {
        "@timestamp":      { "type": "date" },
        "trace_id":        { "type": "keyword" },
        "session_id":      { "type": "keyword" },
        "backend":         { "type": "keyword" },
        "reason":          { "type": "keyword" },
        "actual_turns":    { "type": "integer" },
        "l_star":          { "type": "double" },
        "deviation_turns": { "type": "double" }
      }
    }
  }
}
```

Add to `scripts/setup-elasticsearch.sh` after the `projector-health` block.

### Step 7 — Quality gates

```bash
make test-file FILE=tests/personal_agent/telemetry/test_budget_compaction_incident.py
make test-file FILE=tests/test_orchestrator/test_cache_reset_cadence_monitor.py
make test
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

---

## Acceptance Criteria Trace

| AC | Satisfied by |
|----|-------------|
| A-incidents recorded with severity; queryable | JSONL write + `log.info("budget_compaction_incident", severity=...)` → ES |
| D reset-cadence vs L* surfaced to backend | `agent-monitors-cache-reset-cadence-*` ES docs with `actual_turns`, `l_star`, `deviation_turns` |
| `make test` green | Step 7 |
| No user-facing change | No `turn_status` changes; no PWA/transport changes |

---

## ES Mapping Guard

`agent-monitors-cache-reset-cadence-*` floats: `l_star` (`double`, nullable) and `deviation_turns` (`double`, nullable) — explicit properties prevent the first-value-0.0→`long` trap. `actual_turns` is `integer`. All identity fields `keyword`. `dynamic: false` so unmapped fields don't pollute.
