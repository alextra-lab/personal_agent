"""Static validation of monitors_joinability_slm.ndjson saved-object format and redesign.

FRE-703 rebuilt this dashboard via the Kibana UI (Playwright-driven, never hand-authored),
consolidating 10 classic ``visState`` panels (none carrying ``visualizationType`` -- the
FRE-406/593/702 render trap) down to 6 Lens/Discover panels mapped onto the stated decision:
**is telemetry joinable / is the SLM healthy?**

Step 0 (raw-event inspection, 2026-07-01) found the worklist's "~59 docs" estimate was stale
by orders of magnitude -- the real volume is ~1,028 joinability runs, ~8,073 SLM health
probes, and ~2,030+ per-substrate checks over 90 days -- and surfaced a real accuracy bug:
the original ``agent-monitors-joinability-*`` Kibana data-view title glob-matched BOTH the
run-level ``agent-monitors-joinability-YYYY.MM.DD`` indices AND the
``agent-monitors-joinability-substrate-YYYY.MM.DD`` indices (a real, if unintentional,
prefix collision), silently blending per-substrate walk durations into the run-level
``duration_ms`` average (24.9ms contaminated vs. 71.9ms correct -- a ~64% understatement).
Fixed by narrowing the data-view title to ``agent-monitors-joinability-2*`` (matches only
indices starting with a digit, excluding the ``-substrate-`` indices).

Panels:

* **Joinability summary** (lens table) -- run outcome counts (green/yellow/red/skipped) +
  avg run duration, using the bug-fixed data view.
* **SLM health summary** (lens table) -- reachable/unreachable counts + avg probe latency.
  Split into its own panel from joinability summary because they are two different index
  patterns/data views and a single Lens table layer cannot mix data views.
* **Joinability outcome over time** (lens bar stacked) -- run-level, bug-fixed pattern.
* **SLM reachability over time** (lens bar stacked).
* **Per-substrate joinability detail** (lens table) -- consolidates the old Per-Substrate
  Check Status / Orphan Counts by Substrate / Per-Substrate Walk Duration histogram panels
  into one table (checks, green-status count, red/yellow orphan sums, avg walk duration per
  substrate).
* **Recent unreachable SLM probes** (search) -- direct carryover of the original panel.

Dropped: the markdown "Artifact-Envelope..." -- no, this dashboard never had one; dropped
was the 3-viz joinability-outcome pie/metric/duration trio which duplicated the trend panel,
and the SLM availability pie/latency-line pair which duplicated the trend panel + summary
scorecard.

These tests are *static* (no live cluster) and guard against:
1. No top-level ``migrationVersion`` (legacy Kibana export format).
2. No ``attributes.references`` nested inside a ``lens`` object.
3. Every ``lens`` object carries ``attributes.visualizationType``.
4. The three monitor index-patterns keep their stable ids + correct time fields, and the
   joinability pattern's title is narrowed to exclude the substrate-contamination bug.
5. Every lens/search panel references exactly one of the three monitor index-patterns.
6. Dashboard panel references resolve for BOTH lens and search panel types.
7. Data-backing -- Lens sourceFields are pinned to the set verified live on 2026-07-01.

The FRE-550 substrate ES-template tests (mapping/priority traps) are orthogonal to the
saved-object format and remain valid as written -- kept unchanged below.
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

# The three monitor index-patterns this dashboard self-includes, each with its own time
# field (these docs carry NO @timestamp). The joinability pattern's title is narrowed to
# exclude the substrate-contamination bug (FRE-703 finding).
EXPECTED_INDEX_PATTERNS = {
    "agent-monitors-joinability-pattern": ("started_at", "agent-monitors-joinability-2*"),
    "agent-monitors-slm-health-pattern": ("probed_at", "agent-monitors-slm-health-*"),
    "agent-monitors-joinability-substrate-pattern": (
        "started_at",
        "agent-monitors-joinability-substrate-*",
    ),
}

# Every Lens column sourceField verified live on 2026-07-01 (90d window): ~1,028
# joinability runs (green=537, skipped=309, red=140, yellow=42), ~8,073 SLM probes
# (reachable=5,962-5,970, unreachable=2,111), ~2,030+ per-substrate checks across 14
# substrates (all green except elasticsearch.agent_logs which shows persistent yellow
# orphans).
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "outcome",
        "duration_ms",
        "started_at",
        "reachable",
        "probe_latency_ms",
        "probed_at",
        "substrate",
        "status",
        "orphan_red_count",
        "orphan_yellow_count",
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
    """File parses as NDJSON: 1 dashboard + 5 lens + 1 search + 3 index-patterns."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 5, "expected five lens panel objects"
    assert len(_by_type(objs, "search")) == 1, "expected one saved-search panel object"
    assert len(_by_type(objs, "index-pattern")) == 3, "expected three index-pattern objects"


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


def test_expected_index_patterns_with_time_fields_and_titles() -> None:
    """The three monitor index-patterns keep their stable ids, time fields, and titles.

    The joinability pattern's title is narrowed to ``agent-monitors-joinability-2*`` so
    it no longer glob-matches the ``-substrate-`` indices (FRE-703 accuracy-bug fix).
    """
    index_patterns = _by_type(_objects(), "index-pattern")
    found = {
        ip["id"]: (ip["attributes"]["timeFieldName"], ip["attributes"]["title"])
        for ip in index_patterns
    }
    assert found == EXPECTED_INDEX_PATTERNS, (
        f"index-patterns must be exactly {EXPECTED_INDEX_PATTERNS}, got {found}"
    )


def test_joinability_title_excludes_substrate_indices() -> None:
    """The joinability pattern's title glob must not also match the substrate indices.

    A title like ``agent-monitors-joinability-*`` matches both
    ``agent-monitors-joinability-2026.06.09`` (run-level) and
    ``agent-monitors-joinability-substrate-2026.06.09`` (per-substrate), silently
    blending two different ``duration_ms`` semantics (FRE-703 finding: 24.9ms
    contaminated vs. 71.9ms correct). The fix must exclude the literal ``substrate``
    segment while still matching real run-level index names.
    """
    ip = next(
        o
        for o in _by_type(_objects(), "index-pattern")
        if o["id"] == "agent-monitors-joinability-pattern"
    )
    title = ip["attributes"]["title"]
    assert "substrate" not in title, (
        f"joinability pattern title {title!r} must not be able to match "
        f"agent-monitors-joinability-substrate-* indices"
    )


def test_every_lens_and_search_references_a_monitor_index_pattern() -> None:
    """Every lens/search references exactly one of the three monitor index-patterns."""
    objs = _objects()
    for obj in _by_type(objs, "lens") + _by_type(objs, "search"):
        ip_refs = [r["id"] for r in obj.get("references", []) if r.get("type") == "index-pattern"]
        assert len(ip_refs) == 1, (
            f"{obj.get('type')} {obj.get('id')!r} must reference exactly one "
            f"index-pattern, got {ip_refs}"
        )
        assert ip_refs[0] in EXPECTED_INDEX_PATTERNS, (
            f"{obj.get('type')} {obj.get('id')!r} references {ip_refs[0]!r}, "
            f"not a monitor index-pattern"
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
    assert "monitors_joinability_slm.ndjson" in IMPORT_SCRIPT.read_text(), (
        "monitors_joinability_slm.ndjson must be present in the FILES list in import_dashboards.sh"
    )


# --------------------------------------------------------------------------- #
# FRE-550 substrate ES template -- mapping + priority traps.
#
# Orthogonal to the dashboard saved-object format above: these validate the ES index
# templates directly and remain correct regardless of how the Kibana panels are built.
# --------------------------------------------------------------------------- #


def test_substrate_template_duration_ms_is_float() -> None:
    """``duration_ms`` is mapped ``float`` (not the long-trap default).

    A first sub-millisecond value written under dynamic mapping would freeze the
    field as ``long`` and silently truncate every later float; the explicit
    ``float`` mapping is the guard (the FRE-534/536 float->long trap).
    """
    tmpl = json.loads(SUBSTRATE_TEMPLATE.read_text())
    props = tmpl["template"]["mappings"]["properties"]
    assert props["duration_ms"]["type"] == "float"


def test_substrate_template_outranks_parent() -> None:
    """Substrate template priority strictly exceeds the parent's.

    ``agent-monitors-joinability-substrate-*`` is a strict subset of the parent
    ``agent-monitors-joinability-*`` index-name pattern (ES template matching, which
    is independent of the Kibana data-view title glob fixed above). The parent is
    ``dynamic:false`` with no substrate-field properties, so if it won the match
    every substrate field would be silently dropped. A strictly higher priority
    guarantees the substrate template wins for the ``-substrate-*`` indices.
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
    each field the substrate panel aggregates on.
    """
    tmpl = json.loads(SUBSTRATE_TEMPLATE.read_text())
    props = tmpl["template"]["mappings"]["properties"]
    substrate_agg_fields = {
        "substrate",
        "status",
        "duration_ms",
        "orphan_red_count",
        "orphan_yellow_count",
    }
    for field in substrate_agg_fields:
        assert field in props, (
            f"{field!r} aggregated by a panel but unmapped (dynamic:false drops it)"
        )
