"""Static validation of the FRE-538 (C3) Monitors dashboard.

Surfaces the two monitor index families that previously had **zero**
visualizations:

* ``agent-monitors-joinability-*`` (ADR-0074 joinability probe)
* ``agent-monitors-slm-health-*`` (ADR-0083 / FRE-399 SLM health probe)

These tests are *static* — they parse the repo Kibana NDJSON under
``config/kibana/dashboards/`` without touching a live cluster. They encode two
project-specific traps so the "first-pass-wrong dashboard" failure mode is caught
in CI rather than discovered live:

1. The FRE-533 ``.keyword``-on-a-bare-keyword terms-agg trap (silent empty panel).
2. The **SLM-health mapping straddle** (FRE-538 finding): ``status``/``error`` are
   ``text``+``.keyword`` in the historical dynamic-mapped indices but bare
   ``keyword``/``text`` in the post-FRE-534 template index, so **no single field
   name** aggregates across all data. The only join-safe SLM fields are
   ``reachable`` (boolean) and ``probe_latency_ms`` (float). The guard below pins
   every aggregation to the verified-safe field set.

Source of truth for the field types: the live ``_field_caps`` + per-index
``_mapping`` verification recorded in
``docs/research/2026-06-09-fre-538-monitors-dashboard.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "monitors_joinability_slm.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"
JOINABILITY_TEMPLATE = (
    REPO_ROOT / "docker" / "elasticsearch" / "monitors-joinability-index-template.json"
)
SUBSTRATE_TEMPLATE = (
    REPO_ROOT / "docker" / "elasticsearch" / "monitors-joinability-substrate-index-template.json"
)

# The monitor index-patterns this dashboard defines inline, each with its own
# time field (these docs carry NO @timestamp).
EXPECTED_INDEX_PATTERNS = {
    "agent-monitors-joinability-pattern": "started_at",
    "agent-monitors-slm-health-pattern": "probed_at",
    # FRE-550 flat per-substrate projection.
    "agent-monitors-joinability-substrate-pattern": "started_at",
}

# Every aggregation must reference a verified-safe field **for its own index
# pattern**. The set is per-pattern, not global: ``status`` is straddle-safe on
# the FRE-550 substrate index (single bare-keyword mapping from index creation)
# but is the straddle trap on the SLM-health index, so it must never leak into
# the SLM safe set. Source of truth: live _field_caps + per-index _mapping in
# docs/research/2026-06-09-fre-538-monitors-dashboard.md (+ FRE-550 template).
SAFE_AGG_FIELDS_BY_PATTERN: dict[str, frozenset[str]] = {
    # joinability run doc (bare keyword / float / date — consistent everywhere)
    "agent-monitors-joinability-pattern": frozenset(
        {"outcome", "source", "duration_ms", "started_at"}
    ),
    # slm health (boolean / float / date — consistent across the straddle)
    "agent-monitors-slm-health-pattern": frozenset({"reachable", "probe_latency_ms", "probed_at"}),
    # FRE-550 flat substrate projection — explicit-mapped keyword/long/float
    # from index creation (priority-200 template, dynamic:false). ``status`` is
    # straddle-safe HERE because this index has a single bare-keyword mapping.
    "agent-monitors-joinability-substrate-pattern": frozenset(
        {
            "substrate",
            "status",
            "expected",
            "duration_ms",
            "started_at",
            "observed_count",
            "orphan_count",
            "orphan_red_count",
            "orphan_yellow_count",
        }
    ),
}


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


def _agg_fields(viz: dict) -> list[str]:
    """Every ``params.field`` referenced by a visualization's aggs."""
    vis_state = json.loads(viz["attributes"]["visState"])
    return [
        agg["params"]["field"]
        for agg in vis_state.get("aggs", [])
        if isinstance(agg.get("params"), dict) and agg["params"].get("field")
    ]


# --------------------------------------------------------------------------- #
# Structural validity.
# --------------------------------------------------------------------------- #


def test_ndjson_is_valid_and_has_one_dashboard() -> None:
    """The file parses as NDJSON and contains one dashboard + 10 viz + 1 search."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "visualization")) == 10, (
        "expected ten visualization panels (7 original + 3 FRE-550 substrate)"
    )
    assert len(_by_type(objs, "search")) == 1, "expected one saved search panel"


def test_expected_index_patterns_with_time_fields() -> None:
    """Exactly the two monitor index-patterns, each with the correct time field."""
    index_patterns = _by_type(_objects(), "index-pattern")
    found = {ip["id"]: ip["attributes"]["timeFieldName"] for ip in index_patterns}
    assert found == EXPECTED_INDEX_PATTERNS, (
        f"index-patterns must be exactly {EXPECTED_INDEX_PATTERNS}, got {found}"
    )


def test_every_panel_references_a_monitor_index_pattern() -> None:
    """Every viz/search points at one of the two monitor patterns (never agent-logs)."""
    objs = _objects()
    for obj in _by_type(objs, "visualization") + _by_type(objs, "search"):
        ip_refs = [r["id"] for r in obj["references"] if r["type"] == "index-pattern"]
        assert len(ip_refs) == 1, (
            f"{obj['id']} must reference exactly one index-pattern, got {ip_refs}"
        )
        assert ip_refs[0] in EXPECTED_INDEX_PATTERNS, (
            f"{obj['id']} references {ip_refs[0]}, not a monitor index-pattern"
        )


def test_panel_references_resolve() -> None:
    """Every dashboard panel reference resolves to a viz/search in the file."""
    objs = _objects()
    dashboard = _by_type(objs, "dashboard")[0]
    panel_ids = {o["id"] for o in _by_type(objs, "visualization") + _by_type(objs, "search")}

    panel_refs = {
        r["name"]: r["id"]
        for r in dashboard["references"]
        if r["type"] in {"visualization", "search"}
    }
    for name, ref_id in panel_refs.items():
        assert ref_id in panel_ids, f"dashboard panel ref {name} -> {ref_id} has no object"

    panels = json.loads(dashboard["attributes"]["panelsJSON"])
    panel_names = {p["panelRefName"] for p in panels}
    assert panel_names == set(panel_refs), "panelsJSON refs must match dashboard references"


# --------------------------------------------------------------------------- #
# The straddle / A1 trap guard.
# --------------------------------------------------------------------------- #


def _pattern_of(viz: dict) -> str:
    """The single index-pattern id a visualization references."""
    refs = [r["id"] for r in viz["references"] if r["type"] == "index-pattern"]
    assert len(refs) == 1, f"{viz['id']} must reference exactly one index-pattern, got {refs}"
    return refs[0]


def test_aggregations_only_use_straddle_safe_fields() -> None:
    """No panel aggregates on a straddled or ``.keyword`` field — per pattern.

    The safe set is resolved from each viz's **own** index-pattern, so the
    SLM-health ``status``/``error`` straddle stays blocked even though
    ``status`` is legitimately aggregatable on the FRE-550 substrate index.
    """
    for viz in _by_type(_objects(), "visualization"):
        pattern = _pattern_of(viz)
        safe = SAFE_AGG_FIELDS_BY_PATTERN[pattern]
        for field in _agg_fields(viz):
            assert not field.endswith(".keyword"), (
                f"{viz['id']} aggregates on {field!r}; a ``.keyword`` agg is the "
                f"straddle/A1 trap — use the bare field or a saved-search _source column"
            )
            assert field in safe, (
                f"{viz['id']} (pattern {pattern}) aggregates on {field!r}, which is not "
                f"in its straddle-safe set {sorted(safe)}"
            )


# --------------------------------------------------------------------------- #
# Registration parity.
# --------------------------------------------------------------------------- #


def test_registered_in_import_script() -> None:
    """The dashboard is registered in import_dashboards.sh so it actually loads."""
    assert "monitors_joinability_slm.ndjson" in IMPORT_SCRIPT.read_text(), (
        "monitors_joinability_slm.ndjson must be appended to FILES in import_dashboards.sh"
    )


# --------------------------------------------------------------------------- #
# FRE-550 substrate ES template — mapping + priority traps.
# --------------------------------------------------------------------------- #


def test_substrate_template_duration_ms_is_float() -> None:
    """``duration_ms`` is mapped ``float`` (not the long-trap default).

    A first sub-millisecond value written under dynamic mapping would freeze the
    field as ``long`` and silently truncate every later float; the explicit
    ``float`` mapping is the guard (the FRE-534/536 float→long trap).
    """
    tmpl = json.loads(SUBSTRATE_TEMPLATE.read_text())
    props = tmpl["template"]["mappings"]["properties"]
    assert props["duration_ms"]["type"] == "float"


def test_substrate_template_outranks_parent() -> None:
    """Substrate template priority strictly exceeds the parent's.

    ``agent-monitors-joinability-substrate-*`` is a strict subset of the parent
    ``agent-monitors-joinability-*`` pattern. The parent is ``dynamic:false``
    with no substrate-field properties, so if it won the match every substrate
    field would be silently dropped. A strictly higher priority guarantees the
    substrate template wins for the ``-substrate-*`` indices.
    """
    parent = json.loads(JOINABILITY_TEMPLATE.read_text())
    substrate = json.loads(SUBSTRATE_TEMPLATE.read_text())
    assert substrate["priority"] > parent["priority"], (
        f"substrate priority {substrate['priority']} must exceed parent "
        f"{parent['priority']} or the dynamic:false parent shadows the fields"
    )


def test_substrate_template_keyword_agg_fields_explicit() -> None:
    """Every field the dashboard aggregates on is explicitly mapped (not dropped).

    ``dynamic:false`` means an unmapped field is silently not indexed, so a
    terms/avg agg on it returns nothing. Pin that the substrate template maps
    each field the substrate panels aggregate on.
    """
    tmpl = json.loads(SUBSTRATE_TEMPLATE.read_text())
    props = tmpl["template"]["mappings"]["properties"]
    substrate_agg_fields = SAFE_AGG_FIELDS_BY_PATTERN[
        "agent-monitors-joinability-substrate-pattern"
    ]
    for field in substrate_agg_fields:
        assert field in props, (
            f"{field!r} aggregated by a panel but unmapped (dynamic:false drops it)"
        )
