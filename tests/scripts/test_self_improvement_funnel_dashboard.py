"""Static validation of self_improvement_funnel.ndjson (ADR-0105 D6/AC-5, FRE-719).

Retires the two disjoint dashboards (Insights Engine, Reflection Insights) with one
funnel: produced -> promoted -> shipped/canceled, reading real `agent-captains-
reflections-*` documents, plus a raw-event log of ADR-0040 budget-throttle events
from the new `agent-captains-funnel-events-*` index (built via Playwright against
Kibana, per the create-visualization skill -- never hand-authored).

The funnel-stage x-axis uses a Lens `Filters` bucket (KQL, not a field-picker
aggregation) rather than a `Top values` breakdown on `proposed_change.source`: as of
this ticket, zero historical proposals carry the `source` discriminator (FRE-715
landed after all existing reflections were produced), so a source facet would render
as an all-empty chart today -- a real, honest, not-yet-populated state, not a defect.
The mechanism (Filters/KQL) does not require Kibana to have "discovered" a field with
at least one non-null value, unlike Top values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "self_improvement_funnel.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

REFLECTIONS_INDEX_PATTERN_ID = "agent-captains-reflections-pattern"
FUNNEL_EVENTS_INDEX_PATTERN_ID = "agent-captains-funnel-events-pattern"
CANONICAL_INDEX_PATTERN_IDS = frozenset(
    {REFLECTIONS_INDEX_PATTERN_ID, FUNNEL_EVENTS_INDEX_PATTERN_ID}
)

VERIFIED_SOURCE_FIELDS = frozenset({"___records___"})


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
    """File parses as NDJSON and contains exactly 1 dashboard + 1 lens + 1 search + 2 index-patterns."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 1, "expected one lens panel object"
    assert len(_by_type(objs, "search")) == 1, "expected one saved-search panel object"
    assert len(_by_type(objs, "index-pattern")) == 2, "expected two index-pattern objects"


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


def test_only_canonical_index_pattern_ids() -> None:
    """Both index-pattern objects carry the canonical shared ids."""
    ids = {ip["id"] for ip in _by_type(_objects(), "index-pattern")}
    assert ids == CANONICAL_INDEX_PATTERN_IDS, f"index-pattern ids are {ids!r}"


def test_index_pattern_objects_match_canonical() -> None:
    """Both self-included data-views are byte-identical to the canonical copies in
    data_views.ndjson.

    ``import_dashboards.sh`` uses ``overwrite=true``; a stale/sparse copy under a
    canonical id would clobber the canonical version (e.g. dropping a runtime field
    another dashboard depends on). Byte identity ensures the overwrite is
    canonical -> canonical (a no-op).
    """
    canonical_objs = [
        json.loads(line) for line in DATA_VIEWS_FILE.read_text().splitlines() if line.strip()
    ]
    canonical_by_id = {o["id"]: o for o in canonical_objs if o.get("type") == "index-pattern"}
    for expected_id in CANONICAL_INDEX_PATTERN_IDS:
        assert expected_id in canonical_by_id, (
            f"{DATA_VIEWS_FILE.name} must define an index-pattern with id={expected_id!r}"
        )

    for local_ip in _by_type(_objects(), "index-pattern"):
        canonical_ip = canonical_by_id[local_ip["id"]]
        assert json.dumps(local_ip, sort_keys=True) == json.dumps(canonical_ip, sort_keys=True), (
            f"self-included index-pattern {local_ip['id']!r} differs from the canonical "
            f"copy in {DATA_VIEWS_FILE.name}; use the verbatim canonical object to "
            f"prevent a stale overwrite"
        )


def test_lens_references_canonical_reflections_index_pattern() -> None:
    """The lens panel's top-level ``references`` points at the reflections index-pattern."""
    for lens in _by_type(_objects(), "lens"):
        ip_ref_ids = [
            r["id"] for r in lens.get("references", []) if r.get("type") == "index-pattern"
        ]
        assert ip_ref_ids == [REFLECTIONS_INDEX_PATTERN_ID], (
            f"lens {lens.get('id')!r} references index-pattern ids {ip_ref_ids!r}; "
            f"must be [{REFLECTIONS_INDEX_PATTERN_ID!r}]"
        )


def test_search_references_canonical_funnel_events_index_pattern() -> None:
    """The saved-search panel's ``references`` points at the funnel-events index-pattern."""
    for search in _by_type(_objects(), "search"):
        ip_ref_ids = [
            r["id"] for r in search.get("references", []) if r.get("type") == "index-pattern"
        ]
        assert ip_ref_ids == [FUNNEL_EVENTS_INDEX_PATTERN_ID], (
            f"search {search.get('id')!r} references index-pattern ids {ip_ref_ids!r}; "
            f"must be [{FUNNEL_EVENTS_INDEX_PATTERN_ID!r}]"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a lens/search object in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    embeddable_ids = {o["id"] for o in objs if o.get("type") in ("lens", "search")}

    # Reference names are "<panelIndex>:panel_<panelIndex>" (per-panel-index
    # namespacing); panelsJSON's panelRefName is the suffix after the colon.
    panel_refs = {
        r["name"].split(":", 1)[-1]: r["id"]
        for r in dashboard["references"]
        if r["type"] in ("lens", "search")
    }
    for name, ref_id in panel_refs.items():
        assert ref_id in embeddable_ids, (
            f"dashboard panel ref {name!r} -> {ref_id!r} has no matching lens/search object"
        )

    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_ref_names = {p["panelRefName"] for p in panels}
    assert panel_ref_names == set(panel_refs), (
        f"panelsJSON panelRefNames {sorted(panel_ref_names)} must match "
        f"dashboard references {sorted(panel_refs)}"
    )


def test_lens_source_fields_are_verified_live() -> None:
    """Every Lens column sourceField is in the set verified live against real ES data.

    Verified 2026-07-06: agent-captains-reflections-* has 1904 real documents
    (produced), a real (small) number with linear_issue_id set (promoted), 0 with
    ticket_outcome=shipped, and 4 with ticket_outcome in (owner-rejected,
    canceled-as-noise) (backfilled from 6 historically-linked tickets' real Linear
    state). The funnel-stage x-axis and count metric use no field-picker sourceField
    beyond Lens's own count-of-records sentinel; the KQL filter text itself is not a
    "sourceField" and is not subject to this check.
    """
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "self_improvement_funnel.ndjson" in IMPORT_SCRIPT.read_text(), (
        "self_improvement_funnel.ndjson must be present in the FILES list in import_dashboards.sh"
    )


def test_legacy_dashboards_are_retired() -> None:
    """The two dashboards this ticket replaces are gone from the repo and the import script."""
    dashboards_dir = DASHBOARD_FILE.parent
    for legacy in ("insights_engine.ndjson", "reflection_insights.ndjson"):
        assert not (dashboards_dir / legacy).exists(), f"{legacy} must be deleted (retired)"
        assert legacy not in IMPORT_SCRIPT.read_text(), (
            f"{legacy} must be removed from import_dashboards.sh"
        )
