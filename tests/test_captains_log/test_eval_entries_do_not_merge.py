"""FRE-1608 AC-4: an eval reflection changes no production proposal.

``CaptainLogManager.save_entry`` merges an entry into any file with the same
fingerprint and raises that file's ``seen_count``, which is the number promotion
reads. Before FRE-1608 an eval sighting merged into a production file, and a
production sighting merged into an eval file that promotion never reads.

The tests write only to ``tmp_path``.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import patch

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


def _entry(*, eval_mode: bool, entry_id: str = "") -> CaptainLogEntry:
    category, scope = ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT
    return CaptainLogEntry(
        entry_id=entry_id,
        type=CaptainLogEntryType.REFLECTION,
        title="Task: test",
        rationale="Test rationale",
        proposed_change=ProposedChange(
            what="Add retry logic",
            why="Improves reliability",
            how="Wrap calls in tenacity",
            category=category,
            scope=scope,
            fingerprint=compute_proposal_fingerprint(category, scope, "Add retry logic"),
            first_seen=datetime(2026, 10, 1, tzinfo=timezone.utc),
            source=ProposalSource.REFLECTION,
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        eval_mode=eval_mode,
    )


def _seen_count(path: pathlib.Path) -> int:
    return int(json.loads(path.read_text())["proposed_change"]["seen_count"])


def test_an_eval_sighting_leaves_the_production_entry_unchanged(tmp_path: pathlib.Path) -> None:
    manager = CaptainLogManager(log_dir=tmp_path / "captains_log")

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        production = manager.save_entry(_entry(eval_mode=False))
        before = production.read_text()
        eval_path = manager.save_entry(_entry(eval_mode=True, entry_id="CL-eval-001"))

    assert eval_path != production
    assert production.read_text() == before
    assert _seen_count(production) == 1
    assert json.loads(eval_path.read_text())["eval_mode"] is True


def test_a_production_sighting_does_not_vanish_into_an_eval_entry(tmp_path: pathlib.Path) -> None:
    manager = CaptainLogManager(log_dir=tmp_path / "captains_log")

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        eval_path = manager.save_entry(_entry(eval_mode=True))
        production = manager.save_entry(_entry(eval_mode=False, entry_id="CL-prod-001"))

    assert production != eval_path
    assert json.loads(production.read_text())["eval_mode"] is False
    assert _seen_count(eval_path) == 1


def test_same_provenance_still_merges(tmp_path: pathlib.Path) -> None:
    """Seeded positive: deduplication is unchanged inside one provenance."""
    manager = CaptainLogManager(log_dir=tmp_path / "captains_log")

    with patch("personal_agent.captains_log.manager.schedule_es_index"):
        first = manager.save_entry(_entry(eval_mode=False))
        second = manager.save_entry(_entry(eval_mode=False, entry_id="CL-dup-001"))
        eval_first = manager.save_entry(_entry(eval_mode=True, entry_id="CL-eval-001"))
        eval_second = manager.save_entry(_entry(eval_mode=True, entry_id="CL-eval-002"))

    assert first == second
    assert _seen_count(first) == 2
    assert eval_first == eval_second
    assert _seen_count(eval_first) == 2
