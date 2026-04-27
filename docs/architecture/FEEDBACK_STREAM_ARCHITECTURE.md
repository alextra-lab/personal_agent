# Feedback Stream Architecture

> **Status**: Living document — updated 2026-04-24 (ADR-0055 drafted, In Review; ADR-0056 implemented 2026-04-24, ADR-0057 implemented 2026-04-24)
> **Context**: Surfaced during FRE-233 (ADR-0053) development
> **Owner**: Project owner

This is the authoritative reference for the Personal Agent's self-monitoring and self-improvement architecture. It catalogs every feedback stream, defines the observability framework they belong to, sequences the ADRs that govern them, and names the Linear projects where their generated issues land.

---

## The Four-Level Observability Framework

The agent's self-monitoring operates at four distinct levels, each observing a different granularity of system behaviour. Every feedback stream belongs to exactly one level.

| Level | What it observes | Timescale | Streams |
|-------|-----------------|-----------|---------|
| **1 — System metrics** | CPU, memory, disk, GPU — hardware signals; operational mode | Seconds (5s poll) | Stream 5: Brainstem Sensors / Mode Manager ([ADR-0055](../architecture_decisions/ADR-0055-system-health-homeostasis-stream.md) Drafted — In Review 2026-04-24) |
| **2 — Gate decisions** | Intent classification quality, strategy distribution, per-stage latency, confidence scores | Per-request | Stream NEW: Gate Feedback Monitoring ([ADR-0053](../architecture_decisions/ADR-0053-gate-feedback-monitoring.md) Proposed) |
| **3 — Application errors** | Exceptions, ERROR/WARNING log events, tool failures, LLM errors, repeated failure patterns | Rolling window | Stream NEW: Error Pattern Monitoring ([ADR-0056](../architecture_decisions/ADR-0056-error-pattern-monitoring.md) Proposed — In Review 2026-04-23) |
| **4 — Self-reflection** | LLM-generated post-task analysis — what happened, what to improve, capability gaps | Per-task | Streams 1–3: Self-Improvement Pipeline (ADR-0030/0040); Phase 2 failure-path reflection proposed by ADR-0056 |

**Design principle:** Levels 1–3 are automated observation. Level 4 is LLM-mediated interpretation. Together they give the agent the ability to observe itself at every scale — from hardware through pipeline through error to intent.

---

## The Dual-Write Convention (ADR-0054, accepted 2026-04-23)

**The problem:** Five of nine feedback streams did not publish to the event bus. They detected or computed but the signal terminated at a log line or ES index entry. Nothing could subscribe to them; nothing could react in real time; future capabilities could not compose on top of them.

**The convention** established by [ADR-0054](../architecture_decisions/ADR-0054-feedback-stream-bus-convention.md) requires every feedback stream to:
1. **Write durably** — to disk (JSON file) or Elasticsearch, so signals survive Redis outages and process restarts
2. **Publish to the event bus** — a typed, frozen Pydantic event on a named stream, so any future consumer can subscribe without modifying the producer

This is the "dual-write" pattern. The file/ES write is the durable record; the bus event is the composability hook. The durable write **must precede** the bus publish (D4 ordering rule); durable failures propagate; bus failures are logged and swallowed (D6).

**Contract fields on every event.** Per D3, `EventBase` carries `trace_id` / `session_id` (nullable for scheduled/system events; narrowed to required on request-driven events), `source_component` (required — producer identity, independent of stream name), and `schema_version` (defaults to 1; additive field changes keep backward compatibility via Rule 1; breaking changes take a new `event_type` via Rule 2).

**Why this matters:** Once every stream publishes a typed bus event, new capabilities can be built by subscribing to combinations of streams — without touching producer code. For example: "when an error pattern fires AND confidence is low AND cost is anomalous → escalate to a different treatment." That kind of cross-stream composition is now buildable (one new consumer subscribing to three existing streams; zero producer changes).

ADR-0054 establishes the stream naming convention (`stream:<domain>.<signal>[.<subtype>]`), the consumer group convention (`cg:<role>`), the `EventBase` contract fields, the durable-write decision rule, the schema-versioning policy, and the dual-write failure handling. It reserves 8 Phase 2 stream names and 6 consumer group names.

---

## Stream Catalog

All nine feedback streams, their current state, and their target state after ADR acceptance.

### Stream 1: Post-Task Self-Reflection (Captain's Log)
- **Source:** Every completed agent task
- **Collection:** `generate_reflection_entry()` — LLM analysis of trace telemetry
- **Processing:** DSPy ChainOfThought or local SLM → structured `CaptainLogEntry`
- **Signal:** JSON file to `telemetry/captains_log/` + ES index
- **Action:** Promotion pipeline (Stream 3) picks it up
- **Human loop:** Yes — via Linear
- **Bus?** ✅ Yes — `CaptainLogEntryCreatedEvent` on `stream:captain_log.entry_created` (ADR-0058)
- **ADR:** ADR-0030, ADR-0058
- **Project:** Self-Improvement Pipeline
- **Gap:** Closed by ADR-0058 (implemented FRE-248 2026-04-25)

### Stream 2: Linear Human Feedback
- **Source:** Human applies label to Linear issue
- **Collection:** `FeedbackPoller` daily poll
- **Processing:** 6 label handlers (Approved/Rejected/Deepen/Too Vague/Duplicate/Defer)
- **Signal:** `FeedbackReceivedEvent` on `stream:feedback.received`; suppression file
- **Action:** Suppression (Rejected), Approved state, LLM re-analysis (Deepen/Too Vague)
- **Human loop:** Yes — this IS the human side
- **Bus?** ✅ Yes
- **ADR:** ADR-0040
- **Project:** Self-Improvement Pipeline

### Stream 3: Promotion Pipeline
- **Source:** `consolidation.completed` event
- **Collection:** Scans `telemetry/captains_log/` for promotable entries
- **Processing:** `seen_count ≥ 3`, `age ≥ 7 days`, budget check → Linear issue creation
- **Signal:** `PromotionIssueCreatedEvent`; Linear issue in target project
- **Action:** Human review in Linear
- **Human loop:** Yes — required
- **Bus?** ✅ Yes (triggered by bus, publishes to bus)
- **ADR:** ADR-0030, ADR-0040
- **Project:** Self-Improvement Pipeline

### Stream 4: Insights Engine
- **Source:** `consolidation.completed` event
- **Collection:** `InsightsEngine.analyze_patterns()` — ES + Neo4j + Postgres cross-query
- **Processing:** 6 insight types (correlation, optimization, trend, anomaly, graph_staleness, feedback_summary)
- **Signal:** Indexed to `agent-insights-*` in ES; published via `InsightsPatternDetectedEvent` and `InsightsCostAnomalyEvent` on bus
- **Action:** Captain's Log entry creation via `create_captain_log_proposals()` wired in handler
- **Human loop:** Yes — via Captain's Log promotion pipeline
- **Bus?** ✅ Yes
- **ADR:** [ADR-0057](../architecture_decisions/ADR-0057-insights-pattern-analysis.md) (Accepted — Implemented, FRE-247, 2026-04-24)
- **Project:** Insights & Pattern Analysis
- **Gap:** None — stream wired end-to-end

### Stream 5: Brainstem Sensors / Mode Manager
- **Source:** `MetricsDaemon` — psutil + powermetrics, 5s poll
- **Collection:** In-memory ring buffer; `evaluate_transitions()` against rule set
- **Processing:** State machine (NORMAL → ALERT → DEGRADED → LOCKDOWN → RECOVERY)
- **Signal:** `MODE_TRANSITION` log to ES + `ModeTransitionEvent` on `stream:mode.transition`; 5 s samples on `stream:metrics.sampled`
- **Action:** ADR-0055 Phase 1 — `cg:mode-controller` consumer holds a 60 s rolling window, evaluates transitions every 30 s, and publishes `mode.transition` events; the 4 hardcoded `Mode.NORMAL` sites in `service/app.py` are replaced by `get_current_mode()`
- **Human loop:** Yes — calibration proposals via Captain's Log → Linear
- **Bus?** ❌ → ✅ (ADR-0055)
- **ADR:** [ADR-0055](../architecture_decisions/ADR-0055-system-health-homeostasis-stream.md) (Drafted 2026-04-24, In Review) — supersedes ADR-0005 (partial)
- **Project:** System Health & Homeostasis
- **Gap:** Closed by ADR-0055 Phase 1; per-condition windowing (`duration_seconds`) and `DEGRADED → NORMAL` recovery path deferred to Phase 2

### Stream 6: Memory Access Freshness
- **Source:** Every Neo4j read in `MemoryService`
- **Collection:** `MemoryAccessedEvent` on `stream:memory.accessed`
- **Processing:** `FreshnessConsumer` batches → Neo4j `last_accessed_at`, `access_count` writes
- **Signal:** `StalenessTier` per entity (WARM/COOLING/COLD/DORMANT)
- **Action:** Weekly freshness review → `CaptainLogEntry` for dormant entities → Linear
- **Human loop:** Yes (via Captain's Log promotion)
- **Bus?** ✅ Yes (consumer)
- **ADR:** ADR-0042
- **Project:** Knowledge Graph Quality
- **Gap:** Decay scores computed but not used in recall reranking

### Stream 7: Compaction Quality Detection
- **Source:** Stage 7 (Budget) fires `log_compaction()` when context overflows
- **Collection:** `_dropped_entities_by_session` in-memory cache; recall controller overlap check
- **Processing:** Substring match between dropped entities and user noun phrases
- **Signal:** `compaction_quality.poor` WARNING log to ES
- **Action:** Nothing — pure log line, no consumer
- **Human loop:** No
- **Bus?** ❌ No
- **ADR:** ADR-0047 D3 (detection only)
- **Project:** Context Quality Monitoring
- **Gap:** Dead end — the loop does not close

### Stream 8: Consolidation Quality Monitor
- **Source:** `BrainstemScheduler._run_quality_monitoring()` — daily at 5 AM UTC
- **Collection:** Neo4j entity/relationship metrics + ES extraction failure counts
- **Processing:** `detect_anomalies()` against hardcoded targets
- **Signal:** `quality_monitor_anomaly_detected` log to ES; `Anomaly` objects in memory
- **Action:** Nothing — anomaly objects not forwarded anywhere
- **Human loop:** No
- **Bus?** ❌ No
- **ADR:** None (FRE-23, FRE-32)
- **Project:** Knowledge Graph Quality
- **Gap:** Detection only — no response path

### Stream 9: Cost Anomaly Detection
- **Source:** `InsightsEngine.detect_cost_anomalies()` — triggered by consolidation
- **Collection:** Postgres `api_costs` table daily aggregates
- **Processing:** 3-sigma + 2x-floor threshold; `CostAnomaly` with confidence score
- **Signal:** `InsightsCostAnomalyEvent` published on bus; indexed to `agent-insights-*` in ES
- **Action:** Captain's Log entry creation via `create_captain_log_proposals()` wired in handler
- **Human loop:** Yes — via Captain's Log promotion pipeline
- **Bus?** ✅ Yes
- **ADR:** [ADR-0057](../architecture_decisions/ADR-0057-insights-pattern-analysis.md) (Accepted — Implemented, FRE-247, 2026-04-24)
- **Project:** Insights & Pattern Analysis
- **Gap:** None — stream wired end-to-end (Phase 2 governance response deferred to follow-on ADR)

---

## ADR Sequence & Dependencies

ADRs must be drafted in this order. Each builds on what precedes it. Numbers are assigned at writing time; the sequence is what matters.

```
FOUNDATION (already accepted)
├── ADR-0041: Event Bus — Redis Streams
├── ADR-0043: Three-Layer Architectural Separation
├── ADR-0047: Context Management & Observability
└── ADR-0053: Gate Feedback Monitoring [DRAFTED — FRE-233]
    └── Establishes: Feedback Stream ADR Template

PHASE 1 — CONVENTION ✅ Accepted 2026-04-23
└── ADR-0054: Feedback Stream Bus Convention [FRE-245 done]
    Establishes: dual-write pattern, stream naming (stream:<domain>.<signal>),
                 consumer group naming (cg:<role>), flattened EventBase with
                 trace_id/session_id/source_component/schema_version
    Depends on: ADR-0041
    Enables: ALL subsequent stream ADRs — Phase 2 drafting unblocked

PHASE 2 — FIX BROKEN STREAMS (parallel, all depend on ADR-0054)
├── ADR-0055: System Health & Homeostasis Stream [FRE-246 — drafted 2026-04-24, In Review]
│   Fixes: Mode Manager disconnect (hardcoded Mode.NORMAL)
│   Adds: MetricsSampledEvent + ModeTransitionEvent; cg:mode-controller consumer
│   Depends on: ADR-0054, ADR-0041, ADR-0053 (pattern)
│   Project: System Health & Homeostasis
│   File: docs/architecture_decisions/ADR-0055-system-health-homeostasis-stream.md
│
├── ADR-0056: Error Pattern Monitoring Stream [FRE-244 — Accepted, Implemented 2026-04-24]
│   Level 3 observability — agent reads its own error logs
│   stream:errors.pattern_detected + cg:error-monitor consumer
│   Phase 2: failure-path reflection (GEPA-inspired) inside DSPy GenerateReflection
│   Depends on: ADR-0053 (template), ADR-0054
│   Project: Error Pattern Monitoring
│
├── ADR-0057: Insights & Pattern Analysis Stream [FRE-247 — Accepted, Implemented 2026-04-24]
│   Wires InsightsEngine to full loop; implements delegation patterns
│   Adds InsightsPatternDetectedEvent + InsightsCostAnomalyEvent
│   Depends on: ADR-0054, ADR-0041
│   Project: Insights & Pattern Analysis
│
└── ADR-0058: Self-Improvement Pipeline Stream [FRE-248 — Accepted, Implemented 2026-04-25]
    Formalizes Streams 1–3 with bus convention
    Adds captain_log.entry_created bus event
    Depends on: ADR-0030, ADR-0040, ADR-0054
    Project: Self-Improvement Pipeline

PHASE 3 — COMPLETE PARTIAL STREAMS (depend on Phase 2)
├── ADR-0059: Context Quality Stream [FRE-249]
│   Compaction quality detection → full feedback loop
│   Depends on: ADR-0047, ADR-0054, ADR-0056 (error monitoring pattern)
│   Project: Context Quality Monitoring
│
└── ADR-0060: Knowledge Graph Quality Stream [FRE-250]
    Consolidation quality + cost anomaly → full feedback loop
    Decay scores → recall reranking
    Depends on: ADR-0042, ADR-0054, ADR-0057
    Project: Knowledge Graph Quality
```

---

## Linear Projects

| Project | Streams | ADRs | Issues land here |
|---------|---------|------|-----------------|
| Gate Health Monitoring | Stream NEW (gate decisions) | ADR-0053 | Gate anomalies: confidence drift, DELEGATE rate, latency SLI violations |
| System Health & Homeostasis | Stream 5 | ADR-0055 | Mode calibration proposals, sensor threshold adjustments |
| Error Pattern Monitoring | Stream NEW (error logs) | ADR-0056 | Repeated error patterns with proposed fixes |
| Insights & Pattern Analysis | Streams 4, 9 | ADR-0057 | Delegation pattern anomalies, cost spikes, correlation findings |
| Context Quality Monitoring | Stream 7 | ADR-0059 | Compaction quality degradation proposals |
| Knowledge Graph Quality | Streams 6, 8 | ADR-0042, ADR-0060 | Dormancy proposals, consolidation quality anomalies |
| Self-Improvement Pipeline | Streams 1, 2, 3 | ADR-0030, ADR-0040, ADR-0058 | All capability improvement proposals from Captain's Log |

---

## Current State Summary

| Stream | Detects | Bus? | Complete loop? | ADR | Project |
|--------|---------|------|----------------|-----|---------|
| 1. Self-reflection | Per-task | ✅ | ✅ | ADR-0030, ADR-0058 (Accepted, Implemented 2026-04-25) | Self-Improvement Pipeline |
| 2. Linear feedback | Human label | ✅ | ✅ | ADR-0040 | Self-Improvement Pipeline |
| 3. Promotion pipeline | Threshold | ✅ | ✅ | ADR-0030/0040 | Self-Improvement Pipeline |
| 4. Insights engine | Patterns | ✅ | ✅ | ADR-0057 (Accepted, Implemented 2026-04-24) | Insights & Pattern Analysis |
| 5. Mode manager | System metrics | ❌ → ✅ | ❌ → ✅ | ADR-0055 (Drafted, In Review) | System Health & Homeostasis |
| 6. Memory freshness | Access patterns | ✅ | ⚠️ partial | ADR-0042 | Knowledge Graph Quality |
| 7. Compaction quality | Context loss | ❌ | ❌ | ADR-0047 D3 | Context Quality Monitoring |
| 8. Consolidation quality | Graph health | ❌ | ❌ | None | Knowledge Graph Quality |
| 9. Cost anomaly | Spend spikes | ✅ | ✅ | ADR-0057 (Accepted, Implemented 2026-04-24) | Insights & Pattern Analysis |
| NEW. Gate monitoring | Pipeline decisions | ❌ → ✅ | ❌ → ✅ | ADR-0053 | Gate Health Monitoring |
| 10. Error patterns | Error logs | ✅ | ✅ | ADR-0056 (Done 2026-04-24, FRE-244) | Error Pattern Monitoring |

---

## FRE-261 Addition (2026-04-27)

**Tool-approval decisions captured in Captain's Log captures.** When primitives are enabled (`AGENT_PRIMITIVE_TOOLS_ENABLED=true`), approval decisions are written to `agent-captains-captures-*.approval_decisions[]` via `AGUITransport.request_tool_approval` → structlog. Each entry records `request_id`, `tool`, `decision` (approve/deny/timeout), `trace_id`, `session_id`, `timestamp`. No new event stream; decisions are written inline alongside the tool execution trace. This data feeds future policy-learning (approval pattern auto-promotion — tracked as a post-pivot ADR, see ADR-0063 Open Question 1).

---

## References

- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline
- ADR-0040: Linear as Async Feedback Channel
- ADR-0041: Event Bus — Redis Streams
- ADR-0042: Knowledge Graph Freshness via Access Tracking
- ADR-0043: Three-Layer Architectural Separation
- ADR-0047: Context Management & Observability
- ADR-0053: Deterministic Gate Feedback-Loop Monitoring Framework (`docs/architecture_decisions/ADR-0053-gate-feedback-monitoring.md`)
- FRE-233: ADR-0053 — Gate Feedback Monitoring (awaiting acceptance)
- FRE-244: ADR-0056 — Error Pattern Monitoring (✅ Accepted, Implemented 2026-04-24)
- FRE-245: ADR-0054 — Feedback Stream Bus Convention (✅ Accepted + implemented 2026-04-23)
- FRE-246: ADR-0055 — System Health & Homeostasis (Drafted 2026-04-24, In Review)
- FRE-247: ADR-0057 — Insights & Pattern Analysis (✅ Accepted, Implemented 2026-04-24)
- FRE-248: ADR-0058 — Self-Improvement Pipeline Stream (✅ Accepted, Implemented 2026-04-25)
- FRE-249: ADR-0059 — Context Quality Monitoring (Needs Approval, blocked by FRE-245 + FRE-244)
- FRE-250: ADR-0060 — Knowledge Graph Quality (Needs Approval, blocked by FRE-245 + FRE-247)
- FRE-251: ADR-0061 — Within-Session Compression (Needs Approval, blocked by FRE-249)
- FRE-252: Governance — Per-TaskType tool allowlist (Needs Approval, independent)
- FRE-226: Agent self-updating skills / agentskills.io format (Approved, blocked by FRE-248)
