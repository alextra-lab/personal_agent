# ADR-0058: Self-Improvement Pipeline Stream

**Status**: Accepted (Implemented — FRE-248 — 2026-04-25)
**Date**: 2026-04-25
**Deciders**: Project owner
**Depends on**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel), ADR-0041 (Event Bus — Redis Streams), ADR-0054 (Feedback Stream Bus Convention)
**Related**: ADR-0042 (Knowledge Graph Freshness), ADR-0053 (Gate Feedback-Loop Monitoring — template), ADR-0056 (Error Pattern Monitoring — consumer pattern), ADR-0057 (Insights & Pattern Analysis — implemented in same wave)
**Enables**: FRE-226 phase 2 (agent self-updating skills — needs `stream:captain_log.entry_created` to know when a new improvement proposal is available), ADR-0059 (Context Quality Stream — pattern consumer), ADR-0061 (Within-Session Compression — pattern consumer)
**Linear Issue**: FRE-248

---

## Context

### Stream 1 terminates at a log line and an ES write

The Self-Improvement Pipeline has three streams:

- **Stream 1 — Post-Task Self-Reflection**: `CaptainLogManager.save_entry()` writes a JSON file to `telemetry/captains_log/` and schedules an ES index to `agent-captains-reflections-<date>`. On every save, it emits the structlog event `CAPTAINS_LOG_ENTRY_CREATED`. That is where the signal ends.
- **Stream 2 — Linear Human Feedback**: `BrainstemScheduler._publish_feedback_events()` publishes `FeedbackReceivedEvent` to `stream:feedback.received`. Already bus-compliant.
- **Stream 3 — Promotion Pipeline**: `PromotionPipeline._publish_promotion_events()` publishes `PromotionIssueCreatedEvent` to `stream:promotion.issue_created`. Already bus-compliant.

Stream 1 is the only of the three that is **not on the bus**. Every reflection, consolidation insight, freshness review proposal, mode-calibration proposal, and error-pattern proposal created by `CaptainLogManager.save_entry()` produces a log line — and nothing else. No downstream system can react to CL entry creation in real time without polling the filesystem. No future consumer can subscribe to "a new proposal was written" without modifying the producer code.

### ADR-0054 compliance gap

ADR-0054 established the dual-write convention: every feedback stream must write durably **and** publish a typed bus event, in that order. Stream 1 satisfies the durable-write half but omits the bus publish entirely. `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` documents this explicitly (Stream 1 row, `Bus? Partial — promotion triggered by bus, entry creation is not`).

ADR-0054 §D1 reserved `stream:captain_log.entry_created` for this gap: "Producer: ADR-0058. Stream 1 bus hook; closes the gap noted in the Stream Catalog."

### The single-funnel opportunity

All 10 CL construction sites in the codebase funnel through one of two persist methods in `CaptainLogManager`:
- `save_entry()` at `captains_log/manager.py:171` — normal write path
- `_merge_into_existing()` at `captains_log/manager.py:278` — ADR-0030 dedup merge path

Both methods ultimately write the JSON file to disk. Adding the bus publish once in each of these two methods is sufficient to close the bus gap for all 10 construction paths — without touching any caller.

### Why this unblocks FRE-226 phase 2

FRE-226 phase 2 (agent self-updating skills — Wave 4) needs to know when a new capability improvement proposal is available so it can evaluate and potentially update the relevant skill doc. Today, there is no composable signal it can subscribe to. Once `stream:captain_log.entry_created` is live, FRE-226 phase 2 can add a consumer group and react to new proposals without touching the Captain's Log manager.

---

## Decision Drivers

1. **Close the ADR-0054 gap for Stream 1.** The bus convention is the foundation of the composable feedback architecture. A stream that cannot be subscribed to is not part of the architecture.
2. **No producer-call-site rewrite.** The single-funnel structure means a two-method change covers all 10 construction sites. Retrofitting 10 construction sites individually would introduce drift risk.
3. **Dedup-merge must also fire.** When an entry is merged (ADR-0030 dedup), the signal is equally meaningful — a repeating pattern is more important, not less. `is_merge=True` distinguishes the event type without a second event class.
4. **Suppression must not fire.** ADR-0040 rejection suppression (`is_fingerprint_suppressed` returns early before any write). The bus publish must not fire for suppressed entries — no durable write means no bus event (ADR-0054 D4 ordering rule).
5. **Ordering: durable first, then bus.** ADR-0054 D4. File write must succeed before the bus publish is scheduled. A failed file write propagates the exception; the bus publish is never attempted.
6. **Bus failures are swallowed.** ADR-0054 D6. The bus publish uses fire-and-forget via `asyncio.create_task()`. A Redis failure must not break CL entry creation — the durable write already occurred and is the authoritative record.
7. **No new consumer group required.** This ADR is producer-only. The event contract is documented for future consumers; no consumer is wired in Phase 1.

---

## Decision

### D1: Sources — One new stream

| Stream | Source | Producer | Event |
|--------|--------|----------|-------|
| `stream:captain_log.entry_created` | `CaptainLogManager.save_entry()` + `_merge_into_existing()` | `captains_log.manager` | `CaptainLogEntryCreatedEvent` |

**Existing streams re-affirmed as ADR-0054 compliant (no changes needed):**

| Stream | Producer | ADR |
|--------|----------|-----|
| `stream:feedback.received` | `brainstem.scheduler` | ADR-0040 |
| `stream:promotion.issue_created` | `captains_log.promotion` | ADR-0030, ADR-0040 |

### D2: Collection — No new consumer group

This ADR is **producer-only**. ADR-0054 §D2 reserved 6 new consumer group names; none of them map to CL entry creation consumers. The reserved names are for streams that require a dedicated reactive role (mode controller, error monitor, etc.). `stream:captain_log.entry_created` is a general-purpose composability hook — its first consumer (FRE-226 phase 2) will introduce its own group at that time.

**Existing consumer groups that are NOT affected by this stream:**

| Group | Role | Why unchanged |
|-------|------|---------------|
| `cg:promotion` | Runs promotion pipeline | Triggered by `consolidation.completed`, not by this new stream. Promotion must not be double-triggered. |
| `cg:captain-log` | Writes CL entries | Writes CL entries; must not subscribe to its own output stream (circular). |

### D3: Data model — `CaptainLogEntryCreatedEvent`

```python
class CaptainLogEntryCreatedEvent(EventBase):
    """Published after a Captain's Log entry is durably written (ADR-0058).

    Fires from CaptainLogManager.save_entry() and _merge_into_existing()
    — the two persist sites all 10 CL construction call sites funnel through.
    Suppressed entries (ADR-0040 rejection fingerprint) do not fire.

    trace_id / session_id are None for scheduled/system-scoped entries;
    populated for task-reflection entries where a request trace exists.
    source_component is always "captains_log.manager".
    """

    event_type: Literal["captain_log.entry_created"] = "captain_log.entry_created"
    source_component: str = "captains_log.manager"

    entry_id: str
    entry_type: str          # CaptainLogEntryType.value, e.g. "REFLECTION"
    title: str
    fingerprint: str | None = None
    seen_count: int = 1
    is_merge: bool = False
    category: str | None = None
    scope: str | None = None
```

**Field rationale:**

| Field | Rationale |
|-------|-----------|
| `entry_id` | Primary key — consumer can fetch full entry from disk if needed |
| `entry_type` | Allows consumers to filter (e.g. "only react to REFLECTION entries") |
| `title` | Human-readable; useful for display in future live-CL-feed UI |
| `fingerprint` | Consumer can check their own suppression list without fetching the file |
| `seen_count` | Conveys repeat severity without consumer needing to read the file |
| `is_merge` | Distinguishes first write from dedup merge without a second event type |
| `category` | Consumer can route to the right Linear project without fetching the file |
| `scope` | Consumer can identify the affected subsystem without fetching the file |

**Schema evolution:** `schema_version = 1` (default from `EventBase`). Additive field additions keep backward compatibility per ADR-0054 Rule 1. Breaking changes (field removal, rename, semantic change) require a new `event_type` per ADR-0054 Rule 2.

**Frozen:** `EventBase.model_config = ConfigDict(frozen=True)` — inherited.

### D4: Dual-write rule — durable first, then fire-and-forget bus publish

The publish follows the same fire-and-forget pattern established by `schedule_es_index` in `captains_log/es_indexer.py`:

```python
import asyncio

async def _publish() -> None:
    try:
        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import (
            STREAM_CAPTAIN_LOG_ENTRY_CREATED,
            CaptainLogEntryCreatedEvent,
        )
        event = CaptainLogEntryCreatedEvent(
            entry_id=...,
            entry_type=...,
            title=...,
            fingerprint=...,
            seen_count=...,
            is_merge=...,
            category=...,
            scope=...,
            trace_id=...,   # from entry.telemetry_refs[0].trace_id if available
            session_id=..., # from entry.telemetry_refs[0].session_id if available
        )
        bus = get_event_bus()
        await bus.publish(STREAM_CAPTAIN_LOG_ENTRY_CREATED, event)
    except Exception as exc:
        log.warning(
            "captain_log_entry_event_publish_failed",
            entry_id=...,
            error=str(exc),
        )

try:
    asyncio.get_running_loop()
    asyncio.create_task(_publish())
except RuntimeError:
    pass  # No running loop (CLI / tests) — skip bus publish
```

**Placement in `save_entry()`**: after `file_path.write_text(...)` and `schedule_es_index(...)` succeed; before `return file_path`.

**Placement in `_merge_into_existing()`**: after the dedup `existing_path.write_text(...)` and `schedule_es_index(...)` succeed; before `return existing_path`. `is_merge=True`; `seen_count` read from the already-incremented `pc["seen_count"]`.

**Suppression path**: `save_entry()` returns `None` before any write when the fingerprint is suppressed — the publish helper is never reached. No code change needed; the ordering rule enforces this naturally.

### D5: Signal — no Captain's Log entries produced by this stream

This stream does not produce CL entries itself. It transports them as composable events. The shapes and categories of CL entries are governed by the producer that called `save_entry()` (ADR-0030, ADR-0056, ADR-0057, etc.), not by this ADR.

### D6: Surfacing channels — existing Self-Improvement Pipeline project

No new Linear project. Issues surfaced via `stream:captain_log.entry_created` will use the existing **Self-Improvement Pipeline** project in Linear, with the same label schema used by Streams 1-3 today.

Future consumers that react to this stream (FRE-226 phase 2, FRE-249 context-quality monitor) will surface findings through their own channels; they do not modify this ADR.

### D7: Full automation cycle

```
POST-TASK / SCHEDULED JOB / CONSOLIDATION INSIGHT / FRESHNESS REVIEW / MODE-CALIBRATION / ERROR-PATTERN
         │
         ▼
CaptainLogManager.save_entry() or _merge_into_existing()
         │
         ├─ durable write ─────────────────────────────► telemetry/captains_log/<entry_id>.json
         │                                                └── ES: agent-captains-reflections-<date>
         │
         ├─ bus publish (fire-and-forget) ───────────────► stream:captain_log.entry_created
         │                                                      CaptainLogEntryCreatedEvent
         │                                                      {entry_id, entry_type, title,
         │                                                       fingerprint, seen_count, is_merge,
         │                                                       category, scope, trace_id, session_id}
         │
         └─ structlog CAPTAINS_LOG_ENTRY_CREATED ──────► Elasticsearch (existing, unchanged)

stream:captain_log.entry_created
         │
         ├─ (Phase 1 — this ADR): no consumer
         │
         ├─ (FRE-226 phase 2): cg:skill-updater — reads entry_type + category, evaluates
         │   whether skill doc update is warranted, updates docs/skills/<name>.md
         │
         └─ (Future): live CL feed UI, real-time alert routing, governance response triggers

─── Existing promotion path (unchanged, not triggered by this new stream) ───────────────
consolidation.completed
         │
         ▼
cg:promotion → PromotionPipeline.run() → creates Linear issue → stream:promotion.issue_created
         │
         ▼
cg:captain-log → writes OBSERVATION CL entry
```

### D8: Scope boundary

**Not in this ADR:**

- Re-architecting Streams 1-3 internals. ADR-0030 (dedup pipeline, promotion gates) and ADR-0040 (feedback label handlers) are referenced as foundational; this ADR does not modify their internals.
- A new `captain_log.entry_updated` event type. The dedup-merge case fires `captain_log.entry_created` with `is_merge=True` — consumers distinguish the two cases via that field. A second event type would force all consumers to subscribe to two streams.
- Removing the `CAPTAINS_LOG_ENTRY_CREATED` structlog event. The log event is for human observability (Kibana, grep); the bus event is for code. Both coexist.
- Wiring a consumer for `stream:captain_log.entry_created`. The first consumer is FRE-226 phase 2 (Wave 4, depends on FRE-248 implementation). This ADR establishes the producer contract only.
- Using `captain_log.entry_created` as a promotion trigger. The promotion pipeline is triggered by `consolidation.completed` (periodic, batched, gated by ADR-0030 thresholds). Re-triggering promotion on every CL write would break the `seen_count ≥ 3` + 7-day-age gate logic.
- Any modification to `stream:feedback.received` or `stream:promotion.issue_created`. Both are already ADR-0054 compliant. This ADR re-affirms their status in D1 without touching their producers.

---

## Alternatives Considered

### A: Two events — `captain_log.entry_created` + `captain_log.entry_merged`

Rejected. Forces every consumer to subscribe to and reconcile two event streams to get a complete picture of CL write activity. `is_merge: bool` on a single event is sufficient discrimination; the semantics ("an entry was written to disk") are identical for both cases.

### B: Hook the publish at each of the 10 construction sites

Rejected. The 10 construction sites span 5 modules (reflection, reflection_dspy, manager, brainstem jobs, pipeline_handlers, insights). Patching each site independently would require 10 separate changes, introduce per-site drift risk, and require 10 separate tests. The single-funnel approach (`save_entry` + `_merge_into_existing`) covers all 10 sites with 2 changes.

### C: Bus-only publish — skip the durable file write

Rejected. Violates ADR-0054 D4 ordering rule. The JSON file on disk is the authoritative record (survives Redis outages, process restarts, and bus unavailability). The bus event is the composability hook — it is additive, not a replacement.

### D: Hook on the `CAPTAINS_LOG_ENTRY_CREATED` structlog event via a custom processor

Rejected. Structlog processors are sync and cannot drive async bus publishes. Structlog events are consumed by humans (Kibana dashboards), not by code. Conflating the two concerns (human observability and code composability) creates a tight coupling between the logging pipeline and the event bus — both would need to be tested together.

### E: Separate async `save_entry_async()` method, migrate all callers

Rejected for Phase 1. All 6 active `save_entry()` call sites are already in async contexts; migrating them to an async wrapper is feasible but adds refactor risk unrelated to the ADR goal. The `asyncio.create_task()` fire-and-forget pattern used by `schedule_es_index` is already established in this codebase; reusing it keeps the change minimal and consistent. If a future requirement demands awaiting the publish result (e.g. back-pressure), `save_entry_async()` can be added then.

---

## Consequences

### Positive

- **Stream 1 is composable.** Any future consumer can subscribe to `stream:captain_log.entry_created` without modifying `CaptainLogManager` — the producer contract is stable.
- **FRE-226 phase 2 unblocked.** The Wave 4 self-updating skills capability has its composability hook.
- **ADR-0054 fully satisfied for Streams 1-3.** All three Self-Improvement Pipeline streams are now dual-write compliant. The Stream Catalog table in `FEEDBACK_STREAM_ARCHITECTURE.md` can be updated to `Bus? ✅` for Stream 1.
- **Minimal code surface.** Two method hooks in one file (`manager.py`) plus one event class and one constant in `events/models.py`. No new files, no new modules, no new consumer groups.
- **All 10 CL construction sites covered.** The single-funnel structure means no call site drift.

### Negative

- **One new event type to maintain.** `CaptainLogEntryCreatedEvent` must be kept in sync with `CaptainLogEntry` fields if significant structural changes are made to the entry model. The fields carried on the event are intentionally minimal (identifiers + metadata, not the full entry payload) to reduce coupling.
- **Fire-and-forget means no delivery guarantee.** If Redis is unavailable when a CL entry is written, the bus publish is silently dropped (ADR-0054 D6). This is consistent with the treatment of all other stream events in the codebase.

### Risks

- **Circular consumer risk.** `cg:captain-log` (the existing consumer that writes OBSERVATION entries after promotion events) must never subscribe to `stream:captain_log.entry_created` — doing so would create an infinite loop. Documented explicitly in D2 and in the consumer group table. Enforced by code review.
- **Promotion double-trigger risk.** Documented in D8. The promotion pipeline is not triggered by this new stream; it is triggered by `consolidation.completed`. Mitigated by the `cg:promotion` exclusion in D2 and by the explicit note in D8.

---

## Implementation Priority

### Phase 1 — this ADR (minimal, closes the bus gap)

1. Add `STREAM_CAPTAIN_LOG_ENTRY_CREATED = "stream:captain_log.entry_created"` constant to `events/models.py` (Wave 2 ADR-0058 block).
2. Add `CaptainLogEntryCreatedEvent(EventBase)` class to `events/models.py` after `InsightsCostAnomalyEvent`.
3. Add dispatch branch `if raw_type == "captain_log.entry_created"` to `parse_stream_event()`.
4. Add `_schedule_entry_created_event()` private helper to `CaptainLogManager`.
5. Call helper at end of normal write path in `save_entry()`.
6. Call helper at end of dedup merge path in `_merge_into_existing()` with `is_merge=True`.
7. Unit tests (7 cases, see Verification section of implementation plan).
8. Update `FEEDBACK_STREAM_ARCHITECTURE.md` — Stream 1 row Bus? → ✅.
9. Update `MASTER_PLAN.md` — move FRE-248 to Completed.

### Phase 2 — first consumer (FRE-226 phase 2, Wave 4)

- `cg:skill-updater` consumer subscribes to `stream:captain_log.entry_created`.
- Filters on `entry_type == "REFLECTION"` and `category in {RELIABILITY, EFFICIENCY, CAPABILITY}`.
- Evaluates whether a `docs/skills/<name>.md` doc warrants update.
- This phase is out of scope for this ADR; it has its own Linear issue and depends on ADR-0058 implementation being live.

---

## Module Placement

| Artifact | Location | Notes |
|----------|----------|-------|
| `STREAM_CAPTAIN_LOG_ENTRY_CREATED` | `src/personal_agent/events/models.py` | Wave 2 ADR-0058 comment block, adjacent to other Wave 2 stream constants |
| `CaptainLogEntryCreatedEvent` | `src/personal_agent/events/models.py` | After `InsightsCostAnomalyEvent` |
| Dispatch branch | `src/personal_agent/events/models.py:parse_stream_event()` | Before the final `raise ValueError` |
| `_schedule_entry_created_event()` | `src/personal_agent/captains_log/manager.py` | Private method on `CaptainLogManager` |
| Tests | `tests/test_captains_log/test_manager_bus.py` | New file, 7 unit tests |

No new Python modules. No new `cg:` consumer group constants. No new config fields.

---

## Open Questions

None at acceptance time. All design points resolved during planning:

- **Why not `captain_log.entry_updated` for merges?** → D3 rationale: `is_merge` flag on a single event type is sufficient; two event types would require subscribers to reconcile two streams.
- **Should `trace_id` / `session_id` be required?** → No. Scheduled/system events (freshness review, mode-calibration, insights proposals) have no request context. Narrowing to required would exclude the majority of CL write paths.
- **Should `seen_count` be read from the entry or from the file?** → From the updated `pc["seen_count"]` for merges (which has already been incremented at the point of publish); from `1` for first writes (the `ProposedChange.seen_count` default).

---

## Dedicated Linear Project — Self-Improvement Pipeline

FRE-248 is within the existing **Self-Improvement Pipeline** project in Linear (team: FrenchForest). No new project is created for this ADR.

**Future issues generated by the `stream:captain_log.entry_created` consumer (FRE-226 phase 2) will use this project:**

| Issue type | Trigger | Label |
|------------|---------|-------|
| Skill doc update proposal | Consumer detects REFLECTION entry with CAPABILITY/EFFICIENCY category | `self-improvement` |
| Skill coverage gap | Consumer detects REFLECTION entry with no matching skill doc | `skill-coverage` |

**Existing feedback labels (unchanged):**

| Linear label | Effect |
|-------------|--------|
| `Approved` | Promotion pipeline picks up; implementation issue spawned |
| `Rejected` | `is_fingerprint_suppressed` returns `True`; future identical proposals skipped |
| `Deepen` | Re-analysis triggered by `handle_deepen()` |
| `Too Vague` | Reformulation requested |
| `Duplicate` | Suppression applied (same as Rejected for dedup purposes) |
| `Defer` | Noted in poller state; re-surfaces next cycle |

---

## End State — What Exists, What Is Automated, What Is Visible

### After Phase 1 (this ADR) ships

| Aspect | Before | After |
|--------|--------|-------|
| Bus compliance | Streams 2 & 3 only | All three streams |
| Stream 1 composability | None | `stream:captain_log.entry_created` subscribable |
| CL write observability | Structlog + ES only | Structlog + ES + bus event |
| FRE-226 phase 2 blocker | Blocked (no signal to subscribe to) | Unblocked |
| Code change surface | — | 2 methods in `manager.py`, 1 class + 1 constant in `models.py` |
| Consumer groups added | — | 0 |

### After Phase 2 (FRE-226 phase 2, future)

| Aspect | State |
|--------|-------|
| Skill docs | Auto-evaluated after each REFLECTION/CAPABILITY/EFFICIENCY CL write |
| Skill coverage gaps | Surfaced as Linear issues in Self-Improvement Pipeline project |
| Agent composability | Agent can update its own skill docs via the promotion → approval → update loop |

---

## Loop Completeness Criteria

**Phase 1 is complete when all of the following hold:**

1. `stream:captain_log.entry_created` XLEN > 0 in Redis after at least one `save_entry()` call in a running service.
2. `XREVRANGE stream:captain_log.entry_created + - COUNT 1` returns a payload with `event_type = "captain_log.entry_created"` and `source_component = "captains_log.manager"`.
3. A new consumer group can `XREADGROUP GROUP cg:test-consumer consumer-1 COUNT 10 STREAMS stream:captain_log.entry_created >` and receive the event — without touching `manager.py`.
4. All 7 unit tests in `tests/test_captains_log/test_manager_bus.py` pass.
5. Suppressed entries produce 0 bus events (verified by unit test `test_suppressed_entry_does_not_publish`).
6. A failed file write produces 0 bus events (verified by unit test `test_durable_failure_does_not_publish`).
7. A failed bus publish does not prevent the file from being written (verified by unit test `test_bus_failure_does_not_block_save`).

---

## Feedback Stream ADR Template — Compliance Checklist

Per ADR-0053 template (ADR-0054 §D7):

- [x] **Dual-write requirement met.** `save_entry()` writes durably before publishing. Ordering rule D4 satisfied.
- [x] **Bus failure handling.** Publish wrapped in try/except; failure logged and swallowed per D6.
- [x] **Stream name uses reserved constant.** `STREAM_CAPTAIN_LOG_ENTRY_CREATED = "stream:captain_log.entry_created"` — reserved in ADR-0054 §D1.
- [x] **No string literal in publish call.** Code references the constant, not the string directly.
- [x] **`source_component` set.** `"captains_log.manager"` on all events from this producer.
- [x] **`schema_version` defaults to 1.** Inherited from `EventBase`; not overridden.
- [x] **`trace_id` / `session_id` nullable.** System-scoped events have no request context.
- [x] **No new consumer group.** Producer-only ADR; consumer group added by FRE-226 phase 2.
- [x] **Suppression path covered.** `is_fingerprint_suppressed` returns early before any write; no bus event fires.
- [x] **Dedup-merge path covered.** `_merge_into_existing()` fires the event with `is_merge=True`.
- [x] **`parse_stream_event()` updated.** New `if raw_type == "captain_log.entry_created"` branch before the final `raise`.
- [x] **Loop completeness criteria defined.** 7 acceptance conditions above.
- [x] **`FEEDBACK_STREAM_ARCHITECTURE.md` update listed.** Stream 1 row Bus? → ✅ in Phase 1 task list.
- [x] **Existing streams re-affirmed.** Streams 2 & 3 confirmed ADR-0054 compliant in D1; no changes made.

---

## References

- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — `docs/architecture_decisions/ADR-0030-captains-log-dedup-and-self-improvement-pipeline.md`
- ADR-0040: Linear as Async Feedback Channel — `docs/architecture_decisions/ADR-0040-linear-async-feedback-channel.md`
- ADR-0041: Event Bus — Redis Streams — `docs/architecture_decisions/ADR-0041-event-bus-redis-streams.md`
- ADR-0054: Feedback Stream Bus Convention — `docs/architecture_decisions/ADR-0054-feedback-stream-bus-convention.md`
- ADR-0053: Gate Feedback Monitoring (template source) — `docs/architecture_decisions/ADR-0053-gate-feedback-monitoring.md`
- Feedback Stream Architecture (living doc) — `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`
- FRE-248: Linear issue — https://linear.app/frenchforest/issue/FRE-248
- FRE-226: Agent self-updating skills (first consumer) — https://linear.app/frenchforest/issue/FRE-226
- Wave Plan — `docs/superpowers/specs/2026-04-22-implementation-sequence-wave-plan-design.md`
