"""Static validation of cost_budget.ndjson saved-object format and redesign.

FRE-703 rebuilt this dashboard via the Kibana UI (Playwright-driven, never hand-authored),
replacing 7 classic ``visState`` panels (none carrying ``visualizationType`` -- the
FRE-406/593/702 render trap) with 6 Lens panels mapped onto the stated decision: **am I
within budget, what drives spend, am I near a cap?**

Step 0 (raw-event inspection, 2026-07-01) found ``cost_gate_committed`` spans two
non-overlapping event-field schemas with a clean cutover and zero overlap:

* OLD (2026-05-01..2026-06-08, 2544 docs): string ``actual_cost``/``reserved``/``delta``
  fields (all mapped ``keyword``), no ``role`` field.
* NEW (2026-06-09 onward, 572 docs): numeric ``actual_cost_usd``/``reserved_usd``/
  ``delta_usd`` (all ``double``), plus ``role`` (keyword).

The spend/settlement panels use the new numeric fields and are naturally scoped to the
new schema (no old-schema contamination). ``litellm_request_budget_denied`` (463 docs)
is a dead signal since 2026-06-01 -- that panel uses a 1-year window, and the dashboard
itself stores that time range, so the historical signal stays visible on load instead of
silently rendering empty under the default "last 15 minutes".

Panels:

* **Budget cap utilization scorecard** (lens table) -- daily/weekly cap, spend, and
  utilization per budget role, from periodic ``budget_counter_snapshot`` events (Last
  value aggregation, filtered per metric by ``time_window``).
* **Actual spend over time by role** (lens bar stacked) -- ``sum(actual_cost_usd)`` over
  ``@timestamp``, broken down by ``role``; new-schema only.
* **Reserve/commit/refund lifecycle funnel** (lens bar) -- event counts across the
  cost_gate reservation lifecycle.
* **Top sessions by model spend** (lens table) -- ``sum(cost_usd)`` from
  ``model_call_completed``, grouped by ``session_id``, top 15.
* **Budget denials over time** (lens bar stacked) -- ``litellm_request_budget_denied``
  count over time by ``budget_role``; 1-year window, dead-signal caveat documented in the
  panel description.
* **Net settlement delta over time** (lens line) -- ``sum(delta_usd)`` from
  ``cost_gate_committed``, new-schema only (old-schema docs lack ``delta_usd`` and are
  naturally excluded, not filtered).

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType``.
4. The canonical ``agent-logs-pattern`` index-pattern is byte-identical to the one in
   ``data_views.ndjson``.
5. Every lens panel references the canonical index-pattern.
6. Dashboard panel references resolve for every lens panel.
7. Data-backing -- Lens sourceFields are pinned to the set verified live on 2026-07-01.
8. The dashboard title carries no ticket id.
9. The file is registered in ``import_dashboards.sh``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "cost_budget.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

# Every Lens column sourceField verified live on 2026-07-01: budget_counter_snapshot
# (role, time_window, cap_usd, running_total, utilization_ratio), cost_gate_committed
# new-schema (actual_cost_usd, role, delta_usd), the reserve/commit/refund lifecycle
# (event_type, ___records___), and model_call_completed (session_id, cost_usd).
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "role",
        "cap_usd",
        "running_total",
        "utilization_ratio",
        "actual_cost_usd",
        "@timestamp",
        "event_type",
        "___records___",
        "session_id",
        "cost_usd",
        "budget_role",
        "delta_usd",
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
    """File parses as NDJSON: 1 dashboard + 6 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 6, "expected six lens panel objects"
    assert len(_by_type(objs, "index-pattern")) == 1, "expected one index-pattern object"


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


def test_index_pattern_is_byte_identical_to_data_views() -> None:
    """The self-included ``agent-logs-pattern`` object matches ``data_views.ndjson``.

    Every dashboard file self-includes the canonical index-pattern so it can be
    imported standalone; it must stay byte-identical to the shared copy so the two
    never drift into two different data views with the same id.
    """
    canonical_line = next(
        line
        for line in DATA_VIEWS_FILE.read_text().splitlines()
        if line.strip() and json.loads(line).get("id") == "agent-logs-pattern"
    )
    canonical = json.loads(canonical_line)
    found = next(
        o for o in _by_type(_objects(), "index-pattern") if o["id"] == "agent-logs-pattern"
    )
    assert found == canonical, "agent-logs-pattern index-pattern has drifted from data_views.ndjson"


def test_every_lens_references_the_canonical_index_pattern() -> None:
    """Every lens references exactly the canonical ``agent-logs-pattern`` index-pattern."""
    for lens in _by_type(_objects(), "lens"):
        ip_refs = [r["id"] for r in lens.get("references", []) if r.get("type") == "index-pattern"]
        assert ip_refs == ["agent-logs-pattern"], (
            f"lens {lens.get('id')!r} must reference exactly agent-logs-pattern, got {ip_refs}"
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
    """Every Lens column sourceField is in the set verified live on 2026-07-01."""
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no Linear ticket id (owner feedback, FRE-406)."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket id"


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "cost_budget.ndjson" in IMPORT_SCRIPT.read_text(), (
        "cost_budget.ndjson must be present in the FILES list in import_dashboards.sh"
    )


def test_denials_panel_documents_dead_signal_caveat() -> None:
    """The denials panel's description documents the 2026-06-01 dead-signal caveat.

    All 463 litellm_request_budget_denied docs fall between 2026-05-07 and 2026-06-01
    with zero since; a viewer who doesn't know this could mistake an empty recent
    window for a broken panel instead of an accurate absence of denials.
    """
    denials = next(
        lens for lens in _by_type(_objects(), "lens") if lens["id"] == "cb-denials-over-time"
    )
    description = denials["attributes"]["description"]
    assert "2026-06-01" in description, (
        "cb-denials-over-time description must document the dead-signal window"
    )


def test_dashboard_stores_a_wide_time_range() -> None:
    """The dashboard stores a time range wide enough to show the denials/settlement data.

    Unlike the other dashboard files (which rely on the viewer's ambient time range),
    this dashboard mixes live spend panels with two historically-scoped panels (denials
    dead since 2026-06-01, settlement delta only since the 2026-06-09 schema cutover).
    Storing a 1-year range means a first-time viewer sees real data across all six
    panels instead of the default "last 15 minutes" showing four panels and two blanks.
    """
    dashboard = _by_type(_objects(), "dashboard")[0]
    attrs = dashboard["attributes"]
    assert attrs.get("timeRestore") is True
    assert attrs.get("timeFrom") == "now-1y/d"
    assert attrs.get("timeTo") == "now"
