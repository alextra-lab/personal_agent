# ruff: noqa: D103
"""Unit tests for the FRE-540 hermetic reconciliation checker (ADR-0090 D5).

Covers the floor checks (mapping↔dashboard, trap-class lint) and the report-only/gate behaviour
with synthetic in-``tmp_path`` templates + dashboards, a frozen gold-regression fixture locking the
FRE-533 classification semantics, and a smoke test over the committed repo files.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts.audit.telemetry_surface_check import (
    DEFAULT_DASHBOARDS_DIR,
    DEFAULT_TEMPLATES_DIR,
    REPO,
    Finding,
    check_mapping_dashboard,
    check_sample_document_types,
    check_trap_lint,
    diff_baseline,
    finding_key,
    load_baseline,
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
# Check 5 — sample document ↔ mapping (FRE-1107)
# ---------------------------------------------------------------------------

# The literal producer output (RequestMonitor._check_thresholds), not the ticket's prose paraphrase.
_VIOLATION_TEXT = "CPU critically high: 100.0% (DEGRADED threshold)"


def _fake_fetch(
    docs: list[dict[str, object]],
) -> "Callable[[str, str, int], list[dict[str, object]]]":
    def fetch(es_url: str, pattern: str, size: int) -> list[dict[str, object]]:
        return docs

    return fetch


def test_sample_mapping_flags_list_in_numeric_field(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-captains-captures-*"],
        properties={
            "metrics_summary": {"properties": {"threshold_violations": {"type": "integer"}}}
        },
    )
    templates = load_templates(tmp_path)
    doc = {"metrics_summary": {"threshold_violations": [_VIOLATION_TEXT]}}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert [(f.check, f.klass, f.field) for f in findings] == [
        ("sample-mapping", "producer-type-mismatch", "metrics_summary.threshold_violations")
    ]


def test_sample_mapping_clean_after_keyword_fix(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-captains-captures-*"],
        properties={
            "metrics_summary": {
                "properties": {"threshold_violations": {"type": "keyword", "ignore_above": 1024}}
            }
        },
    )
    templates = load_templates(tmp_path)
    doc = {"metrics_summary": {"threshold_violations": [_VIOLATION_TEXT]}}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert findings == []


def test_sample_mapping_does_not_flag_legitimate_keyword_list(tmp_path: Path) -> None:
    # A list value in a keyword field (e.g. tools_used) is the NORMAL shape — a blanket
    # "any list = mismatch" rule would false-fire here. Regression-guards the codex-review fix:
    # the check is element-wise, not shape-wise.
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        properties={"tools_used": {"type": "keyword"}},
    )
    templates = load_templates(tmp_path)
    doc = {"tools_used": ["run_python", "web_search"]}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert findings == []


def test_sample_mapping_flags_dict_in_keyword_field(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        properties={"model": {"type": "keyword"}},
    )
    templates = load_templates(tmp_path)
    doc = {"model": {"unexpected": "object"}}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert [f.klass for f in findings] == ["producer-type-mismatch"]


def test_sample_mapping_flags_non_bool_in_boolean_field(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        properties={"eval_mode": {"type": "boolean"}},
    )
    templates = load_templates(tmp_path)
    doc = {"eval_mode": "yes"}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert [f.klass for f in findings] == ["producer-type-mismatch"]


def test_sample_mapping_empty_list_is_inconclusive(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-captains-captures-*"],
        properties={
            "metrics_summary": {"properties": {"threshold_violations": {"type": "integer"}}}
        },
    )
    templates = load_templates(tmp_path)
    doc: dict[str, object] = {"metrics_summary": {"threshold_violations": []}}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert findings == []


def test_sample_mapping_null_value_is_not_mismatch(tmp_path: Path) -> None:
    # FRE-1130: null values in any field type are indexed as absence in ES, not as type mismatches.
    # A numeric field holding null should not trigger "producer-type-mismatch", but will appear
    # in "null-only-field" category.
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-monitors-health-*"],
        properties={"gpu_utilisation_percent": {"type": "float"}},
    )
    templates = load_templates(tmp_path)
    doc = {"gpu_utilisation_percent": None}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert len(findings) == 1
    assert findings[0].klass == "null-only-field"  # Not producer-type-mismatch
    assert findings[0].field == "gpu_utilisation_percent"


def test_sample_mapping_null_only_field_goes_to_null_only_category(tmp_path: Path) -> None:
    # FRE-1130: fields that hold null in every sampled document should appear in a separate
    # "null-only-field" category, not as "producer-type-mismatch".
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-monitors-health-*"],
        properties={
            "gpu_utilisation_percent": {"type": "float"},
            "video_memory_used_bytes": {"type": "integer"},
        },
    )
    templates = load_templates(tmp_path)
    docs = [
        {"gpu_utilisation_percent": None, "video_memory_used_bytes": None},
        {"gpu_utilisation_percent": None, "video_memory_used_bytes": None},
    ]
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch(docs))
    null_only = [f for f in findings if f.klass == "null-only-field"]
    mismatches = [f for f in findings if f.klass == "producer-type-mismatch"]
    assert len(null_only) == 2  # both fields hold only nulls
    assert len(mismatches) == 0  # no type mismatches
    assert {f.field for f in null_only} == {"gpu_utilisation_percent", "video_memory_used_bytes"}


def test_sample_mapping_mixed_null_and_valid_not_null_only(tmp_path: Path) -> None:
    # FRE-1130: a field with some nulls and some valid values is NOT "null-only" — it only
    # becomes null-only if every sampled document has a null value (or list of only-nulls).
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-monitors-health-*"],
        properties={"video_memory_used_bytes": {"type": "integer"}},
    )
    templates = load_templates(tmp_path)
    docs = [
        {"video_memory_used_bytes": None},
        {"video_memory_used_bytes": 1024},
    ]
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch(docs))
    null_only = [f for f in findings if f.klass == "null-only-field"]
    mismatches = [f for f in findings if f.klass == "producer-type-mismatch"]
    assert len(null_only) == 0  # has at least one valid value
    assert len(mismatches) == 0  # type is correct when present


def test_sample_mapping_genuine_mismatch_still_caught_regression(tmp_path: Path) -> None:
    # FRE-1130: regression guard — genuine type mismatches are still reported even with null fix.
    # This proves we didn't disable the check when adding null handling.
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-monitors-health-*"],
        properties={"gpu_utilisation_percent": {"type": "float"}},
    )
    templates = load_templates(tmp_path)
    doc = {"gpu_utilisation_percent": "not a number"}  # String in numeric field
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    mismatches = [f for f in findings if f.klass == "producer-type-mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].field == "gpu_utilisation_percent"


def test_sample_mapping_dedupes_across_multiple_docs(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-captains-captures-*"],
        properties={
            "metrics_summary": {"properties": {"threshold_violations": {"type": "integer"}}}
        },
    )
    templates = load_templates(tmp_path)
    docs = [
        {"metrics_summary": {"threshold_violations": [_VIOLATION_TEXT]}},
        {"metrics_summary": {"threshold_violations": ["Memory high: 91.0% (ALERT threshold)"]}},
    ]
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch(docs))
    assert len(findings) == 1  # one Finding per (family, field), not one per offending document


def test_sample_mapping_real_committed_captures_template_is_clean() -> None:
    # End-to-end against the actual repo template (post-FRE-1107 fix), proving the guard is
    # verified against a document the producer actually writes, not just eyeballed against JSON.
    templates = [
        t
        for t in load_templates(DEFAULT_TEMPLATES_DIR)
        if "captains-captures-index-template" in t.path
    ]
    assert templates, "captains-captures-index-template.json not found"
    doc = {"metrics_summary": {"threshold_violations": [_VIOLATION_TEXT]}}
    findings = check_sample_document_types(templates, "http://es", fetch=_fake_fetch([doc]))
    assert findings == []


# ---------------------------------------------------------------------------
# Real-file smoke (hermetic, against the committed surface)
# ---------------------------------------------------------------------------


def test_real_files_run_hermetically_report_mode() -> None:
    rc = main([])  # defaults → committed templates + dashboards, report mode
    assert rc == 0


def test_real_committed_dashboard_keyword_refs_are_clean() -> None:
    # FRE-555: the two previously-broken `agent-insights` panels are fixed — `insight_type.keyword`
    # rewritten to the bare keyword `insight_type`, `title.keyword` backed by a new keyword subfield.
    report = run_checks(DEFAULT_TEMPLATES_DIR, DEFAULT_DASHBOARDS_DIR)
    md = {f.field for f in report.floor if f.check == "mapping-dashboard"}
    assert md == set(), md


def test_real_joinability_template_is_fully_clean() -> None:
    # FRE-555: _meta is detected at template-root, so the joinability template (the ADR model) is now
    # the ADR exemplar with *zero* floor findings, missing-meta included.
    report = run_checks(DEFAULT_TEMPLATES_DIR, DEFAULT_DASHBOARDS_DIR)
    joinability = [f for f in report.floor if "monitors-joinability" in f.family]
    assert joinability == [], joinability


def test_real_committed_floor_is_exactly_the_allowlisted_exceptions() -> None:
    # FRE-555: after the _meta-detection fix + the two dashboard fixes, the only residual floor
    # findings are the reviewed-correct / deferred trap-lint exceptions captured in the committed
    # allowlist. Locks completeness: a *new* floor finding, or a wrong check/klass/family/source on
    # a surviving one, fails this test (and CI). FRE-1107 removed metrics_summary.threshold_violations
    # from the baseline — it was never a count (see the corrected template _meta), so the mapping fix
    # makes this field's finding disappear rather than need re-allowlisting.
    report = run_checks(DEFAULT_TEMPLATES_DIR, DEFAULT_DASHBOARDS_DIR)
    keys = {finding_key(f) for f in report.floor}
    baseline = REPO / "scripts" / "audit" / "telemetry_surface_baseline.json"
    expected_keys = load_baseline(baseline)
    assert keys == expected_keys, sorted(k[3] for k in keys)
    assert all(k[0] == "trap-lint" for k in keys), keys


def test_real_committed_baseline_makes_gate_pass() -> None:
    # FRE-555: the committed allowlist makes the hermetic `--gate` exit 0 — the safety net the CI flip
    # depends on.
    baseline = REPO / "scripts" / "audit" / "telemetry_surface_baseline.json"
    rc = main(["--gate", "--baseline", str(baseline)])
    assert rc == 0


# ---------------------------------------------------------------------------
# _meta detection at template-root (FRE-555 checker fix)
# ---------------------------------------------------------------------------


def _write_template_meta_at_root(path: Path, *, index_patterns: list[str]) -> None:
    """Write a template whose `_meta` sits at the document root (the FRE-534 convention)."""
    body = {
        "index_patterns": index_patterns,
        "priority": 100,
        "template": {
            "mappings": {"dynamic": True, "properties": {"trace_id": {"type": "keyword"}}}
        },
        "_meta": {"managed_by": "scripts/setup-elasticsearch.sh", "description": "root meta"},
    }
    path.write_text(json.dumps(body))


def test_meta_at_template_root_is_detected(tmp_path: Path) -> None:
    _write_template_meta_at_root(tmp_path / "index-template.json", index_patterns=["agent-logs-*"])
    findings = check_trap_lint(load_templates(tmp_path))
    assert [f for f in findings if f.klass == "missing-meta"] == []


def test_no_meta_anywhere_still_flags_missing(tmp_path: Path) -> None:
    _write_template(
        tmp_path / "index-template.json",
        index_patterns=["agent-logs-*"],
        meta=False,
        properties={"trace_id": {"type": "keyword"}},
    )
    findings = check_trap_lint(load_templates(tmp_path))
    assert [f.klass for f in findings] == ["missing-meta"]


# ---------------------------------------------------------------------------
# Baseline allowlist (FRE-555)
# ---------------------------------------------------------------------------


def test_finding_key_is_the_five_tuple_minus_detail() -> None:
    f = Finding("trap-lint", "numeric-as-long", "fam", "latency_ms", "wording", "src")
    assert finding_key(f) == ("trap-lint", "numeric-as-long", "fam", "latency_ms", "src")


def test_load_baseline_reads_keys_and_ignores_extra_fields(tmp_path: Path) -> None:
    bpath = tmp_path / "baseline.json"
    bpath.write_text(
        json.dumps(
            [
                {
                    "check": "trap-lint",
                    "klass": "numeric-as-long",
                    "family": "fam",
                    "field": "latency_ms",
                    "source": "fam",
                    "detail": "ignored",
                    "note": "ignored documentation",
                }
            ]
        )
    )
    assert load_baseline(bpath) == {("trap-lint", "numeric-as-long", "fam", "latency_ms", "fam")}


def test_load_baseline_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "nope.json")


def test_diff_baseline_partitions_new_grandfathered_stale() -> None:
    floor = [
        Finding("trap-lint", "numeric-as-long", "fam", "latency_ms", "d", "fam"),  # grandfathered
        Finding("trap-lint", "missing-meta", "fam2", "_meta", "d", "fam2"),  # new
    ]
    baseline = {
        ("trap-lint", "numeric-as-long", "fam", "latency_ms", "fam"),  # present
        ("trap-lint", "join-key-not-keyword", "gone", "session_id", "gone"),  # stale
    }
    new, grandfathered, stale = diff_baseline(floor, baseline)
    assert [f.field for f in new] == ["_meta"]
    assert [f.field for f in grandfathered] == ["latency_ms"]
    assert stale == {("trap-lint", "join-key-not-keyword", "gone", "session_id", "gone")}


def test_gate_with_baseline_suppresses_grandfathered(tmp_path: Path) -> None:
    tdir, ddir = _drift_dirs(tmp_path)
    # Snapshot every current floor finding into a baseline → gate passes.
    bpath = tmp_path / "baseline.json"
    rc_w = main(
        [
            "--write-baseline",
            str(bpath),
            "--templates-dir",
            str(tdir),
            "--dashboards-dir",
            str(ddir),
        ]
    )
    assert rc_w == 0
    rc = main(
        [
            "--gate",
            "--baseline",
            str(bpath),
            "--templates-dir",
            str(tdir),
            "--dashboards-dir",
            str(ddir),
        ]
    )
    assert rc == 0


def test_gate_with_baseline_still_fails_on_new_drift(tmp_path: Path) -> None:
    tdir, ddir = _drift_dirs(tmp_path)
    # Baseline covers only the missing-meta finding; the numeric + bare-keyword drift stays gated.
    tmpl_path = str(tdir / "index-template.json")
    bpath = tmp_path / "baseline.json"
    bpath.write_text(
        json.dumps(
            [
                {
                    "check": "trap-lint",
                    "klass": "missing-meta",
                    "family": tmpl_path,
                    "field": "_meta",
                    "source": tmpl_path,
                }
            ]
        )
    )
    rc = main(
        [
            "--gate",
            "--baseline",
            str(bpath),
            "--templates-dir",
            str(tdir),
            "--dashboards-dir",
            str(ddir),
        ]
    )
    assert rc == 1


def test_write_baseline_roundtrips(tmp_path: Path) -> None:
    tdir, ddir = _drift_dirs(tmp_path)
    bpath = tmp_path / "baseline.json"
    main(
        [
            "--write-baseline",
            str(bpath),
            "--templates-dir",
            str(tdir),
            "--dashboards-dir",
            str(ddir),
        ]
    )
    data = json.loads(bpath.read_text())
    assert isinstance(data, list) and data
    assert {"check", "klass", "family", "field", "source"} <= set(data[0])


def test_stale_baseline_entry_is_reported_not_gated(tmp_path: Path) -> None:
    # A clean surface + a baseline that names a finding no longer present → stale, but rc 0.
    tdir = tmp_path / "t"
    ddir = tmp_path / "d"
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
    bpath = tmp_path / "baseline.json"
    bpath.write_text(
        json.dumps(
            [
                {
                    "check": "trap-lint",
                    "klass": "numeric-as-long",
                    "family": "ghost",
                    "field": "gone_ms",
                    "source": "ghost",
                }
            ]
        )
    )
    rc = main(
        [
            "--gate",
            "--baseline",
            str(bpath),
            "--templates-dir",
            str(tdir),
            "--dashboards-dir",
            str(ddir),
        ]
    )
    assert rc == 0
