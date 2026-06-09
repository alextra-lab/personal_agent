# ruff: noqa: D103
"""Unit tests for the FRE-540 hermetic reconciliation checker (ADR-0090 D5).

Covers the floor checks (mapping↔dashboard, trap-class lint) and the report-only/gate behaviour
with synthetic in-``tmp_path`` templates + dashboards, a frozen gold-regression fixture locking the
FRE-533 classification semantics, and a smoke test over the committed repo files.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.audit.telemetry_surface_check import (
    DEFAULT_DASHBOARDS_DIR,
    DEFAULT_TEMPLATES_DIR,
    check_mapping_dashboard,
    check_trap_lint,
    load_templates,
    main,
    parse_panels,
    resolve_template,
    run_checks,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_GUARDED_DYNAMIC_RULES = [
    {
        "ids_keyword": {
            "match": "*_id",
            "match_mapping_type": "string",
            "mapping": {"type": "keyword"},
        }
    },
    {
        "free_text": {
            "match_pattern": "regex",
            "match": r"^(.*_message|.*_text)$",
            "match_mapping_type": "string",
            "mapping": {"type": "text"},
        }
    },
    {
        "default_string_keyword": {
            "match_mapping_type": "string",
            "mapping": {"type": "keyword", "ignore_above": 1024},
        }
    },
]


def _write_template(
    path: Path,
    *,
    index_patterns: list[str],
    properties: dict[str, object],
    priority: int = 100,
    dynamic: bool = True,
    meta: bool = True,
    rules: list[dict[str, object]] | None = None,
) -> None:
    mappings: dict[str, object] = {"dynamic": dynamic, "properties": properties}
    if rules is not None:
        mappings["dynamic_templates"] = rules
    if meta:
        mappings["_meta"] = {"managed_by": "scripts/setup-elasticsearch.sh", "retention_days": 30}
    body = {
        "index_patterns": index_patterns,
        "priority": priority,
        "template": {"mappings": mappings},
    }
    path.write_text(json.dumps(body))


def _write_dashboard(path: Path, objects: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(o) for o in objects))


def _viz(title: str, ip_id: str, fields: list[str]) -> dict[str, object]:
    """A minimal legacy-visualization saved object referencing ``fields`` via terms aggs."""
    aggs = [{"type": "terms", "params": {"field": f}} for f in fields]
    return {
        "id": f"viz-{title}",
        "type": "visualization",
        "attributes": {"title": title, "visState": json.dumps({"aggs": aggs})},
        "references": [
            {
                "id": ip_id,
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
            }
        ],
    }


def _index_pattern(ip_id: str, title: str) -> dict[str, object]:
    return {"id": ip_id, "type": "index-pattern", "attributes": {"title": title}}


# ---------------------------------------------------------------------------
# Template loader + family resolution
# ---------------------------------------------------------------------------


def test_loader_reads_real_templates_and_builds_family_map() -> None:
    templates = load_templates(DEFAULT_TEMPLATES_DIR)
    patterns = {p for t in templates for p in t.index_patterns}
    assert "agent-logs-*" in patterns
    assert "agent-captains-captures-subagents*" in patterns
    # Every loaded template self-declares at least one index pattern.
    assert all(t.index_patterns for t in templates)


def test_resolve_logs_star_title_to_hyphen_template(tmp_path: Path) -> None:
    # Codex catch: data_views uses `agent-logs*` (no hyphen) vs template `agent-logs-*`.
    _write_template(
        tmp_path / "index-template.json", index_patterns=["agent-logs-*"], properties={}
    )
    templates = load_templates(tmp_path)
    resolved = resolve_template("agent-logs*", templates)
    assert resolved is not None
    assert "agent-logs-*" in resolved.index_patterns


def test_resolve_captures_title_prefers_captures_over_subagents(tmp_path: Path) -> None:
    # The captures-superset trap: an `agent-captains-captures-*` title must NOT resolve to the
    # longer-prefixed subagents template.
    _write_template(
        tmp_path / "captains-captures-index-template.json",
        index_patterns=["agent-captains-captures-*"],
        priority=110,
        properties={},
    )
    _write_template(
        tmp_path / "captains-subagents-index-template.json",
        index_patterns=["agent-captains-captures-subagents*"],
        priority=120,
        properties={},
    )
    templates = load_templates(tmp_path)
    captures = resolve_template("agent-captains-captures-*", templates)
    subagents = resolve_template("agent-captains-captures-subagents-*", templates)
    assert captures is not None and "agent-captains-captures-*" in captures.index_patterns
    assert (
        subagents is not None and "agent-captains-captures-subagents*" in subagents.index_patterns
    )


# ---------------------------------------------------------------------------
# Dashboard parsing
# ---------------------------------------------------------------------------


def test_parse_panels_extracts_fields_and_index_pattern(tmp_path: Path) -> None:
    _write_dashboard(
        tmp_path / "d.ndjson",
        [
            _index_pattern("ip1", "agent-logs-*"),
            _viz("Panel A", "ip1", ["model.keyword", "cost_usd"]),
        ],
    )
    panels = parse_panels(tmp_path)
    assert len(panels) == 1
    assert panels[0].index_pattern_title == "agent-logs-*"
    assert set(panels[0].fields) == {"model.keyword", "cost_usd"}


def test_parse_panels_extracts_saved_search_columns(tmp_path: Path) -> None:
    so = {
        "id": "search-1",
        "type": "lens",
        "attributes": {"title": "Search", "state": {"columns": ["trace_id", "phase.keyword"]}},
        "references": [{"id": "ip1", "type": "index-pattern"}],
    }
    _write_dashboard(tmp_path / "d.ndjson", [_index_pattern("ip1", "agent-logs-*"), so])
    panels = parse_panels(tmp_path)
    assert set(panels[0].fields) == {"trace_id", "phase.keyword"}


# ---------------------------------------------------------------------------
# Check 1 — mapping ↔ dashboard field resolution
# ---------------------------------------------------------------------------


def _resolve_one(
    tmp_path: Path, properties: dict[str, object], ref: str, **tmpl_kw: object
) -> list:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        properties=properties,
        rules=_GUARDED_DYNAMIC_RULES,
        **tmpl_kw,  # type: ignore[arg-type]
    )
    _write_dashboard(
        tmp_path / "d.ndjson",
        [_index_pattern("ip1", "agent-logs-*"), _viz("P", "ip1", [ref])],
    )
    templates = load_templates(tmp_path)
    panels = parse_panels(tmp_path)
    return check_mapping_dashboard(panels, templates)


def test_keyword_on_text_with_subfield_is_ok(tmp_path: Path) -> None:
    props = {"label": {"type": "text", "fields": {"keyword": {"type": "keyword"}}}}
    assert _resolve_one(tmp_path, props, "label.keyword") == []


def test_keyword_on_bare_keyword_is_broken(tmp_path: Path) -> None:
    findings = _resolve_one(tmp_path, {"model": {"type": "keyword"}}, "model.keyword")
    assert [f.klass for f in findings] == ["keyword-on-bare-keyword"]


def test_keyword_on_dynamic_default_keyword_is_broken(tmp_path: Path) -> None:
    # `role` is not explicit → default_string_keyword maps it to bare keyword → `.keyword` invalid.
    findings = _resolve_one(tmp_path, {}, "role.keyword")
    assert [f.klass for f in findings] == ["keyword-on-dynamic-bare"]


def test_missing_field_in_dynamic_false_is_broken(tmp_path: Path) -> None:
    findings = _resolve_one(tmp_path, {"trace_id": {"type": "keyword"}}, "ghost", dynamic=False)
    assert [f.klass for f in findings] == ["referenced-but-unmapped"]


def test_numeric_referenced_but_not_explicit_is_flagged(tmp_path: Path) -> None:
    # A numeric-named ref (matches FLOAT_HINT) with no explicit mapping relies on first-value
    # inference (ADR-0090 D2). `rounds_needed`-style renames match no hint and are an emit-corner
    # issue the hermetic floor correctly does not flag — see the gold table.
    findings = _resolve_one(tmp_path, {}, "cost_usd")
    assert [f.klass for f in findings] == ["referenced-but-unmapped"]


def test_plain_string_via_default_rule_is_ok(tmp_path: Path) -> None:
    assert _resolve_one(tmp_path, {}, "some_label") == []


# ---------------------------------------------------------------------------
# Check 2 — trap-class lint
# ---------------------------------------------------------------------------


def test_trap_lint_flags_all_four_classes(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        meta=False,
        properties={
            "cost_usd": {"type": "long"},  # numeric-as-long
            "trace_id": {"type": "text"},  # join-key-not-keyword
            "error_message": {"type": "keyword", "ignore_above": 1024},  # long-text-ignore-above
        },
    )
    findings = check_trap_lint(load_templates(tmp_path))
    classes = {f.klass for f in findings}
    assert classes == {
        "numeric-as-long",
        "join-key-not-keyword",
        "long-text-ignore-above",
        "missing-meta",
    }


def test_trap_lint_clean_template_has_no_findings(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-monitors-joinability-*"],
        dynamic=False,
        properties={
            "trace_id": {"type": "keyword"},
            "cost_usd": {"type": "double"},
            "duration_ms": {"type": "float"},
            "error_message": {"type": "text"},
        },
    )
    assert check_trap_lint(load_templates(tmp_path)) == []


# ---------------------------------------------------------------------------
# Driver + gate behaviour
# ---------------------------------------------------------------------------


def _drift_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A template+dashboard pair with one deliberate drift in each floor check."""
    tdir = tmp_path / "templates"
    ddir = tmp_path / "dashboards"
    tdir.mkdir()
    ddir.mkdir()
    _write_template(
        tdir / "index-template.json",
        index_patterns=["agent-logs-*"],
        meta=False,  # drift: missing _meta
        properties={
            "latency_ms": {"type": "long"},
            "model": {"type": "keyword"},
        },  # drift: numeric-as-long
        rules=_GUARDED_DYNAMIC_RULES,
    )
    _write_dashboard(
        ddir / "d.ndjson",
        [
            _index_pattern("ip1", "agent-logs-*"),
            _viz("P", "ip1", ["model.keyword"]),
        ],  # drift: bare-keyword .keyword
    )
    return tdir, ddir


def test_gate_exits_nonzero_on_introduced_drift(tmp_path: Path) -> None:
    tdir, ddir = _drift_dirs(tmp_path)
    rc = main(["--gate", "--templates-dir", str(tdir), "--dashboards-dir", str(ddir)])
    assert rc == 1


def test_report_mode_exits_zero_despite_drift(tmp_path: Path) -> None:
    tdir, ddir = _drift_dirs(tmp_path)
    rc = main(["--templates-dir", str(tdir), "--dashboards-dir", str(ddir)])
    assert rc == 0


def test_gate_passes_on_clean_surface(tmp_path: Path) -> None:
    tdir = tmp_path / "templates"
    ddir = tmp_path / "dashboards"
    tdir.mkdir()
    ddir.mkdir()
    _write_template(
        tdir / "index-template.json",
        index_patterns=["agent-logs-*"],
        properties={"model": {"type": "text", "fields": {"keyword": {"type": "keyword"}}}},
        rules=_GUARDED_DYNAMIC_RULES,
    )
    _write_dashboard(
        ddir / "d.ndjson",
        [_index_pattern("ip1", "agent-logs-*"), _viz("P", "ip1", ["model.keyword"])],
    )
    rc = main(["--gate", "--templates-dir", str(tdir), "--dashboards-dir", str(ddir)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Frozen gold-regression fixture (FRE-533 classification semantics)
# ---------------------------------------------------------------------------


def test_gold_classification_semantics(tmp_path: Path) -> None:
    """Lock the FRE-533 taxonomy: bare-keyword `.keyword` broken, text+subfield OK, join-key trap."""
    tdir = tmp_path / "t"
    ddir = tmp_path / "d"
    tdir.mkdir()
    ddir.mkdir()
    _write_template(
        tdir / "index-template.json",
        index_patterns=["agent-logs-*"],
        meta=True,
        rules=_GUARDED_DYNAMIC_RULES,
        properties={
            "model": {"type": "keyword"},  # gold: bare keyword → model.keyword broken
            "model_role": {"type": "keyword"},
            "phase": {"type": "keyword"},  # gold: bare keyword → phase.keyword broken
            "labelled": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },  # OK with subfield
            "session_id": {"type": "text"},  # gold: join key as text → trap
        },
    )
    _write_dashboard(
        ddir / "llm.ndjson",
        [
            _index_pattern("ip1", "agent-logs-*"),
            _viz("LLM Call Count by Model", "ip1", ["model.keyword"]),
            _viz("Avg Duration by Phase", "ip1", ["phase.keyword"]),
            _viz("OK panel", "ip1", ["labelled.keyword"]),
        ],
    )
    report = run_checks(tdir, ddir)
    md = {(f.field, f.klass) for f in report.floor if f.check == "mapping-dashboard"}
    assert ("model.keyword", "keyword-on-bare-keyword") in md
    assert ("phase.keyword", "keyword-on-bare-keyword") in md
    assert not any(f.field == "labelled.keyword" for f in report.floor)
    trap = {(f.field, f.klass) for f in report.floor if f.check == "trap-lint"}
    assert ("session_id", "join-key-not-keyword") in trap


# ---------------------------------------------------------------------------
# Real-file smoke (hermetic, against the committed surface)
# ---------------------------------------------------------------------------


def test_real_files_run_hermetically_report_mode() -> None:
    rc = main([])  # defaults → committed templates + dashboards, report mode
    assert rc == 0


def test_real_committed_keyword_refs_are_flagged() -> None:
    report = run_checks(DEFAULT_TEMPLATES_DIR, DEFAULT_DASHBOARDS_DIR)
    flagged = {f.field for f in report.floor if f.check == "mapping-dashboard"}
    # The two `.keyword` refs that actually exist in the committed NDJSON, both broken.
    assert "insight_type.keyword" in flagged
    assert "title.keyword" in flagged


def test_real_joinability_template_is_meta_only_clean() -> None:
    report = run_checks(DEFAULT_TEMPLATES_DIR, DEFAULT_DASHBOARDS_DIR)
    joinability = [
        f for f in report.floor if "monitors-joinability" in f.family and f.klass != "missing-meta"
    ]
    # The joinability template is the ADR model: zero trap findings other than the (backfilled) _meta.
    assert joinability == [], joinability
