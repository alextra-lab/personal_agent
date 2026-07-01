"""Static validation of prompt-cost-cache.ndjson saved-object format and value redesign.

FRE-546 fixed the saved-object envelope: top-level ``migrationVersion`` (object form) and
``attributes.references`` nested inside the two Lens objects are both rejected by the strict
``.kibana`` mapping, causing the import to return ``success:false`` even under HTTP 200 (the
hardened ``import_dashboards.sh`` catches this). FRE-406/FRE-703 then rebuilt both panels via
the Kibana UI (never hand-authored) to fix the still-missing ``visualizationType`` — a Lens
object persists fine without it but renders "Visualization type not found" — and to redesign
the value: per-callsite cost, and cache-hit-rate (not raw hash-count) over time.

These tests are *static* (no live cluster) and guard against:
1. FRE-546 trap A — top-level ``migrationVersion`` re-introduced.
2. FRE-546 trap B — ``attributes.references`` nested inside a ``lens`` object.
3. FRE-535 dedupe lesson — dashboard must use the canonical shared ``agent-logs-pattern``
   index-pattern id, and its self-included data-view object must be byte-identical to the
   canonical copy in ``data_views.ndjson`` (prevents a sparse copy clobbering the rich
   canonical on ``overwrite=true``).
4. FRE-406/FRE-703 trap — every ``lens`` object must carry ``attributes.visualizationType``
   (the render-time-only requirement a hand-authored object silently omits).
5. Data-backing (owner verification ask) — the Lens ``sourceField`` values used by the two
   panels are pinned to the set verified live in ``agent-logs-*`` (8 162+ model_call_completed
   docs, correct ES mapping types).

Source of truth for the field types: live ``agent-logs-*`` verification recorded in the
FRE-406/FRE-703 build session (2026-07-01):
  input_tokens=long, cache_read_tokens=long, cost_usd=double, prompt_callsite=keyword.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "prompt-cost-cache.ndjson"
DATA_VIEWS_FILE = REPO_ROOT / "config" / "kibana" / "dashboards" / "data_views.ndjson"
IMPORT_SCRIPT = REPO_ROOT / "config" / "kibana" / "import_dashboards.sh"

CANONICAL_INDEX_PATTERN_ID = "agent-logs-pattern"

# All Lens sourceField values used by the two panels, verified against live mapping.
VERIFIED_SOURCE_FIELDS = frozenset(
    {
        "prompt_callsite",
        "input_tokens",
        "cache_read_tokens",
        "cost_usd",
        "@timestamp",
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
    """File parses as NDJSON and contains exactly 1 dashboard + 2 lens + 1 index-pattern."""
    objs = _objects()
    assert len(_by_type(objs, "dashboard")) == 1, "exactly one dashboard object expected"
    assert len(_by_type(objs, "lens")) == 2, "expected two lens panel objects"
    assert len(_by_type(objs, "index-pattern")) == 1, "expected exactly one index-pattern object"


# --------------------------------------------------------------------------- #
# FRE-546 trap A — migrationVersion (stale format).
# --------------------------------------------------------------------------- #


def test_no_top_level_migration_version() -> None:
    """No object carries the legacy top-level ``migrationVersion`` dict.

    This is the field that causes ``strict_dynamic_mapping_exception`` for both
    data-view and dashboard objects in the strict ``.kibana`` mapping (FRE-546 trap A).
    """
    for obj in _objects():
        assert "migrationVersion" not in obj, (
            f"object {obj.get('id')!r} (type={obj.get('type')!r}) still carries "
            f"top-level ``migrationVersion`` — replace with ``typeMigrationVersion`` (string)"
        )


# --------------------------------------------------------------------------- #
# FRE-546 trap B — attributes.references nested in lens.
# --------------------------------------------------------------------------- #


def test_no_lens_attributes_references() -> None:
    """No ``lens`` object has ``attributes.references``.

    The strict Lens mapping only allows ``references`` at the top-level envelope;
    having it also inside ``attributes`` causes
    ``strict_dynamic_mapping_exception: dynamic introduction of [references] within [lens]``
    (FRE-546 trap B).
    """
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

    A Lens saved object persists and imports fine without this attribute, but is
    *optional at import, required at render* — omitting it draws "Visualization
    type not found" (FRE-406/FRE-593/FRE-702). Hand-authoring an object (rather
    than exporting one built via the Kibana UI) is exactly how this gets dropped.
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
    """The sole index-pattern object has the canonical shared id.

    Prevents re-introducing an isolated ``prompt-cost-cache-data-view`` id that
    would be absent from ``data_views.ndjson`` (FRE-535 dedupe lesson).
    """
    for ip in _by_type(_objects(), "index-pattern"):
        assert ip["id"] == CANONICAL_INDEX_PATTERN_ID, (
            f"index-pattern id is {ip['id']!r}; must be {CANONICAL_INDEX_PATTERN_ID!r}"
        )


def test_index_pattern_object_matches_canonical() -> None:
    """The self-included data-view is byte-identical to the canonical copy in data_views.ndjson.

    Guards the Codex-flagged overwrite risk: ``import_dashboards.sh`` uses
    ``overwrite=true``; a sparse data-view under the canonical id clobbers rich field metadata
    (fieldAttrs / formats) loaded earlier from ``data_views.ndjson``.  Byte identity ensures
    the overwrite is canonical→canonical (a no-op).
    """
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


# --------------------------------------------------------------------------- #
# Data-backing guard — sourceField pins.
# --------------------------------------------------------------------------- #


def test_lens_source_fields_are_verified_live() -> None:
    """Every Lens column sourceField is in the set verified live in agent-logs-*.

    Prevents a future edit from introducing a field that exists in code but is
    absent from the index mapping (silent empty panel).

    Verified 2026-07-01 against live agent-logs-* mapping:
      input_tokens=long, cache_read_tokens=long, cost_usd=double,
      prompt_callsite=keyword. 8 162+ model_call_completed docs.
    """
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
    assert "prompt-cost-cache.ndjson" in IMPORT_SCRIPT.read_text(), (
        "prompt-cost-cache.ndjson must be present in the FILES list in import_dashboards.sh"
    )
