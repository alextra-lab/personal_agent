# Feedback Stream Architecture

> **Status**: Living document — updated 2026-04-22
> **Context**: Surfaced during FRE-233 (ADR-0053) development
> **Owner**: Project owner

This is the authoritative reference for the Personal Agent's self-monitoring and self-improvement architecture. It catalogs every feedback stream, defines the observability framework they belong to, sequences the ADRs that govern them, and names the Linear projects where their generated issues land.

---

## The Four-Level Observability Framework

The agent's self-monitoring operates at four distinct levels, each observing a different granularity of system behaviour. Every feedback stream belongs to exactly one level.

| Level | What it observes | Timescale | Streams |
|-------|-----------------|-----------|---------|
| **1 — System metrics** | CPU, memory, disk, GPU — hardware signals; operational mode | Seconds (5s poll) | Stream 5: Brainstem Sensors / Mode Manager |
| **2 — Gate decisions** | Intent classification quality, strategy distribution, per-stage latency, confidence scores | Per-request | Stream NEW: Gate Feedback Monitoring (ADR-0053) |
| **3 — Application errors** | Exceptions, ERROR/WARNING log events, tool failures, LLM errors, repeated failure patterns | Rolling window | Stream NEW: Error Pattern Monitoring (ADR-0056) |
| **4 — Self-reflection** | LLM-generated post-task analysis — what happened, what to improve, capability gaps | Per-task | Streams 1–3: Self-Improvement Pipeline (ADR-0030/0040) |

**Design principle:** Levels 1–3 are automated observation. Level 4 is LLM-mediated interpretation. Together they give the agent the ability to observe itself at every scale — from hardware through pipeline through error to intent.

---

## The Dual-Write Convention (ADR-0054, to be drafted)

**The problem:** Five of nine feedback streams do not publish to the event bus. They detect or compute but their signal terminates at a log line or ES index entry. Nothing can subscribe to them; nothing can react to them in real time; future capabilities cannot compose on top of them.

**The convention:** Every feedback stream must:
1. **Write durably** — to disk (JSON file) or Elasticsearch, so signals survive Redis outages and process restarts
2. **Publish to the event bus** — a typed, frozen Pydantic event on a named stream, so any future consumer can subscribe without modifying the producer

This is the "dual-write" pattern. The file/ES write is the durable record; the bus event is the composability hook.

**Why this matters:** Once every stream publishes a typed bus event, new capabilities can be built by subscribing to combinations of streams — without touching producer code. For example: "when an error pattern fires AND confidence is low AND cost is anomalous → escalate to a different treatment." That kind of cross-stream composition is impossible today because most streams don't publish to the bus.

ADR-0054 establishes the event naming conventions, stream name conventions, and required fields for all feedback stream events.

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
- **Bus?** Partial — promotion triggered by bus, entry creation is not
- **ADR:** ADR-0030
- **Project:** Self-Improvement Pipeline
- **Gap:** No `captain_log.entry_created` bus event — not composable

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
- **Signal:** Indexed to `agent-insights-*` in ES; `Improvement` objects (not wired)
- **Action:** None — `suggest_improvements()` and `create_captain_log_proposals()` exist but are not called
- **Human loop:** Partial — Kibana only
- **Bus?** ❌ No output on bus; delegation patterns are a stub
- **ADR:** ADR-0057 (FRE-247 — Needs Approval)
- **Project:** Insights & Pattern Analysis
- **Gap:** Improvement objects are a dead end; delegation patterns unimplemented

### Stream 5: Brainstem Sensors / Mode Manager
- **Source:** `MetricsDaemon` — psutil + powermetrics, 5s poll
- **Collection:** In-memory ring buffer; `evaluate_transitions()` against rule set
- **Processing:** State machine (NORMAL → ALERT → DEGRADED → LOCKDOWN → RECOVERY)
- **Signal:** `MODE_TRANSITION` log to ES; mode state in `ModeManager` singleton
- **Action:** None — **`app.py:176` hardcodes `Mode.NORMAL`; mode never reaches the gateway**
- **Human loop:** No
- **Bus?** ❌ No
- **ADR:** ADR-0005 (partial)
- **Project:** System Health & Homeostasis
- **Gap:** Critical disconnect — the entire mode system has zero runtime effect

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
- **Signal:** `insights_cost_anomaly_detected` warning log; `Insight` in ES
- **Action:** Nothing — `Improvement` objects not wired to any consumer
- **Human loop:** No (Kibana only)
- **Bus?** ❌ No
- **ADR:** None
- **Project:** Insights & Pattern Analysis
- **Gap:** Detection only — no response path

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

PHASE 1 — CONVENTION
└── ADR-0054: Feedback Stream Bus Convention [FRE-245]
    Establishes: dual-write pattern, stream naming, event shapes
    Depends on: ADR-0041
    Enables: ALL subsequent stream ADRs
    ┌── Must be accepted before any Phase 2 ADR is drafted ──┐

PHASE 2 — FIX BROKEN STREAMS (parallel, all depend on ADR-0054)
├── ADR-0055: System Health & Homeostasis Stream [FRE-246]
│   Fixes: Mode Manager disconnect (hardcoded Mode.NORMAL)
│   Depends on: ADR-0054, ADR-0041, ADR-0053 (pattern)
│   Project: System Health & Homeostasis
│
├── ADR-0056: Error Pattern Monitoring Stream [FRE-244]
│   Level 3 observability — agent reads its own error logs
│   Depends on: ADR-0053 (template), ADR-0054
│   Project: Error Pattern Monitoring
│
├── ADR-0057: Insights & Pattern Analysis Stream [FRE-247]
│   Wires InsightsEngine to full loop; implements delegation patterns
│   Depends on: ADR-0054, ADR-0041
│   Project: Insights & Pattern Analysis
│
└── ADR-0058: Self-Improvement Pipeline Stream [FRE-248]
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
| 1. Self-reflection | Per-task | Partial | ✅ | ADR-0030 | Self-Improvement Pipeline |
| 2. Linear feedback | Human label | ✅ | ✅ | ADR-0040 | Self-Improvement Pipeline |
| 3. Promotion pipeline | Threshold | ✅ | ✅ | ADR-0030/0040 | Self-Improvement Pipeline |
| 4. Insights engine | Patterns | ❌ | ❌ | None | Insights & Pattern Analysis |
| 5. Mode manager | System metrics | ❌ | ❌ 🔴 critical | Partial | System Health & Homeostasis |
| 6. Memory freshness | Access patterns | ✅ | ⚠️ partial | ADR-0042 | Knowledge Graph Quality |
| 7. Compaction quality | Context loss | ❌ | ❌ | ADR-0047 D3 | Context Quality Monitoring |
| 8. Consolidation quality | Graph health | ❌ | ❌ | None | Knowledge Graph Quality |
| 9. Cost anomaly | Spend spikes | ❌ | ❌ | None | Insights & Pattern Analysis |
| NEW. Gate monitoring | Pipeline decisions | ❌ → ✅ | ❌ → ✅ | ADR-0053 | Gate Health Monitoring |
| NEW. Error patterns | Error logs | ❌ → ✅ | ❌ → ✅ | ADR-0056 | Error Pattern Monitoring |

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
- FRE-244: ADR-0056 — Error Pattern Monitoring (Needs Approval, blocked by FRE-245)
- FRE-245: ADR-0054 — Feedback Stream Bus Convention (Needs Approval — **draft next**)
- FRE-246: ADR-0055 — System Health & Homeostasis (Needs Approval, blocked by FRE-245)
- FRE-247: ADR-0057 — Insights & Pattern Analysis (Needs Approval, blocked by FRE-245)
- FRE-248: ADR-0058 — Self-Improvement Pipeline Stream (Needs Approval, blocked by FRE-245)
- FRE-249: ADR-0059 — Context Quality Monitoring (Needs Approval, blocked by FRE-245 + FRE-244)
- FRE-250: ADR-0060 — Knowledge Graph Quality (Needs Approval, blocked by FRE-245 + FRE-247)
- FRE-251: ADR-0061 — Within-Session Compression (Needs Approval, blocked by FRE-249)
- FRE-252: Governance — Per-TaskType tool allowlist (Needs Approval, independent)
- FRE-226: Agent self-updating skills / agentskills.io format (Approved, blocked by FRE-248)
