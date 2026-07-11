"""Tests for the shared frozen-artifact JSON writer (FRE-841).

Mirrors `export_snapshot.py`'s `build_manifest`/`compute_content_hash` shape:
a stamped `generated_at` + `content_hash` (sha256 over the payload, excluding
the hash field itself), written pretty-printed and sorted-keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.study.eval_artifacts.freeze import compute_content_hash, freeze_json_artifact


def test_freeze_json_artifact_stamps_generated_at_and_content_hash(tmp_path: Path) -> None:
    payload = {"foo": "bar", "count": 3}
    generated_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    out_path = tmp_path / "artifact.json"

    result = freeze_json_artifact(payload, out_path, generated_at=generated_at)

    assert result["generated_at"] == "2026-07-10T12:00:00+00:00"
    assert "content_hash" in result
    assert result["foo"] == "bar"
    assert result["count"] == 3


def test_freeze_json_artifact_writes_sorted_pretty_json(tmp_path: Path) -> None:
    payload = {"z": 1, "a": 2}
    generated_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    out_path = tmp_path / "artifact.json"

    freeze_json_artifact(payload, out_path, generated_at=generated_at)

    on_disk = json.loads(out_path.read_text())
    assert on_disk["a"] == 2
    assert on_disk["z"] == 1
    # sorted-keys: "a" (and every key) sorts before "z" in the raw text
    text = out_path.read_text()
    assert text.index('"a"') < text.index('"z"')


def test_freeze_json_artifact_creates_parent_dirs(tmp_path: Path) -> None:
    out_path = tmp_path / "nested" / "dir" / "artifact.json"
    generated_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

    freeze_json_artifact({"a": 1}, out_path, generated_at=generated_at)

    assert out_path.exists()


def test_content_hash_is_stable_for_same_payload() -> None:
    payload_a = {"a": 1, "b": [1, 2, 3]}
    payload_b = {"b": [1, 2, 3], "a": 1}

    assert compute_content_hash(payload_a) == compute_content_hash(payload_b)


def test_content_hash_changes_for_different_payload() -> None:
    assert compute_content_hash({"a": 1}) != compute_content_hash({"a": 2})


def test_content_hash_excludes_hash_field_itself() -> None:
    payload = {"a": 1}
    h1 = compute_content_hash(payload)
    payload_with_hash = {"a": 1, "content_hash": "whatever-was-here-before"}

    assert compute_content_hash(payload_with_hash) == h1


def test_freeze_json_artifact_content_hash_is_self_consistent(tmp_path: Path) -> None:
    """The stamped `content_hash` must match recomputing the hash over the
    same payload (minus generated_at/content_hash) — a reader can verify the
    artifact wasn't silently edited after freezing.
    """
    payload = {"foo": "bar"}
    generated_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

    result = freeze_json_artifact(payload, tmp_path / "a.json", generated_at=generated_at)

    verification_payload = {k: v for k, v in result.items() if k != "content_hash"}
    assert compute_content_hash(verification_payload) == result["content_hash"]
