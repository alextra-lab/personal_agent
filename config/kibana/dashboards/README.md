# Kibana dashboards

This folder holds Kibana saved objects for Phase 2.3 dashboards.

## Contents

- **data_views.ndjson** — Data views (index patterns) for Captain's Log and agent logs. Import this first so the indices are available when building or opening dashboards.
- **task_analytics.ndjson** — Task Analytics dashboard (task outcomes, duration by tool, tool frequency, memory usage).
- **reflection_insights.ndjson** — Reflection Insights dashboard (proposed changes over time, improvement categories, impact, metrics).
- **system_health.ndjson** — System Health dashboard (CPU/memory timeline, mode transitions, consolidation, threshold violations).

## Import

See [KIBANA_DASHBOARDS.md](../../docs/KIBANA_DASHBOARDS.md) for:

- Step-by-step import (UI and API)
- How to create or re-export dashboards using Kibana’s Lens and dashboard docs
- Index patterns and field reference

## Order

1. Import `data_views.ndjson`.
2. Import dashboard NDJSON files (they include complete visualizations and layout).
3. If you customize panels, re-export updated NDJSON back into this folder.

## Data in the dashboards

Data is written to Elasticsearch by the **Personal Agent service** when it is running (captures and reflections on each task, plus agent-logs from structlog). Start the service with:

```bash
uv run uvicorn personal_agent.service.app:app --reload --port 9000
```

Ensure Elasticsearch (and optionally Kibana) are up first, e.g. `docker compose up -d elasticsearch kibana`. See [KIBANA_DASHBOARDS.md](../../docs/KIBANA_DASHBOARDS.md) for full prerequisites and import steps.
