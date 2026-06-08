#!/usr/bin/env python3
"""FRE-533 — three-way telemetry reconciliation extractor.

Read-only audit tool. For every ``agent-*`` Elasticsearch index family it walks all
three corners of the reconciliation triangle and dumps deterministic JSON
intermediates plus a flat reconciliation CSV:

* **mapping corner** — unions the live ``_mapping`` across *every* concrete index in
  the family (not just the newest), flattens to leaf fields, and resolves each field
  against the repo template's explicit ``properties`` *and* its ``dynamic_templates``
  block (the rule a naive ``properties``-only read misses).
* **code corner** — greps the source tree for each field name to surface candidate
  emit sites (``file:line``). Spread/``model_dump``/``asdict`` emit paths still need a
  human trace; this only finds literal occurrences.
* **dashboard corner** — parses the repo Kibana NDJSON saved objects for the fields
  each visualisation / lens references, and lists the live Kibana saved objects so
  repo-vs-live drift (the provenance question) can be answered.

Usage::

    python scripts/audit/fre533_reconcile.py --out /tmp/fre533

Nothing is written to Elasticsearch or Kibana. Outputs land under ``--out``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Field-name hints for trap detection. Word-anchored so "iteration" does NOT match
# "ratio" (a real false positive seen in the first pass).
FLOAT_HINT = re.compile(
    r"(^|_)(cost|usd|ratio|rate|pct|percent|score|confidence|temperature|"
    r"avg|mean|fraction|probability|threshold)(_|$)",
    re.I,
)
MS_HINT = re.compile(r"(_ms|_latency|_duration|_seconds|_offset)$", re.I)
JOIN_KEY = {"trace_id", "session_id", "span_id", "run_id", "entry_id"}
TEXT_TRAP_HINT = re.compile(
    r"(error|message|content|digest|output|response|prompt|stderr|stdout|"
    r"traceback|reason|preview|excerpt|snippet|rationale|summary|body)",
    re.I,
)

ES_URL = "http://localhost:9200"
KIBANA_URL = "http://localhost:5601"
REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Family registry: the six in-scope agent-* families + their repo template.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    """One agent-* index family and its repo source-of-truth template.

    Args:
        key: Short identifier used for output filenames.
        pattern: ES index wildcard matching the family's concrete indices.
        template: Repo template path (relative to repo root) or ``None`` if the
            family is dynamic-mapped with no template.
        note: Human note recorded with the family in the dump.
    """

    key: str
    pattern: str
    template: str | None
    note: str


FAMILIES: tuple[Family, ...] = (
    Family(
        "logs",
        "agent-logs-*",
        "docker/elasticsearch/index-template.json",
        "core event stream; NO ms_fields_as_float dynamic rule (float->long trap on unlisted *_ms)",
    ),
    Family(
        "captains-captures",
        "agent-captains-captures-*,-agent-captains-captures-subagents-*",
        "docker/elasticsearch/captains-index-template.json",
        "captures doc shape; one template shared with reflections (two shapes)",
    ),
    Family(
        "captains-reflections",
        "agent-captains-reflections-*",
        "docker/elasticsearch/captains-index-template.json",
        "reflections doc shape; same template as captures",
    ),
    Family(
        "captains-subagents",
        "agent-captains-captures-subagents-*",
        "docker/elasticsearch/captains-index-template.json",
        "INHERITS captains template via captures-* glob (ticket assumed 'none')",
    ),
    Family(
        "insights",
        "agent-insights-*",
        None,
        "pure dynamic-mapped; no template",
    ),
    Family(
        "monitors-joinability",
        "agent-monitors-joinability-*",
        "docker/elasticsearch/monitors-joinability-index-template.json",
        "dynamic:false; ADR-0074; orphans.detail is object/enabled:false",
    ),
    Family(
        "monitors-slm-health",
        "agent-monitors-slm-health-*",
        None,
        "NO matching template (slm-requests-index-template targets slm-requests-*); dynamic-mapped (ADR-0083)",
    ),
)

# Source dirs grepped for emit sites.
EMIT_DIRS = ["src/personal_agent"]


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only; read-only GETs)
# ---------------------------------------------------------------------------


def _get_json(url: str) -> Any:
    """GET a URL and parse JSON. Raises on transport/HTTP error."""
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Mapping corner
# ---------------------------------------------------------------------------


def flatten_properties(props: Mapping[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Flatten an ES ``properties`` tree to ``leaf_path -> attrs``.

    Object / nested roots are recorded *and* recursed into. Multi-field ``.keyword``
    subfields are noted on the parent as ``has_keyword_subfield``.

    Args:
        props: The ``properties`` mapping from an ES mapping body.
        prefix: Dotted path prefix for recursion.

    Returns:
        Mapping of dotted field path to attribute dict (``type``, ``ignore_above``,
        ``index``, ``enabled``, ``dynamic``, ``has_keyword_subfield``, ``container``).
    """
    out: dict[str, dict[str, Any]] = {}
    for name, body in props.items():
        path = f"{prefix}{name}"
        body = body or {}
        attrs: dict[str, Any] = {
            "type": body.get("type"),
            "ignore_above": body.get("ignore_above"),
            "index": body.get("index", True),
            "enabled": body.get("enabled", True),
            "dynamic": body.get("dynamic"),
            "has_keyword_subfield": "keyword" in (body.get("fields") or {}),
        }
        is_container = "properties" in body or body.get("type") in {"object", "nested"}
        if is_container:
            attrs["container"] = body.get("type") or "object"
        out[path] = attrs
        if "properties" in body and body.get("enabled", True) is not False:
            out.update(flatten_properties(body["properties"], prefix=f"{path}."))
    return out


def deep_merge_props(target: dict[str, Any], src: Mapping[str, Any]) -> None:
    """Recursively merge ES ``properties`` ``src`` into ``target`` (union of fields)."""
    for name, body in src.items():
        if name in target and "properties" in (target[name] or {}) and "properties" in (body or {}):
            deep_merge_props(target[name]["properties"], body["properties"])
        else:
            target.setdefault(name, body)


def union_live_mapping(pattern: str) -> dict[str, dict[str, Any]]:
    """Union the live ``properties`` across all concrete indices matching ``pattern``."""
    url = f"{ES_URL}/{pattern}/_mapping"
    raw = _get_json(url)
    merged: dict[str, Any] = {}
    index_count = 0
    for _index, body in raw.items():
        props = body.get("mappings", {}).get("properties")
        if props:
            index_count += 1
            deep_merge_props(merged, props)
    flat = flatten_properties(merged)
    for attrs in flat.values():
        attrs["_indices_unioned"] = index_count
    return flat


# ---------------------------------------------------------------------------
# Template resolver: explicit properties + dynamic_templates
# ---------------------------------------------------------------------------


@dataclass
class DynamicRule:
    """One ES ``dynamic_templates`` rule, compiled for matching."""

    name: str
    match_glob: str | None
    match_regex: re.Pattern[str] | None
    match_mapping_type: str
    mapped_type: str

    def matches(self, field_name: str) -> bool:
        """Whether ``field_name`` (leaf, last path segment) triggers this rule."""
        leaf = field_name.rsplit(".", 1)[-1]
        if self.match_regex is not None:
            return self.match_regex.match(leaf) is not None
        if self.match_glob is not None:
            return _glob_match(self.match_glob, leaf)
        return False


def _glob_match(glob: str, value: str) -> bool:
    """ES simple glob (``*`` only) match."""
    return re.fullmatch(re.escape(glob).replace(r"\*", ".*"), value) is not None


@dataclass
class Template:
    """Parsed repo template: explicit props + ordered dynamic rules."""

    path: str
    dynamic: bool | str
    explicit: dict[str, dict[str, Any]]
    rules: list[DynamicRule] = field(default_factory=list)

    def expected_type(self, field_name: str, json_kind: str = "string") -> dict[str, Any]:
        """Resolve the type the template assigns ``field_name``.

        Order: explicit property → first matching dynamic rule → ES default for the
        JSON kind (when ``dynamic`` is truthy) → unindexed (``dynamic: false``).

        Args:
            field_name: Dotted leaf field path.
            json_kind: The JSON value kind seen in source: ``string``, ``long``,
                ``double``, ``boolean``.

        Returns:
            Dict with ``via`` (explicit/dynamic-rule/es-default/unindexed) and ``type``.
        """
        if field_name in self.explicit:
            return {"via": "explicit", "type": self.explicit[field_name].get("type")}
        for rule in self.rules:
            if rule.match_mapping_type in {"*", json_kind} and rule.matches(field_name):
                return {"via": f"dynamic:{rule.name}", "type": rule.mapped_type}
        if self.dynamic is False:
            return {"via": "unindexed(dynamic:false)", "type": None}
        es_default = {
            "string": "text+keyword(default)",
            "long": "long",
            "double": "float",
            "boolean": "boolean",
        }.get(json_kind, "text+keyword(default)")
        return {"via": "es-default", "type": es_default}


def load_template(rel_path: str) -> Template:
    """Load and parse a repo ES template file into a :class:`Template`."""
    data = json.loads((REPO / rel_path).read_text())
    mappings = data["template"]["mappings"]
    explicit = flatten_properties(mappings.get("properties", {}))
    rules: list[DynamicRule] = []
    for entry in mappings.get("dynamic_templates", []):
        for name, spec in entry.items():
            is_regex = spec.get("match_pattern") == "regex"
            rules.append(
                DynamicRule(
                    name=name,
                    match_glob=None if is_regex else spec.get("match"),
                    match_regex=re.compile(spec["match"]) if is_regex and "match" in spec else None,
                    match_mapping_type=spec.get("match_mapping_type", "*"),
                    mapped_type=spec.get("mapping", {}).get("type", "?"),
                )
            )
    return Template(rel_path, mappings.get("dynamic", True), explicit, rules)


# ---------------------------------------------------------------------------
# Code corner: candidate emit sites via ripgrep
# ---------------------------------------------------------------------------


def grep_field(field_name: str) -> list[str]:
    """Return up to 6 ``file:line`` candidate emit sites for a leaf field name."""
    leaf = field_name.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", leaf):
        return []
    patterns = [f'"{leaf}"', f"'{leaf}'", f"{leaf}="]
    hits: list[str] = []
    for pat in patterns:
        try:
            res = subprocess.run(
                ["rg", "-n", "--no-heading", "-F", pat, *EMIT_DIRS],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        for line in res.stdout.splitlines():
            loc = ":".join(line.split(":", 2)[:2])
            if loc not in hits:
                hits.append(loc)
            if len(hits) >= 6:
                return hits
    return hits


# ---------------------------------------------------------------------------
# Dashboard corner
# ---------------------------------------------------------------------------

DASH_DIRS = ["config/kibana/dashboards", "docker/kibana/dashboards"]


def _walk_fields(obj: Any) -> Iterable[str]:
    """Yield every value of a ``field`` / ``sourceField`` key found anywhere in ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"field", "sourceField"} and isinstance(v, str):
                yield v
            else:
                yield from _walk_fields(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_fields(item)


def parse_dashboards() -> dict[str, Any]:
    """Parse repo NDJSON saved objects → counts + per-object field references."""
    result: dict[str, Any] = {"objects": [], "counts": {}, "field_refs": {}}
    counts: dict[str, int] = {}
    for d in DASH_DIRS:
        for ndjson in sorted((REPO / d).glob("*.ndjson")):
            for line in ndjson.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    so = json.loads(line)
                except json.JSONDecodeError:
                    continue
                so_type = so.get("type")
                if not so_type:
                    continue
                counts[so_type] = counts.get(so_type, 0) + 1
                title = (so.get("attributes") or {}).get("title")
                fields: set[str] = set()
                attrs = so.get("attributes") or {}
                # Legacy viz: visState + searchSourceJSON are JSON strings.
                for key in ("visState", "kibanaSavedObjectMeta"):
                    blob = attrs.get(key)
                    if isinstance(blob, dict):
                        blob = blob.get("searchSourceJSON")
                    if isinstance(blob, str):
                        try:
                            fields.update(_walk_fields(json.loads(blob)))
                        except json.JSONDecodeError:
                            pass
                # Lens: state is structured JSON.
                if isinstance(attrs.get("state"), dict):
                    fields.update(_walk_fields(attrs["state"]))
                fields.update(_walk_fields(attrs))
                result["objects"].append(
                    {"file": ndjson.name, "type": so_type, "id": so.get("id"), "title": title}
                )
                if fields:
                    result["field_refs"].setdefault(ndjson.name, {})[title or so.get("id")] = (
                        sorted(f for f in fields if not f.startswith("_"))
                    )
    result["counts"] = counts
    return result


def live_kibana_objects() -> dict[str, Any]:
    """List live Kibana saved objects (dashboard/visualization/lens/index-pattern)."""
    types = ["dashboard", "visualization", "lens", "index-pattern"]
    qs = "&".join(f"type={t}" for t in types) + "&per_page=1000&fields=title"
    try:
        data = _get_json(f"{KIBANA_URL}/api/saved_objects/_find?{qs}")
    except Exception as exc:  # noqa: BLE001 — best-effort liveness probe
        return {"error": repr(exc)}
    out: dict[str, list[str]] = {}
    for so in data.get("saved_objects", []):
        out.setdefault(so["type"], []).append((so.get("attributes") or {}).get("title") or so["id"])
    return {t: sorted(v) for t, v in out.items()}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _norm(t: str | None) -> str | None:
    """Collapse numeric type families so int/long and float/double compare equal."""
    return {
        "integer": "int",
        "long": "int",
        "short": "int",
        "float": "fp",
        "double": "fp",
        "half_float": "fp",
    }.get(t or "", t)


def classify(row: dict[str, Any]) -> str:
    """Classify one reconciliation row into the ticket's taxonomy.

    Args:
        row: A row dict produced by :func:`reconcile_family` (pre-classification).

    Returns:
        A classification label (``✅ aligned`` / ``⚠️ …`` / ``ℹ️ …``).
    """
    live = row["live_type"]
    via = row["expected_via"]
    exp = row["expected_type"]
    name = row["field"]
    leaf = name.rsplit(".", 1)[-1]

    if row.get("container") and live in (None, "object", "nested"):
        return "ℹ️ container/structural"
    if via.startswith("explicit"):
        return (
            "✅ aligned (explicit)"
            if _norm(exp) == _norm(live)
            else f"⚠️ type-mismatch (explicit={exp}/live={live})"
        )
    if via.startswith("dynamic:"):
        if _norm(exp) == _norm(live) or (exp and live and str(exp).startswith(str(live))):
            return "✅ aligned (dynamic-rule)"
        return f"⚠️ type-mismatch (rule={exp}/live={live})"
    if via == "unindexed(dynamic:false)":
        return "ℹ️ source-only (dynamic:false)"

    # es-default / no-template → dynamically mapped: scan for trap classes.
    if live in ("long", "integer") and (FLOAT_HINT.search(leaf) or MS_HINT.search(leaf)):
        return "⚠️ TRAP float→long (0.0 first-seen)"
    if live == "text" and (leaf in JOIN_KEY or leaf.endswith("_id")):
        return "⚠️ TRAP join-key-as-text (needs keyword)"
    if row.get("live_ignore_above") in (256, 1024) and TEXT_TRAP_HINT.search(leaf):
        return f"⚠️ TRAP keyword ignore_above:{row['live_ignore_above']} (long-text drop)"
    return "⚠️ emitted-but-unmapped (dynamic)"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_dashboard_index(dash: Mapping[str, Any]) -> dict[str, list[str]]:
    """Map ``leaf field name`` → list of ``dashboard-file:panel`` references."""
    idx: dict[str, list[str]] = {}
    for fname, panels in dash.get("field_refs", {}).items():
        for panel, fields in panels.items():
            for f in fields:
                base = f[:-8] if f.endswith(".keyword") else f
                leaf = base.rsplit(".", 1)[-1]
                idx.setdefault(leaf, []).append(f"{fname}:{panel}")
    return idx


def reconcile_family(fam: Family, dash_idx: Mapping[str, list[str]]) -> dict[str, Any]:
    """Build the full three-corner record for one family."""
    live = union_live_mapping(fam.pattern)
    tmpl = load_template(fam.template) if fam.template else None
    rows: list[dict[str, Any]] = []
    for fname in sorted(live):
        attrs = live[fname]
        json_kind = {
            "long": "long",
            "integer": "long",
            "float": "double",
            "double": "double",
            "boolean": "boolean",
        }.get(attrs.get("type"), "string")
        expected = (
            tmpl.expected_type(fname, json_kind) if tmpl else {"via": "no-template", "type": None}
        )
        row = {
            "field": fname,
            "live_type": attrs.get("type"),
            "live_ignore_above": attrs.get("ignore_above"),
            "live_has_keyword": attrs.get("has_keyword_subfield"),
            "container": attrs.get("container"),
            "expected_via": expected["via"],
            "expected_type": expected["type"],
            "emit_sites": grep_field(fname),
            "dashboard_refs": dash_idx.get(fname.rsplit(".", 1)[-1], []),
        }
        row["classification"] = classify(row)
        rows.append(row)
    return {
        "family": fam.key,
        "pattern": fam.pattern,
        "template": fam.template,
        "note": fam.note,
        "field_count": len(rows),
        "rows": rows,
    }


def main() -> None:
    """Run all corners and dump JSON intermediates under ``--out``."""
    ap = argparse.ArgumentParser(description="FRE-533 telemetry reconciliation extractor")
    ap.add_argument("--out", default="/tmp/fre533", help="output directory")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    dash = parse_dashboards()
    (out / "dashboards.json").write_text(json.dumps(dash, indent=2))
    print(f"[dashboards] repo objects: {dash['counts']}")
    dash_idx = build_dashboard_index(dash)

    all_rows: list[dict[str, Any]] = []
    for fam in FAMILIES:
        rec = reconcile_family(fam, dash_idx)
        (out / f"family_{fam.key}.json").write_text(json.dumps(rec, indent=2))
        for r in rec["rows"]:
            all_rows.append({"family": fam.key, **r})
        print(f"[family] {fam.key}: {rec['field_count']} fields -> family_{fam.key}.json")

    # Consolidated "every field" reconciliation table (acceptance artifact).
    csv_path = out / "reconciliation_table.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "family",
                "field",
                "live_type",
                "live_ignore_above",
                "expected_via",
                "expected_type",
                "classification",
                "emit_sites",
                "dashboard_refs",
            ]
        )
        for r in all_rows:
            w.writerow(
                [
                    r["family"],
                    r["field"],
                    r["live_type"],
                    r["live_ignore_above"],
                    r["expected_via"],
                    r["expected_type"],
                    r["classification"],
                    " | ".join(r["emit_sites"]),
                    " | ".join(r["dashboard_refs"]),
                ]
            )
    print(f"[csv] {len(all_rows)} rows -> {csv_path.name}")

    live_kb = live_kibana_objects()
    (out / "kibana_live.json").write_text(json.dumps(live_kb, indent=2))
    if "error" in live_kb:
        print(f"[kibana] live probe error: {live_kb['error']}")
    else:
        print(f"[kibana] live: {{ {', '.join(f'{k}:{len(v)}' for k, v in live_kb.items())} }}")


if __name__ == "__main__":
    main()
