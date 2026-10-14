"""FRE-1609 AC-3: write-time dedup and ``_mark_promoted`` see archived entries.

The lifecycle job gzips a CL file older than 14 days into
``<telemetry>/archive/captains_log_reflections/YYYY-MM/``. Dedup read only the live
directory, so a re-sighting of an archived idea created a fresh promotable entry:
``CL-20261003-084335`` carried FRE-1378's fingerprint after its source entry aged out.

The archive here is produced by the real ``DataLifecycleManager``, not a hand-built
copy, so the tests fail if the two layouts drift apart. The tests write only to
``tmp_path``.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from personal_agent.captains_log.archive import (
    find_archived,
    reflection_archive_dir,
    restore_archived,
)
from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.promotion import PromotionPipeline
from personal_agent.telemetry.lifecycle_manager import DataLifecycleManager

_FINGERPRINT = compute_proposal_fingerprint(
    ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
)


def _entry(*, entry_id: str = "", eval_mode: bool = False) -> CaptainLogEntry:
    return CaptainLogEntry(
        entry_id=entry_id,
        type=CaptainLogEntryType.REFLECTION,
        title="Task: test",
        rationale="Test rationale",
        proposed_change=ProposedChange(
            what="Add retry logic",
            why="Improves reliability",
            how="Wrap calls in tenacity",
            category=ChangeCategory.RELIABILITY,
            scope=ChangeScope.LLM_CLIENT,
            fingerprint=_FINGERPRINT,
            first_seen=datetime(2026, 9, 1, tzinfo=timezone.utc),
            source=ProposalSource.REFLECTION,
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        eval_mode=eval_mode,
    )


async def _archive_everything(telemetry_root: pathlib.Path) -> None:
    """Age every live CL file past the 14-day hot window and run the real archiver."""
    old = time.time() - 30 * 86400
    for path in (telemetry_root / "captains_log").glob("CL-*.json"):
        os.utime(path, (old, old))
    with patch(
        "personal_agent.telemetry.lifecycle_manager._telemetry_root", return_value=telemetry_root
    ):
        result = await DataLifecycleManager().archive_old_data("captains_log_reflections")
    assert result.archived_count >= 1, result.errors


def _live(log_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(log_dir.glob("CL-*.json"))


def _archived(telemetry_root: pathlib.Path) -> list[pathlib.Path]:
    return sorted((telemetry_root / "archive" / "captains_log_reflections").rglob("*.gz"))


@pytest.mark.asyncio
async def test_a_sighting_of_an_archived_promoted_entry_creates_no_new_entry(
    tmp_path: pathlib.Path,
) -> None:
    """The FRE-1378 case: the promoted entry aged out, then the idea was seen again."""
    log_dir = tmp_path / "captains_log"
    manager = CaptainLogManager(log_dir=log_dir)
    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        original = manager.save_entry(_entry(entry_id="CL-20260901-000000-orig"))
    data = json.loads(original.read_text())
    data["status"] = CaptainLogStatus.APPROVED.value
    data["linear_issue_id"] = "FRE-1378"
    original.write_text(json.dumps(data))
    await _archive_everything(tmp_path)
    assert _live(log_dir) == []

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        path = manager.save_entry(_entry(entry_id="CL-20261003-084335-resighting"))

    assert _live(log_dir) == [original]
    assert path == original
    assert _archived(tmp_path) == []
    restored = json.loads(original.read_text())
    assert restored["entry_id"] == "CL-20260901-000000-orig"
    assert restored["status"] == CaptainLogStatus.APPROVED.value
    assert restored["linear_issue_id"] == "FRE-1378"
    assert restored["proposed_change"]["seen_count"] == 2
    assert "CL-20261003-084335-resighting" in restored["proposed_change"]["related_entry_ids"]
    assert PromotionPipeline(log_dir=log_dir).scan_promotable_entries() == []


@pytest.mark.asyncio
async def test_a_sighting_of_an_archived_awaiting_entry_continues_that_entry(
    tmp_path: pathlib.Path,
) -> None:
    """An idea that went quiet keeps its history when it returns, as if never archived."""
    log_dir = tmp_path / "captains_log"
    manager = CaptainLogManager(log_dir=log_dir)
    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        original = manager.save_entry(_entry(entry_id="CL-20260901-000000-orig"))
    await _archive_everything(tmp_path)

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        manager.save_entry(_entry(entry_id="CL-20261003-000000-again"))

    assert _live(log_dir) == [original]
    restored = json.loads(original.read_text())
    assert restored["status"] == CaptainLogStatus.AWAITING_APPROVAL.value
    assert restored["proposed_change"]["seen_count"] == 2


@pytest.mark.asyncio
async def test_an_archived_entry_of_other_provenance_does_not_match(
    tmp_path: pathlib.Path,
) -> None:
    """FRE-1608's provenance rule holds in the archive too: eval never absorbs production."""
    log_dir = tmp_path / "captains_log"
    manager = CaptainLogManager(log_dir=log_dir)
    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        manager.save_entry(_entry(entry_id="CL-20260901-000000-eval", eval_mode=True))
    await _archive_everything(tmp_path)

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        path = manager.save_entry(_entry(entry_id="CL-20261003-000000-prod"))

    assert path is not None
    assert path.name.startswith("CL-20261003-000000-prod")
    assert len(_archived(tmp_path)) == 1


@pytest.mark.asyncio
async def test_mark_promoted_marks_an_entry_that_was_archived_after_the_scan(
    tmp_path: pathlib.Path,
) -> None:
    """A promoted entry that is not marked stays awaiting and re-promotes every run."""
    log_dir = tmp_path / "captains_log"
    manager = CaptainLogManager(log_dir=log_dir)
    entry = _entry(entry_id="CL-20260901-000000-orig")
    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        original = manager.save_entry(entry)
    await _archive_everything(tmp_path)

    marked = PromotionPipeline(log_dir=log_dir)._mark_promoted(entry, "FRE-9999")

    assert marked is True
    assert _archived(tmp_path) == []
    data = json.loads(original.read_text())
    assert data["status"] == CaptainLogStatus.APPROVED.value
    assert data["linear_issue_id"] == "FRE-9999"


def test_restore_never_overwrites_a_live_file(tmp_path: pathlib.Path) -> None:
    """A concurrent restore and merge already put the entry back: its live bytes win."""
    log_dir = tmp_path / "captains_log"
    log_dir.mkdir()
    month = reflection_archive_dir(log_dir) / "2026-09"
    month.mkdir(parents=True)
    gz_path = month / "CL-20260901-000000-orig.json.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        fh.write('{"stale": true}')
    live = log_dir / "CL-20260901-000000-orig.json"
    live.write_text('{"merged": true}')

    restored = restore_archived(gz_path, log_dir)

    assert restored == live
    assert json.loads(live.read_text()) == {"merged": True}
    assert not gz_path.exists()
    assert [p.name for p in log_dir.iterdir()] == [live.name]


def test_restore_after_a_concurrent_restore_removed_the_archive(tmp_path: pathlib.Path) -> None:
    """The second of two racing restores finds the .gz gone and the entry already live."""
    log_dir = tmp_path / "captains_log"
    log_dir.mkdir()
    gz_path = reflection_archive_dir(log_dir) / "2026-09" / "CL-20260901-000000-orig.json.gz"
    live = log_dir / "CL-20260901-000000-orig.json"
    live.write_text('{"merged": true}')

    assert restore_archived(gz_path, log_dir) == live
    assert json.loads(live.read_text()) == {"merged": True}


def test_a_corrupt_archive_file_is_skipped_not_fatal(tmp_path: pathlib.Path) -> None:
    """One unreadable .gz must not hide the valid entries behind it."""
    log_dir = tmp_path / "captains_log"
    log_dir.mkdir()
    month = reflection_archive_dir(log_dir) / "2026-09"
    month.mkdir(parents=True)
    (month / "CL-20260902-000000-corrupt.json.gz").write_bytes(b"not gzip")
    good = month / "CL-20260901-000000-good.json.gz"
    with gzip.open(good, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"entry_id": "CL-20260901-000000-good"}))

    found = find_archived(log_dir, lambda data: data.get("entry_id") == "CL-20260901-000000-good")

    assert found == good


def test_name_prefix_limits_the_files_read(tmp_path: pathlib.Path) -> None:
    """``_mark_promoted`` reads only its own entry's archive file."""
    log_dir = tmp_path / "captains_log"
    log_dir.mkdir()
    month = reflection_archive_dir(log_dir) / "2026-09"
    month.mkdir(parents=True)
    for name in ("CL-20260901-000000-a", "CL-20260901-000000-b"):
        with gzip.open(month / f"{name}-title.json.gz", "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"entry_id": name}))

    found = find_archived(log_dir, lambda data: True, name_prefix="CL-20260901-000000-a-")

    assert found is not None
    assert found.name == "CL-20260901-000000-a-title.json.gz"
