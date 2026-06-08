"""Static validation of the FRE-537 (C2) Traversal Ledger & Gate-Decision dashboard.

These tests are *static* — they parse the repo Kibana NDJSON under
``config/kibana/dashboards/`` without touching a live cluster. They encode the
FRE-533 reconciliation lesson (the ".keyword-on-a-bare-keyword-field" terms-agg
trap that silently empties panels) so the "first-pass-wrong dashboard" failure
mode is caught in CI rather than discovered live.

Source of truth for the field types: the live ``_field_caps`` verification recorded
in ``docs/research/2026-06-08-fre-537-traversal-gate-dashboard.md`` — every
dimension below is a **bare** ``keyword`` (no ``.keyword`` multifield).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "traversal_gate.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

# The canonical shared index-pattern (defined once in data_views.ndjson, loaded
# first by import_dashboards.sh). Every panel must reference this id — no
# per-dashboard duplicate index-patterns (the A1 dedupe lesson).
CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# Dimensions verified bare ``keyword`` against live ``_field_caps``. A panel that
# aggregates on ``<dim>.keyword`` resolves to nothing → silent empty panel.
BARE_KEYWORD_DIMS = frozenset(
    {
        "decision",
        "reason",
        "tool_name",
        "gateway_label",
        "orchestration_event",
        "state_before",
        "state_after",
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
    """Every ``params.field`` referenced by a visualization's aggs."""
    vis_state = json.loads(viz["attributes"]["visState"])
    return [
        agg["params"]["field"]
        for agg in vis_state.get("aggs", [])
        if isinstance(agg.get("params"), dict) and agg["params"].get("field")
    ]


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_one_dashboard() -> None:
    """The file parses as NDJSON and contains exactly one dashboard + six panels."""
    objs = _objects()
    dashboards = _by_type(objs, "dashboard")
    assert len(dashboards) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "visualization")) == 6, "expected six visualization panels"


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a visualization in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    viz_ids = {v["id"] for v in _by_type(objs, "visualization")}

    panel_refs = {
        r["name"]: r["id"] for r in dashboard["references"] if r["type"] == "visualization"
    }
    for name, ref_id in panel_refs.items():
        assert ref_id in viz_ids, f"dashboard panel ref {name} -> {ref_id} has no visualization"

    # panelsJSON must reference exactly the names declared in references.
    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_names = {p["panelRefName"] for p in panels}
    assert panel_names == set(panel_refs), "panelsJSON refs must match dashboard references"


def test_every_visualization_uses_canonical_index_pattern() -> None:
    """No duplicate index-patterns — every panel points at the shared id."""
    objs = _objects()
    # The file must not redefine extra index-patterns beyond the canonical one.
    index_patterns = _by_type(objs, "index-pattern")
    for ip in index_patterns:
        assert ip["id"] == CANONICAL_INDEX_PATTERN_ID, f"unexpected index-pattern {ip['id']}"

    for viz in _by_type(objs, "visualization"):
        ip_refs = [r["id"] for r in viz["references"] if r["type"] == "index-pattern"]
        assert ip_refs == [CANONICAL_INDEX_PATTERN_ID], (
            f"{viz['id']} must reference {CANONICAL_INDEX_PATTERN_ID}, got {ip_refs}"
        )


# --------------------------------------------------------------------------- #
# The A1 trap guard — bare keyword must not be aggregated as ``.keyword``.
# --------------------------------------------------------------------------- #


def test_no_keyword_suffix_on_bare_keyword_dims() -> None:
    """No panel aggregates on ``<dim>.keyword`` for a verified bare-keyword field."""
    for viz in _by_type(_objects(), "visualization"):
        for field in _agg_fields(viz):
            assert not field.endswith(".keyword"), (
                f"{viz['id']} aggregates on {field!r}; the parent is a bare keyword "
                f"(no .keyword multifield) — this silently empties the panel (A1 trap)"
            )
            base = field
            assert base not in {f + ".keyword" for f in BARE_KEYWORD_DIMS}


# --------------------------------------------------------------------------- #
# Registration parity.
# --------------------------------------------------------------------------- #


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "traversal_gate.ndjson" in IMPORT_SCRIPT.read_text(), (
        "traversal_gate.ndjson must be appended to FILES in import_dashboards.sh"
    )
