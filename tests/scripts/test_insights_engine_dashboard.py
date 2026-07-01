"""Static validation of insights_engine.ndjson saved-object format and value redesign.

FRE-703 rebuilt all three panels via the Kibana UI (Playwright-driven, never hand-authored)
to fix two problems found during step 0 (raw-event inspection):

1. The original "Insight count by type" viz aggregated on ``insight_type``, which is mapped
   as ``text`` (fielddata disabled, not aggregatable) on the older ``agent-insights-*`` daily
   indices and as ``keyword`` on newer ones -- a mid-history mapping change. Aggregating on
   either the bare field or its ``.keyword`` subfield silently drops the shards using the
   *other* convention (49/51 shards failed on one, the other convention's data is simply
   absent from the other), giving a wrong count with no visible error. Fixed via a Kibana
   Data View runtime field (``insight_type_norm``) that coalesces both conventions -- verified
   against a live raw ES aggregation to return the correct total (465 docs / 30d) across all
   51 shards.
2. The stated decision ("what cross-session insights exist, and are they actionable?") was
   only half-answered -- no panel used the ``actionable`` boolean field. Added a dedicated
   "actionable vs not" panel.

The "Anomalies" panel was dropped: zero anomaly-type insights exist in the last 30 days
(legitimate empty state, not a bug -- confirmed live), so it added no value to the redesigned
decision-focused dashboard.

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType`` (render-time-only
   requirement a hand-authored object silently omits).
4. The canonical ``agent-insights-pattern`` index-pattern id is used, and the self-included
   copy (including its ``runtimeFieldMap``) is byte-identical to the canonical copy in
   ``data_views.ndjson`` (prevents a stale/sparse copy clobbering the runtime field on
   ``overwrite=true``).
5. Data-backing -- the Lens ``sourceField`` values used are pinned to the set verified live
   in ``agent-insights-*`` (465 docs/30d via the runtime-field-corrected aggregation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "insights_engine.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-insights-pattern"

VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "insight_type_norm",
        "actionable",
        "confidence",
        "timestamp",
        "___records___",
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


def _lens_source_fields(lens: dict) -> list[str]:
    """All ``sourceField`` values from every column across all formBased layers."""
    try:
        state = lens["attributes"]["state"]
        layers = state["datasourceStates"]["formBased"]["layers"]
    except (KeyError, TypeError):
        return []
    fields: list[str] = []
    for layer in layers.values():
        for col in layer.get("columns", {}).values():
            sf = col.get("sourceField")
            if sf:
                fields.append(sf)
    return fields


def test_ndjson_is_valid_and_has_expected_counts() -> None:
    """File parses as NDJSON and contains exactly 1 dashboard + 3 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 3, "expected three lens panel objects"
    assert len(_by_type(objs, "index-pattern")) == 1, "expected exactly one index-pattern object"


def test_no_top_level_migration_version() -> None:
    """No object carries the legacy top-level ``migrationVersion`` dict."""
    for obj in _objects():
        assert "migrationVersion" not in obj, (
            f"object {obj.get('id')!r} (type={obj.get('type')!r}) still carries "
            f"top-level ``migrationVersion`` -- replace with ``typeMigrationVersion`` (string)"
        )


def test_no_lens_attributes_references() -> None:
    """No ``lens`` object has ``attributes.references``."""
    for lens in _by_type(_objects(), "lens"):
        assert "references" not in lens.get("attributes", {}), (
            f"lens {lens.get('id')!r} has ``attributes.references`` -- remove it "
            f"(the top-level envelope ``references`` is the canonical location)"
        )


def test_every_lens_has_visualization_type() -> None:
    """Every ``lens`` object carries ``attributes.visualizationType``.

    A Lens saved object persists and imports fine without this attribute, but is
    *optional at import, required at render* -- omitting it draws "Visualization
    type not found" (FRE-406/FRE-593/FRE-702).
    """
    for lens in _by_type(_objects(), "lens"):
        viz_type = lens.get("attributes", {}).get("visualizationType")
        assert viz_type, (
            f"lens {lens.get('id')!r} is missing ``attributes.visualizationType`` -- "
            f"it will import but render 'Visualization type not found'"
        )


def test_only_canonical_index_pattern_id() -> None:
    """The sole index-pattern object has the canonical shared id."""
    for ip in _by_type(_objects(), "index-pattern"):
        assert ip["id"] == CANONICAL_INDEX_PATTERN_ID, (
            f"index-pattern id is {ip['id']!r}; must be {CANONICAL_INDEX_PATTERN_ID!r}"
        )


def test_index_pattern_object_matches_canonical() -> None:
    """The self-included data-view (incl. its runtime field) is byte-identical to the
    canonical copy in data_views.ndjson.

    ``import_dashboards.sh`` uses ``overwrite=true``; a stale/sparse copy under the
    canonical id would clobber the ``runtimeFieldMap`` that ``insight_type_norm``
    depends on. Byte identity ensures the overwrite is canonical -> canonical (a no-op).
    """
    canonical_objs = [
        json.loads(line) for line in DATA_VIEWS_FILE.read_text().splitlines() if line.strip()
    ]
    canonical_ip = next(
        (
            o
            for o in canonical_objs
            if o.get("type") == "index-pattern" and o.get("id") == CANONICAL_INDEX_PATTERN_ID
        ),
        None,
    )
    assert canonical_ip is not None, (
        f"{DATA_VIEWS_FILE.name} must define an index-pattern with id={CANONICAL_INDEX_PATTERN_ID!r}"
    )

    local_ips = _by_type(_objects(), "index-pattern")
    assert len(local_ips) == 1
    local_ip = local_ips[0]

    assert json.dumps(local_ip, sort_keys=True) == json.dumps(canonical_ip, sort_keys=True), (
        "self-included index-pattern differs from the canonical copy in data_views.ndjson; "
        "use the verbatim canonical object (including runtimeFieldMap) to prevent a stale overwrite"
    )


def test_index_pattern_has_insight_type_norm_runtime_field() -> None:
    """The canonical index-pattern carries the ``insight_type_norm`` runtime field.

    Without it, the "Insight count by type" panel's ``insight_type_norm`` sourceField
    would not exist on the live cluster and the panel would show empty/error.
    """
    ip = _by_type(_objects(), "index-pattern")[0]
    runtime_field_map = json.loads(ip["attributes"].get("runtimeFieldMap", "{}"))
    assert "insight_type_norm" in runtime_field_map, (
        "index-pattern is missing the insight_type_norm runtime field definition"
    )


def test_every_lens_references_canonical_index_pattern() -> None:
    """Every lens top-level ``references`` points at the canonical index-pattern id."""
    for lens in _by_type(_objects(), "lens"):
        ip_ref_ids = [
            r["id"] for r in lens.get("references", []) if r.get("type") == "index-pattern"
        ]
        assert ip_ref_ids == [CANONICAL_INDEX_PATTERN_ID], (
            f"lens {lens.get('id')!r} references index-pattern ids {ip_ref_ids!r}; "
            f"must be [{CANONICAL_INDEX_PATTERN_ID!r}]"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a lens object in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    lens_ids = {o["id"] for o in _by_type(objs, "lens")}

    panel_refs = {r["name"]: r["id"] for r in dashboard["references"] if r["type"] == "lens"}
    for name, ref_id in panel_refs.items():
        assert ref_id in lens_ids, (
            f"dashboard panel ref {name!r} -> {ref_id!r} has no matching lens object"
        )

    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_ref_names = {p["panelRefName"] for p in panels}
    assert panel_ref_names == set(panel_refs), (
        f"panelsJSON panelRefNames {sorted(panel_ref_names)} must match "
        f"dashboard references {sorted(panel_refs)}"
    )


def test_lens_source_fields_are_verified_live() -> None:
    """Every Lens column sourceField is in the set verified live in agent-insights-*.

    Verified 2026-07-01: 465 model insight docs / 30d, correctly split via the
    insight_type_norm runtime field (prompt_composition=265, graph_staleness=100,
    trend=100); actionable true=265/false=200; confidence avg=0.72.
    """
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "insights_engine.ndjson" in IMPORT_SCRIPT.read_text(), (
        "insights_engine.ndjson must be present in the FILES list in import_dashboards.sh"
    )
