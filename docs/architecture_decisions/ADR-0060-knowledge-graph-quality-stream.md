# ADR-0060: Knowledge Graph Quality Stream

**Status**: Proposed — In Review
**Date**: 2026-04-29
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0042 (Knowledge Graph Freshness via Access Tracking), ADR-0043 (Three-Layer Architectural Separation), ADR-0053 (Gate Feedback-Loop Monitoring Framework — template), ADR-0054 (Feedback Stream Bus Convention), ADR-0057 (Insights & Pattern Analysis — establishes anomaly→proposal pattern)
**Related**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel), ADR-0059 (Context Quality Stream — sibling)
**Linear Issue**: FRE-250

---

## Context

### Two streams share a Linear project but neither is closed end-to-end

`docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` groups **Stream 6** (Memory Access Freshness) and **Stream 8** (Consolidation Quality Monitor) under the same project — "Knowledge Graph Quality" — because both answer the same question: *is the knowledge graph serving the agent's recall well?* Neither stream completes its loop today.

### Stream 8 — Consolidation Quality Monitor (dead end)

`BrainstemScheduler._run_quality_monitoring()` (`src/personal_agent/brainstem/scheduler.py:609-622`) fires daily at 05:00 UTC. It calls `ConsolidationQualityMonitor.detect_anomalies()` (`src/personal_agent/second_brain/quality_monitor.py:203-272`) which runs five checks against Neo4j and Elasticsearch:

| Anomaly type | Trigger | Severity |
|---|---|---|
| `entity_conversation_ratio_out_of_range` | entity/conversation ratio outside (0.5, 2.0) | `"medium"` or `"high"` (> 150 % out of range) |
| `relationship_density_out_of_range` | relationship density outside (1.0, 3.0) | `"medium"` or `"high"` |
| `duplicate_rate_high` | duplicate entity rate > 5 % | `"medium"` |
| `extraction_failure_rate_high` | extraction failure rate > 1 % | `"high"` |
| `no_relationships_created` | entity nodes exist, relationship count = 0 | `"high"` |
| `entity_extraction_spike` | daily extraction volume > 3σ above rolling baseline | `"medium"` |

After `detect_anomalies()` returns, the scheduler captures only `len(anomalies)` in a log line (`quality_monitor_run_completed`) and drops the list. No bus event, no Captain's Log write, no Linear path. The detection is live; the response is not.

### Stream 6 — Memory Access Freshness (partial — tier signal not applied)

ADR-0042 marks all eight implementation steps complete. Two gaps remain outside its checklist:

**Gap A — `StalenessTier` unused in recall reranking.** `_calculate_relevance_scores()` in `src/personal_agent/memory/service.py:1581-1592` uses the continuous `compute_freshness()` float (ADR-0042 Step 5). It does not apply the discrete `StalenessTier` classification (`WARM`/`COOLING`/`COLD`/`DORMANT` from `src/personal_agent/memory/freshness.py:37`). A DORMANT entity and a WARM entity with the same float score are treated identically. The tier penalty for stale knowledge is never applied.

**Gap B — `run_freshness_review()` has no bus event.** The weekly freshness review (`src/personal_agent/brainstem/jobs/freshness_review.py:240-323`) aggregates tier counts, writes a JSON snapshot, and calls `CaptainLogManager.save_entry()` directly for dormant proposals. It emits a rich `freshness_review_completed` log event. It does not publish to the event bus — ADR-0054 requires all feedback signals to be bus-composable.

*Catalog note:* Stream 6's current catalog entry ("Decay scores computed but not used in recall reranking") is half-correct. Decay scores *are* used; tier penalties are not. ADR-0060 corrects this wording.

### Composability with ADR-0057

ADR-0057 established the pattern that `detect_cost_anomalies()` → `InsightsCostAnomalyEvent` → `cg:insights` handler → `CaptainLogManager.save_entry()`. ADR-0060 replicates this for graph quality anomalies. The fingerprint helpers in `src/personal_agent/insights/fingerprints.py` are reused directly.

### Feedback Stream Bus Convention applies

ADR-0054 dual-write ordering: durable record first, bus publish second. ADR-0060 follows this in both streams. ADR-0054 §D1 has reserved `stream:graph.quality_anomaly` for this ADR; `stream:memory.staleness_reviewed` is introduced here for Stream 6.

---

## Decision Drivers

1. **Compose, do not duplicate.** ADR-0057's cost-anomaly path already exists; ADR-0060 wraps graph-quality anomalies into the same lifecycle. Same fingerprint helpers, same `CaptainLogManager`, same Linear feedback labels.
2. **Close the response path without changing the detection logic.** Stream 8 detection works. The three-line change is in the scheduler — forward the list instead of dropping it.
3. **Apply the tier signal where it already matters.** The reranking hot path already fetches freshness floats per entity. Deriving a tier from the float avoids a second Neo4j round-trip and composes cleanly with the existing weighted scoring.
4. **Make the freshness review bus-composable.** The `freshness_review_completed` log line should become a bus event so future consumers (analytics, governance) can subscribe without a code change.
5. **Phase-2 governance is feature-flagged.** Downstream effects on brainstem mode decisions are observable-only by default; flip after 14 days of validated Phase 1 data.
6. **No new infrastructure.** Redis Streams, Captain's Log, promotion pipeline, and Linear are all reused. Two new event types, two new streams, one new handler module, one helper function.

---

## Decision

### D1: Source — two complementary signals

**Stream 8 source** — per-anomaly, daily:

`BrainstemScheduler._run_quality_monitoring()` is the integration point. After `detect_anomalies()` returns, the scheduler iterates the list and emits one `GraphQualityAnomalyEvent` per anomaly before logging `quality_monitor_run_completed`. Granularity: one event per detected anomaly. Frequency: daily (05:00 UTC).

**Stream 6 source** — per-review-run, weekly:

`run_freshness_review()` (`brainstem/jobs/freshness_review.py:240-323`) is the integration point. After writing the snapshot and the existing direct CL proposals for dormant counts, it publishes one `MemoryStalenessReviewedEvent` carrying the full `GraphStalenessSummary` tier counts for that week. Granularity: one event per weekly run. Frequency: weekly (Sunday 03:00 UTC per default cron `0 3 * * 0`).

The existing direct `CaptainLogManager.save_entry()` calls in `freshness_review.py` are **retained unchanged**. The bus event is additive — it enables future consumers and the trend-delta handler without replacing the proven path.

### D2: Severity contract

The `Anomaly.severity` field retains its free-string type ("high" / "medium") for backward compatibility. ADR-0060 formalises the meaning:

| Severity | Captain's Log | Phase 2 governance |
|---|---|---|
| `"high"` | ✅ `RELIABILITY` category | ✅ eligible (flag-gated) |
| `"medium"` | ✅ `KNOWLEDGE_QUALITY` category | ❌ CL only |
| any other / unknown | ✅ `KNOWLEDGE_QUALITY` category | ❌ CL only |

Current `"high"` anomaly types: `extraction_failure_rate_high`, `no_relationships_created`, and range anomalies where observed value is outside 150 % of the target bound (`_range_anomaly` at `quality_monitor.py:374`).

### D3: Data model — three layers

**Layer A — in-memory structs (per event):**

```python
@dataclass(frozen=True)
class GraphQualityAnomaly:
    """One graph-quality anomaly for bus/durable write."""
    fingerprint: str           # sha256(anomaly_type:observation_date:message)[:16]
    trace_id: str
    anomaly_type: str
    severity: str              # "high" | "medium"
    message: str
    observed_value: float
    expected_range: tuple[float, float] | None
    metadata: dict[str, Any] | None
    observation_date: str      # ISO date, e.g. "2026-04-29"

@dataclass(frozen=True)
class GraphStalenessReviewSummary:
    """Weekly review summary for bus/durable write."""
    fingerprint: str           # sha256(staleness_review:<iso_week>:<dominant_tier>)[:16]
    trace_id: str
    iso_week: str              # e.g. "2026-W18"
    entities_warm: int
    entities_cooling: int
    entities_cold: int
    entities_dormant: int
    relationships_dormant: int
    never_accessed_old_entity_count: int
    dominant_tier: str         # "dormant" | "cold" | "cooling" | "warm"
```

**Layer B — bus events:**

```python
class GraphQualityAnomalyEvent(EventBase):
    """One graph-quality anomaly published per detection.

    Consumers:
      • cg:graph-monitor → CaptainLogEntry(severity-gated)
      • Phase 2: high-severity → ModeAdvisoryEvent (flag-gated)
    """
    event_type: Literal["graph.quality_anomaly"] = "graph.quality_anomaly"
    fingerprint: str
    anomaly_type: str
    severity: str
    message: str
    observed_value: float
    expected_range: tuple[float, float] | None = None
    metadata: dict[str, Any] | None = None
    observation_date: str
    # trace_id / session_id: required (scheduled event → session_id nullable)
    # source_component: "brainstem.scheduler"

class MemoryStalenessReviewedEvent(EventBase):
    """Weekly staleness-review summary published after each review run.

    Consumers:
      • cg:graph-monitor → trend-delta CaptainLogEntry (worsening dormant count)
    """
    event_type: Literal["memory.staleness_reviewed"] = "memory.staleness_reviewed"
    fingerprint: str
    iso_week: str
    entities_warm: int
    entities_cooling: int
    entities_cold: int
    entities_dormant: int
    relationships_dormant: int
    never_accessed_old_entity_count: int
    dominant_tier: str
    # trace_id nullable (scheduled); source_component: "brainstem.jobs.freshness_review"
```

Stream name constants in `src/personal_agent/events/models.py`:
```python
STREAM_GRAPH_QUALITY_ANOMALY = "stream:graph.quality_anomaly"
STREAM_MEMORY_STALENESS_REVIEWED = "stream:memory.staleness_reviewed"
CG_GRAPH_MONITOR = "cg:graph-monitor"
```

**Layer C — durable JSONL:**

- `telemetry/graph_quality/GQ-<YYYY-MM-DD>.jsonl` — one line per `GraphQualityAnomaly`. Written by `_run_quality_monitoring()` before each bus publish.
- `telemetry/freshness_review/FR-<YYYY-WISO>.jsonl` — one line per `GraphStalenessReviewSummary`. Written by `run_freshness_review()` before the bus publish.

Both follow ADR-0054 D4 ordering: durable append first; failure aborts publish with a WARNING log; bus failure is logged and swallowed.

### D4: Fingerprinting

**Stream 8 — graph quality anomaly:**
```python
from personal_agent.insights.fingerprints import pattern_fingerprint

fingerprint = pattern_fingerprint(
    "graph_quality",
    anomaly.anomaly_type,
    anomaly.message,
)
```
Reuses `insights/fingerprints.py` directly. `pattern_fingerprint("graph_quality", t, m)` = `sha256(f"graph_quality:{t}:{normalise_insight_title(m)}".encode()).hexdigest()[:16]`. One fingerprint per anomaly type × normalised message — daily recurrences of the same anomaly dedup into a single Captain's Log entry with incrementing `seen_count`.

**Stream 6 — staleness review:**
```python
from personal_agent.insights.fingerprints import cost_fingerprint

dominant_tier = _dominant_tier(summary)  # "dormant" | "cold" | "cooling" | "warm"
fingerprint = cost_fingerprint(
    f"staleness_review_{dominant_tier}",
    iso_week,
)
```
`cost_fingerprint(t, d)` = `sha256(f"{t}:{d}".encode()).hexdigest()[:16]`. Keyed per ISO week so each week's review produces a distinct entry — dedup across weeks is intentional (the trend is what matters, not just "dormant entities exist").

`_dominant_tier` logic:
```
if entities_dormant > 0: return "dormant"
elif entities_cold > 0: return "cold"
elif entities_cooling > 0: return "cooling"
else: return "warm"
```

### D5: StalenessTier multiplier in recall reranking

The tier penalty is derived from the already-fetched freshness float to avoid a second Neo4j query. A new helper is added to `src/personal_agent/memory/freshness.py`:

```python
def staleness_tier_from_freshness_score(score: float) -> StalenessTier:
    """Derive a staleness tier from a pre-computed freshness score.

    Uses approximate thresholds that align with classify_staleness() at
    default settings (half_life_days=30, cold_threshold_days=90):
      WARM     ≥ 0.50  (accessed within ~30 days)
      COOLING  ≥ 0.25  (accessed within ~30–60 days)
      COLD     ≥ 0.10  (accessed within ~60–90 days)
      DORMANT  <  0.10  (last access >90 days ago or never accessed)
    """
    if score >= 0.50:
        return StalenessTier.WARM
    if score >= 0.25:
        return StalenessTier.COOLING
    if score >= 0.10:
        return StalenessTier.COLD
    return StalenessTier.DORMANT
```

The reranking block in `memory/service.py:1585-1592` is updated to apply a tier factor:

```python
# 6. Freshness score (ADR-0042) + tier multiplier (ADR-0060 D5)
if use_freshness and conv.key_entities:
    conv_freshness_scores = [
        freshness_scores[e]
        for e in conv.key_entities
        if e in freshness_scores and freshness_scores[e] > 0.0
    ]
    if conv_freshness_scores:
        best_freshness = max(conv_freshness_scores)
        if settings.freshness_tier_reranking_enabled:
            tier = staleness_tier_from_freshness_score(best_freshness)
            tier_factor = settings.freshness_tier_factors.get(tier.value, 1.0)
            best_freshness *= tier_factor
        score += best_freshness * w_freshness_cfg
```

Default tier factors (configurable via `freshness_tier_factors` dict in `AppConfig`):

| Tier | Default factor | Effect |
|------|---------------|--------|
| `warm` | `1.0` | No change |
| `cooling` | `0.85` | 15 % penalty |
| `cold` | `0.60` | 40 % penalty |
| `dormant` | `0.30` | 70 % penalty |

`freshness_tier_reranking_enabled` defaults to `True` — unlike Phase 2 governance, this is a purely additive refinement of an existing weighted signal with no user-observable side effects other than better recall quality.

New config fields in `AppConfig`:
```python
freshness_tier_reranking_enabled: bool = True
freshness_tier_factors: dict[str, float] = Field(
    default_factory=lambda: {
        "warm": 1.0,
        "cooling": 0.85,
        "cold": 0.60,
        "dormant": 0.30,
    }
)
graph_quality_stream_enabled: bool = True
graph_quality_governance_enabled: bool = False  # Phase 2 flip gate
```

### D6: Captain's Log signal

**Stream 8 handler** (`build_graph_quality_captain_log_handler()` in `events/pipeline_handlers.py`):

Subscribes `cg:graph-monitor` to `stream:graph.quality_anomaly`. For each event:

```python
category = ChangeCategory.RELIABILITY if event.severity == "high" else ChangeCategory.KNOWLEDGE_QUALITY
CaptainLogEntry(
    type=CaptainLogEntryType.CONFIG_PROPOSAL,
    title=f'[Graph quality] {event.anomaly_type}: {event.message}',
    rationale=(
        f'Consolidation quality monitor detected anomaly type "{event.anomaly_type}" '
        f'(severity: {event.severity}). Observed value {event.observed_value:.4f}; '
        f'expected range {event.expected_range}.'
    ),
    proposed_change=ProposedChange(
        what=f"Investigate {event.anomaly_type} anomaly in knowledge graph",
        why=(
            f'Daily anomaly scan ({event.observation_date}) found "{event.message}". '
            f'Observed: {event.observed_value:.4f}. Range: {event.expected_range}.'
        ),
        how=(
            "1) Check the telemetry/graph_quality/GQ-*.jsonl entry for this fingerprint.\n"
            "2) Run the quality monitor interactively via brainstem diagnostics to inspect "
            "raw metrics.\n"
            "3) For extraction failures, check the ES agent-logs-* index for "
            "entity_extraction_failed events around the observation date.\n"
            "4) For structural anomalies (no_relationships_created), inspect Neo4j directly."
        ),
        category=category,
        scope=ChangeScope.SECOND_BRAIN,
        fingerprint=event.fingerprint,
    ),
    supporting_metrics=[
        f"anomaly_type: {event.anomaly_type}",
        f"severity: {event.severity}",
        f"observed_value: {event.observed_value:.4f}",
        f"observation_date: {event.observation_date}",
    ],
    metrics_structured=[
        Metric(name="observed_value", value=event.observed_value, unit=None),
    ],
    telemetry_refs=[TelemetryRef(trace_id=event.trace_id, metric_name=None, value=None)],
)
```

**Stream 6 handler** (same `build_graph_quality_captain_log_handler`, subscribed to `stream:memory.staleness_reviewed`):

Creates a Captain's Log entry only when the review reveals a *worsening dormant trend*: `entities_dormant ≥ settings.freshness_dormant_entity_proposal_threshold` (same gate already used in direct-write path). The bus path produces a *trend summary* entry distinct from the dormant-entity proposals the direct write creates.

```python
# Only fire if threshold exceeded — mirrors existing direct-write gate
if event.entities_dormant >= settings.freshness_dormant_entity_proposal_threshold:
    CaptainLogEntry(
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title=f'KG freshness review {event.iso_week}: {event.entities_dormant} dormant entities',
        ...
        category=ChangeCategory.KNOWLEDGE_QUALITY,
        scope=ChangeScope.SECOND_BRAIN,
        fingerprint=event.fingerprint,
    )
```

The existing direct-write dormant proposals from `freshness_review.py` are separate fingerprints (keyed by `_dormant_entity_what_text()` via `compute_proposal_fingerprint()`) and thus do not collide with the bus-path trend summary.

### D7: Phase 2 — governance response for ALERT-severity anomalies (flag-gated `False`)

When `graph_quality_governance_enabled=True`, the `cg:graph-monitor` handler additionally publishes a `ModeAdvisoryEvent` (defined in ADR-0055) to `stream:mode.transition` for each `"high"` severity anomaly. The advisory advises DEGRADED state to the brainstem mode controller for the `"consolidation"` surface tag, with a `reason="graph_quality_anomaly:{anomaly_type}"` payload.

The mode controller (ADR-0055 `cg:mode-controller`) already handles advisory events; no changes to the controller are needed for Phase 2.

**Flip gate:** enable `graph_quality_governance_enabled=True` after 14 days of Phase 1 telemetry confirms that:
- Captain's Log entries are being created (check `telemetry/graph_quality/GQ-*.jsonl` growth).
- False-positive rate (Rejected labels in Linear) < 20 % over a full two-week window.

### D8: Full automation cycle

**Stream 8 — consolidation quality:**

```
1. BrainstemScheduler._run_quality_monitoring() — daily 05:00 UTC
   └─ detect_anomalies() → list[Anomaly] (5 check types)

2. For each anomaly:
   a) Build GraphQualityAnomaly (incl. fingerprint via pattern_fingerprint)
   b) Append JSON line to telemetry/graph_quality/GQ-<YYYY-MM-DD>.jsonl  (DURABLE)
   c) Publish GraphQualityAnomalyEvent to stream:graph.quality_anomaly    (BUS)

3. cg:graph-monitor handler receives event
   └─ build CaptainLogEntry (severity-gated category: RELIABILITY / KNOWLEDGE_QUALITY)
   └─ CaptainLogManager.save_entry()
      ├─ fingerprint suppressed (Rejected) → discard silently
      ├─ matching fingerprint on disk → increment seen_count, merge (ADR-0030)
      └─ else → write CL-…-*.json, index to ES

4. Next consolidation.completed → cg:promotion → PromotionPipeline
   └─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 d
   └─ creates Linear issue in "Knowledge Graph Quality" project

5. Human reviews issue, applies label → ADR-0040 handlers process it.

6. (Phase 2, flag-gated) high-severity anomaly:
   └─ cg:graph-monitor also publishes ModeAdvisoryEvent(DEGRADED, "consolidation")
   └─ cg:mode-controller evaluates transition
```

**Stream 6 — memory freshness:**

```
1. BrainstemScheduler._run_freshness_review() — weekly Sunday 03:00 UTC
   └─ run_freshness_review() → GraphStalenessSummary

2. EXISTING PATH (unchanged):
   └─ _build_entity_dormant_proposal() → CaptainLogManager.save_entry()
   └─ _build_relationship_dormant_proposal() → CaptainLogManager.save_entry()
   └─ _write_snapshot() → freshness_tier_snapshot.json

3. NEW PATH (ADR-0060):
   a) Build GraphStalenessReviewSummary (incl. fingerprint via cost_fingerprint)
   b) Append JSON line to telemetry/freshness_review/FR-<YYYY-WISO>.jsonl  (DURABLE)
   c) Publish MemoryStalenessReviewedEvent to stream:memory.staleness_reviewed (BUS)

4. cg:graph-monitor handler receives event
   └─ if entities_dormant ≥ threshold:
      └─ build trend-summary CaptainLogEntry(KNOWLEDGE_QUALITY, SECOND_BRAIN)
      └─ CaptainLogManager.save_entry() → fingerprint dedup applies

5. Promotion → Linear → ADR-0040 labels (same path as Stream 8)
```

**Loop closed.** Both streams: detection → JSONL → bus → Captain's Log → promotion → Linear → feedback suppression.

### D9: Scope boundary

In scope:
- `GraphQualityAnomalyEvent` + `MemoryStalenessReviewedEvent` + stream/CG constants in `events/models.py`.
- Durable JSONL writes from `_run_quality_monitoring()` and `run_freshness_review()`.
- `build_graph_quality_captain_log_handler()` in `events/pipeline_handlers.py` (handles both stream subscriptions).
- Wire `cg:graph-monitor` subscription in `service/app.py` behind `graph_quality_stream_enabled` flag.
- `staleness_tier_from_freshness_score()` helper in `memory/freshness.py`.
- Tier multiplier in `memory/service.py:_calculate_relevance_scores()`.
- Config flags: `graph_quality_stream_enabled`, `freshness_tier_reranking_enabled`, `freshness_tier_factors`, `graph_quality_governance_enabled`.
- Unit tests for all new paths.

Out of scope:
- Real-time alerting (Slack/email/PagerDuty) for "high" severity anomalies — ops tooling, separate concern.
- LLM-based anomaly classifier — the six hard-coded checks are sufficiently precise.
- Backfill of historical anomalies to JSONL — orthogonal; the rolling window provides context.
- Direct Neo4j tier lookup in reranking — freshness-float-derived tier is accurate enough for v1.
- Per-relationship-type tier penalties in reranking — entity-level tier signal first, relationship level in follow-on.
- ADR-0060 amendment to `Anomaly.severity` typing — free-string retained; a future ADR may formalize as `Literal`.

---

## Alternatives Considered

### Source / collection mechanism (Stream 8)

| Option | Verdict |
|--------|---------|
| A. ES scan triggered by `consolidation.completed`, mirroring ADR-0056 | Rejected — duplicates ADR-0056 infrastructure; the anomaly detection already produces structured objects that are richer than a raw log scan |
| B. Emit one aggregate event per daily run (all anomalies in a list payload) | Rejected — one event per anomaly aligns with ADR-0057 cost-anomaly pattern; per-anomaly fingerprinting is cleaner |
| **C. Per-anomaly events from the existing scheduler integration point** | **Selected** — zero new detection code; the scheduler is already the owner; one-line loop addition |

### StalenessTier integration mechanism (Stream 6 D5)

| Option | Verdict |
|--------|---------|
| A. Second Neo4j query per reranking call to fetch tier classifications directly | Rejected — doubles the Neo4j round-trips in the reranking hot path; the freshness float already encodes the decay |
| **B. Derive tier from freshness float via threshold helper** | **Selected** — zero extra I/O; approximately correct for default settings; helper is unit-testable in isolation |
| C. Store `staleness_tier` as a property on the Neo4j entity node, updated by `FreshnessConsumer` | Rejected — adds write amplification to the consumer and a schema migration; not justified when option B is available |
| D. Skip tier multiplier entirely (freshness float already encodes decay) | Rejected — the float alone cannot distinguish DORMANT (should be heavily suppressed) from COOLING (minor penalty); the tier multiplier is explicitly what ADR-0042 Decision 5 was for |

### Consumer group — new vs reuse (Stream 6)

| Option | Verdict |
|--------|---------|
| A. New `cg:graph-monitor` consumer handles both streams | **Selected** — `cg:graph-monitor` is the ADR-0054-reserved group for ADR-0060; single handler builder handles both subscriptions (mirroring ADR-0059's single `cg:captain-log` subscription model) |
| B. Route `stream:memory.staleness_reviewed` to existing `cg:captain-log` group | Rejected — `cg:captain-log` is `stream:captain_log.entry_created` + `stream:context.compaction_quality_poor`; adding a third subscription silently expands its scope beyond what was designed |
| C. Keep existing direct-write path for Stream 6 entirely, skip bus event | Rejected — violates ADR-0054 principle that all feedback signals should be bus-composable; future consumers would need a code change to react to freshness reviews |

### Phase 2 governance response shape

| Option | Verdict |
|--------|---------|
| A. Pause consolidation scheduling while anomaly exists | Rejected — may worsen the problem; consolidation is what drives anomaly resolution |
| **B. Mode advisory to brainstem suggesting DEGRADED for consolidation surface** | **Selected** — uses existing ADR-0055 infrastructure; advisory is non-binding (mode controller evaluates context before transitioning); reversible |
| C. Write a suppression file preventing new consolidation runs | Rejected — hard-coded stop with no feedback loop; far more invasive than needed |

---

## Consequences

### Positive

- **Both streams closed end-to-end** for the first time. `detect_anomalies()` finally produces human-visible proposals; the freshness review finally has a bus signal.
- **Tier multiplier improves recall quality.** DORMANT entities — those not accessed in 90+ days — will contribute only 30 % of their raw freshness weight. Actively-used entities are promoted. This is the completion of ADR-0042 Decision 5's stated intent.
- **Composability proved again.** `pattern_fingerprint` and `cost_fingerprint` from `insights/fingerprints.py` serve a third stream. `build_graph_quality_captain_log_handler` follows the same shape as `build_compaction_quality_captain_log_handler` — the pattern is stable.
- **Existing dormant proposals untouched.** `freshness_review.py`'s direct CL writes keep working; the bus path is additive.

### Negative

- **One new consumer group** (`cg:graph-monitor`) in `service/app.py` lifespan.
- **Two new event types** + parse arms in `events/models.py`.
- **Tier multiplier changes reranking output** (non-flag-gated because it's a refinement of an existing signal, not a new behaviour). In rare cases a heavily-dormant entity that was previously recalled may be suppressed. This is the intended effect; monitoring via `freshness_review_completed` logs is adequate.
- **JSONL directories added** (`telemetry/graph_quality/`, `telemetry/freshness_review/`) — operator is responsible for rotation policy (no retention policy set here, same as ADR-0059).

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Tier multiplier thresholds in `staleness_tier_from_freshness_score` diverge from `classify_staleness` at non-default settings | Low | Helper documents its assumptions on `half_life_days=30`; unit test verifies alignment at default config; future calibration via `freshness_tier_factors` dict |
| Daily anomaly detection produces the same anomalies repeatedly, flooding Captain's Log | Low | ADR-0030 fingerprint dedup: same fingerprint → increment `seen_count` only; no new file per day |
| `MemoryStalenessReviewedEvent` and existing direct-write dormant proposals create parallel proposals for the same root cause | Low | Different fingerprints by construction (`compute_proposal_fingerprint` uses text hash; `cost_fingerprint` uses type+week key); ADR-0030 dedup operates per-fingerprint |
| Phase 2 `ModeAdvisoryEvent` causes spurious DEGRADED transitions during noisy anomaly periods | Low | Flag default off; mode controller already has transition-frequency guard (ADR-0055 §anomalous cadence) |
| `staleness_tier_from_freshness_score` returns DORMANT for new entities with zero accesses (freshness=0.0) | Medium | `_calculate_relevance_scores` already skips entities where `freshness_scores[e] == 0.0` (line 1589) — the tier block is only reached for entities with `freshness_scores[e] > 0.0`, so zero-access entities are unaffected |

---

## Implementation Priority

### Phase 1 — Close both stream loops

| Order | Work | Tier |
|-------|------|------|
| 1 | `STREAM_GRAPH_QUALITY_ANOMALY`, `STREAM_MEMORY_STALENESS_REVIEWED`, `CG_GRAPH_MONITOR` constants + `GraphQualityAnomalyEvent` + `MemoryStalenessReviewedEvent` + parse arms in `events/models.py` | Tier-3: Haiku |
| 2 | `staleness_tier_from_freshness_score()` helper in `memory/freshness.py` | Tier-3: Haiku |
| 3 | Config flags: `graph_quality_stream_enabled`, `freshness_tier_reranking_enabled`, `freshness_tier_factors`, `graph_quality_governance_enabled` | Tier-3: Haiku |
| 4 | `GraphQualityAnomaly` + `GraphStalenessReviewSummary` dataclasses + `_dominant_tier()` helper | Tier-3: Haiku |
| 5 | Durable JSONL write + bus publish in `BrainstemScheduler._run_quality_monitoring()` | Tier-2: Sonnet |
| 6 | Durable JSONL write + bus publish in `run_freshness_review()` (after existing direct-write calls) | Tier-2: Sonnet |
| 7 | `build_graph_quality_captain_log_handler()` in `events/pipeline_handlers.py` (handles both streams) | Tier-2: Sonnet |
| 8 | Wire `cg:graph-monitor` subscription in `service/app.py` lifespan behind `graph_quality_stream_enabled` flag | Tier-3: Haiku |
| 9 | Tier multiplier block in `memory/service.py:_calculate_relevance_scores()` | Tier-2: Sonnet |
| 10 | `FEEDBACK_STREAM_ARCHITECTURE.md` Stream 6 + Stream 8 row updates | Tier-3: Haiku |
| 11 | Unit tests: fingerprint determinism, durable-before-bus ordering, CL entry shape, tier threshold boundaries, zero-access entity guard | Tier-2: Sonnet |

### Phase 2 — Governance response (flag default `False`)

| Order | Work | Tier |
|-------|------|------|
| 1 | `cg:graph-monitor` handler: high-severity path publishes `ModeAdvisoryEvent` to `stream:mode.transition` | Tier-2: Sonnet |
| 2 | Unit test: governance path fires for "high" but not "medium"; no-op when flag disabled | Tier-2: Sonnet |
| 3 | Document flip gate (this ADR §D7) — done | Tier-3: Haiku |

Phase 1 ships in the implementation PR. Phase 2 ships in the same PR, flag-gated off.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| Stream/CG constants, `GraphQualityAnomalyEvent`, `MemoryStalenessReviewedEvent` | `src/personal_agent/events/models.py` | Infrastructure |
| `GraphQualityAnomaly`, `GraphStalenessReviewSummary`, `_dominant_tier` | `src/personal_agent/second_brain/quality_monitor.py` or new `telemetry/graph_quality.py` | Observation |
| `build_graph_quality_captain_log_handler` | `src/personal_agent/events/pipeline_handlers.py` | Observation |
| `staleness_tier_from_freshness_score` | `src/personal_agent/memory/freshness.py` | Observation |
| Tier multiplier block | `src/personal_agent/memory/service.py` | Observation |
| Durable JSONL write + bus publish (Stream 8) | `src/personal_agent/brainstem/scheduler.py` | Execution |
| Durable JSONL write + bus publish (Stream 6) | `src/personal_agent/brainstem/jobs/freshness_review.py` | Execution |
| Config flags | `src/personal_agent/config/settings.py` | Infrastructure |

The Execution Layer touches are minimal: both are one-call additions at existing integration points.

---

## Open Questions

1. **Anomaly severity Literal.** The `Anomaly.severity` field is a free string today. A follow-on change could narrow it to `Literal["info", "medium", "high"]` for static safety. Out of scope here — this ADR's `cg:graph-monitor` handler defensively checks `event.severity == "high"` and falls through to KNOWLEDGE_QUALITY for any other value.
2. **Tier factor calibration.** The default 0.3 factor for DORMANT is conservative. If promoted-recall quality metrics (available after 30 days of Phase 1 data) show over-suppression, adjust `freshness_tier_factors.dormant` in `.env`. No code change needed.
3. **`staleness_tier_from_freshness_score` threshold drift.** The thresholds (0.50 / 0.25 / 0.10) are calibrated for `half_life_days=30`. If the operator changes `half_life_days`, the tier derivation will drift. Long-term fix: expose configurable tier-score thresholds. Low priority until there is evidence of miscalibration.
4. **Multiple anomalies per day — proposal volume.** On a degraded day the daily run may produce 4-6 anomalies, each becoming a Captain's Log entry. ADR-0030 dedup (`seen_count` increment) keeps the file count bounded across days. If entry density becomes noisy in Linear, the promotion gate (`seen_count ≥ 3`, `age ≥ 7 d`) acts as a natural throttle.

---

## Dedicated Linear Project — Knowledge Graph Quality

Per `FEEDBACK_STREAM_ARCHITECTURE.md`, Streams 6 and 8 issues land in the **"Knowledge Graph Quality"** project.

### Project configuration

| Field | Value |
|-------|-------|
| Project name | Knowledge Graph Quality |
| Team | FrenchForest |
| Default issue state | Needs Approval |
| Labels on creation | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping | `seen_count ≥ 10` → High; `seen_count ≥ 3` → Normal; else Low |

### Issue formats

**Stream 8 (anomaly):**
```
Title: [Graph quality] <anomaly_type>: <message>

Body:
  ## Anomaly summary
  Type:           <anomaly_type>
  Severity:       <severity>
  Observed:       <observed_value>
  Expected range: <expected_range>
  Observation:    <observation_date>
  Fingerprint:    <fingerprint>
  Seen count:     <seen_count>

  ## How to investigate
  Check telemetry/graph_quality/GQ-<observation_date>.jsonl.
  For extraction failures: search ES agent-logs-* for entity_extraction_failed events.
  For structural anomalies: inspect Neo4j directly via brainstem diagnostics.
```

**Stream 6 (staleness review trend):**
```
Title: [KG freshness] <iso_week> — <entities_dormant> dormant entities

Body:
  ## Review summary
  Week:                <iso_week>
  Entities  — warm: <w>  cooling: <co>  cold: <c>  dormant: <d>
  Relationships — dormant: <rd>
  Never-accessed old entities: <n>
  Dominant tier: <dominant_tier>
  Fingerprint:   <fingerprint>

  ## What to do
  Review dormant entity samples in the linked Captain's Log entry.
  Validate whether these entities should be archived or re-validated.
```

### Feedback labels (inherited from ADR-0040)

Same as ADR-0056/0059 — Approved / Rejected / Deepen / Too Vague / Defer. Rejected suppresses the fingerprint for 30 days.

---

## End State

### After Phase 1 ships

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| Daily anomaly detection (existing) | Each anomaly → `GQ-*.jsonl` + bus event + Captain's Log entry | `telemetry/graph_quality/GQ-*.jsonl` grows daily; ES-indexed CL entries |
| Weekly freshness review (existing direct-write retained) | Review → `FR-*.jsonl` + bus event + trend CL entry (when threshold exceeded) | `telemetry/freshness_review/FR-*.jsonl` grows weekly |
| `cg:graph-monitor` handler | Both streams → `CaptainLogEntry(KNOWLEDGE_QUALITY / RELIABILITY, SECOND_BRAIN)` | Entries in `telemetry/captains_log/` with graph-quality fingerprints |
| Promotion pipeline (existing) | After `seen_count ≥ 3` + `age ≥ 7 d` → Linear issue | Issues in "Knowledge Graph Quality" project |
| `StalenessTier` multiplier in reranking | DORMANT entities contribute 30 % of freshness weight | `_calculate_relevance_scores` output shifts for dormant entities; observable via memory recall telemetry |

Human action required: review and label Linear issues in "Knowledge Graph Quality".

### After Phase 2 enabled (flag flipped)

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| `graph_quality_governance_enabled=True` | High-severity anomaly → `ModeAdvisoryEvent(DEGRADED)` published after CL entry | `mode_advisory_received` log events in Kibana; possible DEGRADED mode transition visible via `cg:mode-controller` logs |

Human action required: monitor false-positive rate in Linear over first month; tune if DEGRADED transitions are spuriously frequent.

---

## Loop Completeness Criteria

The streams are verified closed and working when, over a trailing 14-day window post-merge:

1. **Stream 8 detection**: `XLEN stream:graph.quality_anomaly ≥ 1` in a typical-load week (daily run fires each day; at least one anomaly expected per week if the graph is active).
2. **Stream 8 durable**: `telemetry/graph_quality/GQ-*.jsonl` files created daily; every bus event has a matching JSONL line.
3. **Stream 6 detection**: `XLEN stream:memory.staleness_reviewed ≥ 1` (one event per weekly review run).
4. **Stream 6 durable**: `telemetry/freshness_review/FR-*.jsonl` files created weekly.
5. **Captain's Log ingestion**: at least one `GraphQualityAnomalyEvent → CaptainLogEntry → telemetry/captains_log/CL-*.json` round trip traceable by fingerprint.
6. **Promotion**: at least one Linear issue in "Knowledge Graph Quality" project created by the promotion pipeline.
7. **Feedback**: at least one Linear label (Approved / Rejected / Deepen / Too Vague / Defer) processed by `FeedbackPoller`.
8. **Suppression**: after a `Rejected` label, next scan that would have re-emitted the same fingerprint logs `captains_log_proposal_suppressed` and writes no new entry.
9. **Tier multiplier**: `freshness_tier_reranking_enabled=True` is confirmed (default); recall telemetry shows no regressions (baseline: existing unit tests pass).

---

## Feedback Stream ADR Template — Compliance Checklist

Per the Feedback Stream ADR Template established in ADR-0053:

- [x] **1. Stream identity** — Phase 3 streams; Observation + Execution touch points; depends on ADR-0041/0042/0043/0053/0054/0057
- [x] **2. Source** — scheduler `_run_quality_monitoring()` (daily) + `run_freshness_review()` (weekly); per-anomaly and per-run granularity
- [x] **3. Collection mechanism** — durable JSONL append then bus publish (ADR-0054 D4 ordering); existing direct-write retained; graceful degradation via `graph_quality_stream_enabled` flag
- [x] **4. Processing algorithm** — `pattern_fingerprint("graph_quality", anomaly_type, message)` and `cost_fingerprint(staleness_review_{tier}, iso_week)`; CL dedup via ADR-0030; severity-gated CL category
- [x] **5. Signal produced** — `GraphQualityAnomalyEvent` + `MemoryStalenessReviewedEvent` on bus; per-day/per-week JSONL on disk; `CaptainLogEntry(RELIABILITY / KNOWLEDGE_QUALITY, SECOND_BRAIN)` via handler
- [x] **6. Full automation cycle** — D8 traces two 5-step loops (Stream 8 and Stream 6) end-to-end
- [x] **7. Human review interface** — "Knowledge Graph Quality" Linear project; two issue formats; label semantics inherited from ADR-0040
- [x] **8. End state table** — Phase 1, Phase 2 enabled
- [x] **9. Loop completeness criteria** — 9-point check, 14-day evaluation window

---

## References

- FRE-250: Draft ADR-0060 — Knowledge Graph Quality Stream (this ADR)
- ADR-0041: Event Bus via Redis Streams — transport
- ADR-0042: Knowledge Graph Freshness via Access Tracking — Stream 6 foundation (7 steps complete); ADR-0060 closes the final gaps
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0053: Gate Feedback-Loop Monitoring Framework — Feedback Stream ADR Template
- ADR-0054: Feedback Stream Bus Convention — dual-write, stream naming, `EventBase` fields; reserves `stream:graph.quality_anomaly` + `cg:graph-monitor`
- ADR-0057: Insights & Pattern Analysis — establishes `anomaly → bus → Captain's Log` pattern; `insights/fingerprints.py` reused
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — surfacing channel, fingerprint dedup
- ADR-0040: Linear as Async Feedback Channel — label semantics, suppression
- ADR-0059: Context Quality Stream — sibling ADR; composability note at §Consequences
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — feedback-stream catalogue (Streams 6 + 8 rows updated by this ADR)
- `src/personal_agent/second_brain/quality_monitor.py` — `Anomaly` dataclass, `detect_anomalies()`, `_range_anomaly()`, `_detect_spike()`
- `src/personal_agent/brainstem/scheduler.py:609-622` — dead-end integration point (Stream 8 fix site)
- `src/personal_agent/brainstem/jobs/freshness_review.py:240-323` — Stream 6 integration point
- `src/personal_agent/memory/freshness.py` — `StalenessTier`, `compute_freshness`, `classify_staleness`; new `staleness_tier_from_freshness_score`
- `src/personal_agent/memory/freshness_aggregate.py` — `GraphStalenessSummary`, `StalenessTierCounts`
- `src/personal_agent/memory/service.py:1581-1592` — tier multiplier insertion point
- `src/personal_agent/insights/fingerprints.py` — `pattern_fingerprint`, `cost_fingerprint` (reused)
- `src/personal_agent/events/pipeline_handlers.py:519-618` — `build_compaction_quality_captain_log_handler` (shape template)
