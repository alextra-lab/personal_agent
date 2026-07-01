"""Static validation of llm_performance.ndjson saved-object format and redesign.

FRE-703 rebuilt this dashboard via the Kibana UI (Playwright-driven, never hand-authored),
replacing 8 classic ``visState`` panels (none carrying ``visualizationType`` -- the
FRE-406/593/702 render trap) with 6 Lens panels mapped onto the stated decision: **model-call
health -- latency, tokens, error rate, by model/role.**

Step 0 (raw-event inspection, 2026-07-01) confirmed the real time field is ``@timestamp`` and
found two attribution gaps the old panels silently glossed over:

* ``model_call_completed`` carries ``role`` (not ``model_role`` -- several old panels
  referenced a field that doesn't exist on this event).
* ``model_call_error`` carries neither ``model`` nor ``role`` -- only ``error_type``,
  ``duration_ms``, and ``session_id``. The error panel can only break down by
  ``error_type``, not by model/role; the old dashboard's title ("LLM Errors Over Time")
  implied a per-model breakdown that the raw event cannot support.
* All 531 ``model_call_error`` docs fall between 2026-04-13 and 2026-06-01 with zero since
  -- a dead signal, not a broken panel; the panel description documents this and the
  dashboard stores a 1-year time range so the historical signal stays visible on load.

Panels (consolidated from 8 -- the old avg-latency-by-role and p95-latency-by-role panels
merged into one table, same for avg/percentile prompt-tokens-by-role):

* **Call volume by model** (lens table) -- count of ``model_call_completed`` by ``model``.
* **Latency by role** (lens table) -- avg/p50/p95/p99 ``latency_ms`` by ``role``.
* **Latency over time by role** (lens line) -- avg ``latency_ms`` over ``@timestamp`` by role.
* **Prompt tokens by role** (lens table) -- avg/p50/p95/p99 ``input_tokens`` by ``role``.
* **Token usage over time** (lens line) -- ``sum(input_tokens)`` and ``sum(output_tokens)``
  over ``@timestamp``.
* **Model call errors over time** (lens bar stacked) -- ``model_call_error`` count by
  ``error_type`` over ``@timestamp``; 1-year window, dead-signal caveat documented.

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType``.
4. The canonical ``agent-logs-pattern`` index-pattern is byte-identical to the one in
   ``data_views.ndjson``.
5. Every lens panel references the canonical index-pattern.
6. Dashboard panel references resolve for every lens panel.
7. Data-backing -- Lens sourceFields are pinned to the set verified live on 2026-07-01
   (in particular: ``role``, never ``model_role``).
8. The dashboard title carries no ticket id.
9. The file is registered in ``import_dashboards.sh``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "llm_performance.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

# Every Lens column sourceField verified live on 2026-07-01: model_call_completed
# (model, role, latency_ms, input_tokens, output_tokens, @timestamp) and model_call_error
# (error_type, @timestamp, ___records___). Note: "model_role" is NOT in this set -- the old
# dashboard's panels referenced it but the real field on model_call_completed is "role".
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "model",
        "role",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "@timestamp",
        "error_type",
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
    """Every Lens column sourceField is in the set verified live on 2026-07-01.

    In particular, this catches the old dashboard's bug: several panels referenced
    ``model_role``, a field that does not exist on ``model_call_completed`` (the real
    field is ``role``).
    """
    for lens in _by_type(_objects(), "lens"):
        for field in _lens_source_fields(lens):
            assert field in VERIFIED_SOURCE_FIELDS, (
                f"lens {lens.get('id')!r} uses sourceField {field!r}, which is not in "
                f"the live-verified set {sorted(VERIFIED_SOURCE_FIELDS)}"
            )


def test_error_panel_has_no_model_or_role_breakdown() -> None:
    """The errors panel never breaks down by model/role -- the raw event lacks both fields.

    model_call_error carries neither ``model`` nor ``role``; a panel that implies a
    per-model error breakdown (as the old "LLM Errors Over Time" title did) would be
    misleading. This panel breaks down by ``error_type`` only.
    """
    errors_lens = next(
        lens for lens in _by_type(_objects(), "lens") if lens["id"] == "llm-call-errors-over-time"
    )
    fields = _lens_source_fields(errors_lens)
    assert "model" not in fields
    assert "role" not in fields
    assert "error_type" in fields


def test_errors_panel_documents_dead_signal_caveat() -> None:
    """The errors panel's description documents the 2026-06-01 dead-signal caveat."""
    errors_lens = next(
        lens for lens in _by_type(_objects(), "lens") if lens["id"] == "llm-call-errors-over-time"
    )
    description = errors_lens["attributes"]["description"]
    assert "2026-06-01" in description, (
        "llm-call-errors-over-time description must document the dead-signal window"
    )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no Linear ticket id (owner feedback, FRE-406)."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket id"


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "llm_performance.ndjson" in IMPORT_SCRIPT.read_text(), (
        "llm_performance.ndjson must be present in the FILES list in import_dashboards.sh"
    )


def test_dashboard_stores_a_wide_time_range() -> None:
    """The dashboard stores a time range wide enough to show the errors panel's dead data.

    All 531 model_call_error docs sit in a 2026-04-13..2026-06-01 window; storing a 1-year
    range means a first-time viewer sees real data across all six panels instead of the
    default "last 15 minutes" leaving the errors panel blank.
    """
    dashboard = _by_type(_objects(), "dashboard")[0]
    attrs = dashboard["attributes"]
    assert attrs.get("timeRestore") is True
    assert attrs.get("timeFrom") == "now-1y/d"
    assert attrs.get("timeTo") == "now"
