# ADR-0057: Insights & Pattern Analysis Stream

**Status**: Accepted (Implemented — FRE-247 — 2026-04-24)
**Date**: 2026-04-23
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Architectural Separation), ADR-0053 (Gate Feedback-Loop Monitoring Framework — template), ADR-0054 (Feedback Stream Bus Convention)
**Related**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel), ADR-0042 (Knowledge Graph Freshness via Access Tracking)
**Enables**: FRE-250 (ADR-0060 Knowledge Graph Quality Stream — reuses `stream:insights.pattern_detected` for consolidation-quality anomalies), Slice 3 adaptive improvement (needs patterns-as-events to close a learning loop)
**Linear Issue**: FRE-247

---

## Context

### The two most sophisticated detection systems in the agent terminate at a dashboard

The `InsightsEngine` performs cross-query analysis across Elasticsearch, Neo4j, and PostgreSQL to produce six insight types — `correlation`, `optimization`, `trend`, `anomaly`, `graph_staleness`, `feedback_summary`. It also runs 3-sigma + 2×-floor statistical anomaly detection on daily API-cost history to detect `daily_cost_spike` anomalies with confidence scores. Both are Level 4-style analyses (LLM-mediated-equivalent statistical inference) packaged as structured outputs — and both are detection-only dead ends:

- `InsightsEngine.suggest_improvements()` and `InsightsEngine.create_captain_log_proposals()` exist, are fully implemented, and are **never called**. The existing `build_consolidation_insights_handler` in `events/pipeline_handlers.py` calls `analyze_patterns()` — but only to log the count. The `list[Insight]` it returns is dropped on the floor.
- `InsightsEngine.detect_delegation_patterns()` is a documented stub. It logs start/complete events but returns an empty list with a comment saying "Scaffold: full implementation requires ES query support which will be added when delegation outcomes accumulate" — and delegation outcomes *have been* accumulating since Slice 2. The ES event name is `delegation_outcome_recorded`; the structured fields are `task_id`, `success`, `rounds_needed`, `what_worked`, `what_was_missing`, `artifacts_count`, `duration_minutes`, `user_satisfaction`.
- `CostAnomaly` objects are returned, indexed to `agent-insights-*`, and then discarded — the `Improvement` objects downstream of them never reach any consumer because `suggest_improvements()` never runs on the response path.

These are the dead ends explicitly called out in `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` as Streams 4 and 9. This ADR closes both loops simultaneously and adds a real implementation of delegation pattern detection.

### Feedback Stream Bus Convention gaps

ADR-0054 established the dual-write convention: every feedback stream writes durably AND publishes a typed bus event. Stream 4 (Insights Engine) *partially* complies — insights are indexed to `agent-insights-*` (durable) — but publishes nothing on the bus. Stream 9 (Cost Anomaly) does the same. Neither stream is composable: the only way to act on an insight today is to query ES yourself. A future consumer cannot subscribe to "pattern detected" because there is nothing to subscribe to.

### Why delegation patterns matter now

Delegation is the most strategic routing decision the agent makes — it sends work to an external agent (Claude Code, Codex) and receives back a `DelegationOutcome` with `success`, `rounds_needed`, `what_was_missing`. Patterns in these outcomes answer:

- Which delegation targets succeed most often?
- What missing-context reasons recur?
- How many rounds does a delegation typically need?
- Do delegation success rates vary by TaskType or by complexity?

Today none of these can be answered programmatically. The agent cannot self-correct its delegation matrix because it cannot see its delegation history as structured data.

---

## Decision Drivers

1. **Wire what already works.** `analyze_patterns()`, `detect_cost_anomalies()`, `create_captain_log_proposals()`, and `suggest_improvements()` already run correctly. The fix is calling them in the right sequence and publishing their output.
2. **Dual-write convention (ADR-0054).** Insights are already durable in `agent-insights-*`. Add the bus publish so downstream consumers can subscribe.
3. **Fingerprint dedup belongs on `ProposedChange`.** `create_captain_log_proposals()` today builds `ProposedChange` without a fingerprint. Add a deterministic fingerprint so the existing ADR-0030 dedup pipeline absorbs repeat proposals instead of flooding Captain's Log every consolidation.
4. **Delegation pattern analysis from real data.** `delegation_outcome_recorded` events have been accumulating in `agent-logs-*` since Slice 2. Implement the stub with ES queries now; no synthetic data, no test harness.
5. **Cost anomaly → governance mapping is out of scope for Phase 1.** Publishing the event creates the composability hook. The *response* (e.g. trigger ALERT mode, reduce budget) requires coordination with ADR-0055 (Mode Manager fix) and is deferred to a Phase 2 ADR or a Slice 3 capability.
6. **No new scheduling surface.** All triggers are existing events: `consolidation.completed` (periodic) and `feedback.received` (human labels).
7. **Three event families reflect three analysis classes.** Pattern insights, cost anomalies, and delegation patterns deserve distinct event types — subscribers filter by `event_type`, not by a free-text `insight_type` field.

---

## Decision

### D1: Sources — Three analysis classes mapped to three bus events

| Source | Detection | Existing method | New event |
|--------|-----------|-----------------|-----------|
| Cross-data patterns | `analyze_patterns()` → 6 insight types (correlation, optimization, trend, anomaly, graph_staleness, feedback_summary) | `InsightsEngine.analyze_patterns(days)` | `InsightsPatternDetectedEvent` (per insight) |
| Cost anomalies | 3-sigma + 2×-floor daily cost threshold | `InsightsEngine.detect_cost_anomalies(days)` | `InsightsCostAnomalyEvent` (per anomaly) |
| Delegation patterns | Rolling-window analysis of `delegation_outcome_recorded` events | `InsightsEngine.detect_delegation_patterns(days, trace_id)` *(today a stub — this ADR specifies the implementation)* | `InsightsPatternDetectedEvent` with `pattern_kind="delegation"` |

`analyze_patterns()` remains the fan-out method; it already calls `detect_cost_anomalies()` internally and wraps the anomaly as an `insight_type="anomaly"` `Insight`. This ADR adds one more internal call — `detect_delegation_patterns()` — which today returns `[]`. The difference is that each `Insight` returned now produces a bus event, and each `CostAnomaly` additionally produces a typed `InsightsCostAnomalyEvent` (carrying the structured anomaly fields) rather than a generic pattern event.

### D2: Wiring — Extend `build_consolidation_insights_handler`

The `cg:insights` handler today runs on `stream:consolidation.completed`, calls `analyze_patterns(days=7)`, and logs the count. The handler is extended to:

```
for each Insight in analyze_patterns():
    1. (ADR-0054 durable): indexing to agent-insights-* already happens inside
       InsightsEngine._index_insights() — no change.
    2. publish InsightsPatternDetectedEvent(insight_type, title, summary,
                                             confidence, evidence,
                                             fingerprint=sha256(insight_type:normalised_title)[:16])
       to stream:insights.pattern_detected.

for each CostAnomaly in detect_cost_anomalies() (called inside analyze_patterns):
    1. (durable): agent-insights-* — already indexed as part of analyze_patterns().
    2. publish InsightsCostAnomalyEvent(...) to stream:insights.cost_anomaly.

proposals = await InsightsEngine.create_captain_log_proposals(insights)
for proposal in proposals:
    CaptainLogManager.save_entry(proposal)
    — ADR-0030 fingerprint dedup takes over (seen_count increment when repeat).
```

The file write is the durable record (per ADR-0054 D4: durable precedes bus). The bus publish is the composability hook. Both failures are logged; neither poisons the pipeline.

**Why `create_captain_log_proposals()` does NOT publish an additional event.** Captain's Log entries already publish `captains_log_entry_created` (existing structlog event). The bus event family for CL entries is owned by ADR-0058 (Self-Improvement Pipeline Stream). This ADR deliberately does not duplicate that surface.

**Backward compatibility.** The current `handler` logs `insights_analysis_from_consolidation` with `insights_count`. This log line is preserved — it is still the at-a-glance run marker for operators. The new behaviour is strictly additive.

### D3: Data model — Three layers

#### Layer A — existing `Insight` / `CostAnomaly` dataclasses (unchanged)

The frozen `Insight` and `CostAnomaly` dataclasses in `insights/engine.py` stay as they are. They carry everything the bus events need.

#### Layer B — Two new bus events

```python
class InsightsPatternDetectedEvent(EventBase):
    """Published when InsightsEngine.analyze_patterns() produces an insight.

    One event per insight per consolidation. Consumers:
      • cg:captain-log       → already reached via create_captain_log_proposals()
                                flow; this event is for FUTURE consumers that want
                                to act on patterns without touching the proposal
                                pipeline.
      • FRE-250 (KG Quality) → subscribe and filter by insight_type in
                                {"graph_staleness", "graph_staleness_trend"}.
      • FRE-226 (skills)     → subscribe and filter by insight_type == "trend"
                                when trend indicates a workflow change.
    """

    event_type: Literal["insights.pattern_detected"] = "insights.pattern_detected"
    insight_type: str                          # "correlation" | "optimization" | "trend" |
                                               # "anomaly" | "graph_staleness" |
                                               # "graph_staleness_trend" | "feedback_summary" |
                                               # "feedback_category" | "delegation"
    pattern_kind: str                          # discriminator within insight_type, e.g.
                                               # "delegation_success_rate", "delegation_rounds",
                                               # "delegation_missing_context", or "" for the
                                               # built-in 6 types that don't need a sub-kind
    title: str
    summary: str
    confidence: float                          # 0..1
    actionable: bool
    evidence: dict[str, float | int | str]
    fingerprint: str                           # sha256(insight_type:pattern_kind:normalised_title)[:16]
    analysis_window_days: int
    # trace_id / session_id: None  (consolidation-triggered, not request-correlated)
    # source_component: "insights.engine"


class InsightsCostAnomalyEvent(EventBase):
    """Published when InsightsEngine.detect_cost_anomalies() detects a spike.

    Separate from pattern events because the response path is fundamentally
    different — patterns propose self-improvement; cost anomalies may trigger
    governance responses (Phase 2).
    """

    event_type: Literal["insights.cost_anomaly"] = "insights.cost_anomaly"
    anomaly_type: str                          # today: "daily_cost_spike"
    message: str
    observed_cost_usd: float
    baseline_cost_usd: float
    ratio: float                               # observed / baseline
    confidence: float                          # 0..1
    severity: str                              # "low" | "medium" | "high" (D5)
    fingerprint: str                           # sha256(anomaly_type:observation_date)[:16]
    observation_date: str                      # ISO yyyy-mm-dd — the day spiked
    # trace_id / session_id: None
    # source_component: "insights.engine"
```

Stream names (per ADR-0054 `<domain>.<signal>`):

- `stream:insights.pattern_detected`
- `stream:insights.cost_anomaly`

`cg:insights` stays attached to `stream:consolidation.completed` (consumer group — existing). No new consumer group on the *publish* side; `cg:insights` is simultaneously the producer for these new streams.

#### Layer C — Durable writes

Insights are already indexed to `agent-insights-YYYY-MM-DD` (existing behaviour in `InsightsEngine._index_insights()`). ADR-0054's D4 ordering rule is respected because indexing happens inside `analyze_patterns()` before the handler reaches the `publish` calls.

Cost anomalies are indexed through the same path (they are wrapped as `Insight(insight_type="anomaly", …)` and flow through `_index_insights()`). The `InsightsCostAnomalyEvent` also carries the full anomaly shape so subscribers do not need to query ES.

No new files on disk. The ES indices are the durable record.

### D4: Delegation pattern implementation

The stub becomes real. Implementation specification:

**ES query:** `event_type == "delegation_outcome_recorded"` over trailing `days` (default 30) from `agent-logs-*`. Returned fields: `task_id`, `success`, `rounds_needed`, `what_worked`, `what_was_missing`, `artifacts_count`, `duration_minutes`, `user_satisfaction`, `trace_id`, `@timestamp`.

**Aggregations and resulting insights:**

| Aggregation | Threshold | Pattern_kind | Confidence |
|-------------|-----------|--------------|------------|
| Success rate (overall) | `count ≥ 10 && success_rate < 0.60` | `delegation_success_rate` | `0.70 + min(0.20, (10 - observed_success_rate * 10) * 0.02)` — higher with more data |
| Rounds needed | `count ≥ 10 && p75 ≥ 3` | `delegation_rounds` | `0.60 + 0.05 * min(10, p75)` (capped at 0.85) |
| Missing-context theme | terms aggregation on `what_was_missing.keyword` where bucket count ≥ 3 | `delegation_missing_context` | `0.55 + 0.05 * min(8, bucket_count)` |

Each triggering aggregation produces one `Insight(insight_type="delegation", …)` with `evidence` populated with the exact sample counts and percentiles. `pattern_kind` is carried through to the bus event so subscribers can filter without parsing the title.

**Why three pattern_kinds, not one.** A low success rate and a high rounds-needed are *different* proposals — the former suggests the delegation target is the wrong fit; the latter suggests the briefing is incomplete. Bundling them under a generic "delegation pattern" event would force every subscriber to re-parse.

**Minimum sample size** (`count ≥ 10`) is intentionally conservative — delegation is low-volume (target: 1–5 delegations per day under Slice 2 workloads), so the 30-day default window yields ~30–150 samples. A tighter window would fire false patterns before real trends appear.

**What the stub becomes.** Today: `delegation_pattern_analysis_start` log → empty list → `delegation_pattern_analysis_complete`. After this ADR: same log wrapper, but the body runs three aggregations, builds up to three `Insight` objects, and returns them.

### D5: Cost anomaly severity — in-band classification, out-of-band response

A `CostAnomaly` carries `ratio = observed / baseline`. Classification for the event:

| `ratio` | `confidence` | `severity` |
|---------|--------------|------------|
| `< 2.5` | `0.60` | `low` |
| `2.5 ≤ ratio < 4.0` | `0.75` | `medium` |
| `≥ 4.0` | `0.85` | `high` |

These boundaries match (and extend) the existing `CostAnomaly.confidence` assignment — no behavioural regression; we just add a discrete severity field for subscribers.

**Response path in Phase 1:** `InsightsCostAnomalyEvent` is published. A CL entry is *also* created through `create_captain_log_proposals()` (anomalies today produce `cost_control` `Improvement` objects — Phase 1 wires the CL emission).

**Response path in Phase 2 (deferred to a follow-on ADR):** a governance consumer subscribes to `stream:insights.cost_anomaly` and, on `severity=high`, triggers an ALERT mode transition through the Mode Manager (once ADR-0055 closes the Mode Manager disconnect). Phase 2 is out of scope for this ADR; publishing the event is the prerequisite.

### D6: Fingerprinting

Fingerprint construction is critical because `analyze_patterns()` is idempotent-ish — the same consolidation trigger can re-produce the same insight minutes later if inputs are stable. Without a stable fingerprint, CL entries would duplicate every consolidation and the Linear project would flood.

**Pattern fingerprint:** `sha256(f"{insight_type}:{pattern_kind}:{title_normalised}".encode())[:16]`, where `title_normalised` replaces digits with `#` (so "Cost spike: $4.12 on 2026-04-19" and "Cost spike: $5.23 on 2026-04-20" collapse to the same fingerprint for dedup purposes — the CL entry `seen_count` is what tracks repetition).

**Cost anomaly fingerprint:** `sha256(f"{anomaly_type}:{observation_date}".encode())[:16]`. Each distinct *day* that spikes gets one entry; repeated consolidations on the same day merge via `seen_count`.

**Where fingerprint flows:** onto `ProposedChange.fingerprint` inside `create_captain_log_proposals()`. `CaptainLogManager.save_entry()` already reads this field and absorbs duplicates into the first entry for that fingerprint. The bus event carries the fingerprint for future subscribers to use as a correlation key.

### D7: Surfacing Channels

**Primary — Captain's Log + promotion pipeline + Linear:**

`InsightsEngine.create_captain_log_proposals()` builds `CaptainLogEntry(CONFIG_PROPOSAL, category=<derived>)` for each actionable insight. Category mapping from `insight_type`:

| `insight_type` | `ChangeCategory` | `ChangeScope` |
|----------------|-------------------|---------------|
| `correlation` | `PERFORMANCE` | `CROSS_CUTTING` |
| `optimization` | `PERFORMANCE` | `BRAINSTEM` |
| `trend` | `OBSERVABILITY` | `CROSS_CUTTING` |
| `anomaly` | `COST` | `LLM_CLIENT` |
| `graph_staleness` / `graph_staleness_trend` | `KNOWLEDGE_QUALITY` | `SECOND_BRAIN` |
| `feedback_summary` / `feedback_category` | `OBSERVABILITY` | `CAPTAINS_LOG` |
| `delegation` | `RELIABILITY` | `ORCHESTRATOR` |

(Today `create_captain_log_proposals()` does not set category/scope. This ADR adds the mapping.)

Entries flow through the existing ADR-0030 / ADR-0040 promotion pipeline into the **"Insights & Pattern Analysis"** Linear project (already exists — see `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`).

**Secondary — Kibana:**

`agent-insights-*` is already a Kibana index. Two new panels ship with this ADR's implementation:

- "Insight types breakdown": terms aggregation on `insight_type` over the trailing 7 days.
- "Cost anomaly timeline": date histogram of `cost_anomaly` records over the trailing 30 days, coloured by `severity`.

Both are added to the existing "Self-Improvement" dashboard.

**Tertiary — Programmatic:**

`TelemetryQueries` is **not** extended by this ADR — `analyze_patterns()` already owns the query surface. The implementation issue may add a thin `InsightsEngine.get_recent_patterns(days, insight_type=None)` helper for a future `query_insights` native tool, but that tool is out of scope for this ADR.

### D8: Full Automation Cycle

```
1. Brainstem scheduler runs consolidation → publishes ConsolidationCompletedEvent
   on stream:consolidation.completed

2. cg:insights (existing) receives the event
   └─ old behaviour: calls analyze_patterns(days=7); logs count
   └─ NEW: for each Insight produced:
       a) publish InsightsPatternDetectedEvent to stream:insights.pattern_detected (BUS)
   └─ NEW: for each CostAnomaly (wrapped inside analyze_patterns as anomaly insight):
       a) publish InsightsCostAnomalyEvent to stream:insights.cost_anomaly (BUS)
   └─ NEW: call InsightsEngine.create_captain_log_proposals(insights)
   └─ NEW: for each proposal:
       a) CaptainLogManager.save_entry(proposal)
          ├─ ADR-0030 fingerprint dedup: if fingerprint matches an existing entry,
          │   seen_count is incremented; otherwise a new CL-YYYYMMDD-*.json is
          │   written and indexed.
          └─ ADR-0040 suppression: if fingerprint is under rejection suppression,
             save_entry returns None; no file is written.

3. Next consolidation.completed → cg:promotion → PromotionPipeline.scan_promotable_entries()
   └─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 days
   └─ creates Linear issue in "Insights & Pattern Analysis" project
   └─ publishes PromotionIssueCreatedEvent to stream:promotion.issue_created

4. Human receives Linear issue
   └─ reviews insight, evidence, proposed action
   └─ applies label: Approved / Rejected / Deepen / Too Vague / Defer

5. FeedbackPoller (daily) dispatches to label handler (ADR-0040)
   └─ Rejected  → suppression (30 days); next matching fingerprint is dropped at step 2
   └─ Approved  → implementation is human-owned
   └─ Deepen    → LLM re-analysis; refined proposal posted as comment
   └─ Too Vague → refined proposal with more specific evidence
   └─ Defer     → re-evaluated next consolidation

6. Delegation pattern special case (D4):
   └─ step 2 uses the now-real detect_delegation_patterns(days=30) output
   └─ delegation Insight objects flow through the same pipeline
   └─ missing-context themes become repeatable proposals for briefing improvements

7. Cost anomaly special case (D5):
   └─ InsightsCostAnomalyEvent is on the bus — Phase 2 governance consumer (future)
     will subscribe and trigger ALERT mode when severity=high and Mode Manager
     is wired (ADR-0055 accepted)
   └─ CL entry is also created (cost_control Improvement via create_captain_log_proposals)
   └─ Linear issue in "Insights & Pattern Analysis" with cost delta as headline
```

**Loop closed.** Patterns surface through the existing promotion pipeline. Suppression and approval flow through existing Linear label handlers. The two new bus events make future subscribers possible without touching insights code.

### D9: Scope Boundary

In scope:

- Wire `create_captain_log_proposals()` into the existing `cg:insights` handler.
- Add `fingerprint` construction to `create_captain_log_proposals()` so dedup works.
- Add `InsightsPatternDetectedEvent` and `InsightsCostAnomalyEvent` and publish them on the respective streams.
- Implement `detect_delegation_patterns()` (replaces the stub) with three aggregations (D4).
- Add category/scope mapping for `insight_type → ChangeCategory/ChangeScope`.
- Add `severity` classification for cost anomalies (D5).
- Kibana panel extensions on the "Self-Improvement" dashboard.

Out of scope:

- **Cost anomaly → governance response.** Publishing the event is the hook; ALERT mode triggering requires ADR-0055 (Mode Manager) accepted. Phase 2.
- **Insight embedding clustering.** Pattern similarity via embeddings is not needed — 6 `insight_type` buckets + fingerprint dedup + ADR-0040 suppression cover the use case.
- **Real-time pattern detection.** Consolidation-triggered is sufficient. Per-request pattern detection is what ADR-0053 (Gate Feedback Monitoring) owns.
- **Adaptive delegation matrix tuning.** Slice 3 concern. This ADR surfaces the signal; tuning the matrix based on the signal is a future ADR.
- **Non-delegation RPC patterns.** MCP tool-call latency patterns, memory-query latency patterns, etc. — these are error-pattern-like and belong to ADR-0056 (Error Pattern Monitoring) or a Phase 3 latency ADR.

---

## Alternatives Considered

### Wiring Strategy

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Call `create_captain_log_proposals` from a new separate consumer | A new `cg:insights-proposals` group subscribes to `stream:consolidation.completed` alongside `cg:insights` | Rejected — forces a second ES roundtrip for the same insights; the existing handler already has them in memory |
| B. Call `create_captain_log_proposals` inside `InsightsEngine.analyze_patterns()` | Push the CL write down into the engine itself | Rejected — violates layering: engine produces data, Observation Layer acts on it. Keeping the engine pure makes it testable without CL infrastructure |
| **C. Extend `build_consolidation_insights_handler`** | The existing handler receives insights; it is the right seam | **Selected** — one handler owns the full consolidation fan-out for insights; matches the pattern used by `build_consolidation_promotion_handler` |

### Event Granularity

| Option | Description | Verdict |
|--------|-------------|---------|
| A. One `InsightsAnalysisCompletedEvent` per consolidation carrying a `list[Insight]` | Bulky event; subscribers iterate | Rejected — subscribers that only care about `anomaly` insights would still receive every `trend`; filtering is done at publish time not subscribe time (event-bus best practice) |
| **B. One event per insight** | Small events; subscribers filter by `insight_type` / `pattern_kind` / `fingerprint` | **Selected** — natural composability; matches how `ErrorPatternDetectedEvent` (ADR-0056) handles its clusters |
| C. Event per category | One event for all correlation insights, one for all trends, etc. | Rejected — the cardinality is wrong; consumers want to filter on fingerprint and individual confidence |

### Cost Anomaly Event Shape

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Reuse `InsightsPatternDetectedEvent` with `insight_type="anomaly"` | Single event type | Rejected — cost anomalies have structured numeric fields (ratio, baseline, observation_date) that pattern events would carry in `evidence` dict; future governance consumers want typed fields |
| **B. Dedicated `InsightsCostAnomalyEvent`** | Separate event type; typed fields | **Selected** — subscribers can filter at stream level (`stream:insights.cost_anomaly`) and access typed fields without dict unpacking |

### Delegation Pattern Threshold

| Option | Description | Verdict |
|--------|-------------|---------|
| A. `count ≥ 3` | Very low threshold; catches patterns early | Rejected — with delegation volume of 1–5/day, 3 samples is one bad week; too many false positives |
| **B. `count ≥ 10`** | Conservative; ~2 weeks under typical load | **Selected** — absorbs noise; patterns surface only when the signal is durable |
| C. `count ≥ 30` | Very conservative | Rejected — would delay surfacing by ~2 months; unacceptable for a first-pass stream |

---

## Consequences

### Positive

- **Streams 4 and 9 close.** Both are listed as "detection only — dead end" in the feedback-stream catalogue; both become full loops.
- **InsightsEngine output becomes composable.** Any future consumer can subscribe to `stream:insights.pattern_detected` or `stream:insights.cost_anomaly` without touching insights code. FRE-250 (KG Quality) will filter on `insight_type in {graph_staleness, graph_staleness_trend}`.
- **Delegation analysis becomes real.** The stub becomes three live aggregations. Delegation success rate, rounds-needed, and missing-context themes surface as proposals with sample-count evidence.
- **Fingerprint dedup prevents proposal flooding.** Today `create_captain_log_proposals()` is unused; when turned on, without fingerprints it would create a new CL entry every consolidation. D6 prevents that.
- **ADR-0054 dual-write compliance.** Insights already durable in ES; adding the bus publish completes the convention. Streams 4 and 9 go from "❌ No bus" to "✅ full loop" in the catalogue.
- **Cost anomaly becomes actionable.** Today `CostAnomaly` is a returned-and-discarded object; Phase 1 wires it to Captain's Log; Phase 2 can add governance response.
- **No new infrastructure.** Two event types, two stream names. No new consumer groups. Existing `cg:insights` handler is extended in place.

### Negative

- **Two new event types and stream names.** `parse_stream_event()` gains two dispatch arms; ADR-0054's reserved-names list loses two entries.
- **`create_captain_log_proposals()` signature changes.** Today it builds `ProposedChange` without fingerprint/category/scope; the implementation issue must add those. Callers outside the insights pipeline (if any) will see unchanged behaviour because the fields are optional.
- **Insight volume becomes CL volume.** Each consolidation could produce 0–10 insights, and now each insight (if actionable and confidence ≥ threshold) can become a CL entry. Fingerprint dedup keeps the *unique* CL file count bounded; but the first consolidation after turn-on will create a batch of entries.
- **Delegation pattern detection is real work.** D4 specifies three aggregations — not trivial to implement but bounded (well-defined ES composite aggregation).

### Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| CL flood on first activation (every existing pattern becomes a proposal at once) | Medium | Feature flag `insights_wiring_enabled` (default `True` after an initial rollout observation window); ADR-0030 dedup absorbs repeats within a single consolidation scan |
| Delegation aggregation cost grows with event volume | Low | Events are in `agent-logs-*` with short retention; `days=30` window stays bounded under Slice 2 delegation volumes |
| Missing-context terms aggregation produces noisy cluster labels (e.g. long sentences as bucket keys) | Medium | Normalise `what_was_missing` to lowercase + first 80 chars before the terms aggregation; if still noisy, preprocess via a short per-field canonicalisation map |
| Fingerprint collisions across `insight_type` | Very low | 16-hex (64 bits) with `insight_type:pattern_kind:title_normalised` namespacing; keyspace occupancy is low for the expected insight volume |
| Cost anomaly event published when ES / Postgres are both down | Low | `analyze_patterns()` already guards ES errors; `_get_daily_costs()` guards Postgres errors; the handler's outer try/except logs and continues |

---

## Implementation Priority

Single phase (Phase 2 governance response is a separate ADR).

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | `InsightsPatternDetectedEvent`, `InsightsCostAnomalyEvent`, stream constants in `events/models.py`; `parse_stream_event()` dispatch arms | Types first | Tier-3: Haiku |
| 2 | Fingerprint helper in `insights/engine.py` — `_pattern_fingerprint(insight_type, pattern_kind, title)` + `_cost_fingerprint(anomaly_type, date)` | Dedup backbone | Tier-3: Haiku |
| 3 | Category/scope mapping (`_category_for_insight_type`, `_scope_for_insight_type`) in `insights/engine.py` | Classification | Tier-3: Haiku |
| 4 | Extend `create_captain_log_proposals()` to set fingerprint + category + scope on `ProposedChange` | Dedup wiring | Tier-2: Sonnet |
| 5 | `_severity_for_cost_ratio()` helper in `insights/engine.py` | Classification | Tier-3: Haiku |
| 6 | Extend `build_consolidation_insights_handler` in `events/pipeline_handlers.py`: publish pattern + cost events; call `create_captain_log_proposals`; save via `CaptainLogManager` | Main wiring | Tier-2: Sonnet |
| 7 | Real implementation of `InsightsEngine.detect_delegation_patterns()` — three aggregations per D4 | Signal production | Tier-2: Sonnet |
| 8 | Unit tests: fingerprint stability, category/scope mapping, event shapes, delegation aggregations, CL dedup integration | Quality gate | Tier-2: Sonnet |
| 9 | Kibana dashboard: insight-types breakdown + cost-anomaly timeline | Visualisation | Tier-3: Haiku |
| 10 | Config flag `insights_wiring_enabled` (default `True`) — allows quick disable if CL flood | Safe rollout | Tier-3: Haiku |

Steps 1–6 constitute the MVP: insights flow to CL + bus. Step 7 activates delegation pattern detection. Steps 8–10 finalise quality and rollout.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| `InsightsPatternDetectedEvent`, `InsightsCostAnomalyEvent`, stream constants | `src/personal_agent/events/models.py` | Infrastructure |
| Fingerprint, category/scope, severity helpers | `src/personal_agent/insights/engine.py` | Observation |
| Real `detect_delegation_patterns()` | `src/personal_agent/insights/engine.py` | Observation |
| Extended `build_consolidation_insights_handler` | `src/personal_agent/events/pipeline_handlers.py` | Observation |
| CL emission sequencing | inside `build_consolidation_insights_handler` | Observation |
| Config flag | `src/personal_agent/config/settings.py` | Infrastructure |

All work stays in the Observation Layer. The `InsightsEngine` remains a pure analysis component with no side-effect on execution.

---

## Open Questions

1. **Should delegation aggregations be per-agent?** Today `DelegationOutcome` carries `task_id` but not `agent_name` as a top-level structured field. The target agent is in `trace_id` metadata. Implementation: if `agent_name` is extractable from the ES record, add it to the aggregation key and emit per-agent delegation patterns; otherwise emit a single aggregate. Decision deferred to implementation.

2. **Severity thresholds for cost anomalies.** 2.5 and 4.0 are ratio boundaries based on anecdotal observation; 30 days of Phase 1 data should be used to calibrate. Thresholds live in `insights/engine.py` constants for easy tuning.

3. **`analysis_window_days` per insight_type.** Today all insights use the same `days` argument to `analyze_patterns()`. Delegation pattern detection (D4) uses 30 days; others typically use 7. Implementation decision: `analyze_patterns(days)` stays the outer knob; `detect_delegation_patterns(days=max(days, 30))` ensures the inner call has enough samples without forcing every caller to pass 30.

4. **Missing-context theme normalisation.** `what_was_missing` is free text from the delegation outcome. A terms aggregation over free text produces noisy buckets. Options: (a) lowercase + trim; (b) first 80 chars; (c) canonicalisation via small LLM pre-pass. Start with (a) + (b); revisit if noise dominates after 30 days.

5. **Kibana panel owner.** The existing "Self-Improvement" dashboard is maintained manually. If the panels are added via API, should they be committed as JSON fixtures? Implementation decision pending dashboard-as-code convention (not this ADR).

---

## Dedicated Linear Project — Insights & Pattern Analysis

Existing project (see `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`). No new project created by this ADR.

### Project configuration

| Field | Value |
|-------|-------|
| Project name | Insights & Pattern Analysis |
| Team | FrenchForest |
| Default issue state | Needs Approval |
| Labels on creation | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping | `confidence ≥ 0.80` → High; `confidence ≥ 0.65` → Normal; else Low |

### Issue format — Pattern insight

```
Title: [Insight: <insight_type>] <title>
  e.g. "[Insight: delegation] Low success rate for ClaudeCode delegations (42%)"

Body:
  ## Insight summary
  Type:            delegation
  Pattern kind:    delegation_success_rate
  Confidence:      0.78
  Analysis window: 30 days
  Fingerprint:     8a3f1c2d4e5b6a7f
  Seen count:      3

  ## Summary
  Low success rate for ClaudeCode delegations: 13/31 successful (42%) over 30 d.

  ## Evidence
    total_delegations: 31
    successful:        13
    failed:            18
    success_rate:      0.42
    median_rounds:     2
    p75_rounds:        4

  ## Proposed action
  Address insight pattern: Low success rate for ClaudeCode delegations.
  Running targeted mitigation experiment for 7 days, monitoring impact metrics,
  and applying change if confidence improves.
```

### Issue format — Cost anomaly

```
Title: [Cost anomaly] daily_cost_spike — $4.12 (3.2× baseline) on 2026-04-19
Body:
  ## Cost anomaly summary
  Anomaly type:   daily_cost_spike
  Severity:       medium
  Confidence:     0.75
  Observation:    2026-04-19
  Observed:       $4.12
  Baseline:       $1.28
  Ratio:          3.22×
  Fingerprint:    c1d2e3f4a5b6c7d8

  ## Proposed action
  Review model usage for high-cost traces on 2026-04-19; consider enforcing
  temporary budget-aware routing on expensive workflows. See agent-insights-*
  in Kibana for the trace breakdown.
```

### Feedback labels (inherited from ADR-0040)

| Label | Meaning for insights |
|-------|----------------------|
| Approved | Proceed with the proposed action; human implements |
| Rejected | Insight is noise / expected; suppress fingerprint for 30 days |
| Deepen | Re-run analyse_patterns with deeper evidence; post refined proposal as comment |
| Too Vague | Refined proposal with more specific evidence fields |
| Defer | Re-evaluate on next consolidation; no suppression |

---

## End State — What Exists, What Is Automated, What Is Visible

### After Phase 1 MVP (Implementation Priority steps 1–6)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| `InsightsPatternDetectedEvent`, `InsightsCostAnomalyEvent`, stream constants, parse dispatch arms | `cg:insights` handler publishes a pattern event per insight on every consolidation | Events visible on `stream:insights.pattern_detected` and `stream:insights.cost_anomaly` via `redis-cli XRANGE` |
| Fingerprint + category/scope + severity helpers | `create_captain_log_proposals()` runs inside the handler; fingerprint dedup prevents duplicate CL files | CL entries in `telemetry/captains_log/` with `insight_type`-derived category/scope |
| Extended `build_consolidation_insights_handler` | ADR-0030 dedup absorbs repeat proposals | ADR-0040 suppression blocks proposals whose fingerprint was previously rejected |

Human action required: none (other than operational monitoring). No Linear issues yet unless promotion thresholds are met.

### After Phase 1 complete (Implementation Priority steps 7–10)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| Real `detect_delegation_patterns()` with three aggregations | Delegation patterns (success rate, rounds, missing context) flow through same CL pipeline | Kibana "Self-Improvement" dashboard gains insight-types + cost-anomaly panels |
| Kibana panel extensions | Promotion → Linear "Insights & Pattern Analysis" project after `seen_count ≥ 3`, `age ≥ 7 d` | Linear issues with insight summary + evidence table |
| Config flag `insights_wiring_enabled` | Label → suppression / approval behaviour inherited from ADR-0040 | Cost anomaly issues tagged by severity |

Human action required: review and label Linear issues in "Insights & Pattern Analysis". Everything else is automatic.

### After Phase 2 (deferred ADR — governance response to cost anomalies)

Out of scope for this ADR. Documented here so the end-state table is complete for the reader.

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| Governance consumer subscribes to `stream:insights.cost_anomaly` | `severity=high` → Mode Manager ALERT transition (requires ADR-0055 accepted) | Mode transitions in Kibana correlated with cost spikes |

---

## Loop Completeness Criteria

The stream is verified closed and working when, over a trailing 14-day window, all five hold:

1. **Production**: `count(XLEN stream:insights.pattern_detected) ≥ 1` per week under normal load; `XLEN stream:insights.cost_anomaly` grows when a spike occurs.
2. **Durability**: every bus event has a corresponding document in `agent-insights-*` (sampled check: 5 random event fingerprints across the window).
3. **CL emission**: `telemetry/captains_log/` contains at least one `CONFIG_PROPOSAL` entry whose fingerprint matches a bus event published within the window.
4. **Promotion**: at least one insight-driven CL entry has been promoted to a Linear issue in the "Insights & Pattern Analysis" project.
5. **Suppression**: after a `Rejected` label on an insight issue, the next matching fingerprint is dropped at `CaptainLogManager.save_entry()` (log line: `captains_log_proposal_suppressed`).

Additionally, for delegation patterns:

6. `detect_delegation_patterns(days=30)` returns non-empty when the ES delegation event count ≥ 10 for the analysed window.

If (1)–(5) hold but (6) does not, delegation volume in the deployment is too low for the threshold; tune `min_occurrences` in config, not in this ADR.

---

## Feedback Stream ADR Template — Compliance Checklist

Per the Feedback Stream ADR Template established in ADR-0053:

- [x] **1. Stream identity** — Observation Layer insights; depends on ADR-0041/0043/0053/0054
- [x] **2. Source** — `ConsolidationCompletedEvent`-triggered; rolling 7 d (default) / 30 d (delegation)
- [x] **3. Collection mechanism** — extended `build_consolidation_insights_handler`; fallback on Redis/ES/Postgres documented
- [x] **4. Processing algorithm** — six insight-type classifiers + cost anomaly + three delegation aggregations
- [x] **5. Signal produced** — `InsightsPatternDetectedEvent`, `InsightsCostAnomalyEvent`, `CaptainLogEntry(CONFIG_PROPOSAL)`; fingerprint dedup policy D6
- [x] **6. Full automation cycle** — D8 traces the 7-step loop end to end (including delegation and cost anomaly special cases)
- [x] **7. Human review interface** — existing "Insights & Pattern Analysis" Linear project; two issue formats (pattern + cost anomaly); label semantics; SLA inherited
- [x] **8. End state table** — Phase 1 MVP, Phase 1 complete; Phase 2 noted as deferred
- [x] **9. Loop completeness criteria** — 5-point check + delegation-specific criterion (6)

---

## References

- FRE-247: Draft ADR-0057 — Insights & Pattern Analysis Stream (this ADR)
- ADR-0041: Event Bus via Redis Streams — transport
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0053: Gate Feedback-Loop Monitoring Framework — establishes the Feedback Stream ADR Template this ADR follows
- ADR-0054: Feedback Stream Bus Convention — dual-write, stream naming, `EventBase` contract fields
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — surfacing channel, fingerprint dedup
- ADR-0040: Linear as Async Feedback Channel — label semantics, suppression
- ADR-0042: Knowledge Graph Freshness via Access Tracking — related to Stream 6; `graph_staleness` insight subtype covered by this ADR
- ADR-0055 (Proposed): System Health & Homeostasis Stream — required for Phase 2 cost-anomaly governance response
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — feedback-stream catalogue (updated to reference this ADR)
- `src/personal_agent/insights/engine.py` — `InsightsEngine`, the main subject of this ADR
- `src/personal_agent/events/pipeline_handlers.py` — `build_consolidation_insights_handler`, extended by this ADR
- `src/personal_agent/events/models.py` — event and stream definitions
- `src/personal_agent/request_gateway/delegation.py::record_delegation_outcome` — source of `delegation_outcome_recorded` events consumed by D4 aggregations
- `src/personal_agent/captains_log/models.py` — `CaptainLogEntry`, `ProposedChange`, `ChangeCategory`, `ChangeScope` — fingerprint + category wiring target
- `src/personal_agent/captains_log/manager.py` — `CaptainLogManager.save_entry()` — ADR-0030 dedup, ADR-0040 suppression entry point
