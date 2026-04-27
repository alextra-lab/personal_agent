# ADR-0059: Context Quality Stream

**Status**: Accepted — Implemented 2026-04-27 (FRE-249)
**Date**: 2026-04-27
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Architectural Separation), ADR-0047 (Context Management & Observability — D3), ADR-0053 (Gate Feedback-Loop Monitoring Framework — template), ADR-0054 (Feedback Stream Bus Convention), ADR-0056 (Error Pattern Monitoring Stream — composes on top of)
**Related**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel), ADR-0061 (Within-Session Progressive Context Compression — downstream)
**Linear Issue**: FRE-249

---

## Context

### Stream 7 is dead end-to-end today

`docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` lists Stream 7 (Compaction Quality Detection) as the most user-visible feedback stream — when it fires, it means the agent just dropped something from context that the user is *actively* referencing. The detection design (ADR-0047 D3) has three pieces:

1. **Producer** — Stage 7 (Budget) compacts oversized context and records what was dropped via `log_compaction()` in `src/personal_agent/telemetry/compaction.py`, populating an in-memory `_dropped_entities_by_session` cache.
2. **Detector** — the recall controller (Stage 4b) extracts noun phrases from the user message and substring-matches them against the dropped-entity cache; on hit, emits a `compaction_quality.poor` WARNING.
3. **Response** — none. The warning lands in Elasticsearch and a Kibana chart can show it. The loop does not close.

An audit performed during ADR-0059 drafting found the design is also dead at the *source*, in two places:

- **Bug A (producer)** — `apply_budget()` in `src/personal_agent/request_gateway/budget.py` constructs every `CompactionRecord` with `entities_dropped=()` and `entities_preserved=()`. The dropped-entity cache is therefore *never populated*; it is always empty.
- **Bug B (detector)** — `run_recall_controller()` in `src/personal_agent/request_gateway/recall_controller.py` does not receive `session_id`. Line 186 falls back to `session_id_for_check = trace_id  # best available proxy when session_id not threaded here`. The compaction cache is keyed by `session_id`, the lookup queries by `trace_id`, and the substring-match block is therefore unreachable.

Result: Stream 7 has not produced a single signal in production. Closing the loop requires fixing both bugs *before* any bus event has data to publish.

### Composability with ADR-0056

ADR-0056 (Error Pattern Monitoring) already includes `compaction_quality.poor` in `WARNING_EVENT_ALLOWLIST`. That path delivers **cross-trace 24 h clustering** — "12 occurrences in 24 h" → cluster-level proposals through the existing error-monitor pipeline. After Bug A and Bug B are fixed, that path will start firing automatically.

ADR-0059 adds the complementary **per-incident** path: a typed event with full `noun_phrase` + `dropped_entity` + `session_id` context, fired *inline* (no 24 h wait), feeding the Captain's Log with finer-grained fingerprints and Phase 2 governance with same-session response. ADR-0030 fingerprint dedup at `CaptainLogManager.save_entry()` merges any overlap between the two paths cleanly.

The two streams are therefore complementary, not redundant. ADR-0056 answers *"this kind of compaction failure keeps happening"*; ADR-0059 answers *"in this session, dropping X just hurt the user; tighten the next request's budget"*.

### Feedback Stream Bus Convention applies

ADR-0054 dual-write: durable record first (file/ES), bus event second (composability hook). ADR-0059 follows the same pattern — append-only JSONL on disk plus a typed event on a named stream.

---

## Decision Drivers

1. **Fix the bugs first, *then* close the loop.** No bus event design changes anything until producer and detector are wired correctly. The bug fixes are intrinsic to ADR-0059.
2. **Compose, do not duplicate.** ADR-0056 cluster-detection on `compaction_quality.poor` warnings is already a feedback path. ADR-0059's per-incident stream adds context (noun phrase, dropped entity, session) that cluster aggregation throws away.
3. **In-line publish, no ES scan.** Detection happens once per request inside the recall controller — ADR-0056's scan-from-ES pattern is unnecessary at this granularity.
4. **Reuse Captain's Log promotion pipeline.** Same fingerprint dedup, same `seen_count ≥ 3 / age ≥ 7 d` promotion gate, same Linear project flow as every other Phase-2/Phase-3 stream.
5. **Phase-2 governance is feature-flagged.** Tightening Stage 7 budget changes user-observable behaviour; ship behind a flag with default off, flip after 14 days of validated data.
6. **No new infrastructure.** Reuse the event bus, Captain's Log, the promotion pipeline, Linear, structlog. One new event type, one new stream, one new module of ~150 lines.

---

## Decision

### D1: Source — per-incident detection at recall controller

The signal is fired exactly when the recall controller's substring match (Stage 4b) overlaps a noun phrase against an entity that the budget stage dropped earlier in the same session.

After Bug B fix, the detection block in `src/personal_agent/request_gateway/recall_controller.py` runs against the correct `session_id`. After Bug A fix, that cache contains real entity identifiers extracted from `memory_context` items at compaction time.

The existing `log.warning("compaction_quality.poor", ...)` is **retained** — ADR-0056's allowlist consumes it for cross-trace clustering. The bus publish is *added*, not substituted.

### D2: Bug fixes (prerequisites, not implementation details)

Treating these as decisions makes them visible in review.

**Bug A — populate `entities_dropped` in Stage 7 Phase 2 trim.** Before assigning `memory_context = None`, extract identifiers from each item:

```python
dropped_entity_ids = tuple(
    str(item.get("entity_id") or item.get("name") or item.get("id") or "")
    for item in (memory_context or [])
    if isinstance(item, dict)
)
```

Pass `entities_dropped=dropped_entity_ids` into the `CompactionRecord` for tier `episodic`. Phase 1 (history) and Phase 3 (tool definitions) remain `()`-empty — those phases do not drop entities, and conflating them would dilute the signal.

**Bug B — thread `session_id` through the recall controller call site.** Add `session_id: str = ""` parameter to `run_recall_controller()`. Pass it from `pipeline.py`'s call site. Replace line 186's `trace_id`-as-`session_id` fallback with the new argument.

### D3: Data model

Three layers, mirroring ADR-0056:

**Layer A — `CompactionQualityIncident` (in-memory, per detection):**

```python
@dataclass(frozen=True)
class CompactionQualityIncident:
    """One detected compaction-quality incident."""
    fingerprint: str            # sha256(noun_phrase:dropped_entity:component)[:16]
    trace_id: str
    session_id: str
    noun_phrase: str            # cue extracted from user message
    dropped_entity: str         # entity that was dropped earlier in session
    recall_cue: str             # regex cue that triggered Stage 4b
    tier_affected: str          # "near" | "episodic" | "long_term"
    tokens_removed: int         # from the originating CompactionRecord (if known; 0 otherwise)
    detected_at: datetime
```

**Layer B — `CompactionQualityIncidentEvent` (bus, per incident):**

```python
class CompactionQualityIncidentEvent(EventBase):
    """Published when the recall controller detects a poor-compaction incident.

    One event per detection. Consumers:
      • cg:captain-log → CaptainLogEntry(category=KNOWLEDGE_QUALITY, scope=ORCHESTRATOR)
      • Phase 2: in-process IncidentTracker (per-session counter for budget hook)
    """

    event_type: Literal["context.compaction_quality_poor"] = "context.compaction_quality_poor"
    fingerprint: str
    noun_phrase: str
    dropped_entity: str
    recall_cue: str
    tier_affected: str
    tokens_removed: int
    detected_at: datetime
    # trace_id / session_id: required (request-correlated; per ADR-0054 D3)
    # source_component: "telemetry.context_quality"
```

Stream name: `stream:context.compaction_quality_poor` (per ADR-0054 `<domain>.<signal>`).

**Layer C — Durable write: `telemetry/context_quality/CQ-<YYYY-MM-DD>.jsonl`:**

One line per incident, appended. Per-day file is naturally rolling — the 30-day retention bound is implicit in operator file rotation rather than explicit cap-per-fingerprint logic. Each line carries the full Layer A record:

```json
{
  "fingerprint": "a4b9c0e2b3d74f8a",
  "trace_id": "trace-…",
  "session_id": "session-…",
  "noun_phrase": "caching system",
  "dropped_entity": "entity-redis-config",
  "recall_cue": "what was our caching system again",
  "tier_affected": "episodic",
  "tokens_removed": 412,
  "detected_at": "2026-04-27T14:33:05Z"
}
```

The file is the durable record per ADR-0054 D4: written *before* the bus publish; its failure aborts the publish with a logged warning.

### D4: Fingerprinting — `(noun_phrase, dropped_entity, component)`

```
fingerprint = sha256(f"{noun_phrase}:{dropped_entity}:request_gateway.recall_controller".encode()).hexdigest()[:16]
```

Finer-grained than ADR-0056's `(component, event_name, error_type)` because the per-incident path captures the *specific* recalled-vs-dropped overlap; cluster-level aggregation throws this away. The 16-hex-char (64-bit) keyspace is plenty for the expected scale.

The two streams produce distinct fingerprints by construction (ADR-0056 fingerprints `compaction_quality.poor + WARNING + <error_type-or-none>`; ADR-0059 fingerprints the noun-phrase/entity pair). If both fire for the same root cause, ADR-0030 fingerprint dedup at `CaptainLogManager.save_entry()` keeps one entry per fingerprint and merges `seen_count` — no duplication risk.

### D5: Captain's Log signal

`cg:captain-log` (existing consumer group from ADR-0058) subscribes to `stream:context.compaction_quality_poor`. The handler builds:

```python
CaptainLogEntry(
    type=CaptainLogEntryType.CONFIG_PROPOSAL,
    title=f'Compaction dropped "{dropped_entity}", user then asked about "{noun_phrase}"',
    rationale=(
        f'Stage 7 dropped entity "{dropped_entity}" (tier: {tier_affected}, '
        f'{tokens_removed} tokens) earlier in this session; the user then '
        f'asked about "{noun_phrase}" with cue "{recall_cue}". '
        f"The recall controller's substring match identified the overlap."
    ),
    proposed_change=ProposedChange(
        what="Investigate Stage 7 trim ordering for entity priority",
        why=(
            "Sustained context-quality incidents indicate the budget stage "
            "is dropping entities that the user actively references."
        ),
        how=(
            "1) Inspect the Captain's Log entry's trace_id in Kibana for the "
            "full compaction record.\n"
            "2) Decide whether the trim ordering needs entity-priority logic, "
            "or whether the budget ceiling itself is too tight.\n"
            "3) If patterns concentrate on a single entity class, consider "
            "promoting the entity in memory-recall scoring."
        ),
        category=ChangeCategory.KNOWLEDGE_QUALITY,
        scope=ChangeScope.ORCHESTRATOR,
        fingerprint=fingerprint,
    ),
    supporting_metrics=[
        f"tokens_removed: {tokens_removed}",
        f"tier_affected: {tier_affected}",
    ],
    metrics_structured=[
        Metric(name="tokens_removed", value=tokens_removed, unit="tokens"),
    ],
    telemetry_refs=[TelemetryRef(trace_id=trace_id, metric_name=None, value=None)],
)
```

`KNOWLEDGE_QUALITY` is the existing category (`captains_log/models.py:25`); no enum change required. `ORCHESTRATOR` scope correctly attributes the proposal to Stage 7 / recall controller.

### D6: Phase 2 — Per-session governance signal (flag-gated, default off)

Phase 1 surfaces incidents to humans via Captain's Log. Phase 2 gives the agent same-session response: when poor compaction has fired ≥ N times for a session in 24 h, Stage 7 reduces `max_tokens` for the *next* request in that session by a configurable percentage.

**Mechanism:**

- `IncidentTracker` (in `telemetry/context_quality.py`) maintains a per-session in-memory `dict[str, deque[datetime]]` of incident timestamps, capped at 1024 sessions LRU. `register(session_id)` is called from `record_incident()`. `count_in_window(session_id, hours)` returns the rolling count.
- `apply_budget()` in Stage 7 reads `IncidentTracker.count_in_window(session_id, 24)`. If `≥ context_quality_governance_threshold` (default 2) and `context_quality_governance_enabled=True`, reduce `max_tokens *= (1 - context_quality_governance_budget_reduction)` (default 15 %).
- Emit `log.info("context_quality_governance_tightened", ...)` so the tightening is visible in telemetry.

**Flip gate:** ship with `context_quality_governance_enabled=False`. After 14 days of Phase 1 telemetry, if the false-positive rate (Captain's Log entries marked `Rejected` in Linear) is below 20 %, flip the default. No code change at flip — it's a `.env` change.

**Why per-session, not global:** the signal is request-correlated. A specific session that has triggered repeated incidents has a recall pattern that the budget stage is mis-serving for that user/conversation; tightening globally would degrade everyone.

### D7: Composition table — ADR-0056 vs ADR-0059

| Aspect                  | ADR-0056 (cross-trace clustering)                | ADR-0059 (per-incident)                           |
|-------------------------|--------------------------------------------------|---------------------------------------------------|
| Trigger                 | `consolidation.completed` (hourly+)              | Inline at Stage 4b detection                      |
| Window                  | 24 h rolling                                     | None — single incident                            |
| Fingerprint granularity | `(component, event_name, error_type)`            | `(noun_phrase, dropped_entity, component)`        |
| Bus event               | `ErrorPatternDetectedEvent`                      | `CompactionQualityIncidentEvent`                  |
| Stream                  | `stream:errors.pattern_detected`                 | `stream:context.compaction_quality_poor`          |
| Captain's Log category  | `RELIABILITY`                                    | `KNOWLEDGE_QUALITY`                               |
| Question answered       | "Is this kind of failure recurring?"             | "What did we just lose, and for which user cue?"  |
| Phase 2 response        | Failure-path reflection (within-trace)           | Per-session budget tightening                     |
| Linear project          | Error Pattern Monitoring                         | Context Quality Monitoring                        |

Both fire for `compaction_quality.poor`; their fingerprints differ by construction. Captain's Log dedup handles any overlap.

### D8: Full automation cycle

```
1. Stage 7 (apply_budget) drops memory_context
   └─ extracts entity_id list (FIX A)
   └─ writes CompactionRecord(entities_dropped=…) via log_compaction()
   └─ log_compaction() updates _dropped_entities_by_session[session_id]

2. Stage 4b (run_recall_controller) — for the next user request in the session
   └─ extracts noun phrases from user message
   └─ session_id is plumbed in (FIX B)
   └─ get_dropped_entities(session_id) returns the live cache
   └─ substring match noun_phrase × dropped_entity
   └─ on hit:
      a) log.warning("compaction_quality.poor", …)   ← retained (ADR-0056 path)
      b) record_incident(incident, bus)              ← new (ADR-0059 path)

3. record_incident:
   a) Append JSON line to telemetry/context_quality/CQ-<YYYY-MM-DD>.jsonl  (DURABLE)
   b) Publish CompactionQualityIncidentEvent to stream:context.compaction_quality_poor (BUS)
   c) IncidentTracker.register(session_id)

4. cg:captain-log handler receives the bus event
   └─ builds CaptainLogEntry(CONFIG_PROPOSAL, KNOWLEDGE_QUALITY, ORCHESTRATOR)
   └─ CaptainLogManager.save_entry()
      ├─ if fingerprint suppressed (Rejected < 30 d ago) → discard silently
      ├─ if matching fingerprint on disk → increment seen_count, merge (ADR-0030)
      └─ else → write CL-…-*.json, index to ES

5. Next consolidation.completed → cg:promotion → PromotionPipeline
   └─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 d
   └─ creates Linear issue in "Context Quality Monitoring" project

6. Human reviews issue, applies label → ADR-0040 handlers process it.

7. (Phase 2, flag-gated) On every apply_budget() call:
   └─ count = IncidentTracker.count_in_window(session_id, 24)
   └─ if count ≥ threshold and flag enabled:
      └─ max_tokens *= (1 - reduction)
      └─ log.info("context_quality_governance_tightened", …)
```

**Loop closed.** Same surfaces as ADR-0056; same suppression / promotion / Linear feedback semantics; one new module of ~150 lines.

### D9: Scope boundary

In scope:

- Bug A and Bug B fixes (prerequisite).
- `CompactionQualityIncident` + `CompactionQualityIncidentEvent` + stream/file dual-write.
- `cg:captain-log` handler emitting `KNOWLEDGE_QUALITY` proposals.
- Phase 2 per-session governance hook in Stage 7 (flag default `False`).
- Tests for both bug fixes, dual-write ordering, dedup, and governance gate.

Out of scope:

- **LLM-based "is this entity needed?" classifier.** Adding an inference call to Stage 4b is too expensive; the substring match suffices. A future ADR can add embedding-based similarity if signal quality demands it.
- **Token-aware in-budget rebalancing.** Deferred to FRE-251 / ADR-0061 (Within-Session Progressive Context Compression). Phase 2 here only tightens the ceiling, not the trim strategy.
- **Cross-session token-budget calibration.** A long-term knob (e.g. raising the global budget when context quality patterns exceed a threshold) is out of scope; the per-session response is the right granularity for now.
- **Backfill of historical incidents** from the existing `compaction_quality.poor` ES warnings. Orthogonal — ADR-0056's clustering already covers historical patterns through its 24 h rolling window.
- **Real-time alerting.** Same as ADR-0056 D9 — the monitor is rolling-window + pattern-based, not per-event paging.

---

## Alternatives Considered

### Source / collection mechanism

| Option                                                                              | Verdict                                                               |
|-------------------------------------------------------------------------------------|-----------------------------------------------------------------------|
| A. ES scan triggered by `consolidation.completed`, mirroring ADR-0056                | Rejected — duplicates ADR-0056 work; loses the per-incident context (noun phrase / dropped entity) by aggregating away |
| B. New structlog processor that intercepts `compaction_quality.poor` records         | Rejected — every process would carry its own buffer and emit; the detector path already runs in-process where the signal originates |
| **C. In-line publish from the detection site**                                      | **Selected** — the detector already has the full incident structure; one new module + one bus publish call               |

### Fingerprint granularity

| Option                                                       | Verdict                                                                            |
|--------------------------------------------------------------|------------------------------------------------------------------------------------|
| A. `(component, event_name)` — same as ADR-0056              | Rejected — collapses to a single fingerprint per session class; loses entity info  |
| B. `(noun_phrase)` only                                      | Rejected — common phrases ("the system", "our config") would dominate              |
| **C. `(noun_phrase, dropped_entity, component)`**            | **Selected** — distinguishes incidents by what was lost vs what the user wanted    |
| D. `(noun_phrase, dropped_entity, session_id)`               | Rejected — session_id makes the fingerprint unique per session, breaking dedup     |

### Phase 2 response shape

| Option                                                                       | Verdict                                                                |
|------------------------------------------------------------------------------|------------------------------------------------------------------------|
| A. Tighten budget globally when any session crosses threshold                | Rejected — degrades everyone for one user's pattern                    |
| **B. Tighten budget per-session, in-memory counter, flag-gated**             | **Selected** — surgical; reverts when the session ends or the incident pattern stops |
| C. Re-prioritise trim order (drop tools before memory)                       | Deferred to FRE-251 / ADR-0061 — that's the within-session compression problem |
| D. Notify the user inline ("I lost some context, want to re-anchor?")        | Rejected — UX surface change, premature                                |

### New ChangeCategory

| Option                                                          | Verdict                                                       |
|-----------------------------------------------------------------|---------------------------------------------------------------|
| A. Add `CONTEXT_QUALITY` to `ChangeCategory` enum                | Rejected — `KNOWLEDGE_QUALITY` already exists and fits        |
| **B. Reuse `KNOWLEDGE_QUALITY`**                                | **Selected** — losing context = losing knowledge access; one less moving part |

---

## Consequences

### Positive

- **Stream 7 ships as a real feedback loop** for the first time. After Bug A and Bug B, `compaction_quality.poor` finally has data; ADR-0056 cross-trace clustering and ADR-0059 per-incident response both come online together.
- **Composability proves out.** Two streams, distinct fingerprints, same surfacing channel. ADR-0030 dedup handles overlap. The pattern is reusable for ADR-0060 (KG quality) which has a similar cross-trace + per-incident structure.
- **Phase 2 is reversible.** Default-off flag means we can ship the data plane and let humans observe before turning on the governance response.
- **Bug fixes are independently valuable.** Even if the bus/CL machinery were not added, fixing Bugs A and B would already restore a useful Kibana signal.

### Negative

- **One new consumer subscription** in `service/app.py` lifespan.
- **One new event type / parse_stream_event arm** in `events/models.py`.
- **Per-session in-memory state in IncidentTracker** — bounded LRU, but one more piece of process state to reason about. Restart-safe (the durable JSONL is the source of truth; the in-memory tracker is an optimisation for Phase 2).
- **Phase 2 hook adds a settings.read in Stage 7's hot path** — one extra dict lookup per request when the flag is on.

### Risks

| Risk                                                                                       | Likelihood | Mitigation                                                                                       |
|--------------------------------------------------------------------------------------------|------------|--------------------------------------------------------------------------------------------------|
| Substring-match false positives flood Captain's Log with low-quality proposals             | Medium     | Phase 2 starts off; CL fingerprint dedup + Linear `Rejected` suppression both apply per ADR-0030/0040 |
| `entities_dropped` extraction picks wrong identifier shape from `memory_context` items     | Low        | Helper checks `entity_id` then `name` then `id`; missing → empty string filtered out             |
| Per-session tracker grows unbounded                                                        | Low        | LRU cap at 1024 sessions; entries older than 24 h dropped on each `register`                     |
| Phase 2 over-tightens budget on a single bad session and starves the conversation          | Medium     | Flag default off; threshold (2) configurable; reduction (15 %) is moderate; emits visible `tightened` log |
| Concurrent ADR-0056 and ADR-0059 both fire for the same root cause → duplicated CL entries | Low        | Distinct fingerprints by construction; CL dedup merges common entries by fingerprint             |

---

## Implementation Priority

### Phase 1 — Per-incident loop closure

| Order | Work                                                                                                | Tier         |
|-------|-----------------------------------------------------------------------------------------------------|--------------|
| 1     | Bug A fix: `apply_budget()` populates `entities_dropped` in Phase 2 trim                             | Tier-3: Haiku |
| 2     | Bug B fix: thread `session_id` through `run_recall_controller` and pipeline call site               | Tier-3: Haiku |
| 3     | `STREAM_CONTEXT_COMPACTION_QUALITY_POOR` constant + `CompactionQualityIncidentEvent` + parse arm     | Tier-3: Haiku |
| 4     | `telemetry/context_quality.py` — `CompactionQualityIncident`, `record_incident`, `IncidentTracker`   | Tier-2: Sonnet |
| 5     | Replace dead `log.warning` block in recall controller with dual-write call                           | Tier-3: Haiku |
| 6     | `build_compaction_quality_captain_log_handler()` in `events/pipeline_handlers.py`                    | Tier-2: Sonnet |
| 7     | Wire subscription in `service/app.py` behind `context_quality_stream_enabled` flag                   | Tier-3: Haiku |
| 8     | Config flags (4 new fields with `context_quality_` prefix)                                           | Tier-3: Haiku |
| 9     | Unit tests — bug-A regression, bug-B regression, dual-write ordering, handler dedup                  | Tier-2: Sonnet |
| 10    | `FEEDBACK_STREAM_ARCHITECTURE.md` Stream 7 row update                                                | Tier-3: Haiku |

### Phase 2 — Governance response (flag default `False`)

| Order | Work                                                                  | Tier          |
|-------|-----------------------------------------------------------------------|---------------|
| 1     | `IncidentTracker.count_in_window()` consumed in `apply_budget()`      | Tier-2: Sonnet |
| 2     | Reduction math + `context_quality_governance_tightened` log event     | Tier-3: Haiku |
| 3     | Unit test — counter math, flag gate, no-op when disabled              | Tier-2: Sonnet |
| 4     | Document flip gate in this ADR (D6) — done                            | Tier-3: Haiku |

Phase 1 ships in this implementation. Phase 2 ships in the same PR but flag-gated off; the flip is a `.env` change after 14 days.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component                                                                          | Module                                                          | Layer          |
|------------------------------------------------------------------------------------|-----------------------------------------------------------------|----------------|
| Stream/event constants                                                              | `src/personal_agent/events/models.py`                          | Infrastructure |
| `CompactionQualityIncident`, `record_incident`, `IncidentTracker`                   | `src/personal_agent/telemetry/context_quality.py`              | Observation    |
| `build_compaction_quality_captain_log_handler`                                      | `src/personal_agent/events/pipeline_handlers.py`               | Observation    |
| Bug A: `entities_dropped` extraction                                                | `src/personal_agent/request_gateway/budget.py`                 | Execution      |
| Bug B: `session_id` thread                                                           | `src/personal_agent/request_gateway/recall_controller.py`, `pipeline.py` | Execution      |
| Phase 2: budget hook                                                                | `src/personal_agent/request_gateway/budget.py`                 | Execution      |
| Config flags                                                                         | `src/personal_agent/config/settings.py`                        | Infrastructure |

The Execution Layer files (`budget.py`, `recall_controller.py`) are touched only because the bug fixes are intrinsic to the stream — the dependency direction (Execution writes durable telemetry; Observation reads/scans) is preserved.

---

## Open Questions

1. **Identifier extraction shape.** The `memory_context` items are `dict`s today; the Bug A fix probes `entity_id` → `name` → `id`. If memory protocol later switches to typed objects, the helper needs adapting — implementation will add a regression test on the dict shape.
2. **Threshold calibration (2 incidents / 24 h).** Best-guess number; revisit after 30 days of Phase 1 data. Threshold is a settings field — calibration is a `.env` change.
3. **Should Phase 2 escalate at higher counts?** E.g. 2 → 15 % reduction, 5 → 30 %. Out of scope for v1; one knob is enough until we have data.
4. **JSONL retention.** Per-day file naming is implicit rotation. Operator decides when to archive/delete `CQ-*.jsonl`; this ADR does not impose a retention policy.

---

## Dedicated Linear Project — Context Quality Monitoring

Per `FEEDBACK_STREAM_ARCHITECTURE.md`, Stream 7 issues land in the **"Context Quality Monitoring"** project.

### Project configuration

| Field                  | Value                                          |
|------------------------|------------------------------------------------|
| Project name           | Context Quality Monitoring                     |
| Team                   | FrenchForest                                   |
| Default issue state    | Needs Approval                                 |
| Labels on creation     | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping       | `seen_count ≥ 10` → High; `seen_count ≥ 3` → Normal; else Low |

### Issue format

```
Title: [Compaction] dropped "<dropped_entity>" then user asked about "<noun_phrase>"

Body:
  ## Incident summary
  Dropped entity:   <dropped_entity>
  User noun phrase: <noun_phrase>
  Recall cue:       <recall_cue>
  Tier affected:    <tier_affected>
  Tokens removed:   <tokens_removed>
  Fingerprint:      <fingerprint>
  Seen count:       <seen_count>

  ## Representative trace
  - <trace_id>

  ## Proposed action
  Investigate Stage 7 trim ordering for entity priority. Inspect the Captain's
  Log entry's trace_id in Kibana for the full compaction record. Decide whether
  the trim ordering needs entity-priority logic, or whether the budget ceiling
  itself is too tight for this kind of session.

  ## Phase 2 response (when enabled)
  After ≥ <threshold> incidents/24h in a session, Stage 7 reduces max_tokens
  by <reduction>% for the next request in that session.
```

### Feedback labels (inherited from ADR-0040)

Same as ADR-0056 — Approved / Rejected / Deepen / Too Vague / Defer. Rejected suppresses the fingerprint for 30 days.

---

## End State

### After Phase 1 ships

| What exists                                                                     | What is automated                                                            | What is visible                                                            |
|---------------------------------------------------------------------------------|------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Bug A and Bug B fixed                                                           | `entities_dropped` populated; `compaction_quality.poor` warning fires        | Stream 7 detection finally produces signals (visible in Kibana)            |
| `CompactionQualityIncidentEvent` on `stream:context.compaction_quality_poor`    | Dual-write: `CQ-*.jsonl` then bus publish                                    | Per-day JSONL files in `telemetry/context_quality/`                        |
| `cg:captain-log` handler                                                        | Each incident becomes `CaptainLogEntry(KNOWLEDGE_QUALITY, ORCHESTRATOR)`     | Entries in `telemetry/captains_log/` with context-quality fingerprints     |
| Promotion pipeline (existing)                                                   | After `seen_count ≥ 3` and `age ≥ 7 d` → Linear issue                        | Issues in "Context Quality Monitoring" project                             |
| ADR-0056 path stays live                                                        | Cross-trace clustering of `compaction_quality.poor` warnings                 | RELIABILITY proposals in "Error Pattern Monitoring" project                |

Human action required: review and label Linear issues in "Context Quality Monitoring".

### After Phase 2 enabled (flag flipped)

| What exists                                | What is automated                                                                                                          | What is visible                                                              |
|--------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `IncidentTracker` per-session counter       | Stage 7 reads tracker; reduces `max_tokens` by 15 % when threshold exceeded                                               | `context_quality_governance_tightened` log events visible in Kibana          |
| `context_quality_governance_enabled=True`   | Per-session response automatic                                                                                            | Reduced budget visible as `max_tokens` field in `context_budget_applied` log |

Human action required: monitor false-positive rate in Linear over the first month after the flip; tune threshold or revert if signal quality degrades.

---

## Loop Completeness Criteria

The stream is verified closed and working when, over a trailing 14-day window post-merge:

1. **Production**: `XLEN stream:context.compaction_quality_poor ≥ 1` per typical-load week.
2. **Ingestion**: `count(telemetry/context_quality/CQ-*.jsonl)` grows monotonically; every emitted bus event has a matching JSONL line.
3. **Promotion**: at least one `CompactionQualityIncident → CaptainLogEntry → Linear issue` round trip end-to-end, traceable by fingerprint.
4. **Feedback**: at least one Linear label (Approved / Rejected / Deepen / Too Vague / Defer) processed by `FeedbackPoller`.
5. **Suppression**: after a `Rejected` label, the next scan that would have re-emitted the same fingerprint finds it suppressed (log: `captains_log_proposal_suppressed`), and no new entry is written.

If (1) holds but (3) does not, the promotion gate (`seen_count ≥ 3`, `age ≥ 7 d`) is too conservative for the incident rate; tune in config, not in this ADR.

---

## Feedback Stream ADR Template — Compliance Checklist

Per the Feedback Stream ADR Template established in ADR-0053:

- [x] **1. Stream identity** — Phase 3 stream; Observation+Execution touch points; depends on ADR-0041/0043/0047/0053/0054/0056
- [x] **2. Source** — recall-controller substring match against compaction cache; per-incident
- [x] **3. Collection mechanism** — in-line publish; durable JSONL append; ADR-0054 D4 ordering
- [x] **4. Processing algorithm** — fingerprint by `(noun_phrase, dropped_entity, component)`; CL dedup
- [x] **5. Signal produced** — `CompactionQualityIncidentEvent` on bus; per-day JSONL on disk; `CaptainLogEntry(KNOWLEDGE_QUALITY)` via handler
- [x] **6. Full automation cycle** — D8 traces the 7-step loop end to end
- [x] **7. Human review interface** — "Context Quality Monitoring" Linear project; issue format; label semantics inherited
- [x] **8. End state table** — Phase 1, Phase 2 enabled
- [x] **9. Loop completeness criteria** — 5-point check, 14-day evaluation window

---

## References

- FRE-249: Draft ADR — Context Quality Stream (this ADR)
- ADR-0041: Event Bus via Redis Streams — transport
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0047: Context Management & Observability — D3 introduced the dropped-entity cache and the `compaction_quality.poor` warning
- ADR-0053: Gate Feedback-Loop Monitoring Framework — establishes the Feedback Stream ADR Template this ADR follows
- ADR-0054: Feedback Stream Bus Convention — dual-write, stream naming, `EventBase` contract fields
- ADR-0056: Error Pattern Monitoring Stream — composes on top of (`compaction_quality.poor` is in `WARNING_EVENT_ALLOWLIST`)
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — surfacing channel, fingerprint dedup
- ADR-0040: Linear as Async Feedback Channel — label semantics, suppression
- ADR-0061: Within-Session Progressive Context Compression — downstream stream that may consume the per-session governance signal
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — feedback-stream catalogue (Stream 7 row updated)
- `src/personal_agent/request_gateway/budget.py` — Bug A fix site
- `src/personal_agent/request_gateway/recall_controller.py` — Bug B fix site + detection site
- `src/personal_agent/telemetry/context_quality.py` — new module (durable + bus + tracker)
- `src/personal_agent/events/pipeline_handlers.py` — new CL handler builder
