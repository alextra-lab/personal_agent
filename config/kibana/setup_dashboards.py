#!/usr/bin/env python3
"""Create Kibana dashboards for personal_agent telemetry.

Usage:
    python config/kibana/setup_dashboards.py [--kibana-url http://localhost:5601] [--es-url http://localhost:9200]

Creates an ES index template (agent-logs-template) so key fields are mapped as
keyword for Kibana terms aggregations, then creates dashboards using the Kibana
saved objects API.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

KIBANA_URL = "http://localhost:5601"
ES_URL = "http://localhost:9200"
DATA_VIEW_ID = "agent-logs-pattern"


def create_index_template(es_url: str) -> bool:
    """Create ES index template so agent-logs-* fields are keyword for Kibana.

    Ensures event_type, trace_id, session_id, phase, name, role, model_id,
    from_state, to_state, delegated_role, component are mapped as keyword
    (required for Kibana terms aggregations). Template only applies to new
    indices; existing data may need reindex or wait for next daily rollover.

    Returns:
        True if template was applied successfully.
    """
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
        },
    }
    url = f"{es_url.rstrip('/')}/_index_template/agent-logs-template"
    data = json.dumps(template).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                print("  [OK] Index template agent-logs-template applied")
                return True
            return False
    except urllib.error.HTTPError as e:
        print(f"  ERROR ES index template: {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return False
    except OSError as e:
        print(f"  ERROR ES connection: {e}", file=sys.stderr)
        return False


def _api(method: str, path: str, body: dict[str, Any] | list | None = None) -> Any:
    url = f"{KIBANA_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return None


def _vis_state(title: str, vis_type: str, aggs: list[dict], params: dict | None = None) -> str:
    state: dict[str, Any] = {
        "title": title,
        "type": vis_type,
        "aggs": aggs,
        "params": params or {"addTooltip": True, "addLegend": True, "legendPosition": "right"},
    }
    return json.dumps(state)


def _search_source(query_str: str) -> str:
    return json.dumps(
        {
            "query": {"language": "kuery", "query": query_str},
            "filter": [],
            "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
        }
    )


def _create_visualization(
    vid: str,
    title: str,
    desc: str,
    vis_type: str,
    query: str,
    aggs: list[dict],
    params: dict | None = None,
) -> None:
    body = {
        "attributes": {
            "title": title,
            "description": desc,
            "visState": _vis_state(title, vis_type, aggs, params),
            "uiStateJSON": "{}",
            "version": 1,
            "kibanaSavedObjectMeta": {"searchSourceJSON": _search_source(query)},
        },
        "references": [
            {
                "id": DATA_VIEW_ID,
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
            }
        ],
    }
    result = _api("POST", f"/api/saved_objects/visualization/{vid}?overwrite=true", body)
    status = "OK" if result else "FAIL"
    print(f"  [{status}] {title}")


def _create_dashboard(did: str, title: str, desc: str, panel_ids: list[str]) -> None:
    panels = []
    refs = []
    cols = 2
    w = 24
    h = 15
    for i, pid in enumerate(panel_ids):
        x = (i % cols) * w
        y = (i // cols) * h
        panels.append(
            {
                "type": "visualization",
                "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i + 1)},
                "panelIndex": str(i + 1),
                "embeddableConfig": {},
                "panelRefName": f"panel_{i}",
            }
        )
        refs.append({"id": pid, "name": f"panel_{i}", "type": "visualization"})

    body = {
        "attributes": {
            "title": title,
            "description": desc,
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps(
                {
                    "useMargins": True,
                    "syncColors": False,
                    "syncCursor": True,
                    "hidePanelTitles": False,
                }
            ),
            "timeRestore": False,
            "hits": 0,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"language": "kuery", "query": ""}, "filter": []}
                )
            },
        },
        "references": refs,
    }
    result = _api("POST", f"/api/saved_objects/dashboard/{did}?overwrite=true", body)
    status = "OK" if result else "FAIL"
    print(f"  [{status}] Dashboard: {title}")


# ── LLM Performance Dashboard ──────────────────────────────────────────
def create_llm_performance() -> None:
    print("\n── LLM Performance ──")
    q = "event_type:model_call_completed"

    _create_visualization(
        "llm-latency-over-time",
        "LLM Latency Over Time",
        "Average latency_ms over time by model role",
        "line",
        q,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "latency_ms", "customLabel": "Avg Latency (ms)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "terms",
                "schema": "group",
                "params": {"field": "role", "size": 5, "order": "desc", "orderBy": "1"},
            },
        ],
    )

    _create_visualization(
        "llm-latency-by-role",
        "Avg Latency by Model Role",
        "Bar chart of average latency per role",
        "histogram",
        q,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "latency_ms", "customLabel": "Avg Latency (ms)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {"field": "role", "size": 10, "order": "desc", "orderBy": "1"},
            },
        ],
    )

    _create_visualization(
        "llm-tokens-over-time",
        "Token Usage Over Time",
        "Prompt and completion tokens over time",
        "line",
        q,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "sum",
                "schema": "metric",
                "params": {"field": "prompt_tokens", "customLabel": "Prompt Tokens"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "sum",
                "schema": "metric",
                "params": {"field": "completion_tokens", "customLabel": "Completion Tokens"},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1},
            },
        ],
    )

    _create_visualization(
        "llm-call-count",
        "LLM Call Count by Model",
        "Count of calls per model_id",
        "pie",
        q,
        [
            {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {
                    "field": "model_id",
                    "size": 10,
                    "order": "desc",
                    "orderBy": "1",
                },
            },
        ],
        {"addTooltip": True, "addLegend": True, "isDonut": True, "legendPosition": "right"},
    )

    _create_visualization(
        "llm-errors",
        "LLM Errors Over Time",
        "model_call_error events over time",
        "histogram",
        "event_type:model_call_error",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Error Count"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
        ],
    )

    _create_visualization(
        "llm-p95-latency",
        "P95 Latency by Role",
        "95th percentile latency per model role",
        "histogram",
        q,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "percentiles",
                "schema": "metric",
                "params": {"field": "latency_ms", "percents": [50, 95, 99]},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {"field": "role", "size": 10, "order": "desc", "orderBy": "_key"},
            },
        ],
    )

    _create_dashboard(
        "llm-performance-dashboard",
        "LLM Performance",
        "Model call latency, token usage, error rates, and call distribution",
        [
            "llm-latency-over-time",
            "llm-latency-by-role",
            "llm-tokens-over-time",
            "llm-call-count",
            "llm-errors",
            "llm-p95-latency",
        ],
    )


# ── Request Timing Dashboard ───────────────────────────────────────────
def create_request_timing() -> None:
    print("\n── Request Timing ──")
    q_phase = "event_type:request_timing_phase"
    q_timing = "event_type:request_timing"

    _create_visualization(
        "rt-avg-by-phase",
        "Avg Duration by Phase",
        "Average duration_ms per request phase",
        "histogram",
        q_phase,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "duration_ms", "customLabel": "Avg Duration (ms)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {"field": "phase", "size": 20, "order": "desc", "orderBy": "1"},
            },
        ],
    )

    _create_visualization(
        "rt-total-over-time",
        "Total Request Duration Over Time",
        "Total request duration trend",
        "line",
        q_timing,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "max",
                "schema": "metric",
                "params": {"field": "total_duration_ms", "customLabel": "Total Duration (ms)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1},
            },
        ],
    )

    _create_visualization(
        "rt-phase-table",
        "Request Phase Details",
        "Table of all phases with duration and offset",
        "table",
        q_phase,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "duration_ms", "customLabel": "Avg Duration (ms)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "offset_ms", "customLabel": "Avg Offset (ms)"},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Count"},
            },
            {
                "id": "4",
                "enabled": True,
                "type": "terms",
                "schema": "bucket",
                "params": {"field": "phase", "size": 30, "order": "desc", "orderBy": "1"},
            },
        ],
        {"perPage": 25, "showPartialRows": False, "showMetricsAtAllLevels": False},
    )

    _create_visualization(
        "rt-request-count",
        "Request Count",
        "Number of requests over time",
        "metric",
        q_timing,
        [
            {
                "id": "1",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Total Requests"},
            }
        ],
        {
            "addTooltip": True,
            "addLegend": False,
            "type": "metric",
            "metric": {
                "colorSchema": "Green to Red",
                "colorsRange": [{"from": 0, "to": 10000}],
                "style": {"fontSize": 60},
            },
        },
    )

    _create_dashboard(
        "request-timing-dashboard",
        "Request Timing (E2E)",
        "End-to-end request phase breakdown, total duration trends",
        ["rt-avg-by-phase", "rt-total-over-time", "rt-phase-table", "rt-request-count"],
    )


# ── System Health Dashboard ────────────────────────────────────────────
def create_system_health() -> None:
    print("\n── System Health ──")

    _create_visualization(
        "sh-cpu-memory",
        "CPU & Memory Timeline",
        "CPU load and memory usage from metrics snapshots",
        "line",
        "event_type:(system_metrics_snapshot OR sensor_poll)",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "cpu_load", "customLabel": "CPU Load (%)"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "avg",
                "schema": "metric",
                "params": {"field": "memory_used", "customLabel": "Memory Used (%)"},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1},
            },
        ],
    )

    _create_visualization(
        "sh-state-transitions",
        "State Transitions",
        "Orchestrator state transitions over time",
        "histogram",
        "event_type:state_transition",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Transitions"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {
                    "field": "from_state",
                    "size": 10,
                    "order": "desc",
                    "orderBy": "1",
                },
            },
            {
                "id": "3",
                "enabled": True,
                "type": "date_histogram",
                "schema": "group",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
        ],
    )

    _create_visualization(
        "sh-consolidation",
        "Consolidation Events",
        "Scheduler consolidation activity",
        "histogram",
        "event_type:(consolidation_triggered OR consolidation_completed OR consolidation_started)",
        [
            {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {"field": "event_type", "size": 10, "order": "desc", "orderBy": "1"},
            },
        ],
    )

    _create_visualization(
        "sh-error-rate",
        "Error Events",
        "ES indexing failures, model errors, extraction failures",
        "line",
        "event_type:(elasticsearch_index_failed OR model_call_error OR entity_extraction_failed)",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Errors"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "terms",
                "schema": "group",
                "params": {"field": "event_type", "size": 5, "order": "desc", "orderBy": "1"},
            },
        ],
    )

    _create_dashboard(
        "system-health-dashboard",
        "System Health",
        "CPU/memory, state transitions, consolidation, and error rates",
        ["sh-cpu-memory", "sh-state-transitions", "sh-consolidation", "sh-error-rate"],
    )


# ── Task Analytics Dashboard ───────────────────────────────────────────
def create_task_analytics() -> None:
    print("\n── Task Analytics ──")

    _create_visualization(
        "ta-tasks-over-time",
        "Tasks Over Time",
        "Task started/completed events",
        "histogram",
        "event_type:(task_started OR task_completed)",
        [
            {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {"field": "event_type", "size": 5, "order": "desc", "orderBy": "1"},
            },
            {
                "id": "3",
                "enabled": True,
                "type": "date_histogram",
                "schema": "group",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
        ],
    )

    _create_visualization(
        "ta-routing",
        "Routing Decisions",
        "Router model delegation patterns",
        "pie",
        "event_type:routing_delegation",
        [
            {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2",
                "enabled": True,
                "type": "terms",
                "schema": "segment",
                "params": {
                    "field": "delegated_role",
                    "size": 10,
                    "order": "desc",
                    "orderBy": "1",
                },
            },
        ],
        {"addTooltip": True, "addLegend": True, "isDonut": True, "legendPosition": "right"},
    )

    _create_visualization(
        "ta-memory-enrichment",
        "Memory Enrichment",
        "Memory query and enrichment events",
        "histogram",
        "event_type:(memory_query_completed OR memory_enrichment_completed)",
        [
            {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
        ],
    )

    _create_visualization(
        "ta-entity-creation",
        "Entity Creation Rate",
        "Entities created from conversation extraction",
        "line",
        "event_type:entity_created",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "count",
                "schema": "metric",
                "params": {"customLabel": "Entities Created"},
            },
            {
                "id": "2",
                "enabled": True,
                "type": "date_histogram",
                "schema": "segment",
                "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 0},
            },
        ],
    )

    _create_dashboard(
        "task-analytics-dashboard",
        "Task Analytics",
        "Task lifecycle, routing decisions, memory enrichment, entity extraction",
        ["ta-tasks-over-time", "ta-routing", "ta-memory-enrichment", "ta-entity-creation"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Kibana dashboards for personal_agent")
    parser.add_argument("--kibana-url", default="http://localhost:5601")
    parser.add_argument("--es-url", default="http://localhost:9200", help="Elasticsearch URL for index template")
    args = parser.parse_args()

    global KIBANA_URL, ES_URL
    KIBANA_URL = args.kibana_url
    ES_URL = args.es_url

    print(f"Creating dashboards at {KIBANA_URL}")
    print(f"Data view: {DATA_VIEW_ID}")
    create_index_template(ES_URL)

    create_llm_performance()
    create_request_timing()
    create_system_health()
    create_task_analytics()

    print("\nDone. Open Kibana → Dashboard to see results.")
    print("Set time range to 'Last 24 hours' or wider for best results.")


if __name__ == "__main__":
    main()
