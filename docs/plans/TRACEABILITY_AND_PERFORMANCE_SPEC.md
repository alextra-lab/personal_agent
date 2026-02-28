# Traceability and Performance Spec

**ADRs:** ADR-0020 (Request Traceability), ADR-0021 (Continuous Metrics Daemon)
**Projects:** 2.3 Homeostasis & Feedback, 2.6 Conversational Agent MVP
**Date:** 2026-02-23

---

## Task 1: CLI `--new` Flag Bug Fix

**Priority:** Urgent
**Project:** 2.6 Conversational Agent MVP
**File:** `src/personal_agent/ui/service_cli.py`

### Problem

`agent chat "message" --new` fails with `No such command 'message'`. The `main` callback (line 118) defines an optional positional `message` argument with `invoke_without_command=True`. Typer consumes the string `chat` as that positional argument instead of recognizing it as a subcommand.

### Fix

Remove the optional `message` argument and `--new` option from the `main` callback. Direct `agent "message"` usage without the `chat` subcommand is dropped — it creates the parsing ambiguity.

**Replace lines 118-136 with:**

```python
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Personal Agent CLI."""
    if ctx.invoked_subcommand is None:
        console.print("[yellow]No command provided. Use `agent --help` for usage.[/yellow]")
        raise typer.Exit(1)
```

The `chat` command (lines 139-149) stays unchanged — it already has `message` and `--new` defined correctly.

### Validation

```bash
agent chat "hello" --new        # works
agent chat --new "hello"        # works
agent chat "hello"              # works (reuses session)
agent --help                    # shows chat subcommand
```

### Also: Return trace_id in CLI output

In `_send_chat()` (line 110), add trace_id to the dim output:

```python
trace_id = data.get("trace_id", "")
console.print(f"[dim]session: {resolved_session_id}  trace: {trace_id}[/dim]")
```

In `app.py` `/chat` endpoint, include `trace_id` in the response dict (line 414):

```python
return {"session_id": str(session.session_id), "response": response_content, "trace_id": trace_id}
```

---

## Task 2: CLI `--compress` Flag

**Priority:** Normal
**Project:** 2.6 Conversational Agent MVP
**Files:** `src/personal_agent/ui/service_cli.py`, `src/personal_agent/service/app.py`

### Design

Add `--compress` to the `chat` command. When set, the current conversation history is archived and the LLM receives a clean context window. The session ID is preserved.

### CLI changes (`service_cli.py`)

Add to `chat` command parameters:

```python
compress: bool = typer.Option(
    False, "--compress",
    help="Archive context to memory and start with clean context window.",
),
```

Pass to `_send_chat()` and include as query param:

```python
params={"message": message, "session_id": session_id, "compress": str(compress).lower()}
```

### Service changes (`app.py`)

In the `/chat` endpoint, accept `compress: bool = Query(False)`.

When `compress=True`, before the orchestrator call:

1. If `memory_service` is connected and `prior_messages` is non-empty:
   - Call a summarization LLM to produce a short summary of `prior_messages` (use the standard model role, set `max_tokens=256`)
   - Store the summary as a conversation node in the memory service with `summary` field populated and a tag `compressed=True`
   - Log a `context_compressed` event with `trace_id`, `original_message_count`, `summary_length`
2. Clear `prior_messages` to an empty list (the orchestrator gets a fresh context)
3. Optionally: clear the session messages in the DB via `repo.clear_messages(session.session_id)` or mark them as archived

### LLM summarization prompt

```
Summarize the following conversation in 2-3 sentences, preserving key topics, decisions, and any unresolved questions:

{formatted_messages}
```

Use `llm_client.respond()` with `role=ModelRole.STANDARD`, `max_tokens=256`.

### Future

This becomes the bridge to Seshat (ADR-0018). When Seshat is implemented, `--compress` triggers a full memory consolidation instead of simple summarization. The context window is then assembled dynamically from Seshat's memory types (episodic, semantic, working, etc.) rather than raw message history.

---

## Task 3: Request Traceability — Data Model

**Priority:** High
**Project:** 2.3 Homeostasis & Feedback
**ADR:** ADR-0020
**File:** `src/personal_agent/telemetry/request_timer.py`

### Changes to `RequestTimer`

1. Add `_sequence_counter: int = 0` instance field.

2. In `end_span()`, increment `_sequence_counter` and assign to the completed span:

```python
def end_span(self, name: str, **metadata: Any) -> float:
    start_ns = self._active.pop(name, None)
    if start_ns is None:
        return 0.0
    self._sequence_counter += 1
    end_ns = time.monotonic_ns()
    duration_ms = round((end_ns - start_ns) / 1_000_000, 2)
    offset_ms = round((start_ns - self._start_ns) / 1_000_000, 2)
    phase = _classify_phase(name)
    self._spans.append(
        TimingSpan(
            name=name,
            sequence=self._sequence_counter,
            phase=phase,
            offset_ms=offset_ms,
            duration_ms=duration_ms,
            metadata=dict(metadata),
        )
    )
    return duration_ms
```

3. Add `sequence: int` and `phase: str` fields to the `TimingSpan` dataclass.

4. Add phase classification function:

```python
_PHASE_MAP: list[tuple[str, str]] = [
    ("session_", "setup"),
    ("orchestrator_setup", "setup"),
    ("context_window", "context"),
    ("memory_query", "context"),
    ("llm_call:router", "routing"),
    ("routing_", "routing"),
    ("llm_call:", "llm_inference"),
    ("tool_execution:", "tool_execution"),
    ("synthesis", "synthesis"),
    ("session_update", "synthesis"),
    ("db_append_", "persistence"),
    ("memory_storage", "persistence"),
]

def _classify_phase(span_name: str) -> str:
    for prefix, phase in _PHASE_MAP:
        if span_name.startswith(prefix):
            return phase
    return "other"
```

5. Add `to_trace_summary()` method:

```python
def to_trace_summary(self) -> dict[str, Any]:
    phases: dict[str, dict[str, float | int]] = {}
    for span in self._spans:
        if span.phase not in phases:
            phases[span.phase] = {"duration_ms": 0.0, "steps": 0}
        phases[span.phase]["duration_ms"] += span.duration_ms
        phases[span.phase]["steps"] += 1
    return {
        "total_duration_ms": self.get_total_ms(),
        "total_steps": len(self._spans),
        "phases_summary": phases,
    }
```

6. Update `to_breakdown()` to include `sequence` and `phase` in each entry.

### Future fields (add but don't populate yet)

Add to `TimingSpan`:
- `parent_sequence: int | None = None`
- `span_id: str = ""` (auto-generated UUID in `end_span`)
- `depth: int = 0`

---

## Task 4: Request Traceability — ES Indexing

**Priority:** High
**Project:** 2.3 Homeostasis & Feedback
**ADR:** ADR-0020
**Files:** `src/personal_agent/telemetry/es_logger.py`, `src/personal_agent/service/app.py`

### New method on `ElasticsearchLogger`

Replace `index_request_timing()` with `index_request_trace()`:

```python
async def index_request_trace(
    self,
    trace_id: str,
    timer: "RequestTimer",
    session_id: str | None = None,
) -> str | None:
```

This method:

1. Calls `timer.to_trace_summary()` to get the summary.
2. Indexes one `request_trace` document:

```json
{
    "@timestamp": "...",
    "event_type": "request_trace",
    "trace_id": "abc-123",
    "session_id": "...",
    "total_duration_ms": 18200,
    "total_steps": 12,
    "phases_summary": { "setup": {"duration_ms": 450, "steps": 3}, ... }
}
```

Document ID: `trace_{trace_id}` (idempotent).

3. Indexes one `request_trace_step` document per completed span:

```json
{
    "@timestamp": "...",
    "event_type": "request_trace_step",
    "trace_id": "abc-123",
    "session_id": "...",
    "sequence": 4,
    "phase": "routing",
    "name": "llm_call:router",
    "offset_ms": 1200,
    "duration_ms": 2100,
    "total_duration_ms": 18200
}
```

Document ID: `trace_{trace_id}_step_{sequence}` (idempotent).

Step metadata (tokens, model_role, etc.) is flattened into the step document as top-level fields.

### Changes to `app.py`

Replace the `index_request_timing()` call (line 405) with:

```python
asyncio.create_task(
    es_handler.es_logger.index_request_trace(
        trace_id=trace_id,
        timer=timer,
        session_id=str(session.session_id),
    )
)
```

### ES field mapping note

All string fields used in Kibana terms aggregations (`phase`, `name`, `trace_id`) will be auto-mapped as `text` with a `.keyword` sub-field. The Kibana dashboard must use `.keyword` for aggregations. Consider adding an index template:

```json
PUT _index_template/agent-logs-template
{
  "index_patterns": ["agent-logs-*"],
  "template": {
    "mappings": {
      "properties": {
        "phase": { "type": "keyword" },
        "name": { "type": "keyword" },
        "trace_id": { "type": "keyword" },
        "session_id": { "type": "keyword" },
        "event_type": { "type": "keyword" }
      }
    }
  }
}
```

This should be applied once via a setup script (add to `config/kibana/setup_dashboards.py`).

---

## Task 5: Request Traceability — Kibana Dashboard

**Priority:** Normal
**Project:** 2.3 Homeostasis & Feedback
**ADR:** ADR-0020
**File:** `config/kibana/setup_dashboards.py`

### Dashboard: "Request Traces"

Add a new `create_request_traces()` function to the dashboard setup script.

**Panel 1 — Request Overview** (bar chart):
- Query: `event_type:request_trace`
- X-axis: `@timestamp` (date_histogram)
- Y-axis: `total_duration_ms` (max, per bucket)
- Color: by `trace_id.keyword`
- Purpose: shows each request as a bar, height = total duration

**Panel 2 — Phase Averages** (horizontal stacked bar):
- Query: `event_type:request_trace_step`
- X-axis: avg `duration_ms`
- Segments: `phase.keyword` (terms)
- Purpose: average time per phase category across all requests. Works regardless of step count because phases are fixed categories.

**Panel 3 — Single Trace Waterfall** (horizontal bar):
- Query: `event_type:request_trace_step`
- Intended for single-trace view (user applies trace_id filter via Panel 1 click or dropdown)
- Y-axis: `name.keyword` ordered by `sequence`
- X-start: `offset_ms`, X-length: `duration_ms`
- Color: by `phase.keyword`
- Note: Kibana's native aggregation-based charts can't do true Gantt rendering. Approximate with a horizontal bar where X-axis = `offset_ms` and bar length approximated by `duration_ms`. Alternatively, use a data table with conditional formatting, or a Vega visualization if needed.

**Panel 4 — Trace Detail Table**:
- Query: `event_type:request_trace_step`
- Columns: `sequence`, `phase.keyword`, `name.keyword`, `duration_ms`, `offset_ms`
- Sort: `sequence` ascending
- Shows full step-by-step breakdown for the filtered trace

**Panel 5 — Trace Selector** (controls):
- Kibana Options List control filtering on `trace_id.keyword` from `request_trace` events
- Applying a selection filters Panels 3 and 4 to that single trace

### Waterfall visualization note

A true Gantt/waterfall chart is not natively supported by Kibana's agg-based visualizations. Options in priority order:

1. **Horizontal bar with offset** — approximate by using `offset_ms` as the metric and `duration_ms` as a secondary metric. Not perfect but usable.
2. **Vega/Vega-Lite** — Kibana supports custom Vega visualizations. A Vega spec can render a proper Gantt chart from `request_trace_step` data. More complex to implement but accurate.
3. **Data table with sparkline** — show the table (Panel 4) as the primary detail view, with a simple stacked bar (by phase) as the visual summary.

Recommend starting with option 3 (table + stacked bar), with a follow-up issue for Vega if needed.

---

## Task 6: Metrics Daemon

**Priority:** High
**Project:** 2.3 Homeostasis & Feedback
**ADR:** ADR-0021
**New file:** `src/personal_agent/brainstem/sensors/metrics_daemon.py`

### MetricsDaemon class

```python
@dataclass
class MetricsSample:
    timestamp: float       # time.time()
    metrics: dict[str, Any]  # raw output of poll_system_metrics()

class MetricsDaemon:
    def __init__(
        self,
        poll_interval_seconds: float = 5.0,
        es_emit_interval_seconds: float = 30.0,
        buffer_size: int = 720,
    ): ...

    async def start(self) -> None:
        """Start the background polling task."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling and cancel the task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    def get_latest(self) -> MetricsSample | None:
        """Non-blocking read of most recent sample."""
        return self._latest

    def get_window(self, seconds: float) -> list[MetricsSample]:
        """Get samples from the last N seconds."""
        cutoff = time.time() - seconds
        return [s for s in self._buffer if s.timestamp >= cutoff]

    async def _poll_loop(self) -> None:
        polls_since_emit = 0
        while self._running:
            try:
                raw = await asyncio.to_thread(poll_system_metrics)
                sample = MetricsSample(timestamp=time.time(), metrics=raw)
                self._latest = sample
                self._buffer.append(sample)

                polls_since_emit += 1
                if polls_since_emit >= self._es_emit_every_n:
                    log.info(SENSOR_POLL, cpu_load=..., memory_used=..., ...)
                    polls_since_emit = 0
            except Exception as e:
                log.error("metrics_daemon_poll_error", error=str(e))

            await asyncio.sleep(self._poll_interval)
```

### Integration with `app.py`

In the lifespan context manager, start the daemon and make it available:

```python
metrics_daemon = MetricsDaemon(
    poll_interval_seconds=settings.metrics_daemon_poll_interval_seconds,
    es_emit_interval_seconds=settings.metrics_daemon_es_emit_interval_seconds,
)
await metrics_daemon.start()
app.state.metrics_daemon = metrics_daemon
# ... on shutdown:
await metrics_daemon.stop()
```

### Refactor `RequestMonitor`

Remove `_monitor_loop()` entirely. Replace with:

```python
class RequestMonitor:
    def __init__(self, trace_id: str, daemon: MetricsDaemon):
        self.trace_id = trace_id
        self.daemon = daemon
        self._start_time: float | None = None

    async def start(self) -> None:
        self._start_time = time.time()
        log.info("request_monitor_started", trace_id=self.trace_id)

    async def stop(self) -> dict[str, Any]:
        elapsed = time.time() - (self._start_time or time.time())
        samples = self.daemon.get_window(seconds=elapsed)
        summary = self._compute_summary_from_samples(samples)
        log.info("request_monitor_stopped", trace_id=self.trace_id, ...)
        return summary
```

### Refactor `scheduler.py`

Replace `await asyncio.to_thread(poll_system_metrics)` with:

```python
daemon = app.state.metrics_daemon  # or pass daemon reference at init
latest = daemon.get_latest()
if latest:
    metrics = latest.metrics
```

### Config additions (`settings.py`)

```python
metrics_daemon_poll_interval_seconds: float = Field(default=5.0, ge=1.0)
metrics_daemon_es_emit_interval_seconds: float = Field(default=30.0, ge=5.0)
metrics_daemon_buffer_size: int = Field(default=720, ge=60)
```

---

## Task 7: Context Window Optimization

**Priority:** Normal
**Project:** 2.3 Homeostasis & Feedback
**Files:** `src/personal_agent/orchestrator/context_window.py`, `src/personal_agent/config/settings.py`

### Problem

5,511 prompt tokens for a one-sentence question. The LLM spends ~12 seconds on context prefill.

### Investigation steps

1. Read `apply_context_window()` in `context_window.py` — understand what it passes.
2. Check `settings.conversation_max_history_messages` — may be too high.
3. Log the actual message count and token estimate entering the LLM call.

### Likely fixes (implement in order of impact)

1. **Reduce `conversation_max_history_messages`** to 10 (from current value, likely 50+). Add to `settings.py` if not already configurable.

2. **Add a token budget setting** — `context_window_max_tokens: int = 2048`. In `apply_context_window()`, truncate from oldest until under budget. This caps prefill time regardless of conversation length.

3. **System prompt audit** — check if the system prompt is bloated. Read the system prompt template and measure its token count. Trim if over 500 tokens.

### Measurement

Before and after, log:
```python
log.info("context_window_applied",
    trace_id=ctx.trace_id,
    input_messages=len(messages_before),
    output_messages=len(messages_after),
    estimated_tokens=estimate_messages_tokens(messages_after))
```

This event already exists (`context_window_applied`) — verify it includes token count.

---

## Task 8: Kibana Field Mapping Fix

**Priority:** High
**Project:** 2.3 Homeostasis & Feedback
**File:** `config/kibana/setup_dashboards.py`

### Problem

ES auto-maps string fields as `text` with a `.keyword` sub-field. Kibana aggregation-based visualizations require `keyword` type for terms aggregations. Current dashboards use `role`, `model_id`, `phase` without the `.keyword` suffix, so those panels show empty.

### Fix

1. Add an ES index template at the top of `setup_dashboards.py`:

```python
def create_index_template():
    """Ensure agent-logs fields are mapped as keyword for aggregation."""
    template = {
        "index_patterns": ["agent-logs-*"],
        "template": {
            "mappings": {
                "properties": {
                    "event_type": {"type": "keyword"},
                    "trace_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "phase": {"type": "keyword"},
                    "name": {"type": "keyword"},
                    "role": {"type": "keyword"},
                    "model_id": {"type": "keyword"},
                    "from_state": {"type": "keyword"},
                    "to_state": {"type": "keyword"},
                    "delegated_role": {"type": "keyword"},
                    "component": {"type": "keyword"},
                }
            }
        }
    }
    # PUT _index_template/agent-logs-template via urllib
```

2. After applying the template, reindex existing data or create a new daily index (the template only applies to new indices).

3. Remove all `.keyword` suffixes from the dashboard visualization field references — once fields are mapped as `keyword`, the suffix is unnecessary.

### Validation

After running the script, all panels in LLM Performance, System Health, and Task Analytics dashboards should show data.

---

## Task 9: State Transition Logging Fix

**Priority:** Low
**Project:** 2.3 Homeostasis & Feedback
**File:** `src/personal_agent/orchestrator/executor.py`

### Problem

`state_transition` events log `from_state` but not `to_state`. The System Health dashboard's State Transitions panel can only show partial data.

### Fix

Find the `log.info(STATE_TRANSITION, ...)` call in `executor.py` and add `to_state`:

```python
log.info(
    STATE_TRANSITION,
    trace_id=ctx.trace_id,
    from_state=old_state,
    to_state=new_state,   # ADD THIS
    component="executor",
)
```

Search for all `STATE_TRANSITION` emissions and ensure both `from_state` and `to_state` are present.
