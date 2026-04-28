"""Tests for scripts/cleanup_eval_data.py (FRE-277)."""

import json
import os
import pathlib
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow import of the script as a module
import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import cleanup_eval_data as cud


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_results_json(tmp_path: pathlib.Path, run_id: str, count: int = 3) -> pathlib.Path:
    """Write a minimal results.json with deterministic UUIDs."""
    results = []
    # Build a stable 4-char hex prefix from run_id
    prefix = format(hash(run_id) & 0xFFFF, "04x")
    for i in range(count):
        results.append({
            "id": f"prompt-{i}",
            "control": {
                "session_id": f"00000000-0000-4000-{prefix}-{i:012x}",
                "trace_id":   f"11111111-0000-4000-{prefix}-{i:012x}",
                "status": 200,
            },
            "treatment": {
                "session_id": f"22222222-0000-4000-{prefix}-{i:012x}",
                "trace_id":   f"33333333-0000-4000-{prefix}-{i:012x}",
                "status": 200,
            },
        })
    p = tmp_path / f"run-{run_id}" / "results.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(results))
    return p


# ── Unit: parse_results_files ────────────────────────────────────────────────


def test_parse_extracts_session_and_trace_ids(tmp_path):
    p = make_results_json(tmp_path, "aa", count=2)
    sessions, traces = cud.parse_results_files([p])
    assert len(sessions) == 4  # 2 ctrl + 2 trt session_ids
    assert len(traces) == 4    # 2 ctrl + 2 trt trace_ids


def test_parse_deduplicates_across_files(tmp_path):
    p1 = make_results_json(tmp_path, "bb", count=2)
    # Parse the same file twice — should deduplicate
    sessions, traces = cud.parse_results_files([p1, p1])
    assert len(sessions) == 4  # deduped (same 4 IDs, not 8)


def test_parse_skips_missing_ids(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps([{"id": "x", "control": {"status": 200}, "treatment": {"status": 200}}]))
    sessions, traces = cud.parse_results_files([p])
    assert sessions == set()
    assert traces == set()


def test_parse_multiple_files(tmp_path):
    p1 = make_results_json(tmp_path, "cc", count=1)
    p2 = make_results_json(tmp_path, "dd", count=1)
    sessions, traces = cud.parse_results_files([p1, p2])
    assert len(sessions) == 4
    assert len(traces) == 4


# ── Unit: archive_capture_files ──────────────────────────────────────────────


def test_archive_moves_matching_files(tmp_path):
    captures_dir = tmp_path / "captures" / "2026-04-28"
    captures_dir.mkdir(parents=True)
    archive_dir = tmp_path / "archive"

    trace1 = "aaaaaaaa-0000-0000-0000-000000000001"
    trace2 = "bbbbbbbb-0000-0000-0000-000000000002"

    (captures_dir / f"{trace1}.json").write_text('{"trace_id": "' + trace1 + '"}')
    (captures_dir / f"{trace2}.json").write_text('{"trace_id": "' + trace2 + '"}')
    (captures_dir / "unrelated.json").write_text('{"trace_id": "other"}')

    moved = cud.archive_capture_files(
        trace_ids={trace1},
        captures_root=tmp_path / "captures",
        archive_root=archive_dir,
        dry_run=False,
    )

    assert moved == 1
    assert not (captures_dir / f"{trace1}.json").exists()
    assert (captures_dir / f"{trace2}.json").exists()  # untouched
    assert (captures_dir / "unrelated.json").exists()   # untouched
    assert list(archive_dir.rglob("*.json"))[0].name == f"{trace1}.json"


def test_archive_dry_run_does_not_move(tmp_path):
    captures_dir = tmp_path / "captures" / "2026-04-28"
    captures_dir.mkdir(parents=True)
    trace1 = "cccccccc-0000-0000-0000-000000000003"
    (captures_dir / f"{trace1}.json").write_text("{}")

    moved = cud.archive_capture_files(
        trace_ids={trace1},
        captures_root=tmp_path / "captures",
        archive_root=tmp_path / "archive",
        dry_run=True,
    )

    assert moved == 1
    assert (captures_dir / f"{trace1}.json").exists()  # not moved in dry-run


def test_archive_returns_zero_for_no_matches(tmp_path):
    (tmp_path / "captures").mkdir()
    moved = cud.archive_capture_files(
        trace_ids={"dddddddd-0000-0000-0000-000000000004"},
        captures_root=tmp_path / "captures",
        archive_root=tmp_path / "archive",
        dry_run=False,
    )
    assert moved == 0
