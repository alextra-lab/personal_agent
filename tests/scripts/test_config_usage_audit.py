"""Config-parameter usage audit tests (FRE-893, ADR-0099 hygiene).

Backs the FRE-893 acceptance criteria: every AppConfig field is categorized with
evidence into load-bearing / read-but-never-overridden / never-read /
writer-pinned-guardrail. Several tests are regression guards for failure modes a
codex plan-review pass surfaced before implementation: a `getattr(settings, "x")`
indirect-read pattern that a plain `settings.x` grep would miss, a validator-only
field that a naive "reads outside settings.py" check would false-flag as dead, a
manifest-driven dynamic `getattr(settings, field)` resolution in
`config/substrate.yaml` that no literal-string grep can trace without reading the
manifest directly, and a secret-heuristic gap where several `managed_*` fields carry
an authoritative `json_schema_extra={"secret": True}` marker that the regex-based
`_is_secret()` in `config_inventory.py` does not match.

A further batch of tests (the `deployed_env_*` group below) backs the FRE-893 *reopen*
fix: the first version shipped with no deployed-environment override source at all, so
real production overrides were false-flagged as hardcode candidates. These tests use a
temp-directory fixture via the `AUDIT_DEPLOYED_ENV_ROOT` override — never the real
production `/opt/seshat/.env` — both to keep the suite host-independent and because a
real secret value must never appear in a test fixture.

The module-level `os.environ.setdefault` below (mirroring `tests/conftest.py`'s own
`APP_ENV` pattern) is a code-review-confirmed fix: without it, running this suite on the
VPS itself — this project's designated dev environment — silently read the real, live
`/opt/seshat/.env` for every test that doesn't set its own override, making results
host-dependent and baking production key names into `audit_all()`'s process-wide cache.
"""

from __future__ import annotations

import os

os.environ.setdefault("AUDIT_DEPLOYED_ENV_ROOT", "/nonexistent/fre-893-test-isolation-root")

from scripts.audit.config_usage_audit import (  # noqa: E402
    CATEGORIES,
    _deployed_env_key_sources,
    audit_all,
    categorize,
    deployed_env_files_present,
    generate_inventory_section,
    generate_report,
    override_locations,
    splice_inventory_section,
)

from personal_agent.config.settings import AppConfig  # noqa: E402


def test_every_appconfig_field_categorized() -> None:
    """All 311 fields are categorized into one of the 4 allowed buckets."""
    results = audit_all()
    assert len(results) == len(AppConfig.model_fields)
    assert {r.name for r in results} == set(AppConfig.model_fields)
    assert all(r.category in CATEGORIES for r in results)


def test_secrets_and_owner_allowlist_are_guardrail_pinned() -> None:
    """Every secret field (regex OR schema-marked) plus owner_storage_allowlist is guardrail-pinned.

    Includes the 7 `managed_*` fields that carry `json_schema_extra={"secret": True}`
    but that the existing regex heuristic in `config_inventory.py` does not match —
    the gap a codex plan-review pass found. If this test only used the regex
    heuristic, those 7 fields would wrongly categorize as removal/hardcode
    candidates instead of guardrail-pinned.
    """
    results = {r.name: r for r in audit_all()}
    schema_secrets = [
        name
        for name, field in AppConfig.model_fields.items()
        if isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("secret")
    ]
    assert "managed_embedding_token" in schema_secrets
    for name in [*schema_secrets, "owner_storage_allowlist"]:
        assert results[name].category == "writer-pinned-guardrail", name


def test_known_load_bearing_field_detected() -> None:
    """`debug` is read in security.py and has a src-root evidence hit."""
    result = categorize("debug", AppConfig.model_fields["debug"])
    assert result.reads.get("src")


def test_validator_only_field_is_not_never_read() -> None:
    """`owner_storage_allowlist` is consumed only via `self.<field>` inside settings.py.

    A naive "grep for settings.<field> outside settings.py" check finds zero hits for
    this field and would flag it dead. It is in fact actively enforced by a
    cross-field validator — internal_self_read must catch that.
    """
    result = categorize(
        "owner_storage_allowlist", AppConfig.model_fields["owner_storage_allowlist"]
    )
    assert result.internal_self_read is True
    assert result.category != "never-read"


def test_getattr_pattern_catches_indirect_read() -> None:
    r"""`url_guard_allowlist` is read only via `getattr(settings, "url_guard_allowlist", ...)`.

    A `settings\.<field>` -only grep misses this entirely (regression guard for the
    combined pattern).
    """
    result = categorize("url_guard_allowlist", AppConfig.model_fields["url_guard_allowlist"])
    assert result.reads.get("src")


def test_alias_reads_rescue_false_negative_clusters() -> None:
    """AC1 (FRE-896): the three alias-read clusters are no longer false-flagged never-read.

    `proactive_memory_*` (read via `cfg = settings; cfg.<field>`), `insights_wiring_enabled`
    (read via `get_settings().<field>`), and `quality_monitor_*` (read via a multi-line
    `getattr(settings, "<field>")`) each have production (`src`) read evidence under the
    alias-aware AST scan, and none categorize as `never-read`.
    """
    for name in (
        "proactive_memory_w_embedding",
        "insights_wiring_enabled",
        "quality_monitor_daily_run_hour_utc",
    ):
        result = categorize(name, AppConfig.model_fields[name])
        assert result.reads.get("src"), name
        assert result.category != "never-read", name


def test_self_attribute_alias_read_detected() -> None:
    """A field read only through a `self._settings` attribute alias is not never-read.

    `second_brain_cpu_threshold` is read via `self._settings.second_brain_cpu_threshold`
    in `brainstem/optimizer.py` — the codex-flagged wrong-deletion hole the AST resolver
    closes.
    """
    result = categorize(
        "second_brain_cpu_threshold", AppConfig.model_fields["second_brain_cpu_threshold"]
    )
    assert result.reads.get("src")
    assert result.category != "never-read"


def test_manifest_read_detected() -> None:
    """`llm_base_url` and `database_url` resolve via `config/substrate.yaml`'s `setting:` sources.

    `src/personal_agent/config/substrate.py::_resolve_setting` calls
    `getattr(settings, field)` where `field` is a variable sourced from the
    manifest text, not a literal string a grep pattern can match — this is the
    dynamic-resolution gap a codex plan-review pass found.
    """
    for name in ("llm_base_url", "database_url"):
        result = categorize(name, AppConfig.model_fields[name])
        assert result.manifest_read is True, name


def test_compose_override_detected() -> None:
    """`neo4j_uri` is set in docker-compose.cloud.yml's environment block."""
    result = categorize("neo4j_uri", AppConfig.model_fields["neo4j_uri"])
    kinds = {kind for _source, kind in result.overrides}
    assert "compose" in kinds


def test_conftest_override_detected() -> None:
    """`neo4j_uri` is also set via os.environ.setdefault("AGENT_NEO4J_URI", ...) in conftest.py."""
    result = categorize("neo4j_uri", AppConfig.model_fields["neo4j_uri"])
    kinds = {kind for _source, kind in result.overrides}
    assert "test-substrate" in kinds


def test_deployed_env_override_detected(tmp_path, monkeypatch) -> None:
    """A key present in a deployed `.env` is detected as a `deployed-env` override.

    Regression guard for the FRE-893 reopen: the first version had no deployed-env
    source, so real production overrides were false-flagged as hardcode candidates.
    """
    (tmp_path / ".env").write_text("AGENT_NEO4J_URI=bolt://fixture-only:7687\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_DEPLOYED_ENV_ROOT", str(tmp_path))
    result = override_locations("neo4j_uri", AppConfig.model_fields["neo4j_uri"])
    kinds = {kind for _source, kind in result}
    assert "deployed-env" in kinds


def test_deployed_env_value_never_leaked(tmp_path, monkeypatch) -> None:
    """Only the env-var KEY is read from a deployed file — the value never reaches evidence.

    Uses a fixture value that would be unmistakable if it leaked, since a real secret
    value must never appear in generated output.
    """
    secret_marker = "sk-FIXTURE-SECRET-VALUE-DO-NOT-LEAK"
    (tmp_path / ".env").write_text(f"AGENT_ANTHROPIC_API_KEY={secret_marker}\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_DEPLOYED_ENV_ROOT", str(tmp_path))
    sources = _deployed_env_key_sources(tmp_path)
    assert "AGENT_ANTHROPIC_API_KEY" in sources
    assert secret_marker not in sources.values()
    assert secret_marker not in "".join(sources.values())


def test_deployed_env_missing_file_degrades_cleanly(tmp_path, monkeypatch) -> None:
    """No deployed-env file at the configured root — override_locations does not crash."""
    monkeypatch.setenv("AUDIT_DEPLOYED_ENV_ROOT", str(tmp_path))
    result = override_locations("neo4j_uri", AppConfig.model_fields["neo4j_uri"])
    kinds = {kind for _source, kind in result}
    assert "deployed-env" not in kinds


def test_deployed_env_files_present_reports_transparently(tmp_path) -> None:
    """`deployed_env_files_present` names exactly the candidate paths that exist."""
    (tmp_path / ".env").write_text("AGENT_DEBUG=true\n", encoding="utf-8")
    found = deployed_env_files_present(tmp_path)
    assert found == (str(tmp_path / ".env"),)


def test_test_only_read_is_not_treated_as_production_evidence() -> None:
    """A field with zero src/ reads is `never-read` even if it happens to be touched in tests/scripts.

    This directly encodes the codex-review fix: production-bearing evidence is
    src/self-read/manifest-read only, never test/script hits alone.
    """
    for result in audit_all():
        if result.category == "never-read":
            assert not result.reads.get("src")
            assert result.internal_self_read is False
            assert result.manifest_read is False


def test_generate_report_covers_every_field_and_states_env_limitation() -> None:
    """The dated-report renderer produces a complete, evidence-transparent table.

    Never touches disk — pure string rendering, consistent with how
    `config_inventory.py::generate()` is unit-tested (no file IO in the test).
    """
    results = audit_all()
    report = generate_report(results)
    for result in results:
        assert f"`{result.name}`" in report
    assert "writer-pinned-guardrail" in report
    assert "never-read" in report
    assert "read-but-never-overridden" in report
    # AC: the report must state the .env / deployed-environment audit limitation,
    # not silently imply "no override found" means "never overridden in prod."
    assert ".env" in report


def test_generate_inventory_section_extends_not_duplicates() -> None:
    """The CONFIG_INVENTORY.md extension is a short summary + link, not a re-derivation."""
    section = generate_inventory_section(audit_all())
    assert "§10" in section
    assert "FRE-893" in section
    # A summary, not the full per-field table already in the dated report.
    assert len(section) < 4000


def test_splice_inventory_section_is_idempotent() -> None:
    """Regenerating must not accumulate a stray `---` separator on every run.

    Regression guard for a code-review-confirmed defect: an earlier version cut
    only `doc[:marker_index].rstrip()` before re-inserting the section, which
    strips whitespace but not the separator's literal `---` line, so re-running the
    generator repeatedly grew the file by one extra horizontal rule each time.
    """
    doc = "# Doc\n\nSome existing content.\n"
    section = "## §10 — Parameter usage audit (FRE-893)\n\nSummary.\n"

    once = splice_inventory_section(doc, section)
    twice = splice_inventory_section(once, section)
    thrice = splice_inventory_section(twice, section)

    assert once == twice == thrice
    assert twice.count("---") == 1
    assert "Some existing content." in twice


def test_splice_inventory_section_handles_dangling_separator_with_no_marker() -> None:
    r"""A doc with a trailing `---` but no §10 marker must not end up with a doubled rule.

    Regression guard for the FRE-893 redo: a follow-up PR removed the §10 section but
    left its leading separator dangling at EOF; re-running the generator against that
    doc (marker absent, orphan `---` present) previously produced `---\n\n---` above
    the freshly-inserted section instead of a single rule.
    """
    doc = "# Doc\n\nSome existing content.\n\n---\n"
    section = "## §10 — Parameter usage audit (FRE-893)\n\nSummary.\n"

    spliced = splice_inventory_section(doc, section)

    assert spliced.count("---") == 1
    assert "Some existing content." in spliced
