"""Unit tests for the FRE-1001 captains_log source backfill (ADR-0125 D1).

Filesystem-only (no ES/Postgres/Neo4j) -- exercises select_migration_targets and
run_migration against tmp_path fixtures shaped like real CL-*.json entries.
"""

from __future__ import annotations

import json
import pathlib
import stat

from scripts.migrate_fre1001_captains_log_source_backfill import (
    run_migration,
    select_migration_targets,
)

_SENTINEL = "legacy_unattributable"


def _write(log_dir: pathlib.Path, name: str, proposed_change: dict | None) -> pathlib.Path:
    data: dict[str, object] = {
        "entry_id": name,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "type": "reflection",
        "title": "Test",
        "rationale": "Test",
        "status": "awaiting_approval",
    }
    if proposed_change is not None:
        data["proposed_change"] = proposed_change
    path = log_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


class TestSelectMigrationTargets:
    """select_migration_targets is a pure scan -- never writes."""

    def test_missing_source_key_is_a_target(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        targets = select_migration_targets(tmp_path)
        assert [t.name for t in targets] == ["CL-a.json"]

    def test_explicit_null_source_is_a_target(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z", "source": None})
        targets = select_migration_targets(tmp_path)
        assert [t.name for t in targets] == ["CL-a.json"]

    def test_valid_source_is_not_a_target(self, tmp_path: pathlib.Path) -> None:
        _write(
            tmp_path,
            "CL-a",
            {"what": "x", "why": "y", "how": "z", "source": "statistical_detector"},
        )
        assert select_migration_targets(tmp_path) == []

    def test_no_proposed_change_is_not_a_target(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", None)
        assert select_migration_targets(tmp_path) == []

    def test_scan_never_writes(self, tmp_path: pathlib.Path) -> None:
        path = _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        before = path.read_text()
        select_migration_targets(tmp_path)
        assert path.read_text() == before


class TestRunMigration:
    """run_migration classifies + (unless dry_run) writes."""

    def test_migrates_missing_source(self, tmp_path: pathlib.Path) -> None:
        path = _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        report = run_migration(tmp_path, dry_run=False)
        assert report.migrated == ["CL-a.json"]
        assert report.missing_or_null == 1
        data = json.loads(path.read_text())
        assert data["proposed_change"]["source"] == _SENTINEL

    def test_migrates_explicit_null_source(self, tmp_path: pathlib.Path) -> None:
        path = _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z", "source": None})
        run_migration(tmp_path, dry_run=False)
        data = json.loads(path.read_text())
        assert data["proposed_change"]["source"] == _SENTINEL

    def test_valid_source_untouched_byte_for_byte(self, tmp_path: pathlib.Path) -> None:
        path = _write(
            tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z", "source": "reflection"}
        )
        before = path.read_text()
        report = run_migration(tmp_path, dry_run=False)
        assert path.read_text() == before
        assert report.already_valid == 1
        assert report.migrated == []

    def test_no_proposed_change_is_not_applicable(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", None)
        report = run_migration(tmp_path, dry_run=False)
        assert report.not_applicable == 1
        assert report.migrated == []

    def test_malformed_json_is_reported_failed_not_crashed(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "CL-bad.json").write_text("{not valid json")
        report = run_migration(tmp_path, dry_run=False)
        assert report.failed == ["CL-bad.json"]
        assert report.success is False

    def test_proposed_change_not_a_dict_is_not_applicable(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", None)
        data = json.loads((tmp_path / "CL-a.json").read_text())
        data["proposed_change"] = "not a dict"
        (tmp_path / "CL-a.json").write_text(json.dumps(data))
        report = run_migration(tmp_path, dry_run=False)
        assert report.not_applicable == 1

    def test_top_level_json_not_an_object_is_not_applicable_not_crashed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A syntactically-valid, non-object top level (list/str/int/null/bool) is not

        a JSONDecodeError -- must classify as not_applicable rather than raise
        AttributeError from `.get()` on a non-dict.
        """
        (tmp_path / "CL-list.json").write_text(json.dumps(["not", "an", "object"]))
        (tmp_path / "CL-str.json").write_text(json.dumps("just a string"))
        (tmp_path / "CL-null.json").write_text(json.dumps(None))
        report = run_migration(tmp_path, dry_run=False)
        assert report.scanned == 3
        assert report.not_applicable == 3
        assert report.failed == []
        assert report.migrated == []

    def test_unhashable_source_value_is_invalid_not_crashed(self, tmp_path: pathlib.Path) -> None:
        """A dict/list `source` value must not raise TypeError from set membership."""
        _write(tmp_path, "CL-a", None)
        data = json.loads((tmp_path / "CL-a.json").read_text())
        data["proposed_change"] = {"what": "x", "why": "y", "how": "z", "source": {"nested": True}}
        (tmp_path / "CL-a.json").write_text(json.dumps(data))
        report = run_migration(tmp_path, dry_run=False)
        assert report.invalid == ["CL-a.json"]
        assert report.migrated == []

    def test_invalid_non_null_source_is_reported_not_stamped(self, tmp_path: pathlib.Path) -> None:
        path = _write(
            tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z", "source": "made_up_producer"}
        )
        before = path.read_text()
        report = run_migration(tmp_path, dry_run=False)
        assert path.read_text() == before
        assert report.invalid == ["CL-a.json"]
        assert report.success is False

    def test_dry_run_writes_nothing(self, tmp_path: pathlib.Path) -> None:
        path = _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        before = path.read_text()
        report = run_migration(tmp_path, dry_run=True)
        assert path.read_text() == before
        assert report.missing_or_null == 1
        assert report.migrated == ["CL-a.json"]

    def test_second_run_is_idempotent(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        first = run_migration(tmp_path, dry_run=False)
        assert first.missing_or_null == 1
        second = run_migration(tmp_path, dry_run=False)
        assert second.missing_or_null == 0
        assert second.already_valid == 1
        assert second.migrated == []

    def test_mixed_directory_reports_all_buckets_independently(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "CL-missing", {"what": "x", "why": "y", "how": "z"})
        _write(tmp_path, "CL-valid", {"what": "x", "why": "y", "how": "z", "source": "reflection"})
        _write(tmp_path, "CL-noproposal", None)
        _write(tmp_path, "CL-invalid", {"what": "x", "why": "y", "how": "z", "source": "bogus"})
        (tmp_path / "CL-broken.json").write_text("{bad")

        report = run_migration(tmp_path, dry_run=False)
        assert report.scanned == 5
        assert report.missing_or_null == 1
        assert report.already_valid == 1
        assert report.not_applicable == 1
        assert report.invalid == ["CL-invalid.json"]
        assert report.failed == ["CL-broken.json"]
        assert report.success is False

    def test_success_is_true_only_when_no_invalid_or_failed_remain(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "CL-missing", {"what": "x", "why": "y", "how": "z"})
        report = run_migration(tmp_path, dry_run=False)
        assert report.success is True

    def test_atomic_write_leaves_no_temp_file_behind(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        run_migration(tmp_path, dry_run=False)
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_atomic_write_preserves_original_file_permissions(self, tmp_path: pathlib.Path) -> None:
        """tempfile.mkstemp always creates 0600 -- the rename-over-original must not

        silently narrow a 0644 file to owner-only.
        """
        path = _write(tmp_path, "CL-a", {"what": "x", "why": "y", "how": "z"})
        path.chmod(0o644)
        run_migration(tmp_path, dry_run=False)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
