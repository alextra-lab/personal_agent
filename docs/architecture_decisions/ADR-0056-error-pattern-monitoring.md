# ADR-0056: Error Pattern Monitoring Stream

**Status**: Accepted — Implemented 2026-04-24 (FRE-244, commit baed032)
**Date**: 2026-04-23
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Architectural Separation), ADR-0053 (Gate Feedback-Loop Monitoring Framework — template), ADR-0054 (Feedback Stream Bus Convention)
**Related**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel), ADR-0047 (Context Management & Observability)
**Enables**: FRE-249 (Context Quality Stream — inherits the error-pattern detection template), FRE-226 (Agent self-updating skills — Phase 2 produces surgical edits targeting skill text)
**Linear Issue**: FRE-244

---

## Context

### Level 3 of the Four-Level Observability Framework is missing

The agent has a four-level self-observability framework (documented in `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`):

| Level | What it observes                                      | Timescale     | State                                              |
|-------|-------------------------------------------------------|---------------|----------------------------------------------------|
| 1     | System metrics (CPU, memory, GPU, operational mode)   | 5 s poll      | Built, disconnected — ADR-0055 wires it up         |
| 2     | Gate / pipeline decisions                             | Per-request   | ADR-0053 drafted                                   |
| **3** | **Application errors — log events, exceptions**       | **Rolling**   | **Missing entirely (this ADR)**                    |
| 4     | Self-reflection (Captain's Log)                       | Per-task      | ADR-0030 / ADR-0040 working                        |

Level 3 is the gap. The agent emits hundreds of `ERROR` and `WARNING` structlog events per day — tool call failures, LLM inference errors, Neo4j/PostgreSQL/Elasticsearch connection failures, gateway pipeline failures, dead-letter events, entity extraction failures, compaction quality warnings. All of it lands in Elasticsearch (`agent-logs-*` indices) via the `ElasticsearchHandler`. None of it is *read* by the agent. A human must notice a Kibana chart.

The project owner articulated this as one of the most powerful self-improvement capabilities available: *"the agent able to read its own logs and offer corrections."* This ADR defines the feedback stream that makes that capability real.

### Error signals already flow — they just terminate at a dashboard

Today:

```
log.error("mcp_tool_call_failed", tool=…, error=…, trace_id=…)
  → structlog → ElasticsearchHandler → agent-logs-YYYY-MM-DD
  → visible in Kibana; nothing reads it programmatically
```

The structlog handler already writes every ERROR and WARNING record with `level`, `component`, `module`, `function`, `line_number`, and the full event_dict. Fields like `event_type`, `trace_id`, `error`, `tool`, and custom context are already indexed. `TelemetryQueries.get_event_count()` can count by `event_type` — but no clustering, no error-type distribution, no fingerprinting, no pattern emission.

An audit of `src/personal_agent/` shows **295 log.error/log.warning call sites** across 60+ modules. Representative events (sampled via grep):

- `mcp_tool_call_failed`, `mcp_list_tools_timeout`, `mcp_client_connect_failed`
- `fetch_url_timeout`, `fetch_url_connect_failed`, `get_library_docs_timeout`
- `elasticsearch_connection_failed`, `elasticsearch_log_failed`, `elasticsearch_bulk_failed`
- `neo4j_…`, `cost_tracker_connection_failed`, `app_config_load_failed`
- `chat.stream_failed`, `gateway_auth_failed`, `gateway_pipeline_failed`
- `dspy_reflection_failed_fallback_manual`, `feedback_llm_failed`
- `entity_creation_failed`, `extraction_empty_response`, `entity_importance_fetch_failed`

These names are implicitly namespaced by `component` (the structlog logger name — equivalent to the dotted module path). That gives the clustering primitive for free.

### Feedback Stream Bus Convention applies

ADR-0054 established the dual-write convention: every feedback stream writes durably (file or ES) AND publishes to the event bus. Error events already dual-write on the producer side — they are in ES because of `ElasticsearchHandler`. What is missing is the *observation* side: a consumer that reads the ES error index, clusters, and publishes its findings on a named bus stream with a typed event. This ADR closes the observation gap.

### Hermes / GEPA failure-path reflection

The user flagged an inspiration from NousResearch's GEPA paper (Genetic-Pareto Prompt Evolution, ICLR 2026 Oral): shift reflection from *outcome* to *failure path*. The ADR's Phase 2 adopts the insight — no external dependency on GEPA code — by extending the existing DSPy `GenerateReflection` signature to ask, alongside the current general-improvement output, the surgical question *"what specific text change would have prevented this exact failure?"* and passing failed tool call traces as input. Credit to GEPA is acknowledged in the design rationale; the implementation is a prompt-engineering upgrade to the existing `captains_log.reflection_dspy` module.

---

## Decision Drivers

1. **Read what is already written.** Errors already land in Elasticsearch. Do not introduce a second log sink; query what exists.
2. **Dual-write convention (ADR-0054).** The ES index is the durable write. A typed `ErrorPatternDetectedEvent` on a named stream is the composability hook. Durable precedes bus; bus failures are logged and swallowed.
3. **Non-intrusive observation.** Error producers are not modified. Every new `log.error(...)` site automatically feeds the monitor because it already reaches ES.
4. **Clustering, not alerting.** One-off errors are noise. Sustained *patterns* (same component + event + error type, `N` occurrences in a rolling window) are the signal.
5. **Promotion through existing Captain's Log pipeline.** Surfacing mirrors ADR-0030 / ADR-0040: CL entry → dedup by fingerprint → promotion after `seen_count ≥ 3`, `age ≥ 7 d` → Linear issue in the dedicated project → human label → suppression or approval.
6. **Phase 2 failure-path reflection is an extension, not a replacement.** Phase 1 surfaces patterns *across* traces. Phase 2 deepens reflection *within* a single failed trace. Both are valuable. Phase 2 is strictly additive.
7. **No new infrastructure.** Reuse ES, the event bus, Captain's Log, the promotion pipeline, and DSPy. Add one consumer and two event types.

---

## Decision

### D1: Source — ERROR events plus an allowlist of WARNING events

**ERROR**: every `level=ERROR` record in `agent-logs-*` is in scope. No carve-outs.

**WARNING**: only an explicit allowlist is in scope, because most `log.warning(...)` sites are benign degradation messages (e.g. `elasticsearch_not_connected` during startup, `mcp_gateway_shutdown_error` during teardown). Warnings that *are* signals of a broken loop or quality problem are enumerated:

| Warning event name                         | Signal                                                     |
|--------------------------------------------|------------------------------------------------------------|
| `compaction_quality.poor`                  | Context loss — user noun phrase overlaps a dropped entity  |
| `history_sanitised_orphans_removed`        | Cross-provider tool-result orphans (FRE-237 class)         |
| `chat.stream_failed`                       | Chat request degradation                                   |
| `gateway_pipeline_failed`                  | Gateway stage raised                                       |
| `expansion_budget_computation_failed`      | Stage-3 governance signal corruption                       |
| `dspy_reflection_failed_fallback_manual`   | Self-reflection pipeline degradation                       |
| `feedback_llm_failed`                      | Linear feedback pipeline degradation                       |
| `insights_cost_query_failed`               | Cost anomaly pipeline degradation                          |
| `freshness_review_skipped_*`               | Freshness pipeline degradation                             |
| `captains_log_backfill_failed`             | Captain's Log ES backfill broken                           |
| `mcp_*` (timeout/failure family)           | MCP gateway broken — entire tool tier affected             |
| `dead_letter_routed`                       | Bus consumer failed `max_retries` times (per ADR-0041)     |

The allowlist lives in `src/personal_agent/telemetry/error_monitor.py::WARNING_EVENT_ALLOWLIST` as a frozenset so it is testable and reviewable. Adding a name is a one-line change; approval criterion is "this WARNING indicates a broken loop, not a benign degradation."

**Out of scope** (always ignored, even at ERROR level):

- Events where `component` starts with `elastic_transport` / `elasticsearch` / `neo4j` / `httpx` / `httpcore` — already filtered by `ElasticsearchHandler`, but the query re-asserts the filter so replays stay correct.
- Events from `elasticsearch_log_failed` / `elasticsearch_bulk_failed` — monitoring the monitor is a feedback loop we refuse.
- Dead-letter events that *originate from* the error-monitor consumer group (`source_component == "telemetry.error_monitor"` at the event level) — prevents the monitor's own failures from being amplified.

### D2: Collection — Background consumer on `consolidation.completed`, ES-backed

A new consumer group `cg:error-monitor` subscribes to `stream:consolidation.completed` (the existing fan-out trigger already used by `cg:insights` and `cg:promotion`). Each event triggers one error-pattern scan:

```
Input event   : ConsolidationCompletedEvent
Scan window   : trailing 24 hours (configurable: settings.error_monitor_window_hours)
Scan target   : agent-logs-* indices filtered by level ∈ {ERROR, WARNING-allowlist}
Aggregation   : ES composite aggregation on (component, event, error_type_normalised)
Min sample    : cluster count ≥ settings.error_monitor_min_occurrences (default 5)
Output        : list[ErrorPatternCluster] → dual-write → event bus
```

Why `consolidation.completed` and not a schedule?

- Consolidation already runs on idle + configurable cron. Piggy-backing means one scheduling source of truth.
- The scan cost is dominated by a single ES aggregation query — the frequency of consolidation (hourly at minimum, near-real-time when idle) is an appropriate cadence.
- Phase 3 of ADR-0054 explicitly reserves this stream as the consolidation fan-out point for observation-layer analytics.

**Fallback when Redis is down:** `NoOpBus.subscribe()` silently discards; no scans run. This is acceptable — Phase 1 is best-effort pattern surfacing, not on-call alerting. When the bus recovers, the next `consolidation.completed` runs the scan over the full trailing window.

**Fallback when Elasticsearch is down:** the query raises; the handler logs `error_monitor_scan_failed` (at `warning` — *not* `error`, to avoid self-feeding the queue) and returns. State is not advanced; the next event re-attempts the scan.

### D3: Data model

#### Three layers (matching ADR-0053 / ADR-0054 convention)

**Layer A — `ErrorPatternCluster` (in-memory, per-scan):**

```python
@dataclass(frozen=True)
class ErrorPatternCluster:
    """One cluster of error events sharing a fingerprint."""
    fingerprint: str                         # sha256(component:event:error_type)[:16]
    component: str                           # structlog logger/module (e.g. "tools.fetch_url")
    event_name: str                          # structlog event (e.g. "fetch_url_timeout")
    error_type: str                          # normalised exception class or "<no_exc>"
    level: str                               # "ERROR" or "WARNING"
    occurrences: int                         # count in the window
    first_seen: datetime                     # earliest timestamp in window
    last_seen: datetime                      # most recent timestamp in window
    sample_trace_ids: tuple[str, ...]        # up to 5 representative trace_ids
    sample_messages: tuple[str, ...]         # up to 3 distinct error messages
    window_hours: int                        # window that produced this cluster
```

**Layer B — `ErrorPatternDetectedEvent` (bus, per-pattern):**

```python
class ErrorPatternDetectedEvent(EventBase):
    """Published when the error-monitor scan detects a sustained pattern.

    One event per cluster per scan. Consumers:
      • cg:captain-log  → writes CaptainLogEntry(category=RELIABILITY, scope=<derived>)
      • future consumers (FRE-249 context-quality, FRE-226 skill updater) →
        subscribe and filter by fingerprint / event_name without touching the
        producer
    """

    event_type: Literal["errors.pattern_detected"] = "errors.pattern_detected"
    fingerprint: str
    component: str
    event_name: str
    error_type: str
    level: str                               # "ERROR" | "WARNING"
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    window_hours: int
    sample_trace_ids: list[str]              # ≤ 5
    sample_messages: list[str]               # ≤ 3
    # trace_id / session_id: None  (scan is not request-correlated; D3 of ADR-0054)
    # source_component: "telemetry.error_monitor"
```

Stream name: `stream:errors.pattern_detected` (per ADR-0054 `<domain>.<signal>`).

**Layer C — Durable write: `telemetry/error_patterns/EP-<fingerprint>.json`:**

One JSON file per fingerprint (not per detection). On every scan the handler upserts:

```json
{
  "fingerprint": "f1a9c0e2b3d74f8a",
  "component": "tools.fetch_url",
  "event_name": "fetch_url_timeout",
  "error_type": "TimeoutError",
  "level": "ERROR",
  "first_seen": "2026-04-16T07:12:44Z",
  "last_seen":  "2026-04-23T01:03:17Z",
  "total_occurrences": 47,
  "scan_history": [
    {"scan_at": "2026-04-17T00:00:01Z", "window_hours": 24, "occurrences_in_window": 12},
    {"scan_at": "2026-04-18T00:00:03Z", "window_hours": 24, "occurrences_in_window":  9}
  ],
  "sample_trace_ids": ["…", "…", "…"],
  "sample_messages": ["Read timeout after 10s", "Connection reset by peer", "SSL handshake failed"]
}
```

This file is the durable record — it survives Redis restarts and ES index rollovers. `scan_history` is capped at 30 entries (rolling) to bound file size. The file is the "primary" per ADR-0054 D4: written before the bus publish; its failure aborts the scan with a logged warning.

### D4: Clustering — `(component, event_name, normalised_error_type)`

**Fingerprint formula:** `sha256(f"{component}:{event_name}:{error_type}".encode())[:16]` (16 hex chars = 64 bits, plenty for this keyspace).

**`error_type` normalisation** — errors arrive in many shapes:

| Source                           | Raw field                          | Normalised                  |
|----------------------------------|-------------------------------------|-----------------------------|
| `exc_info` attached to log record | `TimeoutError`, `ValueError`, …    | class name verbatim         |
| `error="Read timeout after 10s"` string | prefix up to first whitespace / `:` | `"Read"`, `"timeout"` — too coarse |
| No exception, no error field     | (missing)                          | `"<no_exc>"`                |

The implementation uses, in order: `event_dict["exception"].split("\n")[-2].split(":")[0]` (exception class from traceback tail), else `event_dict.get("error_type")`, else `"<no_exc>"`. This ties fingerprints to structured fields, not free-text messages — so message drift does not fragment a pattern.

**Why not cluster by message similarity?** Adds an embedding model to the hot path; violates the "no new infrastructure" driver. Structured `event_name` + exception class already give the right granularity — a `fetch_url_timeout` / `TimeoutError` cluster is what we want to act on, not the set of URLs that timed out.

**Why not cluster by `trace_id`?** A single bad request can emit many errors; clustering by trace_id would create one-per-request patterns that hide systemic problems. The scan explicitly groups *across* traces.

### D5: Signal — `CaptainLogEntry(category=RELIABILITY, scope=<derived>)`

When a cluster fires (`occurrences ≥ min_occurrences` in the window), the error-monitor *also* emits a Captain's Log entry, via the **bus-driven handler** that subscribes `cg:captain-log` to `stream:errors.pattern_detected`. The handler builds:

```python
CaptainLogEntry(
    entry_id="",                          # generated by CaptainLogManager
    type=CaptainLogEntryType.CONFIG_PROPOSAL,
    title=f"Error pattern: {event_name} in {component} ({occurrences}x/{window_hours}h)",
    rationale=(
        f"{occurrences} occurrences of `{event_name}` in `{component}` over the last "
        f"{window_hours} hours (error_type={error_type}). Sample traces: {trace_ids}. "
        f"Representative messages: {messages}."
    ),
    proposed_change=ProposedChange(
        what=f"Investigate and mitigate repeated {event_name} in {component}",
        why=(
            "Sustained error pattern detected by Level 3 self-observability. "
            "Repeated failures of this class degrade the capability served by "
            f"{component}."
        ),
        how=(
            "1) Open representative traces in Kibana to understand the immediate cause.\n"
            "2) Decide whether the fix is a retry/backoff policy, a guard, a schema "
            "change, or a tool description update.\n"
            "3) If Phase 2 failure-path reflection is enabled, the surgical edit "
            "suggestion is attached in `potential_implementation`."
        ),
        category=ChangeCategory.RELIABILITY,
        scope=_scope_from_component(component),   # see table below
        fingerprint=fingerprint,                  # D4 value — promotes dedup to CL.manager
    ),
    supporting_metrics=[
        f"occurrences: {occurrences}",
        f"window_hours: {window_hours}",
        f"first_seen: {first_seen.isoformat()}",
        f"last_seen: {last_seen.isoformat()}",
    ],
    metrics_structured=[
        Metric(name="occurrences", value=occurrences, unit="count"),
        Metric(name="window_hours", value=window_hours, unit="h"),
    ],
    telemetry_refs=[TelemetryRef(trace_id=tid, metric_name=None, value=None)
                    for tid in sample_trace_ids],
)
```

**Scope derivation** from `component` prefix (first dotted segment):

| Prefix                               | `ChangeScope`           |
|--------------------------------------|--------------------------|
| `tools.*`, `mcp.*`                   | `TOOLS`                 |
| `orchestrator.*`, `request_gateway.*`| `ORCHESTRATOR`          |
| `memory.*`, `second_brain.*`         | `SECOND_BRAIN`          |
| `captains_log.*`                     | `CAPTAINS_LOG`          |
| `brainstem.*`                        | `BRAINSTEM`             |
| `telemetry.*`                        | `TELEMETRY`             |
| `governance.*`                       | `GOVERNANCE`            |
| `insights.*`                         | `INSIGHTS`              |
| `llm_client.*`                       | `LLM_CLIENT`            |
| `config.*`, `service.*`, everything else | `CROSS_CUTTING`      |

The fingerprint on `ProposedChange` is authoritative — `CaptainLogManager.save_entry()` already looks up existing entries by fingerprint (ADR-0030) and increments `seen_count` instead of writing a new file. Error-pattern entries merge naturally with the existing dedup pipeline.

### D6: Phase 2 — Failure-path reflection (GEPA-inspired, within-trace)

Phase 1 operates *across* traces. Phase 2 operates *within* a trace when the reflection pipeline fires — and it only fires for traces that actually had errors. The two phases are complementary and independent; Phase 2 can land before or after the Phase 1 consumer.

**The shift in reflection shape:**

Current reflection (outcome-oriented) produces:

> "The agent struggled with tool-call sequencing. General improvement: plan tool usage before executing."

Phase 2 reflection (failure-path-oriented) produces:

> "Tool call #3 (`query_elasticsearch`) timed out. The agent retried with the same query. Tool call #5 timed out again before changing strategy. The `query_elasticsearch` tool description says nothing about retry behaviour. Proposed change: add 'Fallback: if timeout, reduce query scope before retrying' to the tool description."

**Concretely, Phase 2 is three edits:**

1. **`captains_log/reflection.py`** — `_summarize_telemetry()` already walks the trace's ES events. Add a `_extract_failure_excerpt()` helper that selects, in order: the last N tool-call events where `status in {"timeout", "error"}`, the exception name and message, and the subsequent agent step (retry? different tool? user-visible error?). Returns a `FailureExcerpt` dataclass with `failed_tool_calls: list[FailedToolCall]`, `error_summary: str`, and `recovery_actions: list[str]`.

2. **`captains_log/reflection_dspy.py` — `GenerateReflection` signature** — add two input fields (`failure_excerpt: str`, `had_errors: bool`) and two new output fields:

   ```python
   failure_path_fix_what: str = dspy.OutputField(
       desc="Surgical fix (≤ 80 chars) that would have prevented this exact failure. "
            "Example: 'Add retry-with-scope-reduction note to query_elasticsearch tool description.' "
            "Return empty string if had_errors is False.",
   )
   failure_path_fix_location: str = dspy.OutputField(
       desc="File path + symbol of the text to edit, if known. Example: "
            "'src/personal_agent/tools/query_elasticsearch.py::DESCRIPTION' or "
            "'docs/skills/query_elasticsearch.md'. Empty if had_errors is False.",
   )
   ```

3. **`captains_log/reflection.py` — entry construction** — when `failure_path_fix_what` is non-empty, populate `CaptainLogEntry.potential_implementation` with the surgical fix and tag the entry with `category=ChangeCategory.RELIABILITY`. Empty outputs leave current behaviour unchanged.

**Why this works without new infrastructure:**

- DSPy handles both local (SLM) and cloud models after FRE-253 — no cloud bypass needed.
- The existing reflection pipeline already runs after every task; the extension is three fields on one `dspy.Signature`.
- `potential_implementation` is already a standard Captain's Log field; it flows through dedup, promotion, and Linear untouched.
- No external dependency on GEPA code; the credit is in the ADR.

**Dependency on Phase 1:** None, technically. Phase 2 can ship without Phase 1. But Phase 1 + Phase 2 together are the powerful combination — Phase 1 tells the agent *which patterns* are recurring; Phase 2 gives surgical edits for individual occurrences. Together, over a 30-day window, they produce both cluster-level "this is happening too often" proposals and surgical "this one line of this tool description is wrong" proposals.

**Acknowledgement:** the failure-path approach is inspired by NousResearch's GEPA paper (*Genetic-Pareto Prompt Evolution*, ICLR 2026 Oral). This ADR adopts the insight as a prompt engineering pattern; it does not take a code or library dependency on GEPA.

### D7: Surfacing Channels

**Primary — Captain's Log + promotion pipeline + Linear:**

`stream:errors.pattern_detected` → `cg:captain-log` handler → `CaptainLogManager.save_entry()` → fingerprint dedup → consolidation triggers promotion → `PromotionPipeline.scan_promotable_entries()` after `seen_count ≥ 3`, `age ≥ 7 d` → Linear issue created in the **"Error Pattern Monitoring"** project (FRE-244 already created this project).

**Secondary — Kibana:**

Error events are *already* in Kibana (they have been since Phase 2.3 telemetry). Two additional aggregations ship with this ADR:

- "Error pattern top-N": terms aggregation on `component.event_name` over the trailing 7 days, sorted by count descending.
- "Error pattern timeline": date histogram of matching errors per fingerprint over the trailing 30 days.

Both are added to the existing "Agent Reliability" dashboard; no new dashboard is introduced.

**Tertiary — Programmatic (`TelemetryQueries` extensions):**

`TelemetryQueries` gains two methods so the agent can answer questions about its own error patterns in conversation:

| Method                                           | Returns                         |
|--------------------------------------------------|---------------------------------|
| `get_error_events(days, level_filter)`          | `list[ErrorEventRecord]`        |
| `get_error_patterns(days, min_occurrences)`     | `list[ErrorPatternCluster]`     |

A future `query_error_patterns` native tool (out of scope for this ADR, in scope for the implementation issue) calls `get_error_patterns()` to answer "what have been your most frequent errors this week?".

### D8: Full Automation Cycle

```
1. Producer emits log.error("mcp_tool_call_failed", tool=…, error=…, trace_id=…)
   └─ structlog → ElasticsearchHandler → agent-logs-YYYY-MM-DD in ES (durable)

2. Later: brainstem scheduler triggers consolidation → consolidation.completed event
   └─ published to stream:consolidation.completed

3. cg:error-monitor receives ConsolidationCompletedEvent
   └─ runs ES composite aggregation over trailing window_hours
   └─ groups by (component, event_name, error_type) with occurrences ≥ min_occurrences
   └─ for each cluster:
      a) upsert telemetry/error_patterns/EP-<fingerprint>.json (DURABLE)
      b) publish ErrorPatternDetectedEvent to stream:errors.pattern_detected (BUS)

4. cg:captain-log (existing) receives ErrorPatternDetectedEvent
   └─ builds CaptainLogEntry(CONFIG_PROPOSAL, category=RELIABILITY, scope=<derived>)
   └─ CaptainLogManager.save_entry()
      ├─ if fingerprint suppressed (rejected < 30 days ago) → discard silently
      ├─ if matching fingerprint on disk → increment seen_count, merge (ADR-0030)
      └─ else → write CL-YYYYMMDD-*.json, index to ES

5. Next consolidation.completed → cg:promotion → PromotionPipeline.scan_promotable_entries()
   └─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 days
   └─ checks: LinearClient issue budget not exceeded
   └─ creates Linear issue in "Error Pattern Monitoring" project
   └─ publishes PromotionIssueCreatedEvent to stream:promotion.issue_created

6. Human receives Linear issue
   └─ reviews sample traces in Kibana, decides on fix
   └─ applies label: Approved / Rejected / Deepen / Too Vague / Defer

7. FeedbackPoller (daily) dispatches to label handler (ADR-0040)
   └─ Rejected  → write suppression (30 days); cancel issue
                  fingerprint blocked in step 4 for 30 days
   └─ Approved  → move to Approved state; human owns the fix
   └─ Deepen    → LLM re-analysis with deeper ES query (pulls more trace context);
                  posts refined proposal as a comment
   └─ Too Vague → refined proposal with more specific metric context
   └─ Defer     → no suppression; re-evaluated next scan

8. (Phase 2) On every task completion with had_errors=True
   └─ generate_reflection_entry() extracts failure_excerpt from trace
   └─ DSPy GenerateReflection emits failure_path_fix_what + failure_path_fix_location
   └─ CaptainLogEntry.potential_implementation carries the surgical fix
   └─ fingerprint on ProposedChange dedups the proposal
   └─ same promotion → Linear → feedback loop as Phase 1
```

**Loop closed.** A rejected pattern is suppressed for 30 days. An approved pattern creates a human-owned implementation task. Phase 2 attaches surgical fix suggestions where the reflection pipeline has visibility. Neither path requires manual intervention after the label is applied.

### D9: Scope Boundary

In scope:

- ERROR-level structlog events indexed to `agent-logs-*`.
- WARNING-level events on the explicit allowlist (D1 table).
- Clustering, fingerprinting, Captain's Log emission, bus publish.
- Phase 2 DSPy extension for failure-path reflection.
- Kibana aggregations on the existing "Agent Reliability" dashboard.
- `TelemetryQueries.get_error_events()` / `get_error_patterns()`.

Out of scope:

- **Real-time alerting.** The monitor is rolling-window + pattern-based, not per-event alerting. "Wake someone up when X fails" is a different contract.
- **Automatic remediation.** Phase 1 and Phase 2 surface proposals. Applying fixes is a human decision. Slice 3 (adaptive self-modification) is where automation enters; that is a separate ADR.
- **Error tracking outside the agent.** Client-side (PWA) errors and LLM server errors are not in `agent-logs-*`; they are out of scope.
- **Deep-learning-based clustering.** Embedding-based message similarity is deliberately rejected (D4 rationale). If the structured-field clustering proves too coarse in practice, a follow-up ADR can add an embedding pass gated behind a feature flag.
- **Phase 3 self-modification.** Applying a surgical edit directly to a tool description, skill file, or system prompt is out of scope. Phase 2 *proposes*; humans *apply*.

---

## Alternatives Considered

### Collection Mechanism

| Option | Description | Verdict |
|--------|-------------|---------|
| A. New structlog processor | Intercept every ERROR record in-process; buffer in memory; flush periodically | Rejected — doubles the ingestion surface; every process (agent service, gateway, sub-agents) would carry its own buffer and emit its own bus events; scan-from-ES is centralised |
| B. Scan `telemetry/logs/current.jsonl` file | Local JSONL file is faster than ES and always present | Rejected — single-process view only; the agent runs multiple processes (service + scheduler + consumers) and only ES sees them all. Also couples the monitor to a file format that the ES handler does not own |
| **C. ES-backed scan triggered by `consolidation.completed`** | Query the existing `agent-logs-*` composite aggregation; cluster results; emit bus event | **Selected** — reuses the durable store; no producer changes; clustering is a single ES query; piggy-backs on existing consolidation cadence |
| D. Periodic scheduled job (cron) | `asyncio.create_task(loop_every(1h, scan))` inside brainstem scheduler | Rejected — introduces a second scheduling surface; `consolidation.completed` already fires hourly under normal load and on idle. Piggy-backing is simpler than maintaining a new cron |

### Clustering Granularity

| Option | Description | Verdict |
|--------|-------------|---------|
| A. `(component)` only | Coarser — all errors from the same module fingerprint together | Rejected — masks distinct failure modes in the same module (e.g. `fetch_url_timeout` vs `fetch_url_connect_failed`) |
| B. `(component, event_name)` | Moderately fine — groups by logger + event name | Viable — but loses the error-type distinction. A module that fails with both `TimeoutError` and `ValueError` on the same event would merge; these usually have different root causes |
| **C. `(component, event_name, error_type)`** | Chosen granularity — the exception class fingerprint | **Selected** — separates distinct failure modes while preserving cross-trace aggregation. `error_type` normalisation (D4) keeps cardinality bounded |
| D. `(component, event_name, error_type, message_embedding_cluster)` | Finest — semantic message similarity inside each (event, type) bucket | Rejected — embedding infrastructure cost; Phase 1 delivers value without it; Phase 2 (within-trace) already addresses message specificity |

### Signal Surface

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Log-only | Publish `error_pattern_detected` log event; rely on Kibana | Rejected — repeats the current failure mode (humans must watch Kibana). This is exactly what Level 3 is fixing |
| B. Captain's Log only | Write CL entries directly from the monitor consumer; no bus event | Rejected — violates ADR-0054 dual-write: future consumers (FRE-249, FRE-226) cannot subscribe without touching the monitor code. Bus event is the composability hook |
| **C. Bus event → CL handler → CL entry → promotion** | Dual-write (file + bus), CL handler listens, existing promotion pipeline reuses | **Selected** — ADR-0054 compliant; composable; reuses every piece of the ADR-0030 / ADR-0040 promotion pipeline unchanged |

### Phase 2 Implementation

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Separate "failure reflector" pipeline | New module that subscribes to something (a DLQ? every request?) and emits failure-path Captain's Log entries | Rejected — duplicates the existing reflection pipeline; two places to maintain a prompt |
| B. GEPA library dependency | Pull `gepa` into pyproject; use their primitives directly | Rejected — the insight is what we need, not the code; adding a research-code dependency for a prompt pattern is not justifiable |
| **C. Extend existing DSPy signature** | Two input fields + two output fields on `GenerateReflection`; helper to extract failure excerpt | **Selected** — minimal surface change; rides on existing reflection infrastructure; credit is in the ADR rationale |

---

## Consequences

### Positive

- **Level 3 of the four-level framework ships.** The agent can, for the first time, read its own error logs and surface recurring problems as actionable proposals.
- **Zero new infrastructure.** Reuses Elasticsearch, the event bus, Captain's Log, the promotion pipeline, Linear, DSPy. Two new event/stream names; one new consumer group.
- **Composability.** `stream:errors.pattern_detected` is consumable by any future stream. FRE-249 (Context Quality) will subscribe to patterns matching `event_name="compaction_quality.poor"`. FRE-226 (agent skill files) will subscribe to the `potential_implementation` field of Phase 2 proposals.
- **No producer changes.** Every `log.error(...)` site today already feeds the monitor through ES. Every future `log.error(...)` site is automatically covered.
- **Suppression works.** ADR-0040 rejection suppression applies by fingerprint — the same pattern cannot re-promote for 30 days after a human rejects it.
- **Surgical edits from Phase 2.** When enabled, every errored task produces a targeted fix proposal alongside the existing general reflection — without changing the overall pipeline.

### Negative

- **New consumer group `cg:error-monitor`.** One more subscription to register, start, and stop in `service/app.py`.
- **Two new event types.** `ErrorPatternDetectedEvent` on `stream:errors.pattern_detected`; `parse_stream_event()` gains a dispatch arm.
- **ES scan cost.** Each `consolidation.completed` triggers one composite aggregation on `agent-logs-*`. Under normal load (~10–30k log records/day), this is milliseconds. Under a log-storm (e.g. repeated retries), the scan itself grows — the implementation caps aggregation bucket count at 10,000.
- **DSPy signature adds two output fields.** Minor prompt-length increase. Mitigated by low-cost empty defaults when `had_errors=False`.
- **Failure-excerpt extraction reads trace events.** `_summarize_telemetry()` already does this; the failure-excerpt helper shares the same query. Extra work is one pass over the already-fetched event list.

### Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Log-storm amplification: one broken dependency floods `agent-logs-*` and every scan creates the same cluster | Medium | Fingerprint dedup at CL-manager level already prevents duplicate entries; `scan_history` in the per-fingerprint file caps growth; consider rate-limiting the bus publish to 1/min per fingerprint in Phase 1.5 |
| False-positive patterns from normal degradation (e.g. brief ES outage → `elasticsearch_connection_failed` spikes) | Medium | Minimum sample + 24 h window absorbs brief blips; per-event out-of-scope list (D1) filters monitor-on-monitor amplification; `Rejected` label suppresses benign clusters per ADR-0040 |
| Scan falls behind under consolidation backpressure | Low | Consolidation already runs at most hourly under normal load; scan latency ~100 ms; no queueing |
| Phase 2 DSPy output quality varies by model | Medium | DSPy ChainOfThought already self-corrects via structured fields; empty outputs are the safe fallback; per-task reflection is best-effort, not a gate |
| Fingerprint collisions (16-hex chars) | Very low | 64-bit keyspace; under 10⁹ patterns the collision probability is < 2⁻²⁴; if observed, extend to 24 hex chars |

---

## Implementation Priority

Phase 1 is ordered to deliver a working loop at step 8; Phase 2 is independent and can ship in parallel.

### Phase 1 — Error pattern detection

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | `ErrorPatternCluster` + `ErrorPatternDetectedEvent` + stream/cg constants in `events/models.py` | Types first | Tier-3: Haiku |
| 2 | `parse_stream_event()` dispatch arm for the new event | Deserialisation | Tier-3: Haiku |
| 3 | `telemetry/queries.py::get_error_events()` + `get_error_patterns()` | ES aggregation backbone | Tier-2: Sonnet |
| 4 | `telemetry/error_monitor.py` — scan orchestrator, clustering, fingerprinting, dual-write | Core consumer logic | Tier-2: Sonnet |
| 5 | `events/consumers/error_monitor.py::ErrorMonitorConsumer` — subscribe to `stream:consolidation.completed`; drive scan | Bus wiring | Tier-2: Sonnet |
| 6 | `events/pipeline_handlers.py::build_error_pattern_captain_log_handler()` — subscribe `cg:captain-log` to `stream:errors.pattern_detected`; emit CaptainLogEntry | Surfacing | Tier-2: Sonnet |
| 7 | `service/app.py` — register both subscriptions in lifespan | Integration | Tier-3: Haiku |
| 8 | Config flags: `error_monitor_enabled` (default `True`), `error_monitor_window_hours` (24), `error_monitor_min_occurrences` (5), `error_monitor_max_patterns_per_scan` (50) | Safe rollout | Tier-3: Haiku |
| 9 | Unit tests — clustering, fingerprinting, dual-write ordering, scope derivation, suppression integration | Quality gate | Tier-2: Sonnet |
| 10 | Kibana dashboard: error-pattern top-N + timeline panels | Visualisation | Tier-3: Haiku |
| 11 | Linear project "Error Pattern Monitoring" — confirm labels/priority map per ADR-0053 D7 pattern | Operational | Tier-3: Haiku |

### Phase 2 — Failure-path reflection

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | `captains_log/reflection.py::_extract_failure_excerpt()` | Data plumbing | Tier-2: Sonnet |
| 2 | `FailureExcerpt` / `FailedToolCall` dataclasses | Types | Tier-3: Haiku |
| 3 | `captains_log/reflection_dspy.py::GenerateReflection` — add `failure_excerpt`, `had_errors` inputs + `failure_path_fix_what`, `failure_path_fix_location` outputs | Prompt extension | Tier-2: Sonnet |
| 4 | `reflection.py` wiring — populate `potential_implementation` and `category=RELIABILITY` when outputs non-empty | Surfacing | Tier-2: Sonnet |
| 5 | Unit tests — failure excerpt extraction, DSPy signature fields, empty-output fallback | Quality gate | Tier-2: Sonnet |
| 6 | Feature flag `failure_path_reflection_enabled` (default `False` until validated; flip to `True` after 1 week of observation) | Safe rollout | Tier-3: Haiku |

Steps 1–7 of Phase 1 constitute the MVP (detection, emission, indexing). Steps 8–11 add the feedback, visualisation, and operational polish. Phase 2 is strictly additive.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| `ErrorPatternCluster`, `ErrorPatternDetectedEvent`, stream/cg constants | `src/personal_agent/events/models.py` | Infrastructure |
| `get_error_events`, `get_error_patterns` | `src/personal_agent/telemetry/queries.py` | Observation |
| `ErrorMonitor` scan orchestrator, clustering, dual-write | `src/personal_agent/telemetry/error_monitor.py` | Observation |
| `ErrorMonitorConsumer` (bus subscription) | `src/personal_agent/events/consumers/error_monitor.py` | Observation |
| `build_error_pattern_captain_log_handler` | `src/personal_agent/events/pipeline_handlers.py` | Observation |
| Captain's Log entry construction, scope derivation | inside `build_error_pattern_captain_log_handler` | Observation |
| `_extract_failure_excerpt` + `FailureExcerpt` | `src/personal_agent/captains_log/reflection.py` | Observation |
| `GenerateReflection` signature extension | `src/personal_agent/captains_log/reflection_dspy.py` | Observation |
| Config flags | `src/personal_agent/config/settings.py` | Infrastructure |

All components live in the Observation Layer. No Execution Layer module (gateway, orchestrator, tools) imports the error monitor. Producers reach ES through structlog; the monitor reads ES. This is consistent with ADR-0043's dependency direction rule.

---

## Open Questions

These are unresolved at ADR acceptance time and will be answered during implementation:

1. **ES aggregation shape.** Is a composite aggregation over `(component, event, error_type)` more efficient than three terms aggregations with sub-aggregations, given the `agent-logs-*` index mapping? Implementation will benchmark both on a representative 24 h sample.

2. **`error_type` extraction from log records.** The structured `exception` field added by `ElasticsearchHandler` is a full traceback string. Is the "last frame, split on `:`" heuristic robust across Python 3.11 / 3.12 traceback formats, and across re-raised exceptions? Implementation will add a unit test fixture per exception shape.

3. **Minimum occurrences threshold (5) calibration.** 5/24 h is a guess based on typical error rates in the current deployment. After 30 days of Phase 1 data, revisit — some patterns may need `≥ 3`, others `≥ 10`. Threshold is a settings field so calibration is a config change.

4. **Phase 2 activation gate.** Should Phase 2 fire on every errored task, or only when the trace final state is `"FAILED"`? The latter is narrower (and probably correct), but the former catches degraded-but-completed tasks. Recommendation: start with `had_errors=True` regardless of final state; narrow if signal quality is poor.

5. **Dead-letter self-loop.** A failed `cg:error-monitor` handler routes to the DLQ, which itself emits `dead_letter_routed` (a WARNING the monitor watches). The D1 out-of-scope filter catches this — verify with an explicit test that the monitor does not re-detect its own DLQ routes.

---

## Dedicated Linear Project — Error Pattern Monitoring

Error-pattern anomalies land in a dedicated Linear project named **"Error Pattern Monitoring"** (already created 2026-04-22 per FRE-244).

### Project configuration

| Field | Value |
|-------|-------|
| Project name | Error Pattern Monitoring |
| Team | FrenchForest |
| Default issue state | Needs Approval |
| Labels on creation | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping | `occurrences ≥ 50` → High; `occurrences ≥ 20` → Normal; else Low |

### Issue format

```
Title: [Error: <event_name>] <occurrences>x in <component> (<window_hours>h)
  e.g. "[Error: fetch_url_timeout] 47x in tools.fetch_url (24h)"

Body:
  ## Error pattern summary
  Component:     tools.fetch_url
  Event:         fetch_url_timeout
  Error type:    TimeoutError
  Level:         ERROR
  Occurrences:   47 over the last 24 hours
  First seen:    2026-04-16T07:12:44Z
  Last seen:     2026-04-23T01:03:17Z
  Fingerprint:   f1a9c0e2b3d74f8a
  Seen count:    4 (pattern has fired 4 consolidations)

  ## Representative traces (sample)
  - trace_id_1
  - trace_id_2
  - trace_id_3

  ## Representative messages (sample)
  - "Read timeout after 10s"
  - "Connection reset by peer"
  - "SSL handshake failed"

  ## Proposed action
  Investigate whether `tools.fetch_url` needs a shorter default timeout, a
  retry policy, or better error surfacing. See the trace_ids above for the
  immediate cause; consult the "Agent Reliability" Kibana dashboard for the
  full timeline.

  ## Phase 2 suggestion (when enabled)
  (populated by failure-path reflection when that pipeline has observed
  a representative failure)
```

### Feedback labels (inherited from ADR-0040)

| Label | Meaning for error patterns |
|-------|---------------------------|
| Approved | Proceed with the proposed fix; human implements |
| Rejected | Pattern is acceptable / expected; suppress fingerprint for 30 days |
| Deepen | Re-run ES query with deeper context; post refined proposal as comment |
| Too Vague | Refined proposal with more specific sample traces and distinct messages |
| Defer | Re-evaluate on next scan; no suppression |

---

## End State — What Exists, What Is Automated, What Is Visible

### After Phase 1 MVP (Implementation Priority steps 1–7)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| `ErrorPatternCluster`, `ErrorPatternDetectedEvent`, stream/cg constants | `cg:error-monitor` runs on every `consolidation.completed` event | Per-fingerprint JSON files in `telemetry/error_patterns/` |
| `telemetry/error_monitor.py` with clustering + dual-write | Dual-write: file + bus publish | Bus events on `stream:errors.pattern_detected` (redis-cli XRANGE) |
| `get_error_events` / `get_error_patterns` in `TelemetryQueries` | — | Raw error events queryable via existing ES indices |

Human action required: none (other than standard operational monitoring). No new Linear issues yet (CL emission ships at step 6).

### After Phase 1 complete (Implementation Priority steps 8–11)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| CL handler subscribed to `stream:errors.pattern_detected` | Every pattern becomes a `CaptainLogEntry(CONFIG_PROPOSAL, RELIABILITY)` | Entries in `telemetry/captains_log/` with error-pattern fingerprints |
| Kibana panels: error-pattern top-N + timeline | Promotion → Linear "Error Pattern Monitoring" after `seen_count ≥ 3`, `age ≥ 7 d` | Linear issues with sample traces and proposed actions |
| Suppression, approval, deepen handlers inherited from ADR-0040 | Label → behavioural change (suppress / approve / re-analyse) automatic | Kibana dashboard panel shows cluster distribution |

Human action required: review and label Linear issues in "Error Pattern Monitoring". Everything else is automatic.

### After Phase 2 (Failure-Path Reflection, Implementation Priority Phase 2 steps 1–6)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| `FailureExcerpt`, `FailedToolCall`, `_extract_failure_excerpt()` | Per-task reflection emits `failure_path_fix_what` + `failure_path_fix_location` when trace had errors | `potential_implementation` field populated on CL entries |
| `GenerateReflection` signature with failure-excerpt fields | Surgical fix suggestions flow through the same promotion → Linear pipeline | Linear issues for one-off failures now include a surgical edit suggestion |
| Feature flag `failure_path_reflection_enabled` (default False, flipped after 1 week of validation) | Dedup + suppression inherited from ADR-0030 / ADR-0040 | Phase 2 suggestions appear alongside Phase 1 cluster-level proposals |

Human action required: evaluate whether Phase 2 suggestions are surgical and useful after 1 week; if yes, flip default to `True`.

---

## Loop Completeness Criteria

The stream is verified closed and working when, over a trailing 14-day window, all five hold:

1. **Production**: `count(stream:errors.pattern_detected XLEN) ≥ 1` per week under normal load.
2. **Ingestion**: `count(telemetry/error_patterns/*.json)` grows monotonically; every fingerprint on disk has a matching recent entry in `telemetry/captains_log/`.
3. **Promotion**: at least one `ErrorPatternDetected → CaptainLogEntry → Linear issue` full trip has occurred, verifiable by tracing one `fingerprint` end-to-end.
4. **Feedback**: at least one Linear label (Approved / Rejected / Deepen / Too Vague / Defer) has been processed by `FeedbackPoller`, producing a `FeedbackReceivedEvent`.
5. **Suppression**: after a `Rejected` label, the next scan that would have re-emitted the same fingerprint finds it suppressed (log line: `captains_log_proposal_suppressed`), and no new entry is written.

If (1) holds but (3) does not, the promotion gate (`seen_count ≥ 3`, `age ≥ 7 d`) is tuned too conservatively for the pattern rate; tune in config, not in this ADR.

---

## Feedback Stream ADR Template — Compliance Checklist

Per the Feedback Stream ADR Template established in ADR-0053:

- [x] **1. Stream identity** — Level 3 observability; Observation Layer; depends on ADR-0041/0043/0053/0054
- [x] **2. Source** — ERROR + WARNING-allowlist structlog events; rolling 24 h; triggered per `consolidation.completed`
- [x] **3. Collection mechanism** — ES composite aggregation via `cg:error-monitor`; fallback behaviour on Redis/ES outage documented
- [x] **4. Processing algorithm** — cluster by `(component, event_name, error_type)` with minimum occurrences threshold
- [x] **5. Signal produced** — `ErrorPatternDetectedEvent` on bus; per-fingerprint JSON file on disk; `CaptainLogEntry(RELIABILITY)` via bus handler; fingerprint dedup policy
- [x] **6. Full automation cycle** — D8 traces the 8-step loop end to end
- [x] **7. Human review interface** — "Error Pattern Monitoring" Linear project; issue format; label semantics; SLA inherited
- [x] **8. End state table** — Phase 1 MVP, Phase 1 complete, Phase 2 complete
- [x] **9. Loop completeness criteria** — 5-point check, evaluation window 14 days

---

## References

- FRE-244: Draft ADR — Error Pattern Monitoring (this ADR)
- ADR-0041: Event Bus via Redis Streams — transport
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0053: Gate Feedback-Loop Monitoring Framework — establishes the Feedback Stream ADR Template this ADR follows
- ADR-0054: Feedback Stream Bus Convention — dual-write, stream naming, `EventBase` contract fields
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — surfacing channel, fingerprint dedup
- ADR-0040: Linear as Async Feedback Channel — label semantics, suppression
- ADR-0047: Context Management & Observability — related ADR for Stream 7 (compaction quality)
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — feedback-stream catalogue (updated to reference this ADR)
- NousResearch GEPA — *Genetic-Pareto Prompt Evolution*, ICLR 2026 Oral — inspiration for Phase 2 failure-path reflection (no code dependency)
- `src/personal_agent/telemetry/queries.py` — extended with error aggregation methods
- `src/personal_agent/telemetry/es_handler.py` — the structlog → ES pipeline error events travel through today
- `src/personal_agent/captains_log/reflection.py` / `reflection_dspy.py` — Phase 2 extension target
- `src/personal_agent/events/models.py` / `events/pipeline_handlers.py` / `service/app.py` — integration points for Phase 1
