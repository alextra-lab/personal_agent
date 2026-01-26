# ADR-0004: Telemetry & Metrics Implementation Strategy

**Status:** Proposed
**Date:** 2025-12-28
**Decision Owner:** Project Owner

---

## 1. Context

The Personal Local AI Collaborator is designed with **observability, auditability, and explainability** as first-class architectural properties. The system must be instrumented to support:

1. **Operational visibility**: understand what the agent is doing in real-time
2. **Debugging & root cause analysis**: reconstruct what happened when something goes wrong
3. **Governance enforcement**: verify that safety policies and modes are respected
4. **Evaluation & learning**: measure quality, effectiveness, and improvement over time
5. **Homeostatic control loops**: provide sensors for the Brainstem Service to regulate system health

Multiple components depend on telemetry:

- **Orchestrator Core**: emits execution traces, steps, and decisions
- **Local LLM Client**: emits model call spans with latency, tokens, errors
- **Brainstem Service**: consumes metrics to make mode transition decisions
- **Tool Layer**: emits tool invocations, successes, failures, risk scores
- **Safety Gates**: emit policy violations, blocks, and approvals
- **Captain's Log Manager**: uses telemetry to generate self-reflections
- **Evaluation Framework**: consumes metrics to assess system quality

The system runs **locally** on macOS with strong privacy requirements, which constrains our choices. We cannot depend on cloud-based observability platforms, and we must keep telemetry data inspectable and under the project owner's control.

This ADR defines:

- What telemetry concepts and data structures we emit
- How telemetry is collected and stored
- What trade-offs we accept for MVP
- How future evolution is supported

---

## 2. Decision

### 2.1 Telemetry Model: Structured Logs + Lightweight Metrics

We adopt a **hybrid model** optimized for local development and production-grade introspection:

#### **Structured Logging as the Foundation**

- All components emit **structured logs** using `structlog` (already in dependencies).
- Log entries are JSON-formatted dictionaries with:
  - `timestamp` (ISO 8601, UTC)
  - `level` (DEBUG, INFO, WARNING, ERROR, CRITICAL)
  - `component` (e.g., `orchestrator`, `llm_client`, `brainstem`, `tool_executor`)
  - `event` (semantic event name, e.g., `task_started`, `model_call_completed`, `mode_transition`)
  - `trace_id` and `span_id` (for correlation)
  - arbitrary structured fields (e.g., `model_role`, `latency_ms`, `tokens_used`, `tool_name`)

**Example:**

```json
{
  "timestamp": "2025-12-28T10:23:45.123456Z",
  "level": "info",
  "component": "llm_client",
  "event": "model_call_completed",
  "trace_id": "abc123",
  "span_id": "xyz789",
  "model_role": "reasoning",
  "model_id": "Qwen3-Next-80B-A3B-Thinking",
  "latency_ms": 3421,
  "tokens_prompt": 512,
  "tokens_completion": 256,
  "success": true
}
```

#### **Metrics as Derived Aggregations**

Rather than maintaining a separate in-memory metrics registry (e.g., Prometheus-style), we treat metrics as **queryable aggregations over structured logs**:

- Counters → count log entries matching a filter
- Gauges → latest value from a log entry series
- Histograms → distribution analysis over numeric fields (e.g., `latency_ms`)
- States → latest value of state-tracking log entries (e.g., `mode_transition` events)

This simplifies implementation while preserving full audit trails.

For **real-time dashboard needs** (future), we can optionally maintain a lightweight in-memory metrics cache that updates from the log stream.

---

### 2.2 Trace Context Propagation (Minimal OpenTelemetry-Compatible)

We adopt **minimal trace semantics** compatible with OpenTelemetry concepts but without requiring the full OTel SDK:

- **Trace**: A unique identifier (`trace_id`) for an end-to-end user request or background task
- **Span**: A unit of work within a trace (`span_id`), with:
  - parent span ID (for nested operations)
  - start time, end time (or duration)
  - component/service name
  - semantic attributes (key-value pairs)

**Propagation:**

- The **Orchestrator** generates a `trace_id` when handling a user request.
- The `trace_id` is passed as a **TraceContext** object to all downstream calls:
  - Local LLM Client
  - Tool invocations
  - Brainstem queries
  - Knowledge Base operations
- Each component generates its own `span_id` and logs it along with the `trace_id`.

**Implementation:**

- Define a lightweight `TraceContext` dataclass in `src/personal_agent/telemetry/trace.py`:

```python
from dataclasses import dataclass
import uuid

@dataclass
class TraceContext:
    trace_id: str
    parent_span_id: str | None = None

    @classmethod
    def new_trace(cls) -> "TraceContext":
        return cls(trace_id=str(uuid.uuid4()))

    def new_span(self) -> tuple["TraceContext", str]:
        """Return a new context with this span as parent, plus the new span_id."""
        span_id = str(uuid.uuid4())
        return TraceContext(trace_id=self.trace_id, parent_span_id=span_id), span_id
```

- Components accept an optional `trace_ctx: TraceContext | None` parameter
- If `None`, a new trace is started (for background/autonomous tasks)

---

### 2.3 Storage Strategy (MVP: File-Based, Future: Optional DB)

For **Phase 1 MVP**, we use **file-based storage**:

- **Logs**: Written to `telemetry/logs/` as rotated JSONL files
  - Format: one JSON object per line
  - Rotation: daily or by size (e.g., 100 MB per file)
  - Retention: configurable (default: 30 days)
- **Analysis**: Logs can be queried using simple Python scripts, `jq`, or loaded into pandas/DuckDB for ad-hoc analysis

**Future Evolution:**

- Add an optional **lightweight local database** (SQLite or DuckDB) for faster querying
- Ingest logs into the DB periodically (every N seconds or on shutdown)
- Retain raw JSONL files as the source of truth; DB is a materialized view

**Why file-first:**

- Simple, debuggable, git-diffable (for small samples)
- No additional dependencies
- Easy to inspect with standard Unix tools
- Natural fit for Captain's Log correlation (also file-based)

---

### 2.4 Sensor Implementation for Control Loops

The **Control Loops & Sensors** spec (`../architecture/CONTROL_LOOPS_SENSORS_v0.1.md`) defines logical sensors like:

- `perf_system_cpu_load`
- `safety_tool_high_risk_calls`
- `kb_docs_avg_age_days`

These are **implemented as log event patterns** plus **derived metrics**:

#### Interval Metrics (Polled)

- Emitted by a background poller (part of Brainstem or separate service)
- Example: CPU load sampled every 5 seconds → log event `system_metrics_snapshot`

```json
{
  "event": "system_metrics_snapshot",
  "component": "brainstem",
  "cpu_load_percent": 45.2,
  "mem_used_percent": 62.1,
  "gpu_load_percent": 12.0
}
```

#### Event Metrics (Emitted on Action)

- Emitted when relevant actions occur
- Example: tool call → log event `tool_call_completed`

```json
{
  "event": "tool_call_completed",
  "component": "tool_executor",
  "tool_name": "system_health_check",
  "risk_level": "low",
  "success": true,
  "latency_ms": 234
}
```

#### Derived Metrics (Computed from Logs)

- Computed by aggregating recent log entries
- Example: "tool call rate over last minute" → count `tool_call_completed` events in recent window
- Brainstem reads these either by:
  - scanning recent log files periodically, or
  - subscribing to a log event stream in-memory

---

### 2.5 Observability Interfaces

#### For Humans

1. **Live tail**: `tail -f telemetry/logs/current.jsonl | jq`
2. **CLI query tool** (future): `agent-telemetry query --event=model_call_completed --last=1h`
3. **Dashboard** (future Phase 2): Local web UI showing key metrics, traces, mode state

#### For Brainstem & Control Loops

- **Polling API**: Read recent metrics from log files or in-memory cache
- **Pub/Sub pattern** (future): Event stream for real-time reaction to critical events

#### For Evaluation & Captain's Log

- **Batch analysis**: Load logs into pandas/DuckDB, compute aggregates
- **Trace reconstruction**: Given a `trace_id`, extract all log entries and reconstruct execution

---

### 2.6 What We Explicitly Do NOT Do (MVP)

❌ **Full OpenTelemetry SDK integration**: Too heavyweight for local-only MVP; we adopt compatible semantics but implement minimally

❌ **Prometheus/StatsD metrics exposition**: No external scraping; logs are sufficient

❌ **Real-time dashboarding**: Phase 2+ feature; MVP uses log tailing and scripts

❌ **Cloud telemetry backends**: Violates local-first principle

❌ **Complex APM features**: Flame graphs, service maps, etc. can be added later if needed

---

## 3. Decision Drivers

### Why Structured Logs First?

- **Simplicity**: One output format, one storage mechanism
- **Auditability**: Complete record of all events, not just aggregates
- **Flexibility**: Can compute any metric retroactively
- **Privacy**: No data leaves the machine
- **Debuggability**: Human-readable, greppable, diffable

### Why Minimal Trace Semantics?

- **Compatibility**: Can migrate to OTel later without rewriting instrumentation
- **Lightweight**: No SDK overhead, no collector required
- **Sufficient**: Trace + span IDs enable full execution reconstruction

### Why File-Based Storage?

- **MVP speed**: No database setup, no schema migrations
- **Inspectability**: Can `cat`, `jq`, `grep`, `git diff` (for samples)
- **Retention control**: Simple rotation and deletion policies
- **Future-proof**: Can ingest into DB later without changing log formats

---

## 4. Implementation Plan

### Phase 1: Foundational Telemetry (Week 1)

1. **Create telemetry module structure**:
   - `src/personal_agent/telemetry/trace.py` (TraceContext)
   - `src/personal_agent/telemetry/logger.py` (configure structlog)
   - `src/personal_agent/telemetry/events.py` (semantic event constants)

2. **Configure structlog**:
   - JSON formatter
   - UTC timestamps
   - File handler with rotation (using `logging.handlers.RotatingFileHandler`)
   - Console handler for debugging (pretty-printed)

3. **Instrument key components**:
   - Orchestrator: `task_started`, `task_completed`, `step_executed`
   - Local LLM Client: `model_call_started`, `model_call_completed`, `model_call_error`
   - Tool layer (when implemented): `tool_call_started`, `tool_call_completed`

### Phase 2: Brainstem Integration (Week 2)

1. **Implement metric readers**:
   - `src/personal_agent/telemetry/metrics.py` with functions to query recent logs
   - Example: `get_recent_cpu_load(window_seconds=60) -> list[float]`

2. **Wire Brainstem to sensors**:
   - Brainstem polls metrics using the readers
   - Emits `mode_transition` events when thresholds crossed

### Phase 3: Evaluation & Analysis Tools (Week 3+)

1. **CLI query tool**: `agent telemetry query ...`
2. **Trace viewer**: `agent telemetry trace <trace_id>`
3. **Metrics dashboard** (optional web UI)

---

## 5. Consequences

### Positive

✅ **Complete audit trail**: Every meaningful action is logged
✅ **Simple implementation**: No external dependencies, no complex setup
✅ **Privacy-preserving**: All data stays local
✅ **Governance-ready**: Can verify policy enforcement via log analysis
✅ **Future-proof**: Can add real-time metrics layer or DB without rewriting instrumentation
✅ **Debuggability**: Trace reconstruction enables root cause analysis

### Negative / Trade-offs

⚠️ **No real-time dashboards in MVP**: Must tail logs or run scripts (acceptable for solo developer)
⚠️ **Log volume**: High-frequency events (e.g., token-level streaming) must be sampled or excluded
⚠️ **Query performance**: File-based queries slower than DB; mitigated by log rotation and future DB option
⚠️ **Manual aggregation**: Computing metrics requires scripting; mitigated by helper functions

---

## 6. Open Questions & Future Work

- **Sampling strategy**: How aggressively should we sample high-frequency events to control log volume?
- **Alerting**: Should we add a simple alerting layer (e.g., send macOS notifications on critical events)?
- **Correlation with Captain's Log**: How do we link telemetry traces to Captain's Log reflection entries?
- **Retention policies**: What's the right balance between storage cost and historical analysis needs?
- **Performance impact**: How much overhead does JSON serialization + file I/O add? Benchmark and optimize if needed.

---

## 7. References

- `../architecture/system_architecture_v0.1.md` — Section 4.2 (Telemetry for Safety)
- `../architecture/CONTROL_LOOPS_SENSORS_v0.1.md` — Sensor definitions
- `../architecture/HOMEOSTASIS_MODEL.md` — Control loop requirements
- OpenTelemetry Specification (for semantic compatibility)
- `structlog` documentation (structured logging library)

---

## 8. Acceptance Criteria

This ADR is accepted when:

1. ✅ Telemetry module structure exists with `TraceContext`, logger config, and event constants
2. ✅ Orchestrator and Local LLM Client emit structured logs with trace/span IDs
3. ✅ Logs are written to `telemetry/logs/` with rotation
4. ✅ At least one metric reader function exists (e.g., `get_recent_event_count()`)
5. ✅ A sample trace can be reconstructed from logs using `trace_id`

---

**Next ADRs to unblock**: ADR-0005 (Governance Config), ADR-0006 (Orchestrator Runtime)
