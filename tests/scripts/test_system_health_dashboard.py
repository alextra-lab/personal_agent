"""Static validation of system_health.ndjson saved-object format and value-confirm pass.

FRE-703's worklist marked this dashboard "already renders (master-verified); quick value
confirm" -- but step 0 (inspecting the raw events, including their real time field) found
two real bugs, not just a format upgrade:

1. **CPU & Memory Timeline** OR'd two event types together: ``sensor_poll`` (170,242 docs,
   alive today) and ``system_metrics_snapshot`` (220 docs, dead since 2026-06-20). The
   dead event type turned out to be test-harness pollution -- every one of its docs has
   ``logger: test_elasticsearch_logging`` -- and it uses different field names entirely
   (``cpu_percent``/``memory_percent`` vs. this panel's ``cpu_load``/``memory_used``), so
   it silently contributed zero data to the aggregation even while alive. Rebuilt against
   ``sensor_poll`` alone.
2. **Error Events** hardcoded exactly 3 event types (``elasticsearch_index_failed``,
   ``model_call_error``, ``entity_extraction_failed``). Live verification found 2 of the 3
   had gone dead 24-55 days before this build (``model_call_error`` last emitted
   2026-06-07; ``entity_extraction_failed`` last emitted 2026-05-07) -- so the panel
   visually showed errors declining toward zero, which was actually just instrumentation
   churn, not improving system health (the exact FRE-593-v1 failure mode this whole audit
   exists to catch). Rebuilt as a self-adapting wildcard query
   (``event_type:*error* or event_type:*failed*``, excluding ``error_monitor_*`` --  the
   error-scanner's own housekeeping events, not actual failures) that will not go stale
   the same way as the taxonomy evolves.

**Consolidation Events** and **State Transitions** were already correct (both event
sources are alive today) -- these two panels are value-confirmed, not fixed, with their
live-verified numbers recorded in the description.

All four events used here (``sensor_poll``, ``state_transition``, ``consolidation_*``,
and the broadened error/failed set) are alive as of the FRE-703 build (2026-07-01) --
unlike most other dashboards in this wave, system_health's underlying telemetry is
current, not stale.

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
5. The CPU/memory panel no longer queries the dead, test-polluted
   ``system_metrics_snapshot`` event type.
6. The error panel no longer hardcodes the two now-dead event types, and excludes the
   error-scanner's own housekeeping events.
7. Dashboard title has no ticket ID; panel references resolve with no duplicates;
   registered in ``import_dashboards.sh``.

Source of truth for the field types and real counts: live ``agent-logs-*``
verification recorded in the FRE-703 build session (2026-07-01):
  sensor_poll: cpu_load=float, memory_used=float. 170,242 docs, alive today (avg CPU
  ~6.2%, avg memory ~55.8% over the last 24h).
  system_metrics_snapshot: cpu_percent/memory_percent (NOT cpu_load/memory_used), 220
  docs, all from logger=test_elasticsearch_logging, dead since 2026-06-20.
  state_transition: from_state=keyword (llm_call 5,818 / tool_execution 4,562 / init
  3,392). 13,772 docs, alive today.
  consolidation_triggered/consolidation_started/consolidation_completed: 960/960/1878
  docs respectively, alive today.
  Error/failure wildcard set over the last 7 days: 381 real events, dominated by
  elasticsearch_index_failed (170), generic error (107), slm_health_probe_http_error
  (90). model_call_error dead since 2026-06-07; entity_extraction_failed dead since
  2026-05-07.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "system_health.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the four panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "@timestamp",
        "cpu_load",
        "memory_used",
        "event_type",
        "from_state",
        "___records___",
    }
)

# The dead, test-polluted event type dropped from the CPU/memory panel.
DEAD_TEST_EVENT_TYPE = "system_metrics_snapshot"

# The two hardcoded event types that went dead 24-55 days before this build.
DEAD_HARDCODED_ERROR_TYPES = frozenset({"model_call_error", "entity_extraction_failed"})


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


def _lens_by_title(objs: list[dict], title: str) -> dict:
    lens = next(
        (o for o in _by_type(objs, "lens") if o["attributes"].get("title") == title),
        None,
    )
    assert lens is not None, f"expected a lens panel titled {title!r}"
    return lens


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_expected_counts() -> None:
    """File parses as NDJSON and contains exactly 1 dashboard + 4 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 4, "expected four lens panel objects"
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


def test_panel_references_resolve_with_no_duplicates() -> None:
    """Every dashboard panel reference resolves to a lens object, with no duplicates."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    lens_ids = {o["id"] for o in _by_type(objs, "lens")}

    refs = dashboard["references"]
    ref_keys = [(r["type"], r["id"], r["name"]) for r in refs]
    assert len(ref_keys) == len(set(ref_keys)), (
        f"dashboard references contain duplicate entries: {ref_keys}"
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
        "dashboard references must contain exactly one entry per panel, no duplicates"
    )


def test_dashboard_title_has_no_ticket_id() -> None:
    """The human-facing dashboard title carries no ticket ID."""
    dashboard = _by_type(_objects(), "dashboard")[0]
    title = dashboard["attributes"]["title"]
    assert "FRE-" not in title, f"dashboard title {title!r} must not contain a ticket ID"


# --------------------------------------------------------------------------- #
# The two real bugs found in step 0, and their fix.
# --------------------------------------------------------------------------- #


def test_cpu_memory_panel_drops_dead_test_polluted_event_type() -> None:
    """CPU & memory panel no longer OR's in the dead, test-polluted system_metrics_snapshot.

    That event type's 220 docs all came from logger=test_elasticsearch_logging (a test
    fixture, not real telemetry), used different field names (cpu_percent/
    memory_percent) than this panel's aggregations (cpu_load/memory_used) so it
    silently contributed zero data even while alive, and stopped emitting entirely on
    2026-06-20.
    """
    lens = _lens_by_title(_objects(), "CPU & memory over time")
    query = lens["attributes"]["state"]["query"]["query"]
    assert DEAD_TEST_EVENT_TYPE not in query, (
        f"CPU & memory panel query {query!r} must not reference the dead, "
        f"test-polluted {DEAD_TEST_EVENT_TYPE!r} event type"
    )
    fields = _lens_source_fields(lens)
    assert "cpu_percent" not in fields and "memory_percent" not in fields, (
        "CPU & memory panel must not use system_metrics_snapshot's field names"
    )


def test_error_panel_does_not_hardcode_dead_event_types() -> None:
    """Error events panel uses a self-adapting query, not the two now-dead hardcoded types.

    The prior committed panel hardcoded elasticsearch_index_failed, model_call_error,
    and entity_extraction_failed -- 2 of those 3 went dark 24-55 days before this
    build, making the panel look like errors were declining when it was actually just
    instrumentation churn. The rebuilt panel uses a wildcard query that self-adapts to
    future event-type churn instead.
    """
    lens = _lens_by_title(_objects(), "Error events by type")
    query = lens["attributes"]["state"]["query"]["query"]
    for dead_type in DEAD_HARDCODED_ERROR_TYPES:
        assert dead_type not in query, (
            f"Error events panel query {query!r} must not hardcode the dead event "
            f"type {dead_type!r} -- use a self-adapting wildcard instead"
        )
    assert "error_monitor" in query, (
        "Error events panel query must exclude error_monitor_* (the scanner's own "
        "housekeeping events, not actual failures)"
    )


def test_every_panel_documents_its_verified_finding() -> None:
    """Every panel description records the live-verified numbers behind its story."""
    for lens in _by_type(_objects(), "lens"):
        description = lens["attributes"].get("description", "")
        assert "verified" in description.lower(), (
            f"lens {lens.get('id')!r} description does not document a verified-live finding"
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
    assert "system_health.ndjson" in IMPORT_SCRIPT.read_text(), (
        "system_health.ndjson must be present in the FILES list in import_dashboards.sh"
    )
