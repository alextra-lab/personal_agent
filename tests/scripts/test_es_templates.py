"""Static validation of the Elasticsearch index templates (FRE-534 / A2).

These tests are *static* — they parse the repo template JSON under
``docker/elasticsearch/`` and the registration in ``scripts/setup-elasticsearch.sh``
without touching a live cluster. They encode the FRE-533 reconciliation findings
so the "first-pass-wrong mappings" failure mode is caught in CI rather than in
production (this also seeds the FRE-540 A3 checker).

Source of truth: ``docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ES_DIR = REPO_ROOT / "docker" / "elasticsearch"
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-elasticsearch.sh"


def _load(name: str) -> dict:
    return json.loads((ES_DIR / name).read_text())


def _dynamic_rule(template: dict, rule_name: str) -> dict | None:
    """Return the ``mapping`` block of a named dynamic_template, or None."""
    rules = template["template"]["mappings"].get("dynamic_templates", [])
    for entry in rules:
        if rule_name in entry:
            return entry[rule_name]
    return None


def _props(template: dict) -> dict:
    return template["template"]["mappings"]["properties"]


# --------------------------------------------------------------------------- #
# Every template file is structurally valid.
# --------------------------------------------------------------------------- #

TEMPLATE_FILES = sorted(p.name for p in ES_DIR.glob("*-index-template.json"))


@pytest.mark.parametrize("name", TEMPLATE_FILES)
def test_template_is_structurally_valid(name: str) -> None:
    """Every template file is valid JSON with index patterns and a mappings block."""
    tpl = _load(name)
    assert isinstance(tpl.get("index_patterns"), list) and tpl["index_patterns"]
    assert "mappings" in tpl["template"]
    assert "properties" in tpl["template"]["mappings"]


# --------------------------------------------------------------------------- #
# agent-logs (index-template.json) — trap fixes.
# --------------------------------------------------------------------------- #


def test_logs_has_ms_fields_as_float_rule() -> None:
    """agent-logs has the ms_fields_as_float rule (defeats the 0.0->long trap)."""
    tpl = _load("index-template.json")
    rule = _dynamic_rule(tpl, "ms_fields_as_float")
    assert rule is not None, "ms_fields_as_float rule missing (0.0->long trap)"
    assert rule["mapping"]["type"] == "float"


def test_logs_threshold_floats_are_explicit() -> None:
    """The three threshold fields are pinned float, not dynamic long."""
    props = _props(_load("index-template.json"))
    for field in ("calibration_threshold", "governance_threshold", "threshold"):
        assert props.get(field, {}).get("type") == "float", f"{field} must be float"


def test_logs_cost_gate_money_fields_are_double() -> None:
    """cost_gate money fields are pinned double, not dynamic keyword (FRE-536).

    ``gate.py`` previously emitted these as ``str(Decimal)`` so they fell through
    to the default keyword mapping and could not be summed. FRE-536 renames them
    to ``*_usd`` and emits ``float(...)``; the template must pin each as double so
    the cost & budget dashboard can aggregate them.
    """
    props = _props(_load("index-template.json"))
    for field in (
        "amount_usd",
        "actual_cost_usd",
        "reserved_usd",
        "delta_usd",
        "reservation_amount_usd",
    ):
        assert props.get(field, {}).get("type") == "double", f"{field} must be double"


def test_logs_free_text_covers_genuine_long_text_leaves() -> None:
    """free_text maps the genuine long-text leaves to text."""
    tpl = _load("index-template.json")
    rule = _dynamic_rule(tpl, "free_text")
    assert rule is not None
    pattern = rule["match"]
    compiled = re.compile(pattern)
    for leaf in ("content", "content_value", "response_preview", "message_excerpt", "summary"):
        assert compiled.match(leaf), f"free_text should map leaf {leaf!r} -> text"
    assert rule["mapping"]["type"] == "text"


def test_logs_denial_reason_stays_keyword() -> None:
    """denial_reason stays keyword (it feeds a terms-agg donut)."""
    # denial_reason feeds a terms-agg donut (extraction_retry_health.ndjson);
    # text would break it. Explicit keyword with a raised ignore_above.
    tpl = _load("index-template.json")
    props = _props(tpl)
    assert props.get("denial_reason", {}).get("type") == "keyword"
    assert props["denial_reason"].get("ignore_above", 0) >= 1024
    free_text = _dynamic_rule(tpl, "free_text")
    assert not re.compile(free_text["match"]).match("denial_reason")


# --------------------------------------------------------------------------- #
# New family templates — insights + slm-health.
# --------------------------------------------------------------------------- #


def test_insights_template_exists_and_fixes_join_key_and_costs() -> None:
    """Insights template fixes the component_id join key and pins cost floats."""
    tpl = _load("insights-index-template.json")
    assert "agent-insights-*" in tpl["index_patterns"]
    # join key: evidence.component_id resolves to keyword via ids_keyword (*_id).
    ids = _dynamic_rule(tpl, "ids_keyword")
    assert ids is not None and ids["mapping"]["type"] == "keyword"
    assert re.fullmatch(
        r".*\*_id|\*_id|.*_id", ids["match"].replace("^", "").replace("$", "")
    ) or ids["match"].endswith("_id")
    # cost/ratio/confidence defended as float.
    cost = _dynamic_rule(tpl, "cost_ratio_as_float")
    assert cost is not None and cost["mapping"]["type"] in ("float", "double")
    props = _props(tpl)
    assert props["insight_type"]["type"] == "keyword"
    assert props["record_type"]["type"] == "keyword"
    assert props["confidence"]["type"] in ("float", "double")
    assert props["evidence"]["properties"]["component_id"]["type"] == "keyword"
    assert props["evidence"]["properties"]["baseline_cost_usd"]["type"] in ("float", "double")


def test_slm_health_template_covers_full_model() -> None:
    """slm-health template covers the full 14-field snapshot model."""
    tpl = _load("monitors-slm-health-index-template.json")
    assert "agent-monitors-slm-health-*" in tpl["index_patterns"]
    props = _props(tpl)
    expected = {
        "status": "keyword",
        "reachable": "boolean",
        "model_loaded": "boolean",
        "gpu_util_pct": "float",
        "vram_used_mb": "float",
        "vram_total_mb": "float",
        "queue_depth": "integer",
        "latency_ema_ms": "float",
        "model_id": "keyword",
        "probe_latency_ms": "float",
        "probed_at": "date",
        "trace_id": "keyword",  # join-key fix (was text)
        "error": "text",
        "kind": "keyword",
    }
    for field, typ in expected.items():
        assert props.get(field, {}).get("type") == typ, f"{field} must be {typ}"


# --------------------------------------------------------------------------- #
# Captains 3-way split.
# --------------------------------------------------------------------------- #


def test_captains_three_way_split() -> None:
    """Captains is split three ways with the correct priority ladder."""
    assert not (ES_DIR / "captains-index-template.json").exists(), (
        "old straddling captains-index-template.json should be removed"
    )
    captures = _load("captains-captures-index-template.json")
    reflections = _load("captains-reflections-index-template.json")
    subagents = _load("captains-subagents-index-template.json")

    assert captures["index_patterns"] == ["agent-captains-captures-*"]
    assert reflections["index_patterns"] == ["agent-captains-reflections-*"]
    assert subagents["index_patterns"] == ["agent-captains-captures-subagents*"]

    assert captures["priority"] == 110
    assert reflections["priority"] == 110
    # subagents must out-rank captures for the overlapping index name.
    assert subagents["priority"] > captures["priority"]


def test_subagents_pins_its_shape() -> None:
    """Subagents template pins its own shape (chars/mode/booleans)."""
    props = _props(_load("captains-subagents-index-template.json"))
    for field in (
        "system_prompt_chars",
        "digest_chars",
        "context_chars",
        "full_output_chars",
        "skill_index_block_chars",
    ):
        assert props.get(field, {}).get("type") in ("long", "integer"), field
    assert props["mode"]["type"] == "keyword"
    assert props["memory_in_context"]["type"] == "boolean"
    assert props["success"]["type"] == "boolean"


# --------------------------------------------------------------------------- #
# Registration parity: script <-> files, and stale-template teardown.
# --------------------------------------------------------------------------- #


def test_setup_script_registration_parity() -> None:
    """Every template file is PUT by the setup script and vice-versa."""
    script = SETUP_SCRIPT.read_text()
    on_disk = {p.name for p in ES_DIR.glob("*-index-template.json")}
    for name in on_disk:
        assert name in script, f"{name} exists on disk but is not PUT by setup script"
    # Every template path the script references must exist on disk.
    referenced = set(re.findall(r"docker/elasticsearch/([\w-]+-index-template\.json)", script))
    for name in referenced:
        assert (ES_DIR / name).exists(), f"{name} is PUT by script but missing on disk"


# --------------------------------------------------------------------------- #
# ILM + retention for the daily/monthly diagnostic families (FRE-543).
# --------------------------------------------------------------------------- #

# (policy file, policy name, template file, retention_days)
ILM_FAMILIES = [
    ("insights-ilm-policy.json", "agent-insights-policy", "insights-index-template.json", 365),
    (
        "monitors-slm-health-ilm-policy.json",
        "agent-monitors-slm-health-policy",
        "monitors-slm-health-index-template.json",
        90,
    ),
]


@pytest.mark.parametrize("policy_file, policy_name, template_file, retention_days", ILM_FAMILIES)
def test_ilm_policy_is_minage_delete_not_rollover(
    policy_file: str, policy_name: str, template_file: str, retention_days: int
) -> None:
    """Each family's ILM policy deletes by min_age (no rollover) at its retention window."""
    policy = _load(policy_file)["policy"]
    phases = policy["phases"]

    # Delete phase exists, deletes, and its min_age matches the documented retention.
    delete = phases["delete"]
    assert "delete" in delete["actions"], f"{policy_file}: delete phase must delete"
    assert delete["min_age"] == f"{retention_days}d", (
        f"{policy_file}: delete min_age must be {retention_days}d"
    )

    # No rollover anywhere — these are date-partitioned indices with no write-alias.
    for phase_name, phase in phases.items():
        assert "rollover" not in phase.get("actions", {}), (
            f"{policy_file}: {phase_name} must not use rollover (no write-alias)"
        )

    # Retention is recorded in _meta so the window is self-documenting (acceptance crit).
    assert policy["_meta"]["retention_days"] == retention_days, (
        f"{policy_file}: _meta.retention_days must record {retention_days}"
    )


@pytest.mark.parametrize("policy_file, policy_name, template_file, retention_days", ILM_FAMILIES)
def test_ilm_template_references_policy(
    policy_file: str, policy_name: str, template_file: str, retention_days: int
) -> None:
    """The family template binds new indices to its ILM policy."""
    settings = _load(template_file)["template"]["settings"]
    assert settings.get("index.lifecycle.name") == policy_name, (
        f"{template_file} must set index.lifecycle.name={policy_name}"
    )


@pytest.mark.parametrize("policy_file, policy_name, template_file, retention_days", ILM_FAMILIES)
def test_ilm_policy_registered_in_setup_script(
    policy_file: str, policy_name: str, template_file: str, retention_days: int
) -> None:
    """setup-elasticsearch.sh PUTs each policy and points at its file."""
    script = SETUP_SCRIPT.read_text()
    assert f"/_ilm/policy/{policy_name}" in script, f"{policy_name} not PUT by setup script"
    assert f"docker/elasticsearch/{policy_file}" in script, (
        f"{policy_file} path not referenced by setup script"
    )


def test_retired_captains_template_is_torn_down() -> None:
    """The retired straddling template is DELETEd, never PUT."""
    script = SETUP_SCRIPT.read_text()
    # The retired remote template must be DELETEd (else equal-priority overlap
    # with the split captures/reflections templates breaks the PUT).
    assert re.search(
        r"delete_resource.*?/_index_template/agent-captains-template", script, re.DOTALL
    ), "setup script must delete_resource the retired agent-captains-template"
    # And it must no longer PUT the old single straddling template body file.
    assert "captains-index-template.json" not in script
    # The retired path may appear only on a DELETE line, never a PUT.
    for line in script.splitlines():
        if "/_index_template/agent-captains-template" in line and "X PUT" in line:
            pytest.fail(f"retired template is still PUT: {line.strip()}")
