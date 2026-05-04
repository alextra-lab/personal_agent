# Kibana Dashboards

<!-- markdownlint-disable MD060 MD036 -->

Task analytics, reflection insights, and system health visibility in Kibana. This doc describes the three dashboards, how to import and build them using Kibana’s [Lens](https://www.elastic.co/guide/en/kibana/current/lens.html) and [saved objects](https://www.elastic.co/guide/en/kibana/current/managing-saved-objects.html), and how to export/import dashboard JSON.

## Kibana documentation (use for best results)

- [Lens – Create visualizations](https://www.elastic.co/guide/en/kibana/current/lens.html) — chart types, fields, aggregations
- [Dashboards](https://www.elastic.co/docs/explore-analyze/dashboards) — create and edit dashboards
- [Saved objects – Import and export](https://www.elastic.co/docs/extend/kibana/saved-objects/export) — NDJSON format, APIs
- [Data views](https://www.elastic.co/docs/explore-analyze/find-and-organize/data-views) — index patterns in Kibana 8

---

## Overview

| Dashboard | File | Data view | Purpose |
| --------- | ---- | --------- | ------- |
| System Health | `system_health.ndjson` | `agent-logs-*` | CPU/memory timeline, state transitions, consolidation, errors |
| Task Analytics | `task_analytics.ndjson` | `agent-captains-captures-*` | Task outcomes, duration by tool, tool frequency, entity/memory enrichment |
| Request Latency | `request_latency.ndjson` | `agent-logs-*` | Request-to-reply latency by phase, P95 over time, trace table |
| Request Timing (E2E) | `request_timing.ndjson` | `agent-logs-*` | Per-phase breakdown, phase waterfall, duration over time |
| Request Traces | `request_traces.ndjson` | `agent-logs*` | Single-trace drilldown, phase averages, trace selector |
| Reflection Insights | `reflection_insights.ndjson` | `agent-captains-reflections-*` | Proposed changes over time, improvement categories, impact, metrics |
| Insights Engine | `insights_engine.ndjson` | `agent-insights-*` | Insight count by type, confidence trend, anomalies, weekly proposals |
| Extraction Retry Health | `extraction_retry_health.ndjson` | `agent-logs-*` | Median attempts to success, dead-letter rate, denial_reason breakdown |
| LLM Performance | `llm_performance.ndjson` | `agent-logs-*` | Call count by model, latency, token usage, errors over time |
| Delegation Outcomes | `delegation_outcomes.ndjson` | `agent-logs-*` | Delegation volume by agent, success rate, rounds needed, satisfaction |
| Expansion & Decomposition | `expansion_decomposition.ndjson` | `agent-logs-*` | Strategy distribution, sub-agent spawn/success rate, context budget |
| Intent Classification | `intent_classification.ndjson` | `agent-logs-*` | Task type distribution, confidence scores, signal frequency |

Dashboard JSON lives in **`config/kibana/dashboards/`**. Each file is self-contained (dashboard + visualizations + index-patterns). Use `import_dashboards.sh` to import everything in one step.

---

## Prerequisites

- Elasticsearch running with Captain’s Log and agent logs indices (see [TELEMETRY_ELASTICSEARCH_INTEGRATION.md](TELEMETRY_ELASTICSEARCH_INTEGRATION.md)).
- Kibana connected to the same Elasticsearch (e.g. `http://localhost:5601`).

Index patterns used by the dashboards:

- **agent-captains-captures-\*** — daily indices from `write_capture()`; time field: `timestamp`.
- **agent-captains-reflections-\*** — daily indices from `save_entry()` (reflections); time field: `timestamp`.
- **agent-logs-\*** — general telemetry from the ES handler; time field: `@timestamp`.

### Getting data into the dashboards

Dashboard panels stay empty until the **Personal Agent service** is running and sending data to Elasticsearch. The service writes captures and reflections on each chat/task and streams structured logs to the ES handler.

1. Start the stack (Elasticsearch + Kibana): `docker compose up -d elasticsearch kibana` (or use full `docker compose up -d`).
2. Start the Personal Agent service (connects to ES and indexes as it runs):

   ```bash
   uv run uvicorn personal_agent.service.app:app --reload --port 9000
   ```

3. Use the service (e.g. send chat requests to `http://localhost:9000/chat` or your usual client). New data will appear in the dashboards for the selected time range.

If you only import dashboards and never run the agent, indices may exist but have zero or stale documents—expand the time picker in Kibana or run the agent to see data.

---

## Import procedure

### 1. Import data views (required first)

So that the three index patterns exist in Kibana before you open or build dashboards.

#### Data views import (UI)

1. In Kibana: **Stack Management** → **Saved Objects**.
2. Click **Import**.
3. Choose **data_views.ndjson** from `config/kibana/dashboards/`.
4. Use options as needed (e.g. “Overwrite” if re-importing) and run **Import**.

#### Data views import (API)

```bash
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -H "Content-Type: multipart/form-data" \
  --form file=@config/kibana/dashboards/data_views.ndjson
```

After import you should see data views: `agent-captains-captures-*`, `agent-captains-reflections-*`, `agent-logs-*`, `agent-logs*`, `agent-insights-*`.

### 2. Import dashboards

#### Quickest: use the import script

```bash
# Imports all 12 dashboards in dependency order (local Kibana)
./config/kibana/import_dashboards.sh

# Against VPS Kibana via SSH tunnel:
# ssh -L 5601:localhost:5601 <your-vps-ssh-alias>
# KIBANA_URL=http://localhost:5601 ./config/kibana/import_dashboards.sh
```

#### Dashboards import (UI)

1. **Stack Management** → **Saved Objects** → **Import**.
2. Select one or more files from `config/kibana/dashboards/` (see Overview table for filenames).
3. Complete the import (enable "Overwrite" if updating existing dashboards).

#### Dashboards import (API — individual file)

```bash
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  --form file=@config/kibana/dashboards/system_health.ndjson
```

Imported dashboards are **fully populated** with visualization panels. Use the panel specs below to customize or extend them.

---

## Dashboard panel specs

The shipped NDJSON already contains these panels. Use this section as the source of truth for what each dashboard includes and how to tune it in Kibana.

### Task Analytics dashboard

**Data view:** `agent-captains-captures-*`

| Panel title              | Chart type | X / Bucket | Y / Metric | Breakdown / filters | Notes |
|--------------------------|-----------|-----------|------------|---------------------|--------|
| Task outcome distribution | Pie       | `outcome` (terms) | Count | — | completed / failed / timeout |
| Avg duration by tool     | Bar (vertical) | `tools_used` (terms) | Avg of `duration_ms` | — | Use “Top 10” if many tools |
| Most frequent tools      | Bar (horizontal) or Table | `tools_used` (terms) | Count | — | Top N tools |
| Memory context usage     | Metric or Pie | — | Count where `memory_context_used` = true vs false | Filter or split by `memory_context_used` | Or: % of tasks with memory used |

- **Time:** Use the global time picker; Lens will use the data view’s time field (`timestamp`).
- **Filters:** Add a KQL filter in Lens if you want e.g. a single outcome or date range.

### Reflection Insights dashboard

**Data view:** `agent-captains-reflections-*`

| Panel title              | Chart type | X / Bucket | Y / Metric | Breakdown / filters | Notes |
|--------------------------|-----------|-----------|------------|---------------------|--------|
| Proposed changes over time | Line or Area | `timestamp` (date histogram) | Count | Filter: `proposed_change.what` exists | Or count docs with non-empty proposed_change |
| Top improvement categories | Bar or Pie | `proposed_change.what.keyword` (terms, if mapped) or full-text | Count | — | Top N “what” values |
| Impact distribution      | Pie or Bar | `impact_assessment` (terms) or keyword subfield | Count | — | If impact_assessment is standardized |
| Metrics trending         | Line (multi-series) | `timestamp` (date histogram) | From `metrics_structured` or `supporting_metrics` | — | Use runtime fields or scripted fields if you need e.g. cpu_percent over time from nested metrics |

- Reflections use `timestamp`, `proposed_change.what` / `.why` / `.how`, `impact_assessment`, `supporting_metrics`, and optionally `metrics_structured` (nested).
- If keyword subfields are missing, use the default text field and “Top values” or create a runtime field for aggregation.

### System Health dashboard

**Data view:** `agent-logs-*`

| Panel title            | Chart type        | X / Bucket                 | Y / Metric              | Breakdown / filters        | Notes                                  |
| ----------------------- | ----------------- | -------------------------- | ----------------------- | -------------------------- | -------------------------------------- |
| CPU / memory timeline   | Line (2 series)   | @timestamp (date histogram)| cpu_load, memory_used   | Filter: event_type         | system_metrics_snapshot or sensor_poll |
| Mode transitions        | Table or Timeline | @timestamp                 | —                       | Filter: mode_transition    | Columns: from_mode, to_mode, reason    |
| Consolidation triggers  | Count / Table     | event_type (terms)         | Count                   | Filter: consolidation_*    | Or one panel per event_type            |
| Threshold violations    | Count or Table    | —                          | Count                   | Filter: threshold/violations | Or use reflections metrics_structured  |
| Quality monitor events  | Bar or Table      | event_type (terms)         | Count                   | Filter: `quality_monitor_*` | Daily quality pass + anomaly detections |

- Agent-logs events include `event_type`, `@timestamp`, and event-specific fields (e.g. `from_mode`, `to_mode`, `cpu_load`, `memory_used`). Adjust field names to match your ES logger payload.
- For CPU/memory over time, use the same event_type filter and average numeric fields; Lens will suggest date histogram on `@timestamp`.
- FRE-23 quality events available in logs:
  - `memory_query_quality_metrics` (result counts + relevance + implicit rephrase)
  - `quality_monitor_entity_report`, `quality_monitor_graph_report`, `quality_monitor_anomalies_detected`
  - Note: quality monitor events appear when monitor methods are executed by runtime wiring or manual invocation.

### Insights Engine dashboard

**Data view:** `agent-insights-*`

| Panel title                | Chart type | X / Bucket | Y / Metric | Breakdown / filters | Notes |
|---------------------------|-----------|-----------|------------|---------------------|--------|
| Insight count by type     | Bar       | `insight_type` (terms) | Count | Filter: `record_type: insight` | Correlation/trend/optimization/anomaly volume |
| Confidence trend          | Line      | `timestamp` (date histogram) | Avg of `confidence` | Filter: `record_type: insight` | Tracks insight quality over time |
| Anomalies                 | Pie       | `title` (terms) | Count | Filter: `record_type: insight and insight_type: anomaly` | Highlights anomaly classes |
| Weekly proposals created  | Line      | `timestamp` (date histogram) | Sum of `proposals_created` | Filter: `record_type: weekly_summary` | Weekly Captain's Log proposal output |

### Request Latency dashboard

**Data view:** `agent-logs-*` (filter: `event_type: request_latency_breakdown`)

Request-to-reply latency is indexed by the service after each `/chat` request completes (see telemetry latency breakdown). Each document has `total_duration_ms` and a nested `phases` array (phase name, `duration_ms`, description) so you can see which step (init, planning, llm_call, tool_execution, synthesis, etc.) dominates.

| Panel title                    | Chart type | X / Bucket | Y / Metric | Notes |
|--------------------------------|-----------|------------|------------|--------|
| Avg duration by phase          | Bar       | `phases.phase` (nested terms) | Avg of `phases.duration_ms` | Which phase takes the most time on average |
| Total request-to-reply over time | Line    | @timestamp (date histogram) | Avg of `total_duration_ms` | Trend of mean latency |
| Request count                  | Metric    | —          | Count      | Completed requests in range |
| P95 request-to-reply over time | Line     | @timestamp (date histogram) | 95th percentile of `total_duration_ms` | Tail latency trend |
| Latency by trace               | Table     | `trace_id` (terms) | Avg of `total_duration_ms` | Drill down by trace |

- **Getting data:** Run the Personal Agent service with Elasticsearch enabled; send chat requests to `/chat`. Each completed request writes one `request_latency_breakdown` document to `agent-logs-*`. Use the global time picker to scope the dashboard.
- **Index mapping:** The `agent-logs-*` index template includes `total_duration_ms` (float) and `phases` (nested). If you added the dashboard before updating the template, run `./scripts/setup-elasticsearch.sh` so new indices get the mapping; existing indices keep the old mapping until the next day’s index is created.

---

## Customizing panels in Kibana

1. Open **Dashboard** and open the relevant saved dashboard (e.g. “Task Analytics”).
2. Click **Edit**.
3. Select a panel and choose **Edit visualization** to adjust query, chart options, or aggregation.
4. Add additional visualizations from **Add from library** or **Create visualization**.
5. Resize/arrange panels and save the dashboard.

---

## Exporting dashboards

After editing dashboards:

1. **Stack Management** → **Saved Objects**.
2. Filter by type **Dashboard** (or find the dashboard by name).
3. Select the dashboard(s) and click **Export**.
4. Save the NDJSON into `config/kibana/dashboards/` (e.g. replace `task_analytics.ndjson`). Export includes referenced visualizations and data views by default; you can limit to the dashboard if you prefer and re-document any dependencies.

**API export example:**

```bash
curl -X POST "http://localhost:5601/api/saved_objects/_export" \
  -H "kbn-xsrf: true" \
  -H "Content-Type: application/json" \
  -d '{"type": "dashboard", "includeReferencesDeep": true}' \
  -o dashboards_export.ndjson
```

Then split or copy the relevant lines into the desired files in `config/kibana/dashboards/`.

---

## Field reference

Quick reference for building Lens panels and KQL filters.

**agent-captains-captures-\***
`timestamp`, `trace_id`, `session_id`, `outcome`, `tools_used`, `duration_ms`, `memory_context_used`, `memory_conversations_found`, `title`, `user_message`, `assistant_response`, …

**agent-captains-reflections-\***
`timestamp`, `entry_id`, `type`, `title`, `rationale`, `proposed_change.what` / `.why` / `.how`, `impact_assessment`, `supporting_metrics`, `metrics_structured`, `status`, …

**agent-logs-\***
`@timestamp`, `event_type`, `trace_id`, `level`, `message`, and event-specific fields: `from_mode`, `to_mode`, `reason`, `cpu_load`, `memory_used`, `sensor_data`, …

**agent-insights-\***
`timestamp`, `record_type`, `insight_type`, `title`, `summary`, `confidence`, `actionable`, `evidence`, `analysis_window_days`, `insights_count`, `proposals_created`, …

---

## Error Pattern Monitoring panels (ADR-0056)

Two panels ship with the **Agent Reliability** dashboard. If you need to build them manually:

**Error pattern top-N** (Lens bar chart)
- Data view: `agent-logs-*`
- KQL filter: `level: ERROR`
- Aggregation: Top 10 terms on `source_component.keyword` + `event.keyword` (combined label) sorted by Count descending
- Time range: Last 7 days

**Error pattern timeline** (Lens date histogram)
- Data view: `agent-logs-*`
- KQL filter: `level: ERROR`
- X-axis: `@timestamp` (auto interval)
- Y-axis: Count (doc count)
- Break down by: `source_component.keyword` (top 5)
- Time range: Last 30 days

**Durable pattern files** (for operational drill-down)
- Check `telemetry/error_patterns/EP-*.json` on disk — one file per fingerprint, includes `scan_history` (capped at 30 entries).

**Useful KQL queries:**
- Error events by component: `level: ERROR AND source_component: "tools.*"`, data view `agent-logs-*`
- Error pattern detections: `event_type: error_pattern_detected`, data view `agent-logs-*`
- Specific fingerprint: `event_type: error_pattern_detected AND fingerprint: "<fp>"`, data view `agent-logs-*`

---

## Extraction Retry Health (ADR-0065 / FRE-307)

Three panels make consolidation-pipeline retry health visible. The data
source is the `consolidation_attempt_recorded` log event emitted by
`personal_agent.second_brain.attempts.record_consolidation_attempt`,
which writes one log line per `consolidation_attempts` DB row. Both
sources carry the same fields, joinable on `trace_id`.

| Panel | Type | KQL filter | Aggregation |
|---|---|---|---|
| Median attempts to success (per role) | Line | `event:consolidation_attempt_recorded AND outcome:success` | median(`attempt_number`) over `@timestamp`, split by `role` |
| Dead-letter rate (per role) | Bar | `event:consolidation_attempt_recorded AND outcome:dead_letter` | count() by terms on `role` |
| Top denial_reason (donut) | Donut | `event:consolidation_attempt_recorded AND outcome:budget_denied` | terms on `denial_reason` |

**Data view:** `agent-logs-*`

**Operator interpretation:**
- *Median attempts climbing* → extraction is increasingly costly per success. Check whether a model degraded or a budget cap is biting.
- *Non-zero dead-letter rate* → an attempt class is exhausting retries; combine with `event_type:consumer_handler_error` for the error message.
- *`denial_reason: cap_exceeded` dominating* → the auto-tuning monitor (FRE-311) should propose a cap raise; in the meantime `config/governance/budget.yaml` is the human knob.

**Useful KQL queries:**
- Per-trace timeline: `event:consolidation_attempt_recorded AND trace_id:"<uuid>"`, sorted by `attempt_number`
- Retry-storm density during a budget-exhaustion window: `event:consolidation_attempt_recorded AND outcome:budget_denied`, time-bucketed
- Cross-system join: `event:entity_extraction_failed` shares `trace_id` with the rows in this panel

---

## Custom query examples

- **Tasks that used memory context:**
  Data view `agent-captains-captures-*`, KQL: `memory_context_used: true`
- **Reflections with a proposed change:**
  Data view `agent-captains-reflections-*`, KQL: `proposed_change.what: *`
- **Mode transitions in the last 24h:**
  Data view `agent-logs-*`, KQL: `event_type: mode_transition`, time range: Last 24 hours
- **Consolidation events:**
  Data view `agent-logs-*`, KQL: `event_type: consolidation_*`
- **Memory query quality events:**
  Data view `agent-logs-*`, KQL: `event_type: memory_query_quality_metrics`
- **Consolidation quality monitor events:**
  Data view `agent-logs-*`, KQL: `event_type: (quality_monitor_entity_report or quality_monitor_graph_report or quality_monitor_anomalies_detected)`

---

## Troubleshooting

- **No data in a panel** — Check the data view’s index pattern and time field; confirm indices exist and have data in the chosen time range (e.g. `GET agent-captains-captures-*/_count`).
- **Field not found** — Captures/reflections use `timestamp`; agent-logs use `@timestamp`. Keyword subfields (e.g. `outcome.keyword`) may exist if the template maps them; otherwise use the main field.
- **Import errors** — Ensure NDJSON is one JSON object per line. Preserve `coreMigrationVersion` / `typeMigrationVersion` when editing. Import data views before dashboards that depend on them.
- **Version compatibility** — Kibana only imports saved objects from the same or a newer minor/major; see [Kibana compatibility](https://www.elastic.co/guide/en/kibana/current/managing-saved-objects.html#saved-objects-version-compatibility).

For more on Lens and saved objects, use the official links at the top of this document.
