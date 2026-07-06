"""Tests for check_substrate_manifest (ADR-0112 AC-2 guard, FRE-816).

The machine-checkable half of AC-2: a D3 component omitted from the seam, or a
malformed/dangling ``source`` reference, is a CI/pre-commit finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config.config_guard import (
    REQUIRED_SUBSTRATE_COMPONENTS,
    check_substrate_manifest,
    repo_root,
)

_ALL_SETTING_ROWS = """\
profiles:
  private:
    postgres:      {{ kind: local, source: "setting:database_url" }}
    neo4j:         {{ kind: local, source: "setting:neo4j_uri" }}
    elasticsearch: {{ kind: local, source: "setting:elasticsearch_url" }}
    embedder:      {{ kind: local, source: "setting:llm_base_url" }}
    reranker:      {{ kind: local, source: "setting:llm_base_url" }}
    slm:           {{ kind: local, source: "setting:llm_base_url" }}
{extra}
"""


def _write_manifest(root: Path, body: str) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "substrate.yaml").write_text(body, encoding="utf-8")


class TestRealManifest:
    def test_real_substrate_manifest_has_no_findings(self) -> None:
        # The committed config/substrate.yaml is complete and well-formed.
        assert check_substrate_manifest(repo_root()) == []


class TestMissingComponent:
    def test_missing_vector_index_is_flagged(self, tmp_path: Path) -> None:
        # ADR-0112 AC-2 explicitly: omitting the search/vector index must fail.
        _write_manifest(tmp_path, _ALL_SETTING_ROWS.format(extra="    # vector_index omitted"))
        findings = check_substrate_manifest(tmp_path)
        assert any(
            f.check == "substrate_component_missing" and "vector_index" in f.message
            for f in findings
        )

    def test_all_missing_components_reported(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            'profiles:\n  private:\n    postgres: { kind: local, source: "setting:database_url" }\n',
        )
        findings = check_substrate_manifest(tmp_path)
        missing = {
            f.message.split("'")[3] for f in findings if f.check == "substrate_component_missing"
        }
        assert missing == set(REQUIRED_SUBSTRATE_COMPONENTS) - {"postgres"}


class TestDanglingSource:
    def test_unknown_setting_field_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _ALL_SETTING_ROWS.format(
                extra='    vector_index:  { kind: local, source: "setting:no_such_field" }'
            ),
        )
        findings = check_substrate_manifest(tmp_path)
        assert any(
            f.check == "substrate_source_dangling" and "no_such_field" in f.message
            for f in findings
        )

    def test_unknown_backed_by_component_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _ALL_SETTING_ROWS.format(
                extra='    vector_index:  { kind: local, source: "backed_by:nope" }'
            ),
        )
        findings = check_substrate_manifest(tmp_path)
        assert any(f.check == "substrate_source_dangling" and "nope" in f.message for f in findings)

    def test_valid_backed_by_is_not_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _ALL_SETTING_ROWS.format(
                extra='    vector_index:  { kind: local, source: "backed_by:neo4j" }'
            ),
        )
        assert check_substrate_manifest(tmp_path) == []


class TestMalformedRows:
    def test_invalid_kind_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _ALL_SETTING_ROWS.format(
                extra='    vector_index:  { kind: bogus, source: "backed_by:neo4j" }'
            ),
        )
        findings = check_substrate_manifest(tmp_path)
        assert any(f.check == "substrate_manifest_shape" for f in findings)

    def test_malformed_source_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _ALL_SETTING_ROWS.format(extra="    vector_index:  { kind: local, source: nocolon }"),
        )
        findings = check_substrate_manifest(tmp_path)
        assert any(f.check == "substrate_manifest_shape" for f in findings)

    def test_missing_manifest_yields_no_findings(self, tmp_path: Path) -> None:
        # A fixture/test root without config/substrate.yaml is legitimately empty.
        assert check_substrate_manifest(tmp_path) == []
