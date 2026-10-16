"""FRE-1612: `filed_at` lives on the entry, so a re-sighting does not erase it.

A repeated proposal merges into its stored entry, and the merge re-indexes the whole
reflection document under the same id. A field that only the ES document held would
vanish there, and the latency panel would lose the ticket.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

_FILED_AT = "2026-10-16T10:00:00+00:00"


def _entry(entry_id: str = "CL-20260901-000001") -> CaptainLogEntry:
    return CaptainLogEntry(
        entry_id=entry_id,
        timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title="t",
        rationale="r",
        proposed_change=ProposedChange(
            what="w",
            why="y",
            how="h",
            category=ChangeCategory.RELIABILITY,
            scope=ChangeScope.LLM_CLIENT,
            source=ProposalSource.REFLECTION,
            fingerprint="fp-merge",
            seen_count=5,
        ),
        status=CaptainLogStatus.APPROVED,
        linear_issue_id="FRE-9001",
    )


def test_merge_reindexes_the_document_with_filed_at(tmp_path: pathlib.Path) -> None:
    """The stored entry carries filed_at, so the re-indexed ES document does too."""
    stored = _entry()
    path = tmp_path / f"{stored.entry_id}-t.json"
    data = json.loads(stored.model_dump_json())
    data["filed_at"] = _FILED_AT
    path.write_text(json.dumps(data), encoding="utf-8")

    indexed: list[dict[str, Any]] = []
    manager = CaptainLogManager(log_dir=tmp_path)
    with patch(
        "personal_agent.captains_log.manager.schedule_es_index",
        side_effect=lambda index, doc, **kw: indexed.append(doc),
    ):
        manager._merge_into_existing(path, _entry("CL-20260902-000002"))

    assert len(indexed) == 1
    # Pydantic writes a UTC time as "...Z". It is the same instant.
    assert datetime.fromisoformat(indexed[0]["filed_at"].replace("Z", "+00:00")) == (
        datetime.fromisoformat(_FILED_AT)
    )
    assert indexed[0]["linear_issue_id"] == "FRE-9001"


@pytest.mark.parametrize("linked", [False, True], ids=["fresh-create", "link"])
def test_mark_promoted_writes_filed_at_for_a_fresh_create_only(
    tmp_path: pathlib.Path, linked: bool
) -> None:
    """A fresh create gets filed_at on disk. A link to an existing ticket gets none."""
    entry = _entry()
    entry.status = CaptainLogStatus.AWAITING_APPROVAL
    entry.linear_issue_id = None
    path = tmp_path / f"{entry.entry_id}-t.json"
    path.write_text(entry.model_dump_json(), encoding="utf-8")

    pipeline = PromotionPipeline(log_dir=tmp_path, es_handler=MagicMock(_connected=False))
    promoted: list[dict[str, str]] = []
    with (
        patch.object(PromotionPipeline, "_record_sysgraph_linkage", AsyncMock()),
        patch.object(PromotionPipeline, "_stamp_reflection_linkage", AsyncMock()) as stamp,
    ):
        import asyncio

        asyncio.run(pipeline._finalize_promotion(entry, "FRE-9001", promoted, linked=linked))

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "approved"
    if linked:
        assert on_disk.get("filed_at") is None
        assert stamp.await_args.kwargs["filed_at"] is None
    else:
        assert datetime.fromisoformat(on_disk["filed_at"]).utcoffset().total_seconds() == 0
        # The ES stamp and the disk write carry the same time.
        assert stamp.await_args.kwargs["filed_at"] == on_disk["filed_at"]
