"""Static validation of intent_classification.ndjson saved-object format and value redesign.

FRE-703 rebuilt this dashboard from scratch via the Kibana UI (never hand-authored). The
prior version was four hand-authored classic ``visState`` objects. The stated decision
("intent distribution + confidence; are misclassifications visible?") was only partially
answered by the old panels -- none of them surfaced any misclassification-correction
signal.

Value-pass changes:
1. Kept and rebuilt the four original panels (task type distribution, intent-over-time,
   confidence distribution, signal frequency) as real Lens objects.
2. Discovered and documented that ``confidence`` is categorical, not continuous -- the
   classifier emits only 4 discrete values ever (0.7, 0.8, 0.85, 0.9) and never dips
   below 0.7 across the full emission history, so it cannot itself signal a likely
   misclassification. The confidence panel's description documents this finding.
3. Discovered and documented a real instrumentation bug: the recall-cue detector
   sometimes writes the raw matched user-text verbatim into the ``signals`` array
   instead of a canonical label (e.g. "Refresh my memory"), polluting the signal
   frequency breakdown with one-off strings. Documented as a caveat, not fixed here
   (out of scope for a dashboard redesign).
4. Added a new "Reclassification events" panel -- the actual misclassification-
   correction signal found in the telemetry: ``signals`` contains
   ``recall_cue_reclassified`` when a memory-recall cue overrides the initial
   classification. Directly answers "are misclassifications visible?": yes, but rare
   (5 of 1696 turns, 0.3%, all on a single day).
5. Widened the time window to 90 days (``timeRestore: true``) to cover the full
   77-day gateway_output emission history (earliest doc 2026-04-15), since the
   reclassification events are concentrated in a narrow historical window that a
   shorter default would miss entirely.

These tests are *static* (no live cluster) and guard against:
1. Every ``lens`` object carries both ``attributes.title`` and
   ``attributes.visualizationType`` (a hand-copied lens object missing ``title``
   imports fine but renders a blank panel header -- caught during a prior FRE-703
   iteration and guarded here too).
2. No top-level ``migrationVersion``, no ``attributes.references`` nested inside a
   ``lens`` object.
3. FRE-535 dedupe lesson -- dashboard must use the canonical shared
   ``agent-logs-pattern`` index-pattern id, byte-identical to the canonical copy in
   ``data_views.ndjson``.
4. Data-backing (owner verification ask) -- every Lens ``sourceField`` is pinned to
   the set verified live against ``agent-logs-*`` during the FRE-703 build session.
5. The new reclassification panel's query is present and uses the real signal name.

Source of truth for the field types and real counts: live ``agent-logs-*``
verification recorded in the FRE-703 build session (2026-07-01):
  gateway_output: task_type=keyword (7 distinct values, conversational=1188/1696),
  confidence=double (4 distinct values: 0.7/0.8/0.85/0.9),
  signals=keyword array (14 distinct values incl. 5 raw-text pollutants),
  recall_cue_reclassified signal count = 5 (all 2026-05-10, all task_type=memory_recall).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "intent_classification.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the five panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "task_type",
        "@timestamp",
        "confidence",
        "signals",
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


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_expected_counts() -> None:
    """File parses as NDJSON and contains exactly 1 dashboard + 5 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 5, "expected five lens panel objects"
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


# --------------------------------------------------------------------------- #
# The new "are misclassifications visible?" panel.
# --------------------------------------------------------------------------- #


def test_reclassification_panel_uses_real_signal_name() -> None:
    """The new reclassification-events panel queries the real recall_cue_reclassified signal."""
    lens = next(
        o for o in _by_type(_objects(), "lens") if o["id"] == "intent-reclassification-events"
    )
    query = lens["attributes"]["state"]["query"]["query"]
    assert "recall_cue_reclassified" in query, (
        f"reclassification-events panel query {query!r} must filter on the real "
        f"recall_cue_reclassified signal"
    )


def test_confidence_panel_documents_categorical_caveat() -> None:
    """The confidence panel documents that confidence is categorical, not continuous."""
    lens = next(
        o for o in _by_type(_objects(), "lens") if o["id"] == "intent-confidence-distribution"
    )
    description = lens["attributes"].get("description", "")
    assert "categorical" in description, (
        "confidence-distribution panel must document that confidence is categorical "
        "(4 discrete values) and cannot itself signal misclassification"
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
    assert "intent_classification.ndjson" in IMPORT_SCRIPT.read_text(), (
        "intent_classification.ndjson must be present in the FILES list in import_dashboards.sh"
    )
