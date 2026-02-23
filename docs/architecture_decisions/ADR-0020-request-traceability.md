# ADR-0020: Request Traceability and Observability

**Status:** Accepted  
**Date:** 2026-02-23  
**Deciders:** Alex  
**Relates to:** ADR-0004 (Telemetry), ADR-0012 (Request-Scoped Metrics)

## Context

The agent processes user requests through a multi-step pipeline (session lookup → context assembly → routing → LLM inference → tool execution → synthesis). Currently, telemetry is fragmented across unrelated log events. There is no single view showing the full lifecycle of a request with sequential step numbering, timing, and identity at each phase.

The `RequestTimer` (introduced for FRE-37) records timing spans but lacks:
- Sequential step numbering for ordering
- Phase categorization for cross-request comparison
- Structured ES documents designed for drill-down visualization
- Support for future acyclic execution graphs (tool calls, A2A sub-traces)

## Decision

### 1. Sequential Step Numbering

Each `RequestTimer` maintains a monotonically increasing sequence counter. Every completed span receives a `sequence: int` assigned in order of completion. This provides unambiguous ordering even when spans overlap or nest.

### 2. Phase Categories

Each span is classified into a fixed set of phase categories:

| Phase | Span patterns |
|---|---|
| `setup` | `session_*`, `orchestrator_setup` |
| `context` | `context_window`, `memory_query` |
| `routing` | `llm_call:router`, `routing_*` |
| `llm_inference` | `llm_call:standard`, `llm_call:reasoning`, `llm_call:coding` |
| `tool_execution` | `tool_execution:*` |
| `synthesis` | `synthesis`, `session_update` |
| `persistence` | `db_append_*`, `memory_storage` |

Phase categories solve the variable-step-count problem: Request A (3 steps) and Request B (12 steps) both roll up into the same categories for aggregation.

### 3. ES Document Structure

Two document types per request:

- **`request_trace`** — one per request, contains summary with `total_duration_ms`, `total_steps`, and `phases_summary` (duration and step count per category)
- **`request_trace_step`** — one per step, flat documents with `sequence`, `phase`, `name`, `offset_ms`, `duration_ms`, and metadata. Designed for Kibana terms/avg aggregations.

### 4. Kibana Drill-Down

- Overview panel: bar chart of requests by `total_duration_ms` over time
- Phase average panel: stacked bar by phase category (works across variable step counts)
- Waterfall panel: horizontal bars per step, positioned by `offset_ms`, sized by `duration_ms`, colored by `phase` (single-trace view)
- Detail table: `request_trace_step` rows sorted by `sequence`

### 5. Future: Acyclic Graphs

When tool calls and A2A conversations are implemented, `TraceStep` gains:
- `parent_sequence: int | None` — links sub-steps to their parent
- `span_id: str` — unique ID for this step
- `depth: int` — nesting level (0 = root)

The sequence counter stays global within the root trace. Sub-traces are nested, not separate.

## Consequences

- **Positive:** Single dashboard provides full request lifecycle visibility. Phase categories enable cross-request comparison despite variable step counts. Sequential numbering provides unambiguous ordering.
- **Negative:** Additional ES documents per request (N+1 where N = step count). Phase classification must be maintained as new span types are added.
- **Risk:** Phase category mapping is a manual lookup table — new span names that don't match any pattern silently default to `unknown`.

## Implementation

- **Spec:** `docs/plans/TRACEABILITY_AND_PERFORMANCE_SPEC.md`
- **Files:** `src/personal_agent/telemetry/request_timer.py`, `src/personal_agent/telemetry/es_logger.py`, `config/kibana/setup_dashboards.py`
