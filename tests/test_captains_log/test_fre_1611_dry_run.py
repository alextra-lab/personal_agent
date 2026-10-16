"""FRE-1611 AC-4 — a promotion dry run lists what would file and writes nothing.

The owner sees this list before the cap goes back to 10. A dry run that also created a
ticket, marked an entry promoted or wrote a funnel event would defeat its purpose, so
each test asserts the absence of those writes.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.captains_log.promotion as promotion_module
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline


def _write(log_dir: pathlib.Path, n: int, *, scope: ChangeScope = ChangeScope.LLM_CLIENT) -> str:
    """Write one promotable entry and return its id."""
    entry_id = f"CL-20260901-00000{n}"
    entry = CaptainLogEntry(
        entry_id=entry_id,
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title=f"Task {n}",
        rationale="Corroborated.",
        proposed_change=ProposedChange(
            what=f"Reduce retries in path {n}",
            why=f"Retries doubled latency on path {n}.",
            how="Cap the retries at two.",
            category=ChangeCategory.RELIABILITY,
            scope=scope,
            source=ProposalSource.STATISTICAL_DETECTOR,
            fingerprint=f"fp-dry-{n}",
            seen_count=5,
            first_seen=datetime.now(timezone.utc) - timedelta(days=14),
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[],
    )
    (log_dir / f"{entry_id}-reflection.json").write_text(
        entry.model_dump_json(indent=2), encoding="utf-8"
    )
    return entry_id


def _client(open_pipeline_issues: int, existing: list[dict] | None = None) -> MagicMock:
    lc = MagicMock()
    lc.count_open_pipeline_issues = AsyncMock(return_value=open_pipeline_issues)
    lc.list_issues = AsyncMock(return_value=existing or [])
    lc.resolve_project_id = AsyncMock(return_value="project-uuid")
    return lc


def _pipeline(
    log_dir: pathlib.Path,
    lc: MagicMock,
    create: AsyncMock,
    es_handler: MagicMock | None = None,
) -> PromotionPipeline:
    return PromotionPipeline(
        log_dir=log_dir,
        criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
        create_issue_fn=create,
        linear_client=lc,
        es_handler=es_handler,
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_dry_run_lists_the_entry_with_its_premise_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """The planned row carries the premise. No ticket, no status change, no event."""
    entry_id = _write(tmp_path, 1)
    create = AsyncMock(return_value="FRE-1")
    es_handler = MagicMock()
    es_handler._connected = True
    es_handler.es_logger.index_document = AsyncMock()
    pipeline = _pipeline(tmp_path, _client(0), create, es_handler)

    with patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10):
        promoted = await pipeline.run()

    assert promoted == []
    assert len(pipeline.planned) == 1
    row = pipeline.planned[0]
    assert row["action"] == "file"
    assert row["entry_id"] == entry_id
    assert row["title"] == "[reliability] Reduce retries in path 1"
    assert row["why"] == "Retries doubled latency on path 1."
    assert row["seen_count"] == "5"
    create.assert_not_awaited()
    es_handler.es_logger.index_document.assert_not_awaited()
    on_disk = (tmp_path / f"{entry_id}-reflection.json").read_text(encoding="utf-8")
    assert '"status": "awaiting_approval"' in on_disk
    assert '"linear_issue_id": null' in on_disk


@pytest.mark.asyncio
async def test_dry_run_plans_no_more_than_the_headroom(tmp_path: pathlib.Path) -> None:
    """9 of 10 open leaves one slot. Two eligible entries plan one."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    _write(tmp_path, 2, scope=ChangeScope.ORCHESTRATOR)
    pipeline = _pipeline(tmp_path, _client(9), AsyncMock())

    with patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10):
        await pipeline.run()

    assert len(pipeline.planned) == 1


@pytest.mark.asyncio
async def test_dry_run_at_the_cap_plans_nothing_and_writes_no_funnel_event(
    tmp_path: pathlib.Path,
) -> None:
    """At the cap the real run refuses and indexes a throttle event. The dry run does neither write."""
    _write(tmp_path, 1)
    es_handler = MagicMock()
    es_handler._connected = True
    es_handler.es_logger.index_document = AsyncMock()
    pipeline = _pipeline(tmp_path, _client(10), AsyncMock(), es_handler)

    with patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10):
        await pipeline.run()

    assert pipeline.planned == []
    es_handler.es_logger.index_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_reports_a_link_to_an_existing_ticket_without_linking(
    tmp_path: pathlib.Path,
) -> None:
    """A fingerprint match is planned as a link. The entry is not marked promoted."""
    entry_id = _write(tmp_path, 1)
    existing = [{"identifier": "FRE-77", "description": "fingerprint: fp-dry-1"}]
    create = AsyncMock()
    pipeline = _pipeline(tmp_path, _client(0, existing), create)

    with (
        patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
        patch.object(
            PromotionPipeline,
            "_existing_linear_issue_for_fingerprint",
            AsyncMock(return_value="FRE-77"),
        ),
    ):
        await pipeline.run()

    assert [r["action"] for r in pipeline.planned] == ["link"]
    assert pipeline.planned[0]["existing_issue"] == "FRE-77"
    create.assert_not_awaited()
    on_disk = (tmp_path / f"{entry_id}-reflection.json").read_text(encoding="utf-8")
    assert '"linear_issue_id": null' in on_disk


def test_report_shows_the_headroom_and_each_premise() -> None:
    """The owner reads the premise (why, seen count, first seen) beside each title."""
    from scripts.fre1611_promotion_dry_run import render

    row = {
        "action": "file",
        "entry_id": "CL-1",
        "title": "[reliability] Reduce retries",
        "priority": "4",
        "seen_count": "5",
        "first_seen": "2026-09-01T00:00:00+00:00",
        "source": "statistical_detector",
        "category": "reliability",
        "scope": "llm_client",
        "fingerprint": "fp-1",
        "existing_issue": "",
        "what": "Reduce retries",
        "why": "Retries doubled latency.",
        "how": "Cap at two.",
    }
    report = render([row], cap=10, open_count=3)
    assert "nothing was written" in report
    assert "headroom=7" in report
    assert "FILE  [reliability] Reduce retries" in report
    assert "why: Retries doubled latency." in report
    assert "seen_count: 5" in report
    assert "existing_issue" not in report


@pytest.mark.asyncio
async def test_dry_run_creates_no_directory(tmp_path: pathlib.Path) -> None:
    """Reading the suppression file must not create ``telemetry/feedback_history``."""
    log_dir = tmp_path / "captains_log"
    log_dir.mkdir()
    _write(log_dir, 1)
    root = tmp_path / "root"
    root.mkdir()
    pipeline = _pipeline(log_dir, _client(0), AsyncMock())

    with (
        patch("personal_agent.captains_log.suppression._project_root", return_value=root),
        patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
    ):
        await pipeline.run()

    assert len(pipeline.planned) == 1
    assert not (root / "telemetry").exists()
