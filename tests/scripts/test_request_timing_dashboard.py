"""Static validation of request_timing.ndjson saved-object format and value redesign.

FRE-703 rebuilt this dashboard from scratch via the Kibana UI (never hand-authored). The
prior committed ndjson was independently broken on multiple axes, not just old-format:

1. ``panelsJSON`` referenced two panels (``rt-avg-by-phase``, ``rt-phase-table``) with no
   matching visualization object defined anywhere in the committed file -- both existed
   live (added 2026-05-23, never synced back), the same drift bug found in
   ``task_analytics`` (``ta-routing``) during this same FRE-703 wave.
2. The dashboard's top-level ``kibanaSavedObjectMeta`` carried a global filter locking
   every panel to ``phase.keyword:"llm_call:router"`` -- a value that does not exist in
   the live schema (verified: ``phase`` values are ``setup``/``persistence``/``other``/
   ``llm_inference``/``tool_execution``/``synthesis``, no ``llm_call:router``).
3. The one fully-defined panel (``rt-total-over-time``) queried ``total_duration_ms``
   under ``event_type:request_timing`` -- but that field has **zero documents** under
   that event type (verified live). The real field pairing is ``total_duration_ms`` on
   the *different* event type ``request_trace``.
4. The phase-level detail those two missing panels needed cannot be reached by Lens at
   all: ``event_type:request_timing`` carries phase data as a nested array field
   (ES ``phases`` mapped ``type:nested``), which Lens's standard aggregation UI does not
   expose in its field picker. The Lens-reachable equivalent lives on a *different* event,
   ``request_trace``, as a flat ``phases_summary.<phase>.duration_ms`` object -- one
   field per phase name, not a single aggregatable dimension.

Both ``request_timing`` (960 docs, last seen 2026-06-13) and ``request_trace`` (794 docs,
last seen 2026-06-07) are stale: this end-to-end request-phase telemetry has gone
completely dark for 18-24+ days as of the FRE-703 build, with no live replacement found.
This dashboard is rebuilt against ``request_trace`` (the only Lens-reachable source for
the stage breakdown) and the staleness is documented prominently in every panel
description and the dashboard description -- per the create-visualization skill,
verified-against-real-data means the *current* truth (including "this signal is dark"),
not just historically-accurate numbers.

These tests are *static* (no live cluster) and guard against:
1. Every ``lens`` object carries both ``attributes.title`` and
   ``attributes.visualizationType``.
2. No top-level ``migrationVersion``, no ``attributes.references`` nested inside a
   ``lens`` object.
3. FRE-535 dedupe lesson -- dashboard must use the canonical shared
   ``agent-logs-pattern`` index-pattern id, byte-identical to the canonical copy in
   ``data_views.ndjson``.
4. Data-backing (owner verification ask) -- every Lens ``sourceField`` is pinned to
   the set verified live against ``agent-logs-*`` during the FRE-703 build session.
5. No panel queries the dead ``request_timing_phase`` event type or the fictitious
   ``llm_call:router`` phase value, and every panel documents the staleness caveat.

Source of truth for the field types and real counts: live ``agent-logs-*``
verification recorded in the FRE-703 build session (2026-07-01):
  request_trace: total_duration_ms=double (avg 29830.9ms), phases_summary.<phase>.duration_ms
  =double for phase in {setup, persistence, other, llm_inference, tool_execution, synthesis}
  (verified averages: setup=6.2, persistence=4.5, other=952.0, llm_inference=24922.3,
  tool_execution=5409.4, synthesis=0.04). 794 docs total, 2026-04-23 to 2026-06-07 (=90d window).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "request_timing.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the three panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "@timestamp",
        "total_duration_ms",
        "phases_summary.setup.duration_ms",
        "phases_summary.persistence.duration_ms",
        "phases_summary.other.duration_ms",
        "phases_summary.llm_inference.duration_ms",
        "phases_summary.tool_execution.duration_ms",
        "phases_summary.synthesis.duration_ms",
        "___records___",
    }
)

# The dead event type and fictitious filter value from the prior committed file.
DEAD_EVENT_TYPE = "request_timing_phase"
FICTITIOUS_PHASE_VALUE = "llm_call:router"

# The dropped panel ids that had no matching visualization object in the prior file.
DROPPED_PANEL_IDS = frozenset({"rt-avg-by-phase", "rt-phase-table"})


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


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_expected_counts() -> None:
    """File parses as NDJSON and contains exactly 1 dashboard + 3 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 3, "expected three lens panel objects"
    assert len(_by_type(objs, "index-pattern")) == 1, "expected exactly one index-pattern object"
    assert len(_by_type(objs, "visualization")) == 0, (
        "no legacy visualization-type objects should remain in the rebuilt file"
    )


def test_no_top_level_migration_version() -> None:
    """No object carries the legacy top-level ``migrationVersion`` dict."""
    for obj in _objects():
        assert "migrationVersion" not in obj, (
            f"object {obj.get('id')!r} (type={obj.get('type')!r}) still carries "
            f"top-level ``migrationVersion`` — replace with ``typeMigrationVersion`` (string)"
        )


def test_no_lens_attributes_references() -> None:
    """No ``lens`` object has ``attributes.references``."""
    for lens in _by_type(_objects(), "lens"):
        assert "references" not in lens.get("attributes", {}), (
            f"lens {lens.get('id')!r} has ``attributes.references`` — remove it "
            f"(the top-level envelope ``references`` is the canonical location)"
        )


def test_every_lens_has_title_and_visualization_type() -> None:
    """Every ``lens`` object carries both ``attributes.title`` and ``attributes.visualizationType``."""
    for lens in _by_type(_objects(), "lens"):
        title = lens.get("attributes", {}).get("title")
        assert title, f"lens {lens.get('id')!r} is missing ``attributes.title``"
        viz_type = lens.get("attributes", {}).get("visualizationType")
        assert viz_type, (
            f"lens {lens.get('id')!r} is missing ``attributes.visualizationType`` — "
            f"it will import but render 'Visualization type not found'"
        )


# --------------------------------------------------------------------------- #
# FRE-535 dedupe — canonical index-pattern.
# --------------------------------------------------------------------------- #


def test_only_canonical_index_pattern_id() -> None:
    """The sole index-pattern object has the canonical shared id."""
    for ip in _by_type(_objects(), "index-pattern"):
        assert ip["id"] == CANONICAL_INDEX_PATTERN_ID, (
            f"index-pattern id is {ip['id']!r}; must be {CANONICAL_INDEX_PATTERN_ID!r}"
        )


def test_index_pattern_object_matches_canonical() -> None:
    """The self-included data-view is byte-identical to the canonical copy in data_views.ndjson."""
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
        "use the verbatim canonical object to prevent a sparse overwrite"
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


# --------------------------------------------------------------------------- #
# Panel reference wiring.
# --------------------------------------------------------------------------- #


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a lens object in the file, with no duplicates."""
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
    assert len(dashboard["references"]) == len(panel_refs), (
        "dashboard references must contain exactly one entry per panel, no duplicates"
    )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no ticket ID."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket ID"


def test_no_global_dashboard_filter() -> None:
    """The dashboard carries no top-level searchSourceJSON filter.

    The prior committed dashboard had a global filter locking every panel to
    phase.keyword:"llm_call:router" -- a value the live schema never emits, which
    would have silently emptied every panel regardless of any per-panel query.
    """
    dashboard = _by_type(_objects(), "dashboard")[0]
    search_source = json.loads(dashboard["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
    assert not search_source.get("filter"), (
        f"dashboard has a global filter {search_source.get('filter')!r} -- "
        f"the prior committed file's global filter locked every panel to a "
        f"fictitious phase value and silently emptied the whole dashboard"
    )


# --------------------------------------------------------------------------- #
# The dead event/field bugs and their fix.
# --------------------------------------------------------------------------- #


def test_dropped_dead_panels_not_referenced() -> None:
    """The two panels with no matching visualization object in the prior file are gone."""
    objs = _objects()
    all_ids = {o.get("id") for o in objs}
    assert not (all_ids & DROPPED_PANEL_IDS), (
        f"dropped panel id(s) {DROPPED_PANEL_IDS & all_ids} should not appear in the "
        f"rebuilt file at all"
    )


def test_no_panel_queries_dead_event_type_or_fictitious_phase() -> None:
    """No panel queries the dead request_timing_phase event or the fictitious phase value."""
    for lens in _by_type(_objects(), "lens"):
        query = lens["attributes"]["state"]["query"]["query"]
        assert DEAD_EVENT_TYPE not in query, (
            f"lens {lens.get('id')!r} query {query!r} references the dead "
            f"{DEAD_EVENT_TYPE!r} event type"
        )
        assert FICTITIOUS_PHASE_VALUE not in query, (
            f"lens {lens.get('id')!r} query {query!r} references the fictitious "
            f"phase value {FICTITIOUS_PHASE_VALUE!r}"
        )


def test_every_panel_documents_staleness() -> None:
    """Every panel description flags that the underlying telemetry has gone dark.

    Both request_timing and request_trace stopped emitting 18-24+ days before this
    build with no live replacement found -- readers must not mistake historical
    numbers for current behavior.
    """
    for lens in _by_type(_objects(), "lens"):
        description = lens["attributes"].get("description", "")
        assert "stale" in description.lower() or "dark" in description.lower(), (
            f"lens {lens.get('id')!r} description does not document that the "
            f"underlying telemetry (request_trace) has stopped emitting"
        )


# --------------------------------------------------------------------------- #
# Data-backing guard — sourceField pins.
# --------------------------------------------------------------------------- #


def test_lens_source_fields_are_verified_live() -> None:
    """Every Lens column sourceField is in the set verified live in agent-logs-*."""
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


# --------------------------------------------------------------------------- #
# Registration parity.
# --------------------------------------------------------------------------- #


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "request_timing.ndjson" in IMPORT_SCRIPT.read_text(), (
        "request_timing.ndjson must be present in the FILES list in import_dashboards.sh"
    )
