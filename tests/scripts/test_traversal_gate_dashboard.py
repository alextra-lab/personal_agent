"""Static validation of traversal_gate.ndjson saved-object format and value redesign.

FRE-703 rebuilt this dashboard from scratch via the Kibana UI (never hand-authored). The
prior version was six hand-authored ``visualization``/``visState`` objects with no
``visualizationType`` — they persisted and imported fine but rendered "Visualization type
not found" for every panel (the FRE-406/FRE-593/FRE-702 trap). This redesign also fixed a
real accuracy bug in the "reasons" panel query and documents a systemic ES data-quality
caveat discovered while verifying panels against live data.

These tests are *static* (no live cluster) and guard against:
1. FRE-406/FRE-703 trap — every ``lens`` object must carry ``attributes.visualizationType``.
2. FRE-546-style traps — no top-level ``migrationVersion``, no ``attributes.references``
   nested inside a ``lens`` object.
3. FRE-535 dedupe lesson — dashboard must use the canonical shared ``agent-logs-pattern``
   index-pattern id, byte-identical to the canonical copy in ``data_views.ndjson``.
4. Data-backing (owner verification ask) — every Lens ``sourceField`` is pinned to the set
   verified live against ``agent-logs-*`` during the FRE-703 build session.
5. The KQL-negation bug — the original "reasons" panel used ``not decision:allow``, which
   also matches docs where ``decision`` is entirely absent (dropped by the ES
   ``index.mapping.total_fields.limit=300`` ceiling, ``ignore_dynamic_beyond_limit: true``),
   contaminating the reason breakdown with false allow-side entries like "within
   thresholds". The fix is explicit positive inclusion:
   ``decision: ("warn_consecutive" or "block_consecutive")``.
6. The field-ceiling undercount caveat is documented in the affected panels' descriptions
   (~12.6% of tool_loop_gate docs, ~33% of route_trace_written docs) so a future reader
   does not mistake the undercount for zero-signal.

Source of truth for the field types and real counts: live ``agent-logs-*`` verification
recorded in the FRE-703 build session (2026-07-01):
  tool_loop_gate: decision=keyword, reason=keyword, tool_name=keyword, @timestamp=date.
  route_trace_written: gateway_label=keyword, orchestration_event=keyword, @timestamp=date.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "traversal_gate.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the six panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "decision",
        "reason",
        "tool_name",
        "gateway_label",
        "orchestration_event",
        "@timestamp",
        "___records___",
    }
)

# Panel titles whose description must document the ES field-ceiling undercount caveat.
FIELD_CEILING_CAVEAT_PANELS = frozenset(
    {
        "tg-gate-decisions-over-time",
        "tg-gate-decision-outcomes",
        "tg-route-trace-stimulus-label",
        "tg-route-trace-orchestration-outcome",
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


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_expected_counts() -> None:
    """File parses as NDJSON and contains exactly 1 dashboard + 6 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 6, "expected six lens panel objects"
    assert len(_by_type(objs, "index-pattern")) == 1, "expected exactly one index-pattern object"


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


# --------------------------------------------------------------------------- #
# FRE-406/FRE-703 trap — visualizationType (render-time-only requirement).
# --------------------------------------------------------------------------- #


def test_every_lens_has_visualization_type() -> None:
    """Every ``lens`` object carries ``attributes.visualizationType``.

    The prior version of this dashboard (six hand-authored ``visState`` objects) had no
    such field and rendered "Visualization type not found" for every panel.
    """
    for lens in _by_type(_objects(), "lens"):
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


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no ticket ID."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket ID"


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
# The KQL-negation accuracy bug — Panel D ("reasons") must use positive inclusion.
# --------------------------------------------------------------------------- #


def test_reasons_panel_does_not_use_negation_query() -> None:
    """The block/warn-reasons panel must not use ``not decision:allow``.

    That negation form also matches docs where ``decision`` is entirely absent (dropped
    by the ES field-ceiling bug), which contaminates the reason breakdown with false
    allow-side entries. The fix is explicit positive inclusion of the non-allow values.
    """
    lens = next(o for o in _by_type(_objects(), "lens") if o["id"] == "tg-gate-block-warn-reasons")
    query = lens["attributes"]["state"]["query"]["query"]
    assert "not decision" not in query, (
        f"reasons panel query {query!r} uses negation, which also matches docs with a "
        f"missing decision field — use explicit decision:(warn_consecutive or "
        f"block_consecutive) inclusion instead"
    )
    assert "warn_consecutive" in query and "block_consecutive" in query, (
        f"reasons panel query {query!r} must explicitly include both non-allow decision values"
    )


# --------------------------------------------------------------------------- #
# ES field-ceiling undercount caveat documentation.
# --------------------------------------------------------------------------- #


def test_field_ceiling_caveat_documented_on_affected_panels() -> None:
    """Panels backed by fields dropped by the ES field-ceiling bug document the caveat.

    ``index.mapping.total_fields.limit=300`` with ``ignore_dynamic_beyond_limit: true``
    silently drops new fields once a day's mapping nears its cap. ~12.6% of
    tool_loop_gate docs are missing ``decision``; ~33% of route_trace_written docs are
    missing ``gateway_label``/``orchestration_event``. Undocumented, these look like
    zero-signal rather than an undercount.
    """
    lenses = {o["id"]: o for o in _by_type(_objects(), "lens")}
    for panel_id in FIELD_CEILING_CAVEAT_PANELS:
        assert panel_id in lenses, f"expected panel {panel_id!r} not found in dashboard"
        description = lenses[panel_id]["attributes"].get("description", "")
        assert "total_fields.limit" in description, (
            f"panel {panel_id!r} description does not document the ES field-ceiling "
            f"undercount caveat"
        )


# --------------------------------------------------------------------------- #
# Registration parity.
# --------------------------------------------------------------------------- #


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "traversal_gate.ndjson" in IMPORT_SCRIPT.read_text(), (
        "traversal_gate.ndjson must be present in the FILES list in import_dashboards.sh"
    )
