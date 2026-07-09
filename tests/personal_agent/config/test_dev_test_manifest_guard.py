"""Tests for check_dev_test_profile_isolation (ADR-0112 AC-9 guard, FRE-820).

The machine-checkable half of AC-9's "no live paid endpoint call": the dev/test
profiles in config/substrate.yaml must declare every component `kind: local`,
and must not source a `managed_*`-prefixed AppConfig field even when a row
falsely claims `kind: local` (a manifest could otherwise "lie" and pass a
kind-only check while still resolving to a paid endpoint).
"""

from __future__ import annotations

from pathlib import Path

from personal_agent.config.config_guard import check_dev_test_profile_isolation, repo_root

_LOCAL_ROWS = """\
profiles:
  test:
    postgres:      {{ kind: local, source: "setting:database_url" }}
    neo4j:         {{ kind: local, source: "setting:neo4j_uri" }}
    elasticsearch: {{ kind: local, source: "setting:elasticsearch_url" }}
    embedder:      {{ kind: local, source: "model_endpoint:embedding" }}
    reranker:      {{ kind: local, source: "model_endpoint:reranker" }}
    slm:           {{ kind: local, source: "setting:llm_base_url" }}
{extra}
"""


def _write_manifest(root: Path, body: str) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "substrate.yaml").write_text(body, encoding="utf-8")


class TestRealManifest:
    def test_real_substrate_manifest_has_no_findings(self) -> None:
        # The committed config/substrate.yaml's dev/test profiles are local-only.
        assert check_dev_test_profile_isolation(repo_root()) == []


class TestMissingManifestOrProfile:
    def test_missing_manifest_yields_no_findings(self, tmp_path: Path) -> None:
        # A fixture/test root without config/substrate.yaml is legitimately empty.
        assert check_dev_test_profile_isolation(tmp_path) == []

    def test_manifest_without_dev_test_profiles_yields_no_findings(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            'profiles:\n  private:\n    postgres: { kind: local, source: "setting:database_url" }\n',
        )
        assert check_dev_test_profile_isolation(tmp_path) == []


class TestManagedKindIsFlagged:
    def test_managed_kind_in_test_profile_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _LOCAL_ROWS.format(
                extra='    vector_index:  { kind: managed, source: "backed_by:neo4j" }'
            ),
        )
        findings = check_dev_test_profile_isolation(tmp_path)
        assert any(
            f.check == "dev_test_profile_not_local" and "vector_index" in f.message
            for f in findings
        )

    def test_local_only_rows_are_not_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _LOCAL_ROWS.format(
                extra='    vector_index:  { kind: local, source: "backed_by:neo4j" }'
            ),
        )
        assert check_dev_test_profile_isolation(tmp_path) == []


class TestLyingLocalKindIsFlagged:
    """A row claiming kind: local but sourcing a managed_* field must still fail."""

    def test_local_kind_with_managed_source_is_flagged(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            _LOCAL_ROWS.format(
                extra='    vector_index:  { kind: local, source: "setting:managed_embedding_endpoint" }'
            ),
        )
        findings = check_dev_test_profile_isolation(tmp_path)
        assert any(
            f.check == "dev_test_profile_managed_source" and "vector_index" in f.message
            for f in findings
        )

    def test_dev_profile_same_lie_is_also_flagged(self, tmp_path: Path) -> None:
        body = _LOCAL_ROWS.replace("test:", "dev:").format(
            extra='    vector_index:  { kind: local, source: "setting:managed_database_url" }'
        )
        _write_manifest(tmp_path, body)
        findings = check_dev_test_profile_isolation(tmp_path)
        assert any(f.check == "dev_test_profile_managed_source" for f in findings)
