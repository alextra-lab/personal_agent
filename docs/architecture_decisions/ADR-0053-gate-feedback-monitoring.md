# ADR-0053: Deterministic Gate Feedback-Loop Monitoring Framework

**Status**: Approved
**Date**: 2026-04-22
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Separation)
**Related**: ADR-0030 (Captain's Log & Self-Improvement Pipeline), ADR-0039 (Proactive Memory), ADR-0047 (Context Management & Observability)
**Enables**: FRE-234 (Trigger Effectiveness Analysis — first downstream consumer)
**Linear Issue**: FRE-233

---

## Context

### The gateway makes seven deterministic decisions per request — none are monitored

The Cognitive Architecture Redesign introduced a **pre-LLM request gateway**: a seven-stage sequential pipeline that runs before every orchestrator invocation. Each stage makes a deterministic, rule-based decision:

| Stage | Gate | Decision |
|-------|------|----------|
| 3 | Governance | Is expansion permitted? What is the budget? |
| 4 | Intent | What is the user trying to do? How complex? |
| 4b | Recall Controller | Was a conversational message actually a memory lookup? |
| 5 | Decomposition | SINGLE / HYBRID / DECOMPOSE / DELEGATE? |
| 6 | Context Assembly | What memory and session history to include? |
| 7 | Budget | Does the assembled context fit? What gets dropped? |

These gates are the most consequential code in the system. A wrong intent classification propagates immutably through decomposition, context assembly, and budget trimming — affecting every word the LLM sees. A miscalibrated decomposition matrix routes tasks to external agents that the user expected the system to handle directly. An over-aggressive budget policy drops session context the user assumed was still present.

Today there is **no systematic way to know whether any gate is working well**. The following questions cannot be answered from current telemetry:

- Is the intent classifier frequently uncertain (confidence < 0.75)? Which query types fall into the gap?
- How often does governance block expansion? Is `expansion_denied` a signal of system stress or a miscalibrated mode threshold?
- What percentage of requests go to `DELEGATE`? Is the coding-pattern detection too broad, routing tasks that should stay local?
- How long does context assembly take? Is the Neo4j memory query a latency bottleneck at p90?
- How often does the budget trimmer fire? What does it drop most frequently?

### Current instrumentation is rich but non-aggregatable

The pipeline emits substantial structured logging today:

```
gateway_output (INFO)       — full stage outputs, trace_id — fired once per request
intent_classified (INFO)    — task_type, complexity, confidence, signals
decomposition_assessed (INFO) — strategy, reason, trace_id (logged twice: bug)
context_budget_applied (INFO) — trimmed, overflow_action, token counts
context.compaction (INFO)   — CompactionRecord per trimming phase
recall_reclassified (INFO)  — reclassification events
```

All of these events land in Elasticsearch via structlog. But there is no aggregation layer querying them:

- `TelemetryQueries` has no gateway-aware methods — intent distributions, confidence percentiles, and strategy frequencies cannot be queried programmatically.
- `RequestCompletedEvent` carries a `RequestTimer` snapshot but no gateway data — downstream consumers (`cg:es-indexer`, `cg:insights`) cannot react to gateway decisions.
- No per-stage wall-clock timing exists. Stage 6 (context assembly) is the only variable-latency stage, dominated by a Neo4j query that can run 5–50 ms, but this latency cannot be isolated from aggregate request timing.

### The "tests for gates" insight

Software code has tests. Tests don't run in production but they give developers confidence that the code does what it's supposed to. The monitoring framework is the analog for gates: it runs with every production request and gives the system confidence that gates are making the right decisions.

The deeper opportunity: if monitoring were a **byproduct of gate implementation** — declared structurally rather than bolted on after the fact — every new gate would come with its feedback loop as part of the contract. This ADR designs toward that property.

### Three-layer context

Under ADR-0043 (Three-Layer Separation):

- **Execution Layer** owns the gateway — the stages and their decisions.
- **Observation Layer** owns what gets monitored — traces, metrics, Captain's Log entries, insights.
- The event bus (ADR-0041) is the transport from Execution to Observation.

The monitoring framework sits at this boundary: it is an Execution Layer instrumentation mechanism whose data flows into the Observation Layer. It does not change execution behavior in Phase 1.

---

## Decision Drivers

1. **No new infrastructure.** The event bus (Redis Streams), telemetry (structlog → Elasticsearch), and Captain's Log all exist. The framework must use these rather than introducing new persistence or transport.
2. **Per-invocation data, not just aggregates.** Monitoring needs to capture the specific inputs and outputs of each gate invocation — not just that a gate ran, but what decision it made and why.
3. **Per-stage latency.** Stage 6 is the latency hotspot but is currently invisible in isolation. Per-stage timing is a first-class requirement.
4. **Framework property.** New gates added to the pipeline should come with monitoring as a structural requirement, not an optional afterthought.
5. **Observation Layer receives, Execution Layer does not change.** Gate decisions must not be altered by monitoring in Phase 1. Monitoring is read-only.
6. **Self-improvement pipeline integration.** Anomalies detected by the monitoring framework should surface through the Captain's Log promotion pipeline — the same path that all other self-improvement signals take.

---

## Decision

### D1: Monitoring Axioms — Three Axes per Gate

Each gate is characterized on three axes:

**Correctness** — did the gate fire when it should, and not fire when it shouldn't?
This is the hardest axis to measure directly (the ground truth classification for "was this intent right?" requires a human label). The framework uses **proxy signals** that suggest correctness problems without requiring labels: low confidence, high reclassification rates, unexpected strategy distribution drift.

**Efficiency** — how much does the gate cost?
Wall-clock latency and, where applicable, external resource cost (Neo4j query, token estimation passes).

**Fluidity** — does the gate produce results that feel natural to the user?
This axis is user-perceived but has structural proxies: high `DELEGATE` rates feel like deflection; high budget trimming rates feel like forgetfulness; high `expansion_denied` rates feel like capability limits.

Per-gate monitoring targets:

| Gate | Correctness proxy | Efficiency SLI | Fluidity proxy |
|------|------------------|----------------|----------------|
| Stage 3 Governance | `expansion_denied` rate sustained > 80% → mode calibration drift | < 0.5 ms | `expansion_denied` / `expansion_budget_zero` rate |
| Stage 4 Intent | Confidence p50 < 0.75 sustained → pattern gaps; reclassification rate > 15% → Stage 4/4b overlap | < 1 ms | — |
| Stage 4b Recall | False-positive rate (cue fired, zero candidates) > 30% → over-triggering | < 2 ms | Reclassification rate |
| Stage 5 Decomposition | `DELEGATE` rate > 50% sustained → coding patterns too broad; `expansion_denied` override > 40% → budget calibration | < 0.5 ms | `DELEGATE` rate, expansion override rate |
| Stage 6 Context | `memory_unavailable` degradation rate > 5% → Neo4j health; memory query p90 > 30 ms → performance | < 20 ms p90 | Memory hit/miss rate |
| Stage 7 Budget | Trimming rate > 30% sustained → context budget too small or sessions too large | < 2 ms | Trimming rate, `overflow_action` distribution |

SLIs are soft limits for Captain's Log surfacing — violations at sustained rates trigger anomaly reports, not alerts.

---

### D2: Declaration Mechanism — `GateMonitor` with Explicit `record_gate()` Calls

The pipeline instantiates a **`GateMonitor`** at the start of `run_gateway_pipeline()`. After each stage completes, the pipeline calls `monitor.record_gate()` with the stage name, a lightweight input snapshot, a lightweight output snapshot, and the elapsed wall-clock time. At pipeline exit, `monitor.to_gate_summary()` produces a `GateSummary`.

This is the explicit, boring choice. It follows the `RequestTimer` pattern exactly (`RequestTimer` is the span recorder for orchestrator phases — `GateMonitor` is the same concept applied to gateway stages). No decorators, no metaclasses, no implicit interception.

```python
# Sketch — authoritative shapes defined in D3

t0 = monotonic()
intent = classify_intent(user_message)
monitor.record_gate(
    gate="intent",
    duration_ms=(monotonic() - t0) * 1000,
    inputs={"message_len": len(user_message)},
    outputs={
        "task_type": intent.task_type.value,
        "complexity": intent.complexity.value,
        "confidence": intent.confidence,
    },
)
```

**Why this makes monitoring a structural property of gates:**

New gates added to the pipeline must have a corresponding `record_gate()` call in `pipeline.py` — the `GateSummary` will not include them otherwise. The ADR mandates that any new stage must ship with its `record_gate()` call. A code-review checklist item (`ADR-0053: new stage has record_gate`) makes this enforceable without compiler assistance. This is the "framework opportunity" realized through convention + review, not through magic.

**Why not alternatives** — see Alternatives Considered section.

---

### D3: Data Model — Three Collection Layers

Gate monitoring data flows through three layers, each suited to a different consumer timescale.

#### Layer A — `GateSummary` in `GatewayOutput` (per-request, synchronous)

A new `GateSpan` type captures one stage's worth of monitoring data:

```python
@dataclass(frozen=True)
class GateSpan:
    """Monitoring record for one gateway stage invocation."""
    gate: str                       # "governance", "intent", "recall", "decomposition",
                                    # "context_assembly", "budget"
    duration_ms: float              # wall-clock time, monotonic
    inputs: dict[str, Any]          # lightweight snapshot (lengths, not full values)
    outputs: dict[str, Any]         # key decision fields (task_type, strategy, etc.)
    error: str | None = None        # exception type if stage raised, None otherwise
```

A `GateSummary` collects all spans for a request:

```python
@dataclass(frozen=True)
class GateSummary:
    """Collected monitoring data for all gateway stages in one request."""
    spans: tuple[GateSpan, ...]     # ordered by execution sequence
    total_gateway_ms: float         # sum of all span durations
    slowest_gate: str               # gate name with highest duration_ms
    has_degraded: bool              # any stage raised / degraded gracefully
```

`GatewayOutput` gains an optional field:

```python
@dataclass(frozen=True)
class GatewayOutput:
    # ... existing fields unchanged ...
    monitoring: GateSummary | None = None   # None if GateMonitor disabled via feature flag
```

`GateSummary` is cheap: frozen dataclass, no I/O, sub-microsecond construction. The total monitoring overhead per request is dominated by the six `monotonic()` calls (nanoseconds each) plus one `GateSummary` construction.

#### Layer B — `gateway` key in `RequestCompletedEvent.trace_summary` (per-request, async)

`RequestCompletedEvent` already carries a `trace_summary` dict from `RequestTimer`. The service publishes this event after every `/chat` response. This ADR extends the dict with a `gateway` key:

```python
# Added to RequestCompletedEvent.trace_summary by the service layer
{
  "gateway": {
    "task_type": "analysis",
    "complexity": "moderate",
    "confidence": 0.82,
    "strategy": "hybrid",
    "expansion_permitted": True,
    "expansion_budget": 3,
    "budget_trimmed": False,
    "overflow_action": None,
    "memory_hit": True,
    "memory_degraded": False,
    "recall_reclassified": False,
    "total_gateway_ms": 14.2,
    "slowest_gate": "context_assembly",
    "degraded_stages": []
  }
}
```

No new event types. No new streams. All existing consumers (`cg:es-indexer`, `cg:insights`) receive gateway data automatically at zero additional cost. The ES indexer writes `trace_summary.gateway.*` fields into Elasticsearch, making them immediately queryable.

The service layer (`service/app.py`) extracts the `GateSummary` from `GatewayOutput.monitoring` and includes it when building the `RequestCompletedEvent`. When `monitoring` is `None` (feature flag disabled), the `gateway` key is omitted from `trace_summary`.

#### Layer C — `TelemetryQueries` gateway aggregations (programmatic analytics)

Extend `TelemetryQueries` with six new methods operating on the `trace_summary.gateway.*` fields indexed by the ES indexer:

| Method | Returns | Use |
|--------|---------|-----|
| `get_intent_distribution(days)` | `dict[TaskType, int]` | TaskType frequency over rolling window |
| `get_strategy_distribution(days)` | `dict[DecompositionStrategy, int]` | Strategy frequency over rolling window |
| `get_confidence_percentiles(days)` | `dict[str, float]` | p50/p75/p90/p95 confidence scores |
| `get_gate_latency_percentiles(gate, days)` | `dict[str, float]` | p50/p75/p90/p95 per stage |
| `get_degradation_rate(stage, days)` | `float` | Degraded-stage fraction |
| `get_gateway_health_report(days)` | `GatewayHealthReport` | Consolidated summary for agent queries |

`GatewayHealthReport` is a frozen dataclass designed to be convertible to a human-readable answer. The agent can call a native `query_gateway_health` tool backed by this method to answer "how are your routing decisions?".

---

### D4: Surfacing Channel

**Primary: Kibana.** The `trace_summary.gateway.*` fields indexed by the ES indexer are immediately queryable in Kibana. The Kibana dashboard extended for this ADR adds a **"Gateway Health" panel** with:
- Intent distribution pie chart (7 TaskTypes over trailing 7 days)
- Strategy distribution (4 DecompositionStrategies over trailing 7 days)
- Confidence histogram (p50/p75/p90 over trailing 7 days)
- Per-gate latency percentiles (6 stages, p90 over trailing 7 days)
- Degradation rate timeseries (per stage, 30-day view)
- Budget trimming rate (overflow_action distribution, trailing 30 days)

**Secondary: Captain's Log.** A new background consumer `cg:gateway-monitor` subscribes to `stream:request.completed`. It maintains a rolling in-memory window of gateway summaries (1,000 most recent requests). On a configurable schedule (default: evaluate every 100 requests), it computes the monitoring axiom thresholds from D1 and emits `CaptainLogEntry` objects when anomalies are sustained:

```python
# Example anomaly emission
CaptainLogEntry(
    type=CaptainLogEntryType.config_proposal,
    title="Intent confidence p50 below threshold for 3 days",
    rationale="Stage 4 (intent) confidence p50 = 0.71 over last 500 requests. "
              "Threshold is 0.75. Low confidence suggests pattern gaps for query types "
              "falling into the default CONVERSATIONAL bucket.",
    proposed_change=ProposedChange(
        what="Review and extend intent classification patterns for underperforming query types",
        why="Sustained low confidence propagates downstream to decomposition and context assembly",
        how="Analyze gateway_output logs for low-confidence signals; add patterns for gap categories",
        category=ChangeCategory.observability,
        scope=ChangeScope.orchestrator,
    ),
    metrics_structured=[
        Metric(name="intent_confidence_p50", value=0.71, unit="score"),
        Metric(name="request_count_window", value=500, unit="count"),
    ],
    telemetry_refs=[...]  # linked trace_ids from low-confidence requests
)
```

Fingerprinting via `sha256(category:scope:normalized_what)[:16]` deduplicates repeated anomaly entries. The existing promotion pipeline (ADR-0030, ADR-0040) handles escalation to Linear after the entry reaches `min_seen_count=3` and `min_age=7 days`.

**No new UI surface** is required in Phase 1. The agent can answer "how are your routing decisions?" using `TelemetryQueries.get_gateway_health_report()` via a native tool.

---

### D5: Feedback Loop — Read-Only in Phase 1, Adaptive in Phase 2

**Phase 1 (this ADR):** `GateMonitor` is purely observational. `GateSummary` flows through `GatewayOutput` but is never read by any execution-path code. The monitor does not influence governance mode, does not adjust intent patterns, and does not change decomposition rules. Monitoring is a passenger, not a driver.

**Phase 2 (follow-on ADR):** Threshold-triggered Captain's Log surfacing. When `cg:gateway-monitor` detects sustained anomalies, it creates Captain's Log proposals that, after promotion, become Linear issues for human review. A human decides whether to adjust a pattern, threshold, or rule. The agent does not self-modify gate parameters.

**Phase 3 (Slice 3 scope):** Adaptive parameter tuning. Gate parameters (intent classification confidence thresholds, decomposition matrix weights, budget trimming aggressiveness) become optimization targets for the self-improvement loop. Not in scope for this ADR. The monitoring data collected in Phases 1 and 2 is the training signal that Phase 3 requires.

This staged approach avoids premature automation. Phase 1 gives us visibility and data. Phase 2 gives us human-reviewed insights. Phase 3 gives us automated improvement — but only after Phases 1 and 2 have validated that the monitoring signals are reliable.

---

### D6: Scope Boundary

This ADR covers the **six active stages of the pre-LLM gateway** (Stages 3–7, including 4b). It does not cover:

- Stage 1 (Security) — stub, no decision to monitor.
- Stage 2 (Session) — handled by the caller, not inside the gateway.
- Orchestrator-level routing decisions (`is_memory_recall_query()`, `step_llm_call()` tool filtering) — Execution Layer, but outside the gateway. FRE-234 may extend scope here.
- Brainstem mode transitions — Observation Layer concern, handled by existing `MODE_TRANSITION` events.
- Proactive memory decisions (ADR-0039) — the `suggest_relevant()` path generates its own `proactive_memory_suggest_*` events and is out of scope here.

**FRE-234** (trigger effectiveness / fluidity analysis) is the first downstream consumer of the data produced by this framework. Its implementation will validate that the Layer B and Layer C surfaces (gateway_summary in events, TelemetryQueries extensions) provide the data it needs. If FRE-234 requires additional fields or new aggregation methods, those changes feed back as an addendum to this ADR.

---

## Alternatives Considered

### Declaration Mechanism

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Decorator `@monitor_gate(name="intent")` | Wraps each stage function; auto-captures timing and return value; no changes to call sites in pipeline.py | Rejected — the decorator intercepts without access to `trace_id` or session context; harder to test; opaque; conflicts with the project's preference for explicit code over magic |
| B. `Gate` Protocol / base class | Each stage becomes a `Gate` object with `gate_spec()` and `execute()` methods; monitoring is part of the contract | Rejected — requires refactoring all stage functions into classes; invasive for a monitoring ADR; the stages are pure functions by design |
| C. Inline `async with monitor.span("intent") as span:` | Context manager auto-times the block; caller sets outputs via `span.set_outputs()` | Viable — clean, follows `RequestTimer` pattern. Rejected only because the `record_gate()` variant below is more explicit about timing ownership |
| **D. Explicit `monitor.record_gate()` calls** | After each stage, the pipeline calls `record_gate(gate, duration_ms, inputs, outputs)`; timing managed by caller | **Selected** — maximum explicitness; timing is accurate and caller-controlled; exact mirror of how `RequestTimer.start_span()` / `end_span()` works; no new protocol; fully testable |

### Event Transport

| Option | Description | Verdict |
|--------|-------------|---------|
| A. New `gateway.decision` event on a new stream | Emit a dedicated event per request; new consumer group for monitoring | Rejected — adds a new stream and event type when the existing `stream:request.completed` already reaches all relevant consumers; over-eventing |
| **B. Extend `RequestCompletedEvent.trace_summary`** | Add `gateway` key to existing event; zero new event types | **Selected** — the ES indexer and insights consumers already subscribe to this stream; gateway data appears in their existing data flow with no additional subscription management |
| C. Direct structlog → ES | The existing `gateway_output` log event already goes to ES; rely on it | Rejected as the sole mechanism — log events are not machine-consumable without ES queries; `RequestCompletedEvent` is the right level of abstraction for downstream consumers; the existing log serves as a redundant diagnostic signal |
| D. Prometheus / StatsD counters | Introduce a metrics library for real-time counters and histograms | Rejected — no Prometheus/StatsD infrastructure exists; Elasticsearch already serves the analytics role; adding a separate metrics system doubles the observability surface |

### Surfacing Channel

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Kibana dashboards only | Surface gateway health through existing Kibana; no programmatic access | Rejected as sole channel — Kibana is for humans; programmatic access (`TelemetryQueries`) is required for the agent to self-report and for `cg:gateway-monitor` to evaluate thresholds |
| B. New UI panel | Dedicated gateway health panel in the PWA (ADR-0048) | Rejected for Phase 1 — introduces frontend scope; Kibana serves this need at zero cost |
| **C. Kibana + Captain's Log + TelemetryQueries** | Three-tier surfacing: visual (Kibana), narrative (Captain's Log), programmatic (TelemetryQueries) | **Selected** — each tier serves a different consumer: operators see Kibana, the agent surfaces Captain's Log proposals, and the `cg:gateway-monitor` consumer uses TelemetryQueries for threshold evaluation |

---

## Consequences

### Positive

- **Gates become observable.** For the first time, the system can answer: what percentage of requests are classified as `DELEGATION`? Is confidence trending down? How often does budget trimming fire?
- **Per-stage latency is isolated.** Stage 6 (context assembly) has been the suspected latency driver; monitoring will confirm or refute this.
- **Framework property realized.** New gateway stages must include a `record_gate()` call — monitoring is a structural requirement of adding a gate, not an afterthought.
- **Zero new infrastructure.** The event bus, Elasticsearch, structlog, and Captain's Log all exist and are reused unchanged.
- **FRE-234 unblocked.** Trigger effectiveness analysis depends on the data surfaces this ADR produces.
- **Self-improvement pipeline integration.** Anomalies follow the same Captain's Log → promotion → Linear path that all other improvement signals take — no new escalation mechanism.

### Negative

- **`GatewayOutput` grows a field.** `monitoring: GateSummary | None` adds one field to a frozen dataclass that every downstream code path reads. Type-safe, but a schema change.
- **`RequestCompletedEvent.trace_summary` shape is extended.** Consumers reading this event must handle the presence or absence of the `gateway` key. Existing consumers (`cg:es-indexer`, `cg:session-writer`) are key-agnostic — they don't inspect `trace_summary` contents — so this is a non-breaking extension.
- **`pipeline.py` gains six timing blocks.** Each stage call site grows by 3–4 lines (t0, record_gate). The pipeline file becomes slightly more verbose but remains easy to read.
- **`cg:gateway-monitor` is a new consumer.** One new consumer group must be registered and started. It is stateless and lightweight (in-memory rolling window).

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Monitoring overhead adds latency to the gateway hot path | Low — six `monotonic()` calls + frozen dataclass construction is sub-microsecond | Benchmark before ship; feature-flag (`AGENT_GATE_MONITORING_ENABLED`, default `True`) allows instant disable |
| `GateMonitor` produces wrong timing if stage raises an exception | Medium — exception exits the timing block early | The `record_gate()` call goes in a `finally:` block; errors are recorded with `error=type(e).__name__` and `duration_ms` reflects time-to-error |
| `cg:gateway-monitor` rolling window consumes too much memory at high request rates | Low — 1,000 `GateSummary` objects; each is O(6 spans × ~200 bytes) ≈ 1.2 MB | Cap configurable; clear window on restart; no disk persistence required |
| Monitoring data lags reality at low request volumes | Medium — percentile and rate calculations are unreliable with < 50 samples | Require minimum sample count before emitting anomaly reports; display confidence interval in health report |
| `RequestCompletedEvent.trace_summary` schema drift | Low — dict field, not typed in the event model | Add `gateway_summary` as a typed `Optional[dict[str, Any]]` field on the event model itself (not nested in the free-form dict), giving mypy visibility |

---

## Implementation Priority

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | `GateSpan`, `GateSummary`, `GateMonitor` types in `request_gateway/monitoring.py` | Foundation; all subsequent work depends on these types | Tier-2: Sonnet |
| 2 | Per-stage `monitor.record_gate()` calls in `pipeline.py` | Gate instrumentation; makes monitoring data available | Tier-2: Sonnet |
| 3 | `GatewayOutput.monitoring: GateSummary \| None` field | Propagates data to executor and service layer | Tier-2: Sonnet |
| 4 | `gateway` key construction in `service/app.py` before `RequestCompletedEvent` publish | Connects monitoring data to event bus | Tier-2: Sonnet |
| 5 | Feature flag `AGENT_GATE_MONITORING_ENABLED` in `config/` | Safe rollout | Tier-3: Haiku |
| 6 | Unit tests: `GateMonitor.record_gate()`, `GateSummary.to_gateway_summary_dict()` | Quality gate | Tier-2: Sonnet |
| 7 | `TelemetryQueries` gateway aggregation methods (Layer C) | Analytics; unblocks FRE-234 | Tier-2: Sonnet |
| 8 | `cg:gateway-monitor` consumer + anomaly detection + Captain's Log emission | Observation Layer feedback loop | Tier-2: Sonnet |
| 9 | Kibana "Gateway Health" dashboard panel | Visualization | Tier-3: Haiku |
| 10 | Native tool `query_gateway_health` backed by `TelemetryQueries` | Agent self-reporting | Tier-2: Sonnet |

Steps 1–6 constitute the MVP: monitoring data flows, is time-accurate, and is indexed to ES. Steps 7–10 add the analytics, feedback, and surfacing layers.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| `GateSpan`, `GateSummary`, `GateMonitor` | `src/personal_agent/request_gateway/monitoring.py` | Execution Layer |
| `record_gate()` call sites | `src/personal_agent/request_gateway/pipeline.py` | Execution Layer |
| `GatewayOutput.monitoring` field | `src/personal_agent/request_gateway/types.py` | Execution Layer |
| `gateway` key assembly | `src/personal_agent/service/app.py` | Interface Layer |
| `TelemetryQueries` extensions | `src/personal_agent/telemetry/queries.py` | Observation Layer |
| `cg:gateway-monitor` consumer | `src/personal_agent/events/gateway_monitor.py` | Observation Layer |
| Anomaly → Captain's Log emission | `src/personal_agent/captains_log/` (new handler) | Observation Layer |

All Execution Layer components depend downward (no imports from Observation Layer). The `cg:gateway-monitor` consumer depends on the event bus (infrastructure) and Captain's Log (Observation Layer) — never on request_gateway internals. This is consistent with ADR-0043's dependency direction rule.

---

## Open Questions

These are unresolved at ADR acceptance time. Each will be answered during implementation:

1. **`GatewayOutput.monitoring` or `GateSummary` embedded directly?** The field could be `GateSummary | None` or `dict[str, Any] | None`. The typed frozen dataclass approach is preferred for mypy coverage, but it requires importing `monitoring.py` into `types.py`. If that creates circular imports, fall back to a typed dict.

2. **Should Stage 4b (Recall Controller) record `record_gate()` only when it runs a scan (not when it returns early)?** The early-return path (non-CONVERSATIONAL intent) is a correct skip, not a gate decision. Recording it would inflate "gate invocations" counts. Recommendation: only record when Gate 2 (cue matching) fires.

3. **`GatewayHealthReport` schema.** The shape of the report returned by `TelemetryQueries.get_gateway_health_report()` is left to implementation — it must be human-readable (for agent surfacing) and machine-parseable (for `cg:gateway-monitor` threshold evaluation).

4. **Anomaly threshold tuning.** The thresholds in D1 are initial values derived from reasoning, not from observed data. After 30 days of data collection in Phase 1, thresholds should be reviewed against actual distributions and adjusted. This is expected.

---

## D7: Dedicated Linear Project — Gate Health Monitoring

Gate monitoring anomalies land in a **dedicated Linear project** named **"Gate Health Monitoring"**, separate from the general FrenchForest backlog and from the Self-Improvement Pipeline project (which handles capability proposals). This separation allows:

- Operators to track gateway health issues independently from feature work
- The promotion pipeline to target a specific project, not just a team
- Clear triage: gate anomaly issues have a distinct context and a distinct owner (gateway behaviour) vs. capability gaps or knowledge proposals

### Project configuration

| Field | Value |
|-------|-------|
| Project name | Gate Health Monitoring |
| Team | FrenchForest |
| Default issue state | Needs Approval |
| Labels applied on creation | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping | `seen_count ≥ 10` → High; `seen_count ≥ 5` → Normal; else Low |

### Issue format

Each Linear issue created by `cg:gateway-monitor` → promotion pipeline follows this structure:

```
Title: [Gate: <gate_name>] <anomaly_description>
  e.g. "[Gate: intent] Confidence p50 below threshold (0.71) — 500 requests"

Body:
  ## Anomaly summary
  Gate: intent
  Metric: confidence_p50
  Observed: 0.71 (threshold: 0.75)
  Window: 500 requests over 3 days
  First seen: 2026-04-15
  Seen count: 4

  ## Monitoring axiom
  Correctness proxy: p50 confidence < 0.75 suggests pattern gaps for query types
  falling into the default CONVERSATIONAL bucket.

  ## Proposed action
  Review intent classification patterns (src/personal_agent/request_gateway/intent.py).
  Analyse gateway_output ES logs for signals field of low-confidence requests.
  Add or refine patterns for underperforming query types.

  ## Supporting metrics
  (Linked trace_ids for representative low-confidence requests)

  ## Fingerprint
  <sha256_16char>
```

### Feedback labels and their semantics (inherited from ADR-0040)

| Label | Meaning for gate anomalies |
|-------|---------------------------|
| Approved | Proceed with investigation and pattern adjustment |
| Rejected | Anomaly is acceptable / expected; suppress this fingerprint for 30 days |
| Deepen | FeedbackPoller triggers LLM re-analysis with deeper ES query and posts refined findings as comment |
| Too Vague | FeedbackPoller triggers refined proposal with more specific metric context |
| Defer | Re-evaluate after next evaluation phase (delay 14 days) |

---

## D8: Full Automation Cycle

This section traces the complete loop from gate execution to human review and back to system behaviour. No step is implicit.

```
1. Request arrives
   └─ run_gateway_pipeline() instantiates GateMonitor

2. Each stage executes
   └─ monitor.record_gate(gate, duration_ms, inputs, outputs)

3. Pipeline exits
   └─ GatewayOutput.monitoring = monitor.to_gate_summary()

4. Service layer (app.py) builds RequestCompletedEvent
   └─ trace_summary["gateway"] = gateway_summary_dict(gateway_output.monitoring)
   └─ publish to stream:request.completed

5. cg:es-indexer receives RequestCompletedEvent
   └─ indexes trace_summary.gateway.* fields to agent-logs-YYYY-MM-DD in ES
   (zero new code — existing indexer picks up new dict keys)

6. cg:gateway-monitor receives RequestCompletedEvent
   └─ appends GatewayDecision(task_type, strategy, confidence, …) to rolling window
   └─ every 100 events: evaluate all D1 monitoring axiom thresholds

7. Threshold breach detected (sustained anomaly)
   └─ build CaptainLogEntry(
         type=config_proposal,
         category=observability,
         scope=orchestrator,
         fingerprint=sha256(gate:metric:description)[:16]
      )
   └─ CaptainLogManager.save_entry()
      ├─ if fingerprint suppressed (rejected < 30 days ago) → discard silently
      ├─ if matching fingerprint on disk → increment seen_count, merge
      └─ else → write CL-YYYYMMDD-*.json, index to ES

8. consolidation.completed event fires (brainstem scheduler)
   └─ cg:promotion receives event
   └─ PromotionPipeline.scan_promotable_entries()
      ├─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 days
      ├─ checks: LinearClient issue budget not exceeded
      └─ creates Linear issue in "Gate Health Monitoring" project
         └─ publishes PromotionIssueCreatedEvent to stream:promotion.issue_created

9. Human receives Linear issue in "Gate Health Monitoring" project
   └─ reviews anomaly, supporting metrics, proposed action
   └─ applies label: Approved / Rejected / Deepen / Too Vague / Defer

10. FeedbackPoller (daily, configurable hour)
    └─ polls Linear for issues updated in last 3 days
    └─ dispatches to label handler:
       ├─ Rejected  → write suppression (30-day window); cancel issue
       │              fingerprint blocked in step 7 for 30 days
       ├─ Approved  → move to Approved state; implementation is manual
       ├─ Deepen    → LLM re-analysis (deeper ES query); post as comment; switch to Re-evaluated
       ├─ Too Vague → LLM refined proposal; post as comment; switch to Refined
       └─ Defer     → no suppression; issue remains; re-evaluated on next scan
    └─ publish FeedbackReceivedEvent to stream:feedback.received

11. cg:insights receives FeedbackReceivedEvent
    └─ records feedback signal for pattern analysis

12. cg:feedback receives FeedbackReceivedEvent
    └─ updates suppression state
    └─ next threshold breach for same fingerprint is discarded at step 7
```

**Loop closed.** A rejected gate anomaly is suppressed for 30 days. An approved anomaly creates a human-owned implementation task. Neither path requires manual intervention after the label is applied.

---

## D9: End State — What Exists, What Is Automated, What Is Visible

### After Phase 1 (steps 1–6 of Implementation Priority)

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| `GateSpan`, `GateSummary`, `GateMonitor` types in `request_gateway/monitoring.py` | `GateMonitor.record_gate()` called after every stage on every request | `gateway_output` structlog event (already indexed to ES) |
| `GatewayOutput.monitoring: GateSummary \| None` field | `gateway` key assembled and embedded in `RequestCompletedEvent.trace_summary` | `trace_summary.gateway.*` fields queryable in Kibana via existing log index |
| Feature flag `AGENT_GATE_MONITORING_ENABLED` (default `True`) | ES indexer writes `gateway.*` fields with zero new code | Per-request gate timing visible in raw ES logs |
| Unit tests for `GateMonitor` and `GateSummary` | — | — |

Human action required: none. No new infrastructure. No new dashboards. Gate data is indexed and queryable but not yet aggregated.

### After Phase 2 (steps 7–10 of Implementation Priority)

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| `TelemetryQueries` gateway aggregation methods (6 new methods) | `cg:gateway-monitor` runs on every `request.completed` event | Kibana "Gateway Health" panel: intent distribution, strategy distribution, confidence histogram, per-gate p90 latency, degradation rate, trimming rate |
| `GatewayHealthReport` dataclass | Rolling window threshold evaluation every 100 requests | Captain's Log entries with `category=observability` visible in ES and `telemetry/captains_log/` |
| `cg:gateway-monitor` consumer in `events/gateway_monitor.py` | Captain's Log anomaly entries generated when thresholds sustained | Linear "Gate Health Monitoring" project with actionable issues |
| Captain's Log anomaly handler | PromotionPipeline promotes after `seen_count ≥ 3`, `age ≥ 7 days` | `query_gateway_health` native tool — agent can answer "how are your routing decisions?" in conversation |
| `query_gateway_health` native tool | FeedbackPoller processes labels; suppression/approval automatic | FeedbackPoller `Deepen` response posts LLM re-analysis as Linear comment |
| "Gate Health Monitoring" Linear project | Full loop closed: anomaly → CL entry → Linear issue → human label → suppression | — |

Human action required: review and label Linear issues in "Gate Health Monitoring" project. Everything else is automatic.

---

## Feedback Stream ADR Template

This ADR establishes the standard structure for **feedback stream ADRs**. Every feedback stream in the agent's self-monitoring architecture should be documented with an ADR that answers all of the following. Future stream ADRs should use this section as a checklist.

### Required sections for a feedback stream ADR

**1. Stream identity**
- Name and one-sentence purpose
- Which layer (Execution, Observation, Knowledge) generates the source signal
- Which ADRs this stream depends on (transport, storage, surfacing)

**2. Source**
- What event or condition triggers data generation
- Granularity: per-request, per-session, scheduled, threshold-triggered

**3. Collection mechanism**
- How data is captured: structlog event, event bus publish, direct DB write, file write
- Buffering / batching policy if applicable
- What happens when the collection mechanism is unavailable (graceful degradation)

**4. Processing algorithm**
- What analyzes the raw data
- Where it runs: inline (hot path), background consumer, scheduled job
- Window / aggregation semantics: rolling count, exponential average, daily batch
- Minimum sample size before producing signals (to avoid noise at low volume)

**5. Signal produced**
- Type: Captain's Log entry, mode transition, Neo4j write, ES index, suppression file
- Schema: field names and types
- Deduplication: fingerprint policy if applicable

**6. Full automation cycle**
- Step-by-step trace from signal to closed loop (following the D8 format in this ADR)
- Explicitly name every consumer group and event stream involved

**7. Human review interface**
- Dedicated Linear project name
- Issue format (title pattern, body sections)
- Label semantics (what each feedback label means for this stream specifically)
- SLA expectation (how long before an unreviewed issue should be re-evaluated)

**8. End state table**
- After Phase 1 (MVP): what exists, what is automated, what is visible (following D9 format)
- After Phase 2 (full loop): same three columns

**9. Loop completeness criteria**
- How do you verify the loop is closed and working?
- What metric or observable confirms the stream is functioning (not just emitting)?

---

## References

- FRE-233: Draft ADR — Deterministic Gate Feedback-Loop Monitoring Framework (this issue)
- FRE-234: User interaction analysis — trigger effectiveness/efficiency/fluidity (first downstream consumer; blocked on this ADR)
- ADR-0041: Event Bus via Redis Streams — transport layer
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0047: Context Management & Observability — companion ADR (context quality observability)
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — surfacing channel
- ADR-0039: Proactive Memory — monitoring need analog (has similar per-path observability requirements)
- `src/personal_agent/request_gateway/pipeline.py` — the gate orchestration being instrumented
- `src/personal_agent/request_gateway/types.py` — `GatewayOutput`, `IntentResult`, `DecompositionResult`
- `src/personal_agent/telemetry/request_timer.py` — `RequestTimer`, the span-recording pattern this ADR follows
- `src/personal_agent/events/models.py` — `RequestCompletedEvent`, the event extended in D3 Layer B
- `src/personal_agent/telemetry/queries.py` — `TelemetryQueries`, extended in D3 Layer C
