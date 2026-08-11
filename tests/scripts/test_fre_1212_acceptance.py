#!/usr/bin/env python3
"""Tests for FRE-1212: Delete dead panels and unreferenced datasources."""

import json
import re
from pathlib import Path

from personal_agent.config.config_guard import repo_root


def load_dashboard(name: str) -> dict:
    """Load a Grafana dashboard JSON."""
    path = repo_root() / "config" / "grafana" / "dashboards" / f"{name}.json"
    with open(path) as f:
        return json.load(f)


def load_datasources() -> dict:
    """Load datasources.yaml as a dict (after YAML parsing)."""
    import yaml

    path = repo_root() / "config" / "grafana" / "provisioning" / "datasources" / "datasources.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def test_ac1_request_traces_deleted():
    """AC-1: request_traces dashboard deleted (3 panels)."""
    path = repo_root() / "config" / "grafana" / "dashboards" / "request_traces.json"
    assert not path.exists(), "request_traces.json should be deleted (3 panels all disposed delete)"


def test_ac1_system_health_consolidation_deleted():
    """AC-1: system_health 'Consolidation activity' (panel 3) is deleted."""
    db = load_dashboard("system_health")
    # Should have 3 panels after deleting panel 3
    assert len(db["panels"]) == 3, f"Expected 3 panels, got {len(db['panels'])}"
    # Panel 3 should not exist
    panel_ids = [p["id"] for p in db["panels"]]
    assert 3 not in panel_ids, "Panel 3 (Consolidation activity) still exists"
    # Panels 1, 2, 4 should exist
    assert 1 in panel_ids, "Panel 1 missing"
    assert 2 in panel_ids, "Panel 2 missing"
    assert 4 in panel_ids, "Panel 4 missing"


def test_ac1_turn_session_artifact_panel4_deleted():
    """AC-1: turn_session_artifact panel 4 (empty query) is deleted."""
    db = load_dashboard("turn_session_artifact")
    # Should have 4 panels after deleting panel 4
    assert len(db["panels"]) == 4, f"Expected 4 panels, got {len(db['panels'])}"
    # Panel 4 should not exist
    panel_ids = [p["id"] for p in db["panels"]]
    assert 4 not in panel_ids, "Panel 4 (empty query) still exists"
    # Panels 1, 2, 3, 5 should exist
    assert 1 in panel_ids, "Panel 1 missing"
    assert 2 in panel_ids, "Panel 2 missing"
    assert 3 in panel_ids, "Panel 3 missing"
    assert 5 in panel_ids, "Panel 5 missing"


def test_ac2_no_unreferenced_datasources():
    """AC-2: Unreferenced datasources from the audit (es-captains-captures, es-insights) are deleted."""
    ds_config = load_datasources()
    datasources = {d["uid"]: d["name"] for d in ds_config["datasources"]}

    # Only check that audit-marked unreferenced datasources are gone
    # (pg-ledger is provisioned for future use; others are in-flight rebuilds)
    assert "es-captains-captures" not in datasources, "es-captains-captures still in datasources"
    assert "es-insights" not in datasources, "es-insights still in datasources"


def test_ac3_no_empty_queries_against_agent_logs():
    """AC-3: No panel runs an empty query against es-agent-logs."""
    dashboards = [
        "monitors_joinability_slm",
        "prompt-cost-cache",
        "cost_budget",
        "traversal_gate",
        "turn_session_artifact",
        "intent_classification",
        "expansion_decomposition",
        "task_analytics",
        "system_health",
        "extraction_retry_health",
        "health_check",
        "self_improvement_funnel",
        "llm_performance",
        "request_timing",
        "context_occupancy",
    ]

    for dash_name in dashboards:
        try:
            db = load_dashboard(dash_name)
            for panel in db.get("panels", []):
                ds = panel.get("datasource", {})
                if isinstance(ds, dict) and ds.get("uid") == "es-agent-logs":
                    for target in panel.get("targets", []):
                        query = target.get("query", "")
                        assert query != "", (
                            f"Dashboard '{dash_name}' panel {panel.get('id')} "
                            f"has empty query against es-agent-logs"
                        )
        except FileNotFoundError:
            pass


def test_ac4_no_hardcoded_trace_ids():
    """AC-4: No panel runs a query with a hardcoded UUID literal."""
    # UUID pattern: 8-4-4-4-12 hex groups
    uuid_pattern = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")

    dashboards = [
        "monitors_joinability_slm",
        "prompt-cost-cache",
        "cost_budget",
        "traversal_gate",
        "turn_session_artifact",
        "intent_classification",
        "expansion_decomposition",
        "task_analytics",
        "system_health",
        "extraction_retry_health",
        "health_check",
        "self_improvement_funnel",
        "llm_performance",
        "request_timing",
        "context_occupancy",
    ]

    for dash_name in dashboards:
        try:
            db = load_dashboard(dash_name)
            for panel in db.get("panels", []):
                for target in panel.get("targets", []):
                    query = target.get("query", "")
                    # Check if a UUID literal is in the query (excluding templating vars like $__name__)
                    if uuid_pattern.search(query):
                        assert False, (
                            f"Dashboard '{dash_name}' panel {panel.get('id')} "
                            f"has hardcoded UUID in query: {query}"
                        )
        except FileNotFoundError:
            pass


def test_ac5_es_captains_captures_removed():
    """AC-5: es-captains-captures datasource is removed."""
    ds_config = load_datasources()
    uids = {d["uid"]: d["name"] for d in ds_config["datasources"]}
    assert "es-captains-captures" not in uids, "es-captains-captures datasource still exists"


def test_ac5_es_insights_removed():
    """AC-5: es-insights datasource is removed."""
    ds_config = load_datasources()
    uids = {d["uid"]: d["name"] for d in ds_config["datasources"]}
    assert "es-insights" not in uids, "es-insights datasource still exists"


if __name__ == "__main__":
    import sys

    tests = [
        test_ac1_request_traces_deleted,
        test_ac1_system_health_consolidation_deleted,
        test_ac1_turn_session_artifact_panel4_deleted,
        test_ac2_no_unreferenced_datasources,
        test_ac3_no_empty_queries_against_agent_logs,
        test_ac4_no_hardcoded_trace_ids,
        test_ac5_es_captains_captures_removed,
        test_ac5_es_insights_removed,
    ]

    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1

    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    else:
        print("\nAll tests passed!")
        sys.exit(0)
