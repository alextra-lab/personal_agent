# FRE-107 Findings: Self-Telemetry Usability Analysis

**Date**: 2026-03-13
**Issue**: FRE-107 — Telemetry usability improvement: make self-telemetry actionable and time-aware
**Phase**: 2.3 Homeostasis & Feedback
**Status**: Complete

---

## 1. Failure Analysis

### 1.1 Repro Matrix

| User Query Pattern | Tool Behavior | Quality | Root Cause |
|---|---|---|---|
| "Any errors in the last hour?" | `events` + `event=task_failed` + `window=1h` | Weak | Returns raw events; LLM must count and interpret JSON blobs |
| "Errors since this morning" | LLM must convert "this morning" → hours → `window=Xh` | Inconsistent | No `today` keyword; LLM time arithmetic is unreliable |
| "How many failures in the last 5 interactions?" | No interaction-count scoping exists | Fails | Tool only supports time windows, not interaction counts |
| "Is the agent healthy?" | No health query type | Fails | No aggregation layer; LLM would need to call multiple tools |
| "Why was that last response slow?" | `latency` + trace_id (if known) | OK | Works when trace_id is available; agent must know current trace |
| "Am I getting slower over time?" | No trend analysis | Fails | No comparison between current vs. historical windows |
| "What tools have been failing?" | `events` + `event=tool_call_failed` | Weak | Returns raw events; no grouping by tool name |
| "Show me my last few interactions" | No interaction listing | Fails | Captain's Log captures exist but aren't exposed via tool |

### 1.2 Degradation Points

The end-to-end path from user question to actionable answer has four stages. Degradation occurs at stages 2-4:

```
User question → [1] LLM intent mapping → [2] Tool argument generation
             → [3] Data retrieval       → [4] LLM synthesis of results
```

**Stage 2 — Tool argument generation:**
- The LLM must translate natural time expressions into the rigid `Xh`/`Xm`/`Xd` format. "Since this morning" requires knowing the current time and calculating hours elapsed, which LLMs do inconsistently.
- The LLM must guess the right `event` name from memory. The tool description lists examples (`model_call_error`, `task_failed`) but doesn't cover all useful patterns.
- No `last_n` parameter exists for interaction-count queries.

**Stage 3 — Data retrieval:**
- `query_events()` performs a full O(n) scan of JSONL files every call, parsing every line. For large log files this is slow and wasteful.
- Results are raw log entries — every field from structlog is included (timestamp, level, component, logger, event, plus all structured fields). Most fields are noise for the user's question.
- No aggregation: the tool returns a list of individual events, not counts, groups, or summaries.

**Stage 4 — LLM synthesis:**
- The LLM receives up to 50 raw JSON objects and must count, group, and summarize them. This consumes significant context window and is error-prone (LLMs miscount, miss patterns, or hallucinate trends).
- No structured summary means the LLM's answer quality depends entirely on its ability to parse raw data — the exact task computers are better at.

### 1.3 Gap List

| Gap | Severity | Description |
|-----|----------|-------------|
| G1: No interaction-level queries | High | Users think in interactions, tool queries raw events |
| G2: No pre-computed aggregations | High | Tool returns raw data; LLM must compute summaries |
| G3: No health overview | High | No single query answers "is the agent ok?" |
| G4: No interaction-count scoping | Medium | Can't query "last 5 interactions" |
| G5: Rigid time window format | Medium | Only `Xh`/`Xm`/`Xd`; no `today`/`yesterday` keywords |
| G6: No trend detection | Medium | Can't compare current vs. historical performance |
| G7: No error grouping | Medium | Errors returned individually, not grouped by type/component |
| G8: Captain's Log not exposed | Medium | `TaskCapture` records (per-interaction) exist but aren't queryable via tool |
| G9: JSONL full-scan performance | Low | Acceptable for short windows but degrades with large files |
| G10: ES queries not accessible | Low | `TelemetryQueries` class exists but isn't wired into the tool |

---

## 2. Trace/Event Schema Audit

### 2.1 Available Event Types for Health Queries

| Event | Fields Available | Analysis Use |
|-------|-----------------|--------------|
| `task_completed` | trace_id, duration_ms, session_id | Success counting, latency |
| `task_failed` | trace_id, error info, session_id | Failure counting, error classification |
| `model_call_completed` | model_id, role, latency_ms, tokens_prompt, tokens_completion | LLM performance |
| `model_call_error` | model_id, error, latency_ms | LLM error tracking |
| `tool_call_completed` | tool_name, latency_ms, success | Tool performance |
| `tool_call_failed` | tool_name, error | Tool error tracking |
| `state_transition` | from_state, to_state | Pipeline phase tracking |
| `mode_transition` | from_mode, to_mode, reason | System stability |
| `system_metrics_snapshot` | cpu_load_percent, mem_used_percent | Resource health |
| `request_received` / `reply_ready` | trace_id, timestamp | End-to-end latency |

### 2.2 Schema Sufficiency Assessment

**Sufficient for:**
- Recent-error summaries: `task_failed` + `model_call_error` + `tool_call_failed` events contain error type, component, and trace_id.
- Root-cause hints: `model_call_error` has model_id; `tool_call_failed` has tool_name; trace_id enables drill-down.
- Trend snapshots: Events have timestamps; counting by period is straightforward with aggregation.

**Missing/ambiguous:**
- No `error_category` field on failure events — the LLM must infer category from free-text error messages.
- `task_failed` doesn't always include a structured `error_type` — some failures only have `exc_info` in the log.
- No `interaction_count` or `request_number` field — interactions must be counted from `task_completed` + `task_failed` events or from Captain's Log captures.

### 2.3 Captain's Log Captures — Already Ideal

`TaskCapture` records in `captains_log/capture.py` are the best data source for interaction-level queries:

- One record per user interaction
- Structured `outcome` field: "completed" / "failed" / "timeout"
- `tools_used` list for tool analysis
- `duration_ms` for latency
- `metrics_summary` dict with CPU, memory averages
- `user_message` for context
- Already indexed in ES (`agent-captains-captures-*`)
- `read_captures()` function exists for disk-based queries

**Key insight**: Captain's Log captures should be the primary data source for the health tool, not raw log events.

---

## 3. Storage/Query Architecture Recommendation

### 3.1 Options Evaluated

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A: JSONL + summarization** | Keep current JSONL scanning, add aggregation in executor | No new dependencies, simple | O(n) scan, poor for large windows |
| **B: Elasticsearch primary** | Route all queries through ES aggregations | Indexed, fast aggregations, already receiving data | ES dependency for tool to work; adds latency for simple queries |
| **C: Hybrid (ES + Prometheus)** | Metrics in Prometheus, events in ES | Best-of-breed per data type | Operational complexity; Prometheus adds a new dependency |

### 3.2 Recommendation: Tiered Hybrid (Captain's Log + JSONL + ES)

**None of the original three options are optimal.** The right answer uses what already exists:

**Tier 1 — Captain's Log captures (interaction-level queries)**
- Primary source for `health`, `errors`, `interactions` query types
- `read_captures()` already exists, queries by date, returns structured `TaskCapture` objects
- Fast for recent windows (reads only relevant date directories)
- No additional dependencies

**Tier 2 — JSONL logs (event-level detail, trace reconstruction)**
- Used for `trace` and `latency` query types (existing behavior)
- Used for drill-down when Captain's Log captures need event-level context
- Acceptable performance for short windows (< 24h)

**Tier 3 — Elasticsearch (long-range aggregations, trend analysis)**
- Used for `performance` and `trends` query types over large windows (> 24h)
- `TelemetryQueries` class already implements percentiles, daily counts, task patterns
- Optional: tool gracefully degrades to Tier 1+2 when ES is unavailable

### 3.3 Rationale

| Criterion | Score |
|-----------|-------|
| Query ergonomics | High — Captain's Log captures map directly to interaction-level questions |
| Time-window precision | High — captures have precise timestamps; ES has indexed time ranges |
| Operational complexity | Low — uses only existing infrastructure (files, ES already running) |
| Actionability | High — pre-computed summaries from structured captures, not raw logs |
| Graceful degradation | Good — Tier 1+2 work without ES; ES adds performance, not correctness |

### 3.4 Why Not Prometheus?

Prometheus solves a problem we don't have (high-cardinality time-series with sub-second resolution for alerting/dashboarding). Our system:
- Processes 1-100 interactions/day, not thousands/second
- Already has Kibana dashboards for visual monitoring
- Needs interaction-level summaries, not metric counters

Adding Prometheus would increase operational complexity with marginal benefit.

---

## 4. Implementation Proposal Summary

The full implementation spec is in `docs/specs/AGENT_HEALTH_TOOL_SPEC.md`. Key decisions:

### 4.1 Evolve Existing Tool, Don't Replace

Extend `self_telemetry_query` with new `query_type` values rather than creating a new tool. This keeps the tool list compact (original spec's rationale still applies).

### 4.2 New Query Types

| Query Type | Primary Data Source | Returns |
|-----------|-------------------|---------|
| `health` | Captain's Log captures + system metrics + JSONL events | Structured health verdict |
| `errors` | Captain's Log captures + JSONL events | Error summary with grouping |
| `interactions` | Captain's Log captures | Recent interaction list |
| `performance` | Captain's Log captures + ES (optional) | Latency and throughput summary |
| `trace` | JSONL logs (unchanged) | Single trace events |
| `latency` | JSONL logs (unchanged) | Phase breakdown for one trace |

### 4.3 Dual Scoping: Time + Count

New `last_n` parameter alongside existing `window` for interaction-count scoping. Both are optional; `health` defaults to `window=1h`, `interactions` defaults to `last_n=10`.

### 4.4 Named Time Windows

Extend `_parse_time_window()` to support `today`, `yesterday`, `this_week` in addition to `Xh`/`Xm`/`Xd`.

### 4.5 Pre-Computed Summaries

The executor computes aggregations (counts, rates, groupings, trends) before returning. The LLM receives a structured summary dict, not raw events. This is the single highest-impact change.

---

## 5. References

- Current tool: `src/personal_agent/tools/self_telemetry.py`
- Current spec: `docs/specs/SELF_TELEMETRY_TOOL_SPEC.md`
- Captain's Log captures: `src/personal_agent/captains_log/capture.py`
- JSONL metrics: `src/personal_agent/telemetry/metrics.py`
- ES queries: `src/personal_agent/telemetry/queries.py`
- ADR-0004: Telemetry & Metrics Strategy
- ADR-0020: Request Traceability
- Implementation spec: `docs/specs/AGENT_HEALTH_TOOL_SPEC.md`
