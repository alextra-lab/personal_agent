# Telemetry Elasticsearch Integration

**Status**: ✅ Complete (2026-01-23)

## Overview

All logs and metrics are now automatically sent to Elasticsearch in addition to file/console output. This provides:
- Centralized log aggregation
- Searchable metrics history
- Real-time monitoring via Kibana
- Correlation via trace_id

## Architecture

### Automatic Forwarding

```
structlog (application logs)
    ↓
ElasticsearchHandler (async)
    ↓
Elasticsearch (daily indices: agent-logs-YYYY.MM.DD)
    ↓
Kibana (visualization & search)
```

**Key Components:**

1. **ElasticsearchHandler** (`src/personal_agent/telemetry/es_handler.py`)
   - Custom logging.Handler that forwards to Elasticsearch
   - Async logging (non-blocking)
   - Graceful degradation (logs still go to files if ES unavailable)
   - Automatic JSON serialization

2. **Service Integration** (`src/personal_agent/service/app.py`)
   - ES handler connected during startup
   - Added to logging system via `add_elasticsearch_handler()`
   - All logs automatically forwarded (no manual calls needed)

### What Gets Sent

**All Events:**
- Application logs (INFO, WARNING, ERROR, etc.)
- Metrics (SENSOR_POLL, SYSTEM_METRICS_SNAPSHOT)
- Orchestrator events (TASK_STARTED, TASK_COMPLETED, etc.)
- Tool execution events
- Mode transitions
- Captain's Log events

**Fields Captured:**
- `@timestamp`: ISO timestamp
- `event_type`: Event name (e.g., "task_started", "sensor_poll")
- `trace_id`: Request trace ID for correlation
- `span_id`: Span ID (if present)
- `level`: Log level (INFO, WARNING, ERROR, etc.)
- `logger`: Logger name (e.g., "orchestrator", "sensors")
- `component`: Component name extracted from logger
- All custom fields from the log event

## Usage

### No Code Changes Required

All existing logging automatically goes to Elasticsearch:

```python
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Automatically sent to ES + files
log.info("task_started", task_id="123", trace_id="abc")

# Metrics automatically sent to ES
log.info(SYSTEM_METRICS_SNAPSHOT, cpu_percent=45.2, memory_percent=62.5)
```

### Querying via Kibana

1. Open Kibana: http://localhost:5601
2. Create index pattern: `agent-logs-*`
3. Search by:
   - Event type: `event_type: "task_started"`
   - Trace ID: `trace_id: "your-trace-id"`
   - Component: `component: "orchestrator"`
   - Time range: Use Kibana's time picker

### Querying Programmatically

```python
from personal_agent.telemetry.es_handler import ElasticsearchHandler

es_handler = ElasticsearchHandler()
await es_handler.connect()

# Search events
events = await es_handler.es_logger.search_events(
    event_type="task_started",
    trace_id="abc",
    limit=100
)
```

## Metrics in Elasticsearch

### Sensor Polls (Background Monitoring)

```json
{
  "@timestamp": "2026-01-23T14:30:00Z",
  "event_type": "sensor_poll",
  "level": "DEBUG",
  "component": "sensors",
  "cpu_load": 45.2,
  "memory_used": 62.5,
  "gpu_load": 15.3
}
```

### System Metrics Snapshots

```json
{
  "@timestamp": "2026-01-23T14:30:05Z",
  "event_type": "system_metrics_snapshot",
  "level": "INFO",
  "component": "sensors",
  "trace_id": "abc-123",
  "cpu_percent": 45.2,
  "memory_percent": 62.5,
  "disk_percent": 78.1,
  "gpu_percent": 15.3,
  "cpu_count": 10,
  "disk_total_gb": 500.0,
  "disk_free_gb": 109.5
}
```

### Request-Scoped Metrics (RequestMonitor)

```json
{
  "@timestamp": "2026-01-23T14:30:10Z",
  "event_type": "system_metrics_snapshot",
  "trace_id": "request-abc",
  "cpu_percent": 48.1,
  "memory_percent": 63.2,
  "gpu_percent": 18.5
}
```

## Configuration

### Enable/Disable

Elasticsearch integration is enabled by default when the service starts. To disable:

```python
# In service startup, comment out:
# if await es_handler.connect():
#     add_elasticsearch_handler(es_handler)
```

### Index Configuration

Indices use daily rotation: `agent-logs-YYYY.MM.DD`

ILM policy: `docker/elasticsearch/ilm-policy.json` (30 day retention)

Index template: `docker/elasticsearch/index-template.json`

## Benefits

### Before (File-Only Logging)

- ❌ Logs scattered across rotated files
- ❌ No centralized search
- ❌ Hard to correlate events across components
- ❌ No real-time monitoring
- ❌ Manual grep required for analysis

### After (ES Integration)

- ✅ Centralized log aggregation
- ✅ Full-text search across all logs
- ✅ Trace ID correlation (find all events for a request)
- ✅ Real-time monitoring in Kibana
- ✅ Time-series analysis of metrics
- ✅ Dashboards and visualizations
- ✅ Alerting capabilities (future)

## Testing

### Verify Logs Are Being Sent

1. Start services:
   ```bash
   docker compose up -d
   uv run uvicorn personal_agent.service.app:app --port 9000
   ```

2. Generate some logs:
   ```bash
   curl http://localhost:9000/health
   ```

3. Check Elasticsearch:
   ```bash
   curl http://localhost:9200/agent-logs-*/_search?size=10 | jq
   ```

4. Check Kibana: http://localhost:5601

### Verify Metrics Are Being Sent

1. Make a request that triggers metrics:
   ```bash
   curl -X POST "http://localhost:9000/chat?message=Hello"
   ```

2. Search for metrics in Kibana:
   - Event type: `system_metrics_snapshot`
   - Component: `sensors` or `request_monitor`

## Troubleshooting

### Logs Not Appearing in Elasticsearch

1. Check ES handler connected:
   ```bash
   curl http://localhost:9000/health | jq '.components.elasticsearch'
   ```

2. Check Elasticsearch is running:
   ```bash
   curl http://localhost:9200
   ```

3. Check indices exist:
   ```bash
   curl http://localhost:9200/_cat/indices?v
   ```

4. Check service logs for errors:
   ```bash
   tail -f telemetry/logs/current.jsonl | grep elasticsearch
   ```

### High Latency

The ES handler uses async logging (non-blocking), so it should not impact request latency. If you see issues:

1. Check Elasticsearch performance
2. Reduce log verbosity (set log_level=WARNING)
3. Disable debug-level metrics logging

### Storage Growth

Daily indices with 30-day retention (configurable in ILM policy).

Monitor storage:
```bash
curl http://localhost:9200/_cat/indices/agent-logs-*?v&h=index,store.size
```

## Future Enhancements

- [ ] Metrics aggregation dashboards in Kibana
- [ ] Alerting rules for threshold violations
- [ ] Structured metrics (separate from logs) in Elasticsearch time-series data streams
- [ ] Performance metrics (P50, P95, P99 latencies)
- [ ] Cost tracking dashboard
