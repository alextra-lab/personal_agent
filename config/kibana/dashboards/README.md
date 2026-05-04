# Kibana dashboards

This folder holds Kibana saved objects. Each file contains one dashboard and all its referenced visualizations and index-patterns.

## Contents

- **data_views.ndjson** — All index-patterns and saved searches. Import this first.
- **system_health.ndjson** — System Health (CPU/memory timeline, state transitions, consolidation events, errors).
- **task_analytics.ndjson** — Task Analytics (task outcomes, duration by tool, tool frequency, entity/memory enrichment).
- **request_latency.ndjson** — Request Latency (request-to-reply latency by phase, P95, trace table).
- **request_timing.ndjson** — Request Timing E2E (per-phase breakdown, waterfall, duration over time).
- **request_traces.ndjson** — Request Traces (single-trace drilldown, phase averages, trace selector).
- **reflection_insights.ndjson** — Reflection Insights (proposed changes over time, improvement categories, impact, metrics).
- **insights_engine.ndjson** — Insights Engine (insight count by type, confidence trend, anomalies, weekly proposals).
- **extraction_retry_health.ndjson** — Extraction Retry Health (median attempts, dead-letter rate, denial_reason distribution).
- **llm_performance.ndjson** — LLM Performance (call count by model, latency, token usage, errors over time).
- **delegation_outcomes.ndjson** — Delegation Outcomes (volume by agent, success rate, rounds needed, satisfaction).
- **expansion_decomposition.ndjson** — Expansion & Decomposition (strategy distribution, sub-agent spawn rate/success, context budget).
- **intent_classification.ndjson** — Intent Classification (task type distribution, confidence scores, signal frequency).

## Import

Use the script (imports all dashboards in dependency order):

```bash
./config/kibana/import_dashboards.sh                        # local (http://localhost:5601)
KIBANA_URL=http://kibana:5601 ./config/kibana/import_dashboards.sh  # inside Docker
```

Or see [KIBANA_DASHBOARDS.md](../../docs/guides/KIBANA_DASHBOARDS.md) for UI and manual API steps.

## Re-exporting after edits

After making changes in Kibana, re-export to keep repo in sync:

```bash
curl -s http://localhost:5601/api/saved_objects/_export \
  -H ‘kbn-xsrf: true’ -H ‘Content-Type: application/json’ \
  -d ‘{"type": ["dashboard","visualization","index-pattern","lens","search","map"], "includeReferencesDeep": true}’ \
  -o /tmp/kibana-live.ndjson
```

Then re-run the reconstruction script (see FRE-313 plan) to split the export back into per-dashboard files.

## Data in the dashboards

Data is written by the **Personal Agent service** when running. Start the stack then the service:

```bash
docker compose up -d elasticsearch kibana
uv run uvicorn personal_agent.service.app:app --reload --port 9000
```
