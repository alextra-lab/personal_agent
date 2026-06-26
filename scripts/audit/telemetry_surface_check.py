#!/usr/bin/env python3
"""FRE-540 — hermetic three-way telemetry reconciliation checker (ADR-0090 D5 floor).

Static, no-live-stack guard that closes the **mapping ↔ dashboard** corner pair and lints
the **mapping** corner for the trap classes that fail *silently* in production (the 2026-05-10
and FRE-411 incidents). Both corners are committed files, so the floor checks run in a hermetic
CI job with no Elasticsearch and no Kibana.

Floor checks (gate-able):

1. **Mapping ↔ dashboard.** Every field a dashboard panel references must resolve in its family's
   index template. A panel reading a never-mapped field (or a ``.keyword`` subfield that does not
   exist because the base is bare ``keyword``) aggregates to nothing — a silent-empty panel.
2. **Trap-class mapping lint.** For every template's explicit properties: numeric/float/ratio/cost
   and ``*_ms``-style fields must not be left as ``long`` (the ``0.0``→``long`` trap); join keys
   (``trace_id``/``session_id``/``task_id``/``span_id``/``*_id``) must be ``keyword``; long-text/error
   /digest fields must not be ``keyword`` with the default ``ignore_above`` (silent >limit drop); the
   ``_meta`` block (ADR-0090 D2) must be present.

Report-only / environment-gated checks (never affect exit code, clearly separated from the floor):

3. **Emit → mapping.** Grep the known emit dirs for trap-class field names and report ones with no
   explicit mapping. Heuristic (no runtime hook); a *report* until a field registry exists.
4. **Repo template ↔ live mapping.** When ``--es-url`` is reachable, compare ``GET /<family>/_mapping``
   against the repo template. Environment-gated; cannot run in the hermetic pass.

Phasing (ADR-0090 D5): shipped in **report mode** while FRE-534/535 burned the baseline down. FRE-555
flips CI to ``--gate`` with a small committed **allowlist** of reviewed-correct / deferred exceptions
(``scripts/audit/telemetry_surface_baseline.json``): the gate fails only on *new* drift whose
``(check, klass, family, field, source)`` key is not allowlisted. ``--write-baseline`` regenerates the
file locally (CI never invokes it, so new findings are never silently grandfathered).

Usage::

    python scripts/audit/telemetry_surface_check.py            # report mode, exit 0
    python scripts/audit/telemetry_surface_check.py --gate     # exit 1 on any floor finding
    python scripts/audit/telemetry_surface_check.py --gate --baseline scripts/audit/telemetry_surface_baseline.json  # gate on new drift only
    python scripts/audit/telemetry_surface_check.py --write-baseline scripts/audit/telemetry_surface_baseline.json   # regenerate the allowlist
    python scripts/audit/telemetry_surface_check.py --es-url http://localhost:9200  # + live check

Reuses the validated FRE-533 primitives (``flatten_properties``, ``Template``/``DynamicRule``,
``Template.expected_type``, the trap-hint regexes) so its classifications match the reconciliation
table (``docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.audit.fre533_reconcile import (
    FLOAT_HINT,
    JOIN_KEY,
    MS_HINT,
    TEXT_TRAP_HINT,
    DynamicRule,
    Template,
    flatten_properties,
)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATES_DIR = REPO / "docker" / "elasticsearch"
DEFAULT_DASHBOARDS_DIR = REPO / "config" / "kibana" / "dashboards"
EMIT_DIRS = (
    "src/personal_agent/telemetry",
    "src/personal_agent/captains_log",
    "src/personal_agent/observability",
)

# Join keys that must be exact-match `keyword` (ADR-0074 + ADR-0090 D2 adds task_id).
JOIN_KEYS: frozenset[str] = frozenset(JOIN_KEY | {"task_id"})
# Numeric types that silently swallow the `0.0`→long trap when a float field is mapped to them.
_INT_TYPES = frozenset({"long", "integer", "short", "byte"})
# Default ignore_above values that silently drop long strings from the index.
_DEFAULT_IGNORE_ABOVE = frozenset({256, 1024})

FLOOR_CHECKS = frozenset({"mapping-dashboard", "trap-lint"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedTemplate:
    """One parsed ES index template with the metadata the checker reasons over.

    Args:
        path: Repo-relative-ish path of the template file (for reporting).
        index_patterns: The ``index_patterns`` the template declares (its self-described family).
        priority: The template ``priority`` (ES resolution tiebreak for overlapping patterns).
        has_meta: Whether a ``_meta`` block is present (ADR-0090 D2).
        template: The reused FRE-533 :class:`Template` (explicit props + dynamic rules + resolver).
        text_rules_with_keyword: Names of dynamic rules whose mapping is ``text`` *with* a
            ``fields.keyword`` subfield (so a ``.keyword`` reference against them is valid).
        string_catch_all_type: Mapped type of the template's string catch-all dynamic rule (a rule
            with ``match_mapping_type`` string/``*`` and no ``match`` pattern, e.g. the repo's
            ``default_string_keyword``), or ``None`` if the template has no catch-all. The reused
            FRE-533 ``DynamicRule`` cannot represent a match-less rule, so any otherwise-unmatched
            string is governed by this here instead of by ES's text+keyword default.
        string_catch_all_has_keyword: Whether that catch-all maps to ``text`` *with* a keyword subfield.
    """

    path: str
    index_patterns: tuple[str, ...]
    priority: int
    has_meta: bool
    template: Template
    text_rules_with_keyword: frozenset[str]
    string_catch_all_type: str | None
    string_catch_all_has_keyword: bool


@dataclass(frozen=True)
class PanelRef:
    """A single dashboard panel and the fields it references.

    Args:
        dashboard: NDJSON filename the panel lives in.
        title: Panel/visualization title (or saved-object id when untitled).
        index_pattern_title: The index-pattern title the panel is bound to (family hint).
        fields: The distinct field references extracted from the panel.
    """

    dashboard: str
    title: str
    index_pattern_title: str | None
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    """One reconciliation finding.

    Args:
        check: Which check produced it (floor: ``mapping-dashboard``/``trap-lint``; else report-only).
        klass: Short classification slug (e.g. ``keyword-on-bare-keyword``).
        family: Template path or index-pattern title the finding belongs to.
        field: The offending field path.
        detail: One-line human explanation.
        source: Where the finding was observed (``dashboard:panel`` or template path).
    """

    check: str
    klass: str
    family: str
    field: str
    detail: str
    source: str


# ---------------------------------------------------------------------------
# Template loading (self-describing family map)
# ---------------------------------------------------------------------------


def _build_rules(dynamic_templates: Sequence[Any]) -> tuple[list[DynamicRule], set[str]]:
    """Build :class:`DynamicRule` objects and note which text rules carry a keyword subfield.

    Args:
        dynamic_templates: The raw ``dynamic_templates`` list from a template's mappings.

    Returns:
        A ``(rules, text_rule_names_with_keyword)`` pair. ``rules`` mirrors FRE-533's loader;
        the second element is the subset of rule names whose mapping is ``text`` with a
        ``fields.keyword`` subfield (so ``field.keyword`` against them resolves).
    """
    import re

    rules: list[DynamicRule] = []
    text_with_keyword: set[str] = set()
    for entry in dynamic_templates:
        for name, spec in entry.items():
            is_regex = spec.get("match_pattern") == "regex"
            match = spec.get("match")
            mapping = spec.get("mapping", {}) or {}
            rules.append(
                DynamicRule(
                    name=name,
                    match_glob=None if is_regex else match,
                    match_regex=re.compile(match) if is_regex and match is not None else None,
                    match_mapping_type=spec.get("match_mapping_type", "*"),
                    mapped_type=mapping.get("type", "?"),
                )
            )
            if mapping.get("type") == "text" and "keyword" in (mapping.get("fields") or {}):
                text_with_keyword.add(name)
    return rules, text_with_keyword


def _string_catch_all(dynamic_templates: Sequence[Any]) -> tuple[str | None, bool]:
    """Return the ``(type, has_keyword_subfield)`` of a template's string catch-all rule, if any.

    A catch-all is a ``dynamic_templates`` rule that matches every string field — ``match_mapping_type``
    of ``string``/``*`` with no ``match``/``path_match`` constraint (the repo's
    ``default_string_keyword``). The first such rule in document order wins (ES applies rules in order).

    Args:
        dynamic_templates: The raw ``dynamic_templates`` list from a template's mappings.

    Returns:
        ``(mapped_type, has_keyword_subfield)`` for the catch-all, or ``(None, False)`` if absent.
    """
    for entry in dynamic_templates:
        for spec in entry.values():
            if spec.get("match") or spec.get("path_match"):
                continue
            if spec.get("match_mapping_type") in {"string", "*"}:
                mapping = spec.get("mapping", {}) or {}
                return mapping.get("type"), "keyword" in (mapping.get("fields") or {})
    return None, False


def load_template_file(path: Path) -> LoadedTemplate:
    """Parse one ES index-template JSON file into a :class:`LoadedTemplate`.

    Args:
        path: Absolute path to the template JSON.

    Returns:
        The parsed template with its self-declared ``index_patterns``/``priority``, ``_meta``
        presence, flattened explicit properties, and compiled dynamic rules.
    """
    data = json.loads(path.read_text())
    mappings = data.get("template", {}).get("mappings", {})
    explicit = flatten_properties(mappings.get("properties", {}))
    dyn_templates = mappings.get("dynamic_templates", [])
    rules, text_kw = _build_rules(dyn_templates)
    catch_all_type, catch_all_kw = _string_catch_all(dyn_templates)
    template = Template(str(path), mappings.get("dynamic", True), explicit, rules)
    # ADR-0090 D2 `_meta` is valid at either the composable-template root (the FRE-534 convention,
    # stored as template metadata by `PUT /_index_template/...`) or under `mappings` (index-mapping
    # metadata). Accept both — checking only `mappings` false-flagged every template (FRE-555).
    return LoadedTemplate(
        path=str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path),
        index_patterns=tuple(data.get("index_patterns", [])),
        priority=int(data.get("priority", 0)),
        has_meta="_meta" in mappings or "_meta" in data,
        template=template,
        text_rules_with_keyword=frozenset(text_kw),
        string_catch_all_type=catch_all_type,
        string_catch_all_has_keyword=catch_all_kw,
    )


def load_templates(templates_dir: Path) -> list[LoadedTemplate]:
    """Load every ``*index-template.json`` under ``templates_dir`` (skips ILM/policy files)."""
    return [load_template_file(p) for p in sorted(templates_dir.glob("*index-template.json"))]


def _literal_prefix(pattern: str) -> str:
    """Return the wildcard-free leading literal of an ES index pattern (``agent-logs-*``→``agent-logs-``)."""
    return pattern.split("*", 1)[0]


def resolve_template(title: str, templates: Sequence[LoadedTemplate]) -> LoadedTemplate | None:
    """Resolve a dashboard index-pattern title to the template that governs its family.

    Matching is on wildcard-free literal prefixes. A template whose literal prefix is a prefix of
    the title (``forward`` — the template's family is at least as specific as the title) is preferred;
    only when none match forward do we fall back to a template whose prefix the title is a prefix of
    (``reverse`` — e.g. the ``agent-logs*`` data-view title vs the ``agent-logs-*`` template). Among
    matches the longest literal prefix wins, then higher ``priority``. The forward preference is what
    keeps an ``agent-captains-captures-*`` title from resolving to the longer-but-disjoint
    ``agent-captains-captures-subagents*`` template.

    Args:
        title: The index-pattern saved-object title (itself a glob, e.g. ``agent-logs-*``).
        templates: The loaded templates to resolve against.

    Returns:
        The governing :class:`LoadedTemplate`, or ``None`` if no family prefix matches.
    """
    stem = _literal_prefix(title)
    forward: list[tuple[int, int, LoadedTemplate]] = []
    reverse: list[tuple[int, int, LoadedTemplate]] = []
    for t in templates:
        for pat in t.index_patterns:
            plit = _literal_prefix(pat)
            if stem.startswith(plit):
                forward.append((len(plit), t.priority, t))
            elif plit.startswith(stem):
                reverse.append((len(plit), t.priority, t))
    pool = forward or reverse
    if not pool:
        return None
    pool.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return pool[0][2]


# ---------------------------------------------------------------------------
# Dashboard parsing
# ---------------------------------------------------------------------------


def _walk_field_refs(obj: Any) -> Iterable[str]:
    """Yield every field reference in a saved object: ``field``/``sourceField`` + saved-search ``columns``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"field", "sourceField"} and isinstance(v, str):
                yield v
            elif k == "columns" and isinstance(v, list):
                for col in v:
                    if isinstance(col, str):
                        yield col
            else:
                yield from _walk_field_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_field_refs(item)


def _index_pattern_titles(dashboards_dir: Path) -> dict[str, str]:
    """Map index-pattern saved-object ``id`` → ``attributes.title`` across all NDJSON files."""
    out: dict[str, str] = {}
    for ndjson in sorted(dashboards_dir.glob("*.ndjson")):
        for line in ndjson.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                so = json.loads(line)
            except json.JSONDecodeError:
                continue
            if so.get("type") == "index-pattern":
                title = (so.get("attributes") or {}).get("title")
                if isinstance(so.get("id"), str) and isinstance(title, str):
                    out[so["id"]] = title
    return out


def _panel_index_pattern(so: dict[str, Any], id_to_title: dict[str, str]) -> str | None:
    """Find the index-pattern title a viz/lens saved object is bound to via its ``references``."""
    for ref in so.get("references") or []:
        if isinstance(ref, dict) and ref.get("type") == "index-pattern":
            title = id_to_title.get(ref.get("id"))
            if title:
                return title
    return None


def parse_panels(dashboards_dir: Path) -> list[PanelRef]:
    """Parse all visualization/lens panels with their bound index-pattern and field references.

    Args:
        dashboards_dir: Directory of committed Kibana NDJSON saved objects.

    Returns:
        One :class:`PanelRef` per visualization/lens object that references at least one field.
    """
    id_to_title = _index_pattern_titles(dashboards_dir)
    panels: list[PanelRef] = []
    for ndjson in sorted(dashboards_dir.glob("*.ndjson")):
        for line in ndjson.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                so = json.loads(line)
            except json.JSONDecodeError:
                continue
            if so.get("type") not in {"visualization", "lens"}:
                continue
            attrs = so.get("attributes") or {}
            fields: set[str] = set()
            for key in ("visState", "kibanaSavedObjectMeta"):
                blob = attrs.get(key)
                if isinstance(blob, dict):
                    blob = blob.get("searchSourceJSON")
                if isinstance(blob, str):
                    try:
                        fields.update(_walk_field_refs(json.loads(blob)))
                    except json.JSONDecodeError:
                        pass
            if isinstance(attrs.get("state"), dict):
                fields.update(_walk_field_refs(attrs["state"]))
            fields.update(_walk_field_refs(attrs))
            refs = tuple(sorted(f for f in fields if not f.startswith("_")))
            if not refs:
                continue
            panels.append(
                PanelRef(
                    dashboard=ndjson.name,
                    title=attrs.get("title") or so.get("id") or "<untitled>",
                    index_pattern_title=_panel_index_pattern(so, id_to_title),
                    fields=refs,
                )
            )
    return panels


# ---------------------------------------------------------------------------
# Check 1 — mapping ↔ dashboard
# ---------------------------------------------------------------------------


def _resolve_field(tmpl: LoadedTemplate, ref: str) -> tuple[bool, str, str]:
    """Decide whether a panel field reference resolves to a usable mapping.

    Args:
        tmpl: The governing template.
        ref: The field path the panel references (may end in ``.keyword``).

    Returns:
        ``(ok, klass, detail)``. ``ok`` is True when the reference resolves; otherwise ``klass`` is the
        finding classification and ``detail`` the explanation.
    """
    explicit = tmpl.template.explicit
    if ref in explicit:
        return True, "", ""

    if ref.endswith(".keyword"):
        base = ref[: -len(".keyword")]
        attrs = explicit.get(base)
        if attrs is not None:
            base_type = attrs.get("type")
            if base_type == "text" and attrs.get("has_keyword_subfield"):
                return True, "", ""
            if base_type == "keyword":
                return (
                    False,
                    "keyword-on-bare-keyword",
                    (
                        f"'{base}' is explicitly mapped as bare keyword (no .keyword subfield); "
                        f"the terms aggregation on '{ref}' resolves to nothing"
                    ),
                )
            return (
                False,
                "keyword-on-non-text",
                (f"'{base}' is '{base_type}' without a .keyword subfield; '{ref}' does not exist"),
            )
        # Base not explicit — resolve through dynamic rules / defaults.
        expected = tmpl.template.expected_type(base, "string")
        via, typ = expected["via"], expected["type"]
        if via.startswith("dynamic:"):
            rule = via.split(":", 1)[1]
            if typ == "text" and rule in tmpl.text_rules_with_keyword:
                return True, "", ""
            return (
                False,
                "keyword-on-dynamic-bare",
                (
                    f"'{base}' maps via {via} to '{typ}' with no .keyword subfield; '{ref}' resolves to nothing"
                ),
            )
        if via == "unindexed(dynamic:false)":
            return (
                False,
                "referenced-but-unmapped",
                (
                    f"'{base}' is not mapped and the template is dynamic:false; '{ref}' is never indexed"
                ),
            )
        if via == "es-default":
            # fre533's resolver cannot see a match-less catch-all rule; apply it here. The repo's
            # default_string_keyword maps unmatched strings to bare keyword → no .keyword subfield.
            if tmpl.string_catch_all_type == "keyword":
                return (
                    False,
                    "keyword-on-dynamic-bare",
                    (
                        f"'{base}' maps via the string catch-all to bare keyword; '{ref}' resolves to nothing"
                    ),
                )
            if tmpl.string_catch_all_type == "text" and not tmpl.string_catch_all_has_keyword:
                return (
                    False,
                    "keyword-on-dynamic-bare",
                    (
                        f"'{base}' maps via the string catch-all to text with no .keyword subfield; "
                        f"'{ref}' resolves to nothing"
                    ),
                )
            # No catch-all (or catch-all is text+keyword) → genuine ES text+keyword default has .keyword.
            return True, "", ""
        return True, "", ""  # no-template family — cannot assert hermetically

    # Plain (non-.keyword) reference.
    leaf = ref.rsplit(".", 1)[-1]
    json_kind = "long" if (FLOAT_HINT.search(leaf) or MS_HINT.search(leaf)) else "string"
    expected = tmpl.template.expected_type(ref, json_kind)
    via = expected["via"]
    if via.startswith(("explicit", "dynamic:")):
        return True, "", ""
    if via == "unindexed(dynamic:false)":
        return (
            False,
            "referenced-but-unmapped",
            (f"'{ref}' is not mapped and the template is dynamic:false; the panel reads nothing"),
        )
    if via == "es-default" and json_kind == "long":
        return (
            False,
            "referenced-but-unmapped",
            (
                f"'{ref}' is numeric-named but not explicitly mapped; it relies on first-value inference "
                f"(ADR-0090 D2: numerics must be explicit)"
            ),
        )
    return True, "", ""


def check_mapping_dashboard(
    panels: Sequence[PanelRef], templates: Sequence[LoadedTemplate]
) -> list[Finding]:
    """Run the mapping ↔ dashboard floor check over all panels."""
    findings: list[Finding] = []
    for panel in panels:
        if panel.index_pattern_title is None:
            continue
        tmpl = resolve_template(panel.index_pattern_title, templates)
        if tmpl is None:
            continue
        for ref in panel.fields:
            ok, klass, detail = _resolve_field(tmpl, ref)
            if not ok:
                findings.append(
                    Finding(
                        check="mapping-dashboard",
                        klass=klass,
                        family=panel.index_pattern_title,
                        field=ref,
                        detail=detail,
                        source=f"{panel.dashboard}:{panel.title}",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Check 2 — trap-class mapping lint
# ---------------------------------------------------------------------------


def check_trap_lint(templates: Sequence[LoadedTemplate]) -> list[Finding]:
    """Run the trap-class lint floor check over every template's explicit properties."""
    findings: list[Finding] = []
    for tmpl in templates:
        for fname, attrs in tmpl.template.explicit.items():
            if attrs.get("container"):
                continue
            leaf = fname.rsplit(".", 1)[-1]
            ftype = attrs.get("type")

            if (FLOAT_HINT.search(leaf) or MS_HINT.search(leaf)) and ftype in _INT_TYPES:
                findings.append(
                    Finding(
                        "trap-lint",
                        "numeric-as-long",
                        tmpl.path,
                        fname,
                        f"'{fname}' is numeric/duration-named but mapped '{ftype}'; a non-integer value "
                        f"is rejected or truncated (the 0.0→long trap)",
                        tmpl.path,
                    )
                )

            if (leaf in JOIN_KEYS or leaf.endswith("_id")) and ftype not in (None, "keyword"):
                findings.append(
                    Finding(
                        "trap-lint",
                        "join-key-not-keyword",
                        tmpl.path,
                        fname,
                        f"join key '{fname}' is '{ftype}', not keyword; exact-match term joins "
                        f"silently return nothing (ADR-0074 / FRE-411)",
                        tmpl.path,
                    )
                )

            if (
                TEXT_TRAP_HINT.search(leaf)
                and ftype == "keyword"
                and attrs.get("ignore_above") in _DEFAULT_IGNORE_ABOVE
            ):
                findings.append(
                    Finding(
                        "trap-lint",
                        "long-text-ignore-above",
                        tmpl.path,
                        fname,
                        f"long-text field '{fname}' is keyword ignore_above:{attrs.get('ignore_above')}; "
                        f"values over the limit are silently dropped from the index",
                        tmpl.path,
                    )
                )

        if not tmpl.has_meta:
            findings.append(
                Finding(
                    "trap-lint",
                    "missing-meta",
                    tmpl.path,
                    "_meta",
                    "template has no _meta block (ADR-0090 D2: managed_by / retention_days / description)",
                    tmpl.path,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Report-only check 3 — emit → mapping (heuristic grep)
# ---------------------------------------------------------------------------


def check_emit_mapping(templates: Sequence[LoadedTemplate]) -> list[Finding]:
    """Report-only: grep emit dirs for trap-class field names with no explicit mapping anywhere.

    Heuristic at the emit corner (no runtime hook). A field name appearing in the emit dirs but not
    explicitly mapped by *any* template is reported — never gated (ADR-0090 D5; field-registry upgrade
    is an open decision).
    """
    explicit_fields = {leaf.rsplit(".", 1)[-1] for t in templates for leaf in t.template.explicit}
    findings: list[Finding] = []
    seen: set[str] = set()
    for d in EMIT_DIRS:
        target = REPO / d
        if not target.exists():
            continue
        try:
            res = subprocess.run(
                ["rg", "-oN", "--no-filename", r'"[a-z][a-z0-9_]*_(ms|usd|id|cost)"', str(target)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return findings
        for raw in res.stdout.splitlines():
            name = raw.strip().strip('"')
            if name in seen or name in explicit_fields:
                continue
            seen.add(name)
            findings.append(
                Finding(
                    "emit-mapping",
                    "emitted-not-explicit",
                    d,
                    name,
                    f"trap-class field '{name}' appears in emit dir '{d}' but is not explicitly mapped "
                    f"in any template (heuristic — verify the emit site)",
                    d,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Report-only check 4 — repo template ↔ live mapping (environment-gated)
# ---------------------------------------------------------------------------


def check_live_mapping(templates: Sequence[LoadedTemplate], es_url: str) -> list[Finding]:
    """Environment-gated report-only: compare each template's explicit props to the live mapping.

    Args:
        templates: Loaded repo templates.
        es_url: Base Elasticsearch URL (e.g. ``http://localhost:9200``).

    Returns:
        Findings for explicit fields whose live type differs from the repo template, or an empty list
        if ES is unreachable (best-effort liveness probe — never gated).
    """
    findings: list[Finding] = []
    for tmpl in templates:
        if not tmpl.index_patterns:
            continue
        pattern = tmpl.index_patterns[0]
        try:
            with urllib.request.urlopen(f"{es_url}/{pattern}/_mapping", timeout=10) as resp:  # noqa: S310
                raw = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue
        live: dict[str, Any] = {}
        for body in raw.values():
            props = body.get("mappings", {}).get("properties")
            if props:
                live.update(flatten_properties(props))
        for fname, attrs in tmpl.template.explicit.items():
            if attrs.get("container"):
                continue
            live_attrs = live.get(fname)
            if live_attrs and live_attrs.get("type") != attrs.get("type"):
                findings.append(
                    Finding(
                        "live-mapping",
                        "repo-live-divergence",
                        tmpl.path,
                        fname,
                        f"repo template maps '{fname}' as '{attrs.get('type')}' but live is "
                        f"'{live_attrs.get('type')}' — re-run setup-elasticsearch.sh or a field was hot-added",
                        f"{es_url}/{pattern}",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Reporting + driver
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """Collected findings, partitioned into the gate-able floor and report-only sections."""

    floor: list[Finding] = field(default_factory=list)
    report_only: list[Finding] = field(default_factory=list)


def run_checks(
    templates_dir: Path,
    dashboards_dir: Path,
    es_url: str | None = None,
) -> Report:
    """Run all checks and partition findings into floor vs report-only.

    Args:
        templates_dir: Directory of ES index templates.
        dashboards_dir: Directory of Kibana NDJSON saved objects.
        es_url: Optional Elasticsearch base URL to enable the live-mapping check.

    Returns:
        A :class:`Report` with floor (checks 1–2) and report-only (checks 3–4) findings.
    """
    templates = load_templates(templates_dir)
    panels = parse_panels(dashboards_dir)
    report = Report()
    report.floor.extend(check_mapping_dashboard(panels, templates))
    report.floor.extend(check_trap_lint(templates))
    report.report_only.extend(check_emit_mapping(templates))
    if es_url:
        report.report_only.extend(check_live_mapping(templates, es_url))
    return report


# ---------------------------------------------------------------------------
# Baseline allowlist (FRE-555) — gate on *new* drift only
# ---------------------------------------------------------------------------

FindingKey = tuple[str, str, str, str, str]


def finding_key(f: Finding) -> FindingKey:
    """Stable identity of a finding for baseline matching: everything but the volatile ``detail``.

    ``source`` is part of the key so ``mapping-dashboard`` grandfathering stays *panel-specific* — a
    new broken panel referencing an already-allowlisted field is new drift, not silently accepted.
    For ``trap-lint`` ``source == family`` (the template path), so it is redundant-but-harmless.

    Args:
        f: The finding to key.

    Returns:
        The ``(check, klass, family, field, source)`` tuple.
    """
    return (f.check, f.klass, f.family, f.field, f.source)


def load_baseline(path: Path) -> set[FindingKey]:
    """Load a committed allowlist of accepted floor findings.

    The file is a JSON list of objects carrying at least ``check``/``klass``/``family``/``field``/
    ``source``; any extra keys (``detail``, ``note``) are ignored so the file can self-document *why*
    each entry is an accepted exception.

    Args:
        path: Path to the baseline JSON.

    Returns:
        The set of :func:`finding_key` tuples the baseline grandfathers.

    Raises:
        FileNotFoundError: If ``path`` does not exist (a ``--baseline`` typo must fail loudly, never
            silently grandfather nothing).
    """
    entries = json.loads(path.read_text())
    return {(e["check"], e["klass"], e["family"], e["field"], e["source"]) for e in entries}


def diff_baseline(
    floor: Sequence[Finding], baseline: set[FindingKey]
) -> tuple[list[Finding], list[Finding], set[FindingKey]]:
    """Partition floor findings against a baseline allowlist.

    Args:
        floor: The current floor findings.
        baseline: The allowlisted finding keys.

    Returns:
        ``(new, grandfathered, stale)`` — ``new`` are findings whose key is *not* allowlisted (these
        gate); ``grandfathered`` are allowlisted findings still present; ``stale`` are allowlist keys
        no longer produced (a fix landed — report so the entry can be pruned, but never gate on it).
    """
    new: list[Finding] = []
    grandfathered: list[Finding] = []
    present: set[FindingKey] = set()
    for f in floor:
        key = finding_key(f)
        present.add(key)
        (grandfathered if key in baseline else new).append(f)
    stale = baseline - present
    return new, grandfathered, stale


def write_baseline(path: Path, floor: Sequence[Finding]) -> None:
    """Write the current floor findings to ``path`` as a deterministic, self-documenting allowlist.

    Local-only regeneration helper — CI never invokes this (it only passes ``--baseline``), so new
    findings are never silently grandfathered. Entries are sorted and carry ``detail`` for readability;
    a human adds a ``note`` explaining *why* each is an accepted exception.

    Args:
        path: Destination JSON path.
        floor: The floor findings to snapshot.
    """
    rows = sorted(
        (
            {
                "check": f.check,
                "klass": f.klass,
                "family": f.family,
                "field": f.field,
                "source": f.source,
                "detail": f.detail,
            }
            for f in floor
        ),
        key=lambda r: (r["check"], r["family"], r["field"], r["klass"]),
    )
    path.write_text(json.dumps(rows, indent=2) + "\n")


def _print_section(title: str, findings: Sequence[Finding]) -> None:
    """Print a grouped findings section to stdout."""
    print(f"\n## {title} — {len(findings)} finding(s)")
    if not findings:
        print("  (none)")
        return
    by_check: dict[str, list[Finding]] = {}
    for f in findings:
        by_check.setdefault(f.check, []).append(f)
    for check, items in sorted(by_check.items()):
        print(f"\n  [{check}]")
        for f in items:
            print(f"    {f.klass}: {f.field}  ({f.family})")
            print(f"        {f.detail}")
            print(f"        ↳ {f.source}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code (0 unless ``--gate`` and floor findings exist)."""
    ap = argparse.ArgumentParser(
        description="FRE-540 hermetic telemetry reconciliation checker (ADR-0090 D5)"
    )
    ap.add_argument(
        "--gate", action="store_true", help="exit nonzero if any floor (check 1-2) finding exists"
    )
    ap.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES_DIR)
    ap.add_argument("--dashboards-dir", type=Path, default=DEFAULT_DASHBOARDS_DIR)
    ap.add_argument(
        "--es-url", default=None, help="enable the environment-gated live-mapping check"
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="JSON allowlist of accepted floor findings; with --gate, fail only on NEW (un-allowlisted) drift",
    )
    ap.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="write the current floor findings to this path as a fresh allowlist and exit 0 (local-only helper)",
    )
    args = ap.parse_args(argv)

    report = run_checks(args.templates_dir, args.dashboards_dir, args.es_url)

    if args.write_baseline is not None:
        write_baseline(args.write_baseline, report.floor)
        print(f"wrote {len(report.floor)} floor finding(s) to {args.write_baseline}")
        return 0

    print("# FRE-540 telemetry surface reconciliation (ADR-0090 D5)")
    mode = "GATE" if args.gate else "report-only"
    baseline_note = f" · baseline: {args.baseline}" if args.baseline else ""
    print(
        f"mode: {mode}{baseline_note} · templates: {args.templates_dir} · dashboards: {args.dashboards_dir}"
    )
    _print_section("REPORT-ONLY — emit→mapping + live-mapping (never gated)", report.report_only)

    if args.baseline is not None:
        baseline = load_baseline(args.baseline)
        new, grandfathered, stale = diff_baseline(report.floor, baseline)
        _print_section("FLOOR — NEW drift (gate-able)", new)
        print(f"\n## ALLOWLISTED — accepted exceptions in {args.baseline} — {len(grandfathered)}")
        if stale:
            print(
                f"\n## STALE allowlist entries (finding fixed — prune from baseline) — {len(stale)}"
            )
            for key in sorted(stale):
                print(f"    {key[1]}: {key[3]}  ({key[2]})")
        if args.gate and new:
            print(f"\nFAIL (gate): {len(new)} NEW floor finding(s) not in the baseline.")
            return 1
        if args.gate:
            print(f"\nPASS (gate): no new drift; {len(grandfathered)} allowlisted exception(s).")
        else:
            print(
                f"\nreport-only: {len(new)} new + {len(grandfathered)} allowlisted floor finding(s); not gating."
            )
        return 0

    _print_section("FLOOR — mapping↔dashboard + trap-class lint (gate-able)", report.floor)
    if args.gate and report.floor:
        print(f"\nFAIL (gate): {len(report.floor)} floor finding(s).")
        return 1
    if args.gate:
        print("\nPASS (gate): no floor findings.")
    else:
        print(
            f"\nreport-only: {len(report.floor)} floor + {len(report.report_only)} report-only finding(s); not gating."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
