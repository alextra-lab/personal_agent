# Kibana Intent Classification Dashboard

> Cognitive Architecture Redesign v2 — Slice 1 Telemetry

This guide documents how to configure a Kibana dashboard for monitoring
the gateway pipeline's intent classification telemetry.

## Prerequisites

- Elasticsearch running at `http://localhost:9200`
- Kibana running at `http://localhost:5601`
- Personal Agent service running with structlog → ElasticsearchHandler active

## Index Pattern

Create an index pattern matching: **`agent-*`**

This matches the existing log index pattern used by the
`ElasticsearchHandler` in the service.

## Event Reference

The gateway pipeline emits a `gateway_output` structured log
event after every request. Filter all visualizations with:

```
event: "gateway_output"
```

### Available Fields

| Field | Type | Description |
|-------|------|-------------|
| `task_type` | keyword | Intent classification result (`conversational`, `memory_recall`, `analysis`, `planning`, `delegation`, `self_improve`, `tool_use`) |
| `complexity` | keyword | Estimated complexity (`simple`, `moderate`, `complex`) |
| `confidence` | float | Classification confidence (0.0–1.0) |
| `signals` | keyword[] | Pattern signals that matched (e.g., `memory_recall_pattern`, `coding_pattern`) |
| `mode` | keyword | Governance mode (`normal`, `alert`, `emergency`) |
| `expansion_permitted` | boolean | Whether expansion is allowed in current mode |
| `strategy` | keyword | Decomposition strategy (`single`, `sequential`, `parallel`) |
| `message_count` | integer | Number of messages in assembled context |
| `token_count` | integer | Estimated token count (if available) |
| `has_memory` | boolean | Whether memory context was included |
| `degraded_stages` | keyword[] | Stages that degraded gracefully |
| `trace_id` | keyword | Request trace identifier for correlation |

## Visualizations

### 1. Task Type Distribution (Pie Chart)

Shows the distribution of intent classifications across all requests.

- **Type**: Pie chart
- **Metrics**: Count
- **Buckets**: Split slices → Terms aggregation on `task_type`
- **Size**: 7 (one per TaskType value)

### 2. Intent Classification Over Time (Time Series)

Shows intent classification volume over time, split by task type.

- **Type**: TSVB (Time Series Visual Builder) or Line chart
- **Metrics**: Count
- **Group by**: `task_type`
- **X-axis**: Date histogram on `@timestamp`
- **Interval**: Auto or 1h

### 3. Confidence Distribution (Histogram)

Shows the distribution of classification confidence scores.

- **Type**: Histogram
- **Metrics**: Count
- **X-axis**: Histogram on `confidence`
- **Interval**: 0.1 (gives 10 bins from 0.0 to 1.0)

### 4. Signals Breakdown (Data Table)

Shows which pattern signals fire most frequently.

- **Type**: Data table
- **Metrics**: Count
- **Buckets**: Split rows → Terms aggregation on `signals`
- **Size**: 20
- **Order by**: Count (descending)

## Dashboard Assembly

1. Navigate to **Kibana → Dashboard → Create new dashboard**
2. Add each visualization created above
3. Set the global filter: `event: "gateway_output"`
4. Set a reasonable time range (e.g., Last 24 hours)
5. Save as **"Intent Classification Dashboard"**

## Notes

- The `degraded_stages` field will contain entries like
  `context_assembly:memory_unavailable` when Neo4j is down
- The `trace_id` field can be used to correlate with other service logs
- This dashboard reads from the same ES indices as the existing
  telemetry dashboards documented in `KIBANA_DASHBOARDS.md`
