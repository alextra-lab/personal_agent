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

import ast
import os

os.environ.setdefault("AUDIT_DEPLOYED_ENV_ROOT", "/nonexistent/fre-893-test-isolation-root")

from scripts.audit.config_usage_audit import (  # noqa: E402
    CATEGORIES,
    _deployed_env_key_sources,
    _extract_heredocs,
    audit_all,
    categorize,
    deployed_env_files_present,
    external_reads,
    generate_inventory_section,
    generate_report,
    override_locations,
    splice_inventory_section,
)
from scripts.audit.settings_reads import collect_field_reads  # noqa: E402

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


def test_extract_heredocs_single_python_heredoc() -> None:
    """A basic `<<'PY' ... PY` heredoc is extracted and its body parses as Python."""
    script = "#!/usr/bin/env bash\nuv run python - <<'PY'\nx = 1\nPY\necho done\n"
    bodies = _extract_heredocs(script)
    assert len(bodies) == 1
    looks_like_python, body = bodies[0]
    assert looks_like_python is True
    assert ast.parse(body).body  # parses cleanly


def test_extract_heredocs_line_numbers_align_with_source() -> None:
    """A field read inside the heredoc reports the *real* file line, not the heredoc-local one.

    Needed for the evidence report's `file:line` citations to point at real code (FRE-907).
    """
    script = (
        "#!/usr/bin/env bash\n"  # line 1
        "set -e\n"  # line 2
        "uv run python - <<'PY'\n"  # line 3
        "from personal_agent.config import settings\n"  # line 4
        "x = settings.debug\n"  # line 5
        "PY\n"  # line 6
    )
    _, body = _extract_heredocs(script)[0]
    tree = ast.parse(body)
    reads = collect_field_reads(tree, frozenset({"debug"}))
    assert reads == [("debug", 5)]


def test_extract_heredocs_dash_variant_strips_leading_tabs() -> None:
    """`<<-DELIM` strips each body line's leading tabs before parsing (Bash's own behavior).

    Without this, an indented heredoc body fails `ast.parse` even though Bash runs it fine.
    """
    script = (
        "cmd <<-'PY'\n\tfrom personal_agent.config import settings\n\tx = settings.debug\n\tPY\n"
    )
    bodies = _extract_heredocs(script)
    assert len(bodies) == 1
    _, body = bodies[0]
    tree = ast.parse(body)
    assert collect_field_reads(tree, frozenset({"debug"}))


def test_extract_heredocs_skips_commented_out_marker() -> None:
    """A heredoc marker on a whole-line comment never actually runs — must not be scanned."""
    script = "# uv run python - <<'PY'\n# x = settings.debug\n# PY\necho hi\n"
    assert _extract_heredocs(script) == []


def test_extract_heredocs_skips_marker_text_inside_a_quoted_string() -> None:
    """`<<DELIM`-shaped text inside an unrelated quoted string is not a real heredoc start.

    Regression guard for a code-review-confirmed defect: `echo "usage: cmd <<PY"` used to
    be mistaken for a real heredoc redirect, which then swallowed the *next* genuine
    heredoc's start line and body into a bogus, unparseable match — silently dropping a
    real settings read (the exact wrong-deletion failure mode this ticket exists to close).
    """
    script = (
        'echo "usage: cmd <<PY"\n'
        "uv run python - <<'PY'\n"
        "from personal_agent.config import settings\n"
        "x = settings.debug\n"
        "PY\n"
        "echo done\n"
    )
    bodies = _extract_heredocs(script)
    assert len(bodies) == 1
    _, body = bodies[0]
    reads = {field for field, _ in collect_field_reads(ast.parse(body), frozenset({"debug"}))}
    assert "debug" in reads


def test_extract_heredocs_paired_quotes_before_marker_still_recognized() -> None:
    """Quoted args before the heredoc redirect don't confuse the quote-parity check.

    The real `run_embedder_benchmark.sh` shape — `uv run python - "$EMBEDDER" "$DIMS"
    <<'PY'` — has each quote opened and closed before the redirect, so it's still
    recognized as a real heredoc start.
    """
    script = 'uv run python - "$EMBEDDER" "$DIMS" <<\'PY\'\nx = 1\nPY\n'
    bodies = _extract_heredocs(script)
    assert len(bodies) == 1


def test_extract_heredocs_terminator_requires_exact_match() -> None:
    """The terminator line must match the delimiter exactly — no `.strip()` leniency.

    Regression guard for a code-review-confirmed defect: an earlier version compared
    `.strip()` on both sides, so a merely-indented or trailing-whitespace line (which real
    Bash does NOT treat as a terminator — it keeps reading) ended the heredoc early,
    silently truncating (dropping) genuine reads past that point.
    """
    # Trailing whitespace after `PY` is not a real terminator in Bash — the heredoc must
    # keep reading through it to the real (exact) `PY` below.
    script = (
        "uv run python - <<'PY'\n"
        "from personal_agent.config import settings\n"
        "x = settings.debug\n"
        "PY  \n"  # not a real terminator (trailing whitespace)
        "y = settings.neo4j_uri\n"
        "PY\n"
    )
    _, body = _extract_heredocs(script)[0]
    reads = {
        field
        for field, _ in collect_field_reads(ast.parse(body), frozenset({"debug", "neo4j_uri"}))
    }
    assert reads == {"debug", "neo4j_uri"}


def test_extract_heredocs_field_read_only_inside_heredoc_resolves() -> None:
    """AC (FRE-907): a field read ONLY via a `.sh`-embedded heredoc is not `never-read`.

    Proves the end-to-end mechanism (extraction + AST parse + alias-aware read
    detection) on a synthetic script, independent of `_ast_reads_by_field`'s process-wide
    `lru_cache` (which can't be redirected to a fixture without leaking across other
    tests — see the real-file integration test below for that wiring proof).
    """
    script = "uv run python - <<'PY'\nfrom personal_agent.config import settings\nprint(settings.neo4j_uri)\nPY\n"
    _, body = _extract_heredocs(script)[0]
    reads = {field for field, _ in collect_field_reads(ast.parse(body), frozenset({"neo4j_uri"}))}
    assert "neo4j_uri" in reads


def test_sh_embedded_reads_wired_into_scripts_root_evidence() -> None:
    """`.sh`-embedded heredoc reads are wired into real `scripts`-root evidence.

    Ties to a production file FRE-907 named: `embedding_dimensions` is read inside
    a `uv run python - <<'PY'` heredoc there. Asserts the exact `file:line`
    citation, not just presence, to prove the line-padding (`_extract_heredocs`'s
    blank-line prefix) actually lands on the real source line.

    FRE-916 phase 2 deleted `run_embedder_benchmark.sh` — it existed only to
    select the benchmark catalogs via the retired `AGENT_MODEL_CONFIG_PATH`, so it
    went with them. It carried this test's only `neo4j_uri` heredoc citation, so
    that half is retired rather than re-pointed at a file that does not read it.
    """
    dims_hits = external_reads("embedding_dimensions").get("scripts", [])
    assert "scripts/eval/fre817_corpus_ab_embedder/run_corpus_ab.sh:31" in dims_hits
    assert "scripts/eval/fre817_corpus_ab_embedder/run_corpus_ab.sh:33" in dims_hits


def test_generate_report_describes_ast_scan_not_git_grep() -> None:
    """The Methodology section describes the actual AST alias-aware scan (FRE-907).

    Regression guard for the stale-prose bug: an earlier version of `generate_report()`
    still described the retired `git grep` mechanism, even though FRE-896 had already
    replaced it with the AST scan in `settings_reads.py`.
    """
    report = generate_report(audit_all())
    assert "git grep" not in report
    assert "AST alias-aware scan" in report
    assert ".sh" in report


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
