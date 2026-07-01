"""Static validation of reflection_insights.ndjson saved-object format and value redesign.

FRE-703 rebuilt all three chart panels via the Kibana UI (Playwright-driven, never
hand-authored) to fix two problems found during step 0 (raw-event inspection):

1. Same mapping-drift class of bug as insights_engine (FRE-703): ``status`` and
   ``proposed_change.category`` are mapped as ``text`` (fielddata disabled, not
   aggregatable) on daily indices before ~2026-05-10, and as ``keyword`` on newer ones.
   Aggregating on either field naively drops the shards using the other convention. Fixed
   via two Kibana Data View runtime fields (``status_norm``, ``category_norm``) that
   coalesce both conventions with a try/catch guard (``doc.containsKey`` alone was not
   sufficient here -- Painless still attempted to build fielddata for the text-mapped
   branch even when unreached, throwing at runtime; the try/catch swallows that).
   Verified against a live raw ES aggregation to return the correct totals across all 67
   shards for both fields.
2. The stated decision ("what self-improvement proposals exist, and their status?") was
   never answered -- the previous dashboard used fragile KQL substring-matching on free-text
   fields (``impact_assessment:*high*`` etc., ``proposed_change.what:*threshold*`` etc.) and
   never once referenced the actual ``status`` field, which shows a major real finding:
   all-time distribution is 1864 ``awaiting_approval`` vs. 6 ``approved`` -- proposals are
   essentially never actioned.

The "Impact assessment distribution" and "Reflection metrics trending" panels were dropped:
both were built on hardcoded substring-matching heuristics over free-text fields, which is
exactly the kind of fragile, misleading proxy this audit exists to replace. The redesigned
"proposals by category" panel (using the real ``proposed_change.category`` enum field) and
"proposals by status" panel serve the decision more directly and accurately.

A fourth panel -- a Discover saved search, not a Lens visualization -- was added per owner
request to show *what the individual insights are* (title, category, status; sorted newest
first), not just aggregate counts. While checking whether a Linear-issue link should be a
column, we found ``linear_issue_id`` is present in the schema but ``null`` on all 1,800
reflection docs (all-time) -- no proposal has ever actually been linked to a Linear issue.
That column was therefore left out (it would always render blank) and the finding is
recorded in the dashboard description instead.

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType``.
4. The canonical ``agent-captains-reflections-pattern`` index-pattern id is used, and the
   self-included copy (including its ``runtimeFieldMap``) is byte-identical to the canonical
   copy in ``data_views.ndjson``.
5. Data-backing -- the Lens ``sourceField`` values used are pinned to the set verified live
   in ``agent-captains-reflections-*``.
6. The dashboard's panel references (lens *and* search) all resolve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "reflection_insights.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-captains-reflections-pattern"

VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "status_norm",
        "category_norm",
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
    """File parses as NDJSON and contains exactly 1 dashboard + 3 lens + 1 search + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 3, "expected three lens panel objects"
    assert len(_by_type(objs, "search")) == 1, "expected one search (detail table) panel object"
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
    """Every ``lens`` object carries ``attributes.visualizationType``."""
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
    """The self-included data-view (incl. its runtime fields) is byte-identical to the
    canonical copy in data_views.ndjson.
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


def test_index_pattern_has_status_and_category_runtime_fields() -> None:
    """The canonical index-pattern carries both ``status_norm`` and ``category_norm``
    runtime fields -- without them the two breakdown panels' sourceFields would not
    exist on the live cluster.
    """
    ip = _by_type(_objects(), "index-pattern")[0]
    runtime_field_map = json.loads(ip["attributes"].get("runtimeFieldMap", "{}"))
    assert "status_norm" in runtime_field_map, (
        "index-pattern is missing the status_norm runtime field definition"
    )
    assert "category_norm" in runtime_field_map, (
        "index-pattern is missing the category_norm runtime field definition"
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


def test_search_references_canonical_index_pattern() -> None:
    """The search (detail table) object's index-ref points at the canonical index-pattern id."""
    for search in _by_type(_objects(), "search"):
        ip_ref_ids = [
            r["id"] for r in search.get("references", []) if r.get("type") == "index-pattern"
        ]
        assert ip_ref_ids == [CANONICAL_INDEX_PATTERN_ID], (
            f"search {search.get('id')!r} references index-pattern ids {ip_ref_ids!r}; "
            f"must be [{CANONICAL_INDEX_PATTERN_ID!r}]"
        )


def test_search_does_not_show_linear_issue_id() -> None:
    """The detail table does not include ``linear_issue_id`` as a column.

    Verified 2026-07-01: linear_issue_id is null on all 1,800 reflection docs (all-time) --
    no proposal has ever been linked to a Linear issue. A column that is always blank is
    exactly the kind of misleading noise this audit exists to remove; if this ever becomes
    populated in real telemetry, add the column back deliberately (with a link format).
    """
    for search in _by_type(_objects(), "search"):
        columns = search.get("attributes", {}).get("columns", [])
        assert "linear_issue_id" not in columns, (
            "linear_issue_id is null on every reflection doc to date; showing it as a "
            "column would always render blank"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference (lens and search) resolves to an object in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    lens_ids = {o["id"] for o in _by_type(objs, "lens")}
    search_ids = {o["id"] for o in _by_type(objs, "search")}
    panelable_ids = lens_ids | search_ids

    panel_refs = {
        r["name"]: r["id"] for r in dashboard["references"] if r["type"] in ("lens", "search")
    }
    for name, ref_id in panel_refs.items():
        assert ref_id in panelable_ids, (
            f"dashboard panel ref {name!r} -> {ref_id!r} has no matching lens/search object"
        )

    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_ref_names = {p["panelRefName"] for p in panels}
    assert panel_ref_names == set(panel_refs), (
        f"panelsJSON panelRefNames {sorted(panel_ref_names)} must match "
        f"dashboard references {sorted(panel_refs)}"
    )


def test_lens_source_fields_are_verified_live() -> None:
    """Every Lens column sourceField is in the set verified live in
    agent-captains-reflections-*.

    Verified 2026-07-01: 193 reflections/30d, correctly split via the status_norm/
    category_norm runtime fields (status: awaiting_approval=191, approved=1; category:
    performance=96, ux=62, reliability=28, knowledge=3, cost=1, observability=1, safety=1).
    """
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "reflection_insights.ndjson" in IMPORT_SCRIPT.read_text(), (
        "reflection_insights.ndjson must be present in the FILES list in import_dashboards.sh"
    )
