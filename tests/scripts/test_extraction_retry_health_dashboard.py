"""Static validation of extraction_retry_health.ndjson saved-object format and value redesign.

FRE-703 rebuilt this dashboard from scratch via the Kibana UI (never hand-authored). The
prior committed ndjson had a real mislabeling bug independent of the classic-visState vs
Lens format question: the "Dead-letter rate (per role)" panel's *description* said
``outcome=dead_letter``, but its *query* filtered ``outcome:extraction_returned_fallback``
-- and ``dead_letter`` is not a value ``consolidation_attempt_recorded`` has ever emitted
(verified live against the full outcome value set: ``success``, ``extraction_returned_fallback``,
``budget_denied``). The panel also had no time axis, so it could only ever show an
all-time aggregate.

That aggregate is dangerously misleading on its own: 702 of 1517 attempts (46.3%) are
``extraction_returned_fallback``, which reads as an active, ongoing 46% failure rate. A
live ES weekly-bucket check tells a completely different story:
  - week of 2026-04-27: 702/704 fallback (99.7%) -- an acute, since-resolved incident.
  - week of 2026-05-04: 0 fallback, split between success and budget_denied.
  - 2026-05-11 to 2026-05-18: zero attempts recorded at all (a two-week gap).
  - every week since 2026-05-25 (5+ consecutive weeks): 100% success.
Reading the 46.3% number without a time axis would misdiagnose a resolved historical
incident as a live crisis -- exactly the FRE-593-class failure mode ("renders with data"
is not "useful", and can actively mislead) this skill exists to catch.

Value-pass changes:
1. Replaced "Dead-letter rate (per role)" with "Consolidation outcome over time" (a
   genuine outcome/@timestamp breakdown) plus "Consolidation outcome totals" (the
   all-time numbers, but captioned to point back at the time-axis panel first).
2. Kept "Top denial reason" (real, actionable finding retained: cap_exceeded is 100% of
   budget_denied attempts).
3. Kept "Median attempts to success", but dropped the original per-role breakdown --
   ``role`` is a single value (``entity_extraction``) across all 1517 attempts ever
   recorded, so the split added no information.

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
5. No panel description or query references the fictitious ``dead_letter`` outcome
   value, and at least one panel breaks the outcome/consolidation-health signal down
   by time (the fix for the misleading-aggregate bug).

Source of truth for the field types and real counts: live ``agent-logs-*``
verification recorded in the FRE-703 build session (2026-07-01):
  consolidation_attempt_recorded: outcome=keyword (success=556, extraction_returned_fallback=702,
  budget_denied=259, total=1517, 90d=all-time, earliest doc 2026-05-01 -- actually verified
  earliest 2026-05-01, all data within a 90d window). role=keyword, single value ever:
  "entity_extraction". denial_reason=keyword, single value for budget_denied: "cap_exceeded"
  (259/259). attempt_number=long (median=1, p90=2, p99=12, max=13 for success outcomes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "extraction_retry_health.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the four panels, verified against live mapping.
# "___records___" is Lens's internal sentinel for a Count-of-records metric, not a
# real ES field.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "outcome",
        "@timestamp",
        "denial_reason",
        "attempt_number",
        "___records___",
    }
)

# The fictitious outcome value from the prior committed file's mislabeled panel.
FICTITIOUS_OUTCOME_VALUE = "dead_letter"


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


def _has_time_axis(lens: dict) -> bool:
    """Whether any column in this lens uses date_histogram on @timestamp."""
    try:
        state = lens["attributes"]["state"]
        layers = state["datasourceStates"]["formBased"]["layers"]
    except (KeyError, TypeError):
        return False
    for layer in layers.values():
        for col in layer.get("columns", {}).values():
            if (
                col.get("operationType") == "date_histogram"
                and col.get("sourceField") == "@timestamp"
            ):
                return True
    return False


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
# The mislabeled dead_letter bug and its fix.
# --------------------------------------------------------------------------- #


def test_no_panel_references_fictitious_dead_letter_outcome() -> None:
    """No panel query or description references the fictitious dead_letter outcome value.

    consolidation_attempt_recorded has never emitted outcome:dead_letter (verified live:
    the only values are success, extraction_returned_fallback, budget_denied). The prior
    committed file's "Dead-letter rate" panel description claimed this value while its
    actual query filtered a different, real value -- a description/query mismatch that
    misnamed the panel.
    """
    for lens in _by_type(_objects(), "lens"):
        query = lens["attributes"]["state"]["query"]["query"]
        description = lens["attributes"].get("description", "")
        assert FICTITIOUS_OUTCOME_VALUE not in query, (
            f"lens {lens.get('id')!r} query {query!r} references the fictitious "
            f"{FICTITIOUS_OUTCOME_VALUE!r} outcome value"
        )
        if FICTITIOUS_OUTCOME_VALUE in description:
            assert "not" in description.lower() or "mislabeled" in description.lower(), (
                f"lens {lens.get('id')!r} description mentions "
                f"{FICTITIOUS_OUTCOME_VALUE!r} without clarifying it is fictitious"
            )


def test_at_least_one_panel_has_a_time_axis_for_outcome() -> None:
    """At least one panel breaks the outcome/consolidation-health signal down by time.

    The prior committed dashboard's failure-rate panel had no time axis, so its all-time
    aggregate (46.3% fallback) could not be distinguished from a resolved historical
    incident. This guards against reintroducing an aggregate-only failure-rate panel.
    """
    lenses = _by_type(_objects(), "lens")
    assert any(_has_time_axis(lens) for lens in lenses), (
        "at least one panel must break down consolidation attempts by @timestamp -- "
        "an aggregate-only outcome panel can misrepresent a resolved historical "
        "incident as an ongoing failure rate"
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
    assert "extraction_retry_health.ndjson" in IMPORT_SCRIPT.read_text(), (
        "extraction_retry_health.ndjson must be present in the FILES list in import_dashboards.sh"
    )
