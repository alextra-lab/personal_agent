"""Static validation of the FRE-539 (C4) Turn / Session / Artifact dashboard.

Surfaces three new analytics views over ``agent-logs-*``:

* **Turn-level** — per-turn ``complexity`` (from ``gateway_output``) and cross-turn
  KV cache reuse (``cache_read_tokens`` from ``model_call_completed``). Deliberately
  omits strategy / task_type / model-latency panels already shipped by
  ``expansion_decomposition`` / ``intent_classification`` / ``llm_performance``.
* **Session / trace E2E aggregate** — turns-per-session and calls-per-session rollups
  joined on ``session_id`` / ``trace_id`` (from ``api_cost_recorded``); the aggregate
  layer ``request_traces`` (single-trace waterfall) lacks.
* **Artifact-envelope integrity (ADR-0089)** — ``artifact_envelope_integrity`` /
  ``artifact_gate_decision`` events, joinable on ``artifact_id``.

These tests are *static* — they parse the repo Kibana NDJSON under
``config/kibana/dashboards/`` without touching a live cluster. They encode the
project-specific traps so the "first-pass-wrong dashboard" failure mode is caught in
CI rather than discovered live:

1. The FRE-533 ``.keyword``-on-a-bare-keyword terms-agg trap (silent empty panel) —
   every join key (``trace_id`` / ``session_id`` / ``artifact_id``) is bare ``keyword``,
   so a ``.keyword`` suffix aggregates to nothing.
2. Every aggregation field is pinned to the verified-safe set (live ``_field_caps``,
   recorded in ``docs/research/2026-06-09-fre-539-turn-session-artifact-dashboard.md``).

Unlike the FRE-538 monitors dashboard (self-contained inline index-patterns), this
dashboard references the **shared** ``agent-logs-pattern`` from ``data_views.ndjson``
(no 4th agent-logs duplicate, per the A1/FRE-533 dedupe finding) — so the panels here
must reference exactly that pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "turn_session_artifact.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

# The single shared index-pattern every aggregating panel must reference. Defined in
# data_views.ndjson (loads first in import_dashboards.sh), not inline here.
SHARED_INDEX_PATTERN = "agent-logs-pattern"

# Every aggregation must reference one of these verified-safe fields (live _field_caps,
# all bare keyword / numeric / date — no straddle, no .keyword needed). Anything else is
# either the .keyword trap or an unverified field.
SAFE_AGG_FIELDS = frozenset(
    {
        # turn-level (gateway_output / model_call_completed)
        "complexity",
        "task_type",
        "strategy",
        "cache_read_tokens",
        "input_tokens",
        # session / trace aggregate (api_cost_recorded / level:ERROR)
        "session_id",
        "trace_id",
        "latency_ms",
        # artifact-envelope (ADR-0089)
        "probe_status",
        "gate_decision",
        "envelope_ok",
        # shared time field
        "@timestamp",
    }
)


def _objects() -> list[dict]:
    """Parse the dashboard NDJSON into a list of saved-object dicts."""
    assert DASHBOARD_FILE.exists(), f"{DASHBOARD_FILE} does not exist"
    objs: list[dict] = []
    for i, line in enumerate(DASHBOARD_FILE.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError as e:  # pragma: no cover - failure path
            pytest.fail(f"{DASHBOARD_FILE.name}:{i} is not valid JSON: {e}")
    return objs


def _by_type(objs: list[dict], type_: str) -> list[dict]:
    return [o for o in objs if o.get("type") == type_]


def _agg_fields(viz: dict) -> list[str]:
    """Every ``params.field`` referenced by a visualization's aggs (markdown has none)."""
    vis_state = json.loads(viz["attributes"]["visState"])
    return [
        agg["params"]["field"]
        for agg in vis_state.get("aggs", [])
        if isinstance(agg.get("params"), dict) and agg["params"].get("field")
    ]


def _index_pattern_refs(obj: dict) -> list[str]:
    return [r["id"] for r in obj.get("references", []) if r["type"] == "index-pattern"]


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_expected_object_counts() -> None:
    """One dashboard + 10 visualizations (incl. 1 markdown banner) + 2 searches."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "visualization")) == 11, "expected eleven visualization panels"
    assert len(_by_type(objs, "search")) == 2, "expected two saved-search panels"
    # No inline index-pattern objects — this dashboard reuses the shared one.
    assert len(_by_type(objs, "index-pattern")) == 0, (
        "this dashboard must NOT define its own index-pattern; it references the shared "
        f"{SHARED_INDEX_PATTERN} from data_views.ndjson"
    )


def test_aggregating_panels_reference_only_the_shared_pattern() -> None:
    """Every viz/search that references an index-pattern uses the shared one.

    The markdown banner carries no index-pattern reference and is exempt by
    construction (it has no references to iterate).
    """
    objs = _objects()
    for obj in _by_type(objs, "visualization") + _by_type(objs, "search"):
        ip_refs = _index_pattern_refs(obj)
        if not ip_refs:
            # markdown banner — no data, no index-pattern. Allowed.
            assert not _agg_fields(obj), (
                f"{obj['id']} has no index-pattern but does aggregate — misconfigured"
            )
            continue
        assert ip_refs == [SHARED_INDEX_PATTERN], (
            f"{obj['id']} references {ip_refs}, not exactly [{SHARED_INDEX_PATTERN!r}]"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a viz/search in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    panel_ids = {o["id"] for o in _by_type(objs, "visualization") + _by_type(objs, "search")}

    panel_refs = {
        r["name"]: r["id"]
        for r in dashboard["references"]
        if r["type"] in {"visualization", "search"}
    }
    for name, ref_id in panel_refs.items():
        assert ref_id in panel_ids, f"dashboard panel ref {name} -> {ref_id} has no object"

    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_names = {p["panelRefName"] for p in panels}
    assert panel_names == set(panel_refs), "panelsJSON refs must match dashboard references"


# --------------------------------------------------------------------------- #
# The .keyword / A1 trap guard.
# --------------------------------------------------------------------------- #


def test_aggregations_only_use_safe_fields() -> None:
    """No panel aggregates on a ``.keyword`` field or an unverified field.

    Pins every aggregation to the verified-safe set so a ``.keyword``-on-bare-keyword
    join (the FRE-533 silent-empty trap) can never reach a shipped panel.
    """
    for viz in _by_type(_objects(), "visualization"):
        for field in _agg_fields(viz):
            assert not field.endswith(".keyword"), (
                f"{viz['id']} aggregates on {field!r}; a ``.keyword`` agg on a bare-keyword "
                f"join key is the FRE-533 silent-empty trap — use the bare field"
            )
            assert field in SAFE_AGG_FIELDS, (
                f"{viz['id']} aggregates on {field!r}, not in the verified-safe set "
                f"{sorted(SAFE_AGG_FIELDS)}"
            )


# --------------------------------------------------------------------------- #
# Registration parity.
# --------------------------------------------------------------------------- #


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "turn_session_artifact.ndjson" in IMPORT_SCRIPT.read_text(), (
        "turn_session_artifact.ndjson must be appended to FILES in import_dashboards.sh"
    )
