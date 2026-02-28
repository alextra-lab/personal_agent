# Data Lifecycle Management

**Version**: 1.0
**Date**: 2026-02-22
**Phase**: 2.3 (Homeostasis & Feedback)

## Purpose

Automated retention, archival, and cleanup for file-based telemetry and Captain's Log data so that storage stays bounded and the system remains maintainable without manual intervention.

## Scope

- **In scope**: File logs, Captain's Log captures, Captain's Log reflections, Elasticsearch log/captains indices, disk usage monitoring.
- **Out of scope**: Neo4j graph and PostgreSQL data are not purged by lifecycle (retention policy exists but `cold_duration=0` means "never delete").

## Retention Policy Rationale

| Data Type | Hot | Warm (archive) | Cold (delete) | Rationale |
|-----------|-----|-----------------|---------------|------------|
| File Logs | 7d | 14d | 30d | Recent logs for debugging; archive then delete to limit size. |
| Task Captures | 14d | 14d | 90d | Hot for consolidation; keep archives 2 weeks then delete after 90d. |
| Reflections | 14d | 14d | 180d | Higher value; keep 6 months then delete. |
| ES Event Logs | 14d | — | 30d | No local archive (data in ES); index cleanup only. |
| Neo4j Graph | 365d | 730d | Never | Knowledge graph is long-term; no automated deletion. |

Defaults are defined in `src/personal_agent/telemetry/lifecycle.py` (`RETENTION_POLICIES`). Configuration is via application settings (e.g. `AGENT_DISK_USAGE_ALERT_PERCENT`, `AGENT_DATA_LIFECYCLE_ENABLED`).

## Archival Process

1. **Trigger**: Daily at 2:00 UTC (scheduler).
2. **Action**: For each file-based data type with `archive_enabled`:
   - List files older than `hot_duration`.
   - Compress each with gzip and write to `telemetry/archive/<data_type>/YYYY-MM/<relative_path>.gz`.
   - Remove the original file.
3. **Locations**:
   - File logs: `telemetry/logs/*.jsonl` → `telemetry/archive/file_logs/YYYY-MM/...`
   - Captures: `telemetry/captains_log/captures/**/*.json` → `telemetry/archive/captains_log_captures/YYYY-MM/...`
   - Reflections: `telemetry/captains_log/CL-*.json` → `telemetry/archive/captains_log_reflections/YYYY-MM/...`

## Purge Process

1. **Trigger**: Weekly, Sunday 3:00 UTC (scheduler).
2. **Action**:
   - For each file-based data type with `cold_duration > 0`: delete files (and archived `.gz`) older than `cold_duration`.
   - Elasticsearch: delete indices (e.g. `agent-logs-*`, `agent-captains-captures-*`, `agent-captains-reflections-*`) older than the ES policy's `cold_duration`.
3. **Safety**: Only data past the configured cold age is removed. No purge is performed for policies with `cold_duration=0` (e.g. Neo4j).

## Disk Usage Monitoring

- **Trigger**: Hourly (scheduler).
- **Action**: Compute disk usage for the telemetry root and emit a lifecycle event. If usage ≥ `disk_usage_alert_percent` (default 80%), emit a `lifecycle_disk_alert` event for alerting.

## Configuration

| Setting | Default | Description |
|---------|---------|--------------|
| `AGENT_DISK_USAGE_ALERT_PERCENT` | 80 | Alert when disk usage exceeds this percent. |
| `AGENT_DATA_LIFECYCLE_ENABLED` | true | Enable scheduled archive, purge, and disk checks. |

Paths are derived from `AGENT_LOG_DIR` (e.g. `telemetry/logs`); telemetry root is the parent of that directory.

## Restoration

- **Archived files**: Stored under `telemetry/archive/<data_type>/`. To restore a file, decompress (e.g. `gunzip -k file.jsonl.gz`) and place back under the original path if needed.
- **Purged data**: Not recoverable; ensure cold durations align with your compliance and debugging needs.

## Observability

All lifecycle operations emit structured telemetry events:

- `lifecycle_disk_check` – disk usage snapshot
- `lifecycle_disk_alert` – usage above threshold
- `lifecycle_archive` – archive run per data type (counts, bytes)
- `lifecycle_purge` – purge run per data type
- `lifecycle_es_cleanup` – ES index deletion
- `lifecycle_report` – report generation (would-archive/purge counts)

See `personal_agent.telemetry.events` for event name constants.

## Alignment with Elasticsearch ILM

Elastic's Index Lifecycle Management (ILM) is used for all agent indices:

- **ILM policy**: `docker/elasticsearch/ilm-policy.json` (`agent-logs-policy`) — hot (rollover 7d/1GB), warm 7d, **delete 30d**.
- **Templates**: `index-template.json` (agent-logs) and `captains-index-template.json` (agent-captains-*) both set `index.lifecycle.name: agent-logs-policy`.

Our internal **elasticsearch_logs** retention policy is aligned:

| Concept   | Our policy (`lifecycle.py`) | Elastic ILM (`ilm-policy.json`) |
|----------|-----------------------------|----------------------------------|
| Delete   | cold_duration 30d           | delete phase min_age 30d         |

Application cleanup (`cleanup_elasticsearch_indices()`) also deletes indices older than 30d for the same index patterns. It runs weekly as a backup; ILM remains the primary mechanism for index lifecycle. Captain's Log **files** on disk use longer retention (90d / 180d); only the ES **indices** (search copy) use 30d in both ILM and our cleanup.

## References

- Implementation: `src/personal_agent/telemetry/lifecycle.py`, `lifecycle_manager.py`
- Scheduler integration: `src/personal_agent/brainstem/scheduler.py` (`_lifecycle_loop`)
- Elastic ILM: `docker/elasticsearch/ilm-policy.json`, `scripts/setup-elasticsearch.sh`
- Plan: `docs/plans/PHASE_2.3_PLAN.md` (Part 2: Data Lifecycle Management)
