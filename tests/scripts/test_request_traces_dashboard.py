"""Static validation of request_traces.ndjson saved-object format and value redesign.

FRE-703 rebuilt this dashboard from scratch via the Kibana UI (never hand-authored). The
prior committed ndjson was independently broken on multiple axes, not just old-format:

1. The dashboard's top-level ``references`` array had every entry duplicated (e.g.
   ``rt-request-overview`` appeared twice, ``rt-phase-averages`` twice, and so on for all
   four panels plus the controls index-pattern ref) -- a copy-paste-style corruption
   distinct from the missing-panel drift bug found in ``task_analytics``/``request_timing``
   during this same FRE-703 wave.
2. The dashboard's ``controlGroupInput`` options-list control was bound to the *legacy*
   index-pattern ``eabfafeb-13e6-4fd6-8739-1141cc7e4e8b`` ("agent-logs*") rather than the
   canonical shared ``agent-logs-pattern``.
3. ``rt-single-trace-waterfall`` packed 5 overlapping metrics (avg offset_ms, avg
   duration_ms, terms on name ordered by a synthetic "Step Sequence" metric, terms on
   phase, avg sequence) onto one horizontal_bar chart -- a shape no single Lens chart
   type can faithfully reproduce, and not clearly readable even as a classic vis.
4. ``request_trace_step``'s ``phase`` field is a plain terms-aggregatable keyword (unlike
   ``request_timing``'s nested ``phases.phase``), so the per-step and per-phase
   breakdowns rebuilt here use ordinary Lens Datatable Rows/Metrics rather than the
   multi-metric bar-chart workaround ``request_timing`` needed for its nested field.

This rebuild drops the Controls-based trace-id selector (unestablished UI risk this late
in the build wave) in favor of a fixed example ``trace_id`` in each drill-down panel's
query bar, with instructions in the description to edit it. A new "Slowest traces" panel
(built against the unfiltered ``request_trace`` event, absent from the original dashboard)
surfaces which trace_id to inspect and revealed a real finding: the duration distribution
is extremely right-skewed, with top traces running 4.7-18.8 minutes versus the ~29.8s
average established in the sibling ``request_timing`` dashboard.

Both ``request_trace`` (794 docs, last seen 2026-06-07) and ``request_trace_step`` (9834
docs, last seen 2026-06-07) are stale: this end-to-end request-tracing telemetry has gone
completely dark for 24+ days as of the FRE-703 build, with no live replacement found. The
staleness is documented prominently in every panel description and the dashboard
description -- per the create-visualization skill, verified-against-real-data means the
*current* truth (including "this signal is dark"), not just historically-accurate numbers.

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
5. No duplicate entries in the dashboard's top-level ``references`` (the exact
   corruption found in the prior committed file), and every panel documents the
   staleness caveat.
6. The example trace_id used across the drill-down panels is consistent, and the
   Slowest traces panel's description documents the right-skew finding with the
   verified numbers.

Source of truth for the field types and real counts: live ``agent-logs-*``
verification recorded in the FRE-703 build session (2026-07-01):
  request_trace: total_duration_ms=double (max of top-20 slowest ranges 282,424.6ms to
  1,128,878.5ms), 794 docs, 2026-04-23 to 2026-06-07.
  request_trace_step: sequence=long, phase=keyword (setup/persistence/other/
  llm_inference/tool_execution/synthesis), name=keyword, duration_ms=double,
  offset_ms=double, trace_id=keyword. 9834 docs, 2026-04-23 to 2026-06-07. Example trace
  763278fa-fbac-444b-b689-b79933158909 has 11 steps totaling 19.3s (verified: setup
  4/34.86ms, llm_inference 2/8504.96ms, synthesis 2/0.04ms, other 1/1211.74ms,
  persistence 1/9.06ms, tool_execution 1/1654.28ms).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "request_traces.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"
LEGACY_INDEX_PATTERN_ID = "eabfafeb-13e6-4fd6-8739-1141cc7e4e8b"

EXAMPLE_TRACE_ID = "763278fa-fbac-444b-b689-b79933158909"

# All Lens sourceField values used by the three panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "trace_id",
        "total_duration_ms",
        "sequence",
        "phase",
        "name",
        "duration_ms",
        "offset_ms",
        "___records___",
    }
)

# The dropped panel id whose 5-metric shape was replaced by two simpler panels.
DROPPED_PANEL_IDS = frozenset({"rt-single-trace-waterfall"})


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
    """The sole index-pattern object has the canonical shared id, not the legacy one."""
    for ip in _by_type(_objects(), "index-pattern"):
        assert ip["id"] == CANONICAL_INDEX_PATTERN_ID, (
            f"index-pattern id is {ip['id']!r}; must be {CANONICAL_INDEX_PATTERN_ID!r}"
        )
    all_ids = {o.get("id") for o in _objects()}
    assert LEGACY_INDEX_PATTERN_ID not in all_ids, (
        "the legacy index-pattern (originally bound to the dashboard's Controls "
        "selector) must not appear in the rebuilt file"
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


def test_panel_references_resolve_with_no_duplicates() -> None:
    """Every dashboard panel reference resolves to a lens object, with NO duplicates.

    The prior committed file had every reference entry duplicated (e.g.
    rt-request-overview appeared twice, rt-phase-averages twice, and so on) -- a
    copy-paste corruption independent of the dropped-panel drift bug. This guards
    against that exact defect recurring.
    """
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    lens_ids = {o["id"] for o in _by_type(objs, "lens")}

    refs = dashboard["references"]
    ref_keys = [(r["type"], r["id"], r["name"]) for r in refs]
    assert len(ref_keys) == len(set(ref_keys)), (
        f"dashboard references contain duplicate entries: {ref_keys} — this is the "
        f"exact copy-paste corruption found in the original committed file"
    )

    panel_refs = {r["name"]: r["id"] for r in refs if r["type"] == "lens"}
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
    assert len(refs) == len(panel_refs), (
        "dashboard references must contain exactly one entry per panel, no duplicates, "
        "and no leftover controls/index-pattern reference"
    )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no ticket ID."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket ID"


def test_no_controls_bound_to_legacy_index_pattern() -> None:
    """The dashboard carries no controlGroupInput bound to the legacy index-pattern.

    The prior committed dashboard's options-list control was bound to
    eabfafeb-13e6-4fd6-8739-1141cc7e4e8b ("agent-logs*") rather than the canonical
    shared agent-logs-pattern. This rebuild drops the Controls-based selector
    entirely in favor of an editable trace_id in each panel's query bar, so no
    controlGroupInput should reference the legacy id (or exist at all).
    """
    dashboard = _by_type(_objects(), "dashboard")[0]
    control_group = dashboard["attributes"].get("controlGroupInput")
    if control_group is not None:
        assert LEGACY_INDEX_PATTERN_ID not in json.dumps(control_group), (
            "controlGroupInput must not reference the legacy index-pattern id"
        )


# --------------------------------------------------------------------------- #
# The dropped panel and its replacement.
# --------------------------------------------------------------------------- #


def test_dropped_waterfall_panel_not_referenced() -> None:
    """The 5-metric waterfall panel, replaced by two simpler panels, is gone."""
    objs = _objects()
    all_ids = {o.get("id") for o in objs}
    assert not (all_ids & DROPPED_PANEL_IDS), (
        f"dropped panel id(s) {DROPPED_PANEL_IDS & all_ids} should not appear in the "
        f"rebuilt file at all"
    )


def test_every_panel_documents_staleness() -> None:
    """Every panel description flags that the underlying telemetry has gone dark.

    Both request_trace and request_trace_step stopped emitting 24+ days before this
    build with no live replacement found -- readers must not mistake historical
    numbers for current behavior.
    """
    for lens in _by_type(_objects(), "lens"):
        description = lens["attributes"].get("description", "")
        assert "stale" in description.lower() or "dark" in description.lower(), (
            f"lens {lens.get('id')!r} description does not document that the "
            f"underlying telemetry has stopped emitting"
        )


def test_drilldown_panels_use_consistent_example_trace_id() -> None:
    """The two drill-down panels (detail table, phase totals) query the same example trace."""
    objs = _objects()
    drilldown_titles = {"Trace detail table", "Trace phase totals"}
    found = 0
    for lens in _by_type(objs, "lens"):
        title = lens["attributes"].get("title")
        if title not in drilldown_titles:
            continue
        found += 1
        query = lens["attributes"]["state"]["query"]["query"]
        assert EXAMPLE_TRACE_ID in query, (
            f"lens {lens.get('id')!r} ({title!r}) query {query!r} does not filter on "
            f"the documented example trace_id {EXAMPLE_TRACE_ID!r}"
        )
    assert found == 2, f"expected exactly 2 drill-down panels, found {found}"


def test_slowest_traces_panel_documents_right_skew_finding() -> None:
    """The Slowest traces panel documents the verified outlier/right-skew finding.

    This is the decisive four-gate "verified against real data" discovery for this
    dashboard: the duration distribution is extremely right-skewed, with the top
    traces running far above the ~29.8s average established in the sibling
    request_timing dashboard. This must survive in the shipped description, not just
    in the build session.
    """
    objs = _objects()
    slowest = next(
        (
            lens
            for lens in _by_type(objs, "lens")
            if lens["attributes"].get("title", "").lower().startswith("slowest traces")
        ),
        None,
    )
    assert slowest is not None, "expected a 'Slowest traces' panel"
    description = slowest["attributes"]["description"]
    assert "right-skew" in description.lower(), (
        "Slowest traces description must document the right-skewed distribution finding"
    )
    assert "29.8" in description, (
        "Slowest traces description must cite the ~29.8s average from the sibling "
        "request_timing dashboard for comparison"
    )
    query = slowest["attributes"]["state"]["query"]["query"]
    assert EXAMPLE_TRACE_ID not in query, (
        "Slowest traces panel must be unfiltered by trace_id (it is the entry point "
        "for picking a trace, not a drill-down panel)"
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
    assert "request_traces.ndjson" in IMPORT_SCRIPT.read_text(), (
        "request_traces.ndjson must be present in the FILES list in import_dashboards.sh"
    )
