"""Static validation of turn_session_artifact.ndjson saved-object format and value redesign.

FRE-703 replaced the original 13-panel hand-authored dashboard (10 classic ``visState``
visualizations + 2 searches + 1 markdown banner, none carrying ``visualizationType`` and
therefore all rendering "Visualization type not found") with 5 Lens/Discover panels built
via the Kibana UI, mapped directly onto the stated decision: **per turn/session -- cost,
artifacts produced, envelope integrity**.

Panels, and why the other 8 were dropped rather than carried forward:

* **Session activity: cost, turns, calls, artifacts** (lens table) -- one consolidated
  per-session scorecard (total cost, LLM calls, distinct turns, avg latency, artifacts
  produced), using per-metric "Filter by" to combine ``api_cost_recorded`` and
  ``artifact_gate_decision`` in a single panel. Replaces the old
  "Turns per Session" / "LLM Calls & Avg Latency per Session" / "Active Sessions Over
  Time" panels, which either duplicated this rollup across 3 separate panels or were
  tangential to the cost/artifact decision.
* **Turn complexity mix over time** (lens bar stacked) -- gateway_output complexity
  (simple/moderate/complex) per time bucket. Direct redesign of the old
  "Turn Complexity Over Time" / "Turn Complexity Distribution" pair, consolidated to one.
* **Turn classification detail** (search) -- per-turn task_type/complexity/strategy/
  token_count/mode detail table, direct redesign of the old "Turn Classification Detail".
* **Artifact envelope + gate status summary** (lens table) -- single-row scorecard
  combining ``artifact_gate_decision`` outcomes and ``artifact_envelope_integrity`` probe
  outcomes. Replaces "Envelope Probe Status" / "Degraded Envelopes (alarm)" / "Gate
  Decisions Over Time" / the markdown readiness note (data is sparse enough -- 14 + 11
  docs total -- that a scorecard is more legible than 3 separate charts).
* **Artifact envelope detail (join on artifact_id)** (search) -- direct redesign of the
  old "Artifact Envelope Detail".

Dropped entirely: "Cross-Turn KV Cache Reuse Over Time" (duplicated by
``prompt-cost-cache`` dashboard's cache-erosion panels) and "Top Traces by Error Events"
(trace-keyed, not session/turn-keyed -- tangential to this dashboard's stated decision).

Step 0 (raw-event inspection, 2026-07-01) found ``agent-logs-2026.06.17`` sitting near the
ES 300-field mapping ceiling: 1 of 14 ``artifact_gate_decision`` docs has ``gate_decision``
silently dropped from the mapping on that day (present in ``_source``, absent from
``_field_caps``, appears in the doc's ``_ignored`` array) -- the same systemic
``index.mapping.total_fields.limit`` issue already documented in the ``delegation_outcomes``
retire-proposal (PR #295). It is NOT re-patched here (systemic, cross-cutting); the summary
panel's description calls it out so "Gate: other/missing" = 1 is not misread as a real
non-committed decision.

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType`` (render-time-only
   requirement a hand-authored object silently omits).
4. The canonical ``agent-logs-pattern`` index-pattern id is used, and the self-included
   copy is byte-identical to the canonical copy in ``data_views.ndjson``.
5. Every lens/search panel references exactly the canonical index-pattern.
6. Dashboard panel references resolve for BOTH lens and search panel types.
7. Data-backing -- Lens sourceFields and search columns are pinned to the set verified
   live in ``agent-logs-*`` on 2026-07-01.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "turn_session_artifact.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# Verified live 2026-07-01: 1,696 gateway_output docs/90d (simple=1519, moderate=158,
# complex=19); 14 artifact_gate_decision docs (committed=13, other/missing=1); 11
# artifact_envelope_integrity docs (verified=10, unverified_access_denied=1, envelope_ok
# false=0).
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "session_id",
        "trace_id",
        "cost_usd",
        "latency_ms",
        "complexity",
        "@timestamp",
        "___records___",
    }
)

VERIFIED_SEARCH_COLUMNS = frozenset(
    {
        "trace_id",
        "task_type",
        "complexity",
        "strategy",
        "token_count",
        "mode",
        "artifact_id",
        "session_id",
        "probe_status",
        "envelope_ok",
        "served_mime",
        "http_status",
        "gate_decision",
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
    """File parses as NDJSON: 1 dashboard + 3 lens + 2 search + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 3, "expected three lens panel objects"
    assert len(_by_type(objs, "search")) == 2, "expected two saved-search panel objects"
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
    """The self-included data-view is byte-identical to the canonical copy in
    data_views.ndjson.

    ``import_dashboards.sh`` uses ``overwrite=true``; a stale/sparse copy under the
    canonical id would clobber the canonical shared object. Byte identity ensures the
    overwrite is canonical -> canonical (a no-op).
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
        "use the verbatim canonical object to prevent a stale overwrite"
    )


def test_every_lens_and_search_references_canonical_index_pattern() -> None:
    """Every lens/search top-level ``references`` points at the canonical index-pattern id."""
    for obj in _by_type(_objects(), "lens") + _by_type(_objects(), "search"):
        ip_ref_ids = [
            r["id"] for r in obj.get("references", []) if r.get("type") == "index-pattern"
        ]
        assert ip_ref_ids == [CANONICAL_INDEX_PATTERN_ID], (
            f"{obj.get('type')} {obj.get('id')!r} references index-pattern ids {ip_ref_ids!r}; "
            f"must be [{CANONICAL_INDEX_PATTERN_ID!r}]"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a lens or search object in the file."""
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
    """Every Lens column sourceField is in the set verified live in agent-logs-*."""
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_search_columns_are_verified_live() -> None:
    """Every search panel's displayed columns are in the set verified live."""
    for search in _by_type(_objects(), "search"):
        columns = search.get("attributes", {}).get("columns", [])
        for col in columns:
            assert col in VERIFIED_SEARCH_COLUMNS, (
                f"search {search.get('id')!r} shows column {col!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SEARCH_COLUMNS)}"
            )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no Linear ticket id (owner feedback, FRE-406)."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket id"


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "turn_session_artifact.ndjson" in IMPORT_SCRIPT.read_text(), (
        "turn_session_artifact.ndjson must be present in the FILES list in import_dashboards.sh"
    )
