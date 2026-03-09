# Captain's Log Elasticsearch Backfill Spec

**Date**: 2026-02-22
**Status**: Proposed
**Phase**: 2.3 Homeostasis & Feedback
**Related**: `FRE-8` (Captain's Log -> Elasticsearch indexing)

---

## Purpose

Add eventual consistency for Captain's Log Elasticsearch indexing so entries written while Elasticsearch is unavailable are replayed and indexed once ES becomes available.

Current behavior is best-effort indexing at write time. Failed indexing does not block request flow, but missed documents are not replayed automatically.

---

## Scope

### In Scope

- Replay missed Captain's Log captures from `telemetry/captains_log/captures/`.
- Replay missed Captain's Log reflections from `telemetry/captains_log/`.
- Persist replay checkpoint so replay resumes safely after restart.
- Idempotent indexing to avoid duplicate documents.
- Manual replay trigger and periodic replay in service runtime.
- Structured telemetry for replay progress/failures.

### Out of Scope

- Dashboard design work (covered by `FRE-10`).
- Reprocessing/transforming historical schema versions beyond safe pass-through.
- Full distributed queue infrastructure.

---

## Design

### 1) Replay state checkpoint

Create a checkpoint file:

- `telemetry/captains_log/es_backfill_checkpoint.json`

Suggested shape:

```json
{
  "version": 1,
  "last_scan_started_at": "2026-02-22T14:00:00Z",
  "last_scan_completed_at": "2026-02-22T14:00:03Z",
  "captures": {
    "last_processed_path": "telemetry/captains_log/captures/2026-02-22/trace-x.json",
    "last_processed_mtime": "2026-02-22T13:59:59Z"
  },
  "reflections": {
    "last_processed_path": "telemetry/captains_log/CL-....json",
    "last_processed_mtime": "2026-02-22T13:59:58Z"
  }
}
```

Checkpoint updates happen after successful indexing batches.

### 2) Idempotent indexing

Use deterministic document IDs in Elasticsearch to make replay safe:

- Captures: `doc_id = trace_id`
- Reflections: `doc_id = entry_id`

Index with explicit IDs so repeated replays overwrite same document rather than creating duplicates.

### 3) Backfill engine

New module:

- `src/personal_agent/captains_log/backfill.py`

Core responsibilities:

- Enumerate capture/reflection files in stable order.
- Parse JSON -> validate using existing Pydantic models when possible.
- Compute target index names:
  - `agent-captains-captures-YYYY-MM-DD`
  - `agent-captains-reflections-YYYY-MM-DD`
- Upsert to ES with deterministic doc ID.
- Record per-file success/failure counters.
- Persist checkpoint.

### 4) Service integration

Wire backfill into service lifespan:

- On startup: run one replay pass after ES connects.
- During runtime: periodic replay (for example every 5-15 minutes) via existing scheduler pattern.
- Graceful no-op when ES disconnected.

### 5) Observability

Emit structured events:

- `captains_log_backfill_started`
- `captains_log_backfill_completed`
- `captains_log_backfill_file_failed`
- `captains_log_backfill_checkpoint_updated`

Include counts and timing:

- files_scanned, indexed_count, failed_count, skipped_count, elapsed_ms.

---

## File Plan

- Add: `src/personal_agent/captains_log/backfill.py`
- Update: `src/personal_agent/telemetry/es_logger.py` (support explicit document ID indexing)
- Update: `src/personal_agent/service/app.py` (startup + periodic replay wiring)
- Add tests:
  - `tests/test_captains_log/test_backfill.py`
  - Extend ES logger tests for deterministic ID upserts

---

## Acceptance Criteria

- If ES is down during capture/reflection creation, files are still written locally.
- When ES becomes available, replay indexes missed files automatically.
- Replay is idempotent: re-running replay does not create duplicates.
- Replay progress survives restarts through checkpoint persistence.
- Replay failures are logged with file-level context and do not crash service.
- End-to-end verification demonstrates:
  - missed capture indexed after ES recovery
  - missed reflection indexed after ES recovery

---

## Test Plan

### Unit

- checkpoint read/write and resume logic
- file discovery ordering
- deterministic doc ID derivation
- retry and error handling paths

### Integration

- Simulate ES unavailable:
  1. write capture/reflection with ES down
  2. confirm files exist locally and ES docs absent
  3. restore ES
  4. run replay
  5. confirm docs appear in correct indices

---

## Risks and Mitigations

- **Large backlog replay time**: use batching and bounded per-run work.
- **Schema drift in historical files**: best-effort parse with skip+log for malformed entries.
- **Duplicate indexing**: deterministic document IDs prevent duplication.

---

## Notes

This is intentionally a follow-up reliability task to keep `FRE-8` focused on write-path indexing and non-blocking behavior.
