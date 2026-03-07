"""Tests for Captain's Log promotion pipeline (ADR-0030)."""

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposedChange,
)
from personal_agent.captains_log.promotion import (
    PromotionCriteria,
    PromotionPipeline,
    _format_linear_description,
    _map_seen_count_to_priority,
)


class TestPriorityMapping:
    """Test seen_count -> Linear priority mapping."""

    def test_high_priority_for_10_plus(self) -> None:
        """Test that seen_count 10+ maps to high priority."""
        assert _map_seen_count_to_priority(10) == 2
        assert _map_seen_count_to_priority(15) == 2

    def test_normal_priority_for_5_to_9(self) -> None:
        """Test that seen_count 5-9 maps to normal priority."""
        assert _map_seen_count_to_priority(5) == 3
        assert _map_seen_count_to_priority(9) == 3

    def test_low_priority_for_under_5(self) -> None:
        """Test that seen_count under 5 maps to low priority."""
        assert _map_seen_count_to_priority(3) == 4
        assert _map_seen_count_to_priority(4) == 4
        assert _map_seen_count_to_priority(1) == 4


class TestFormatLinearDescription:
    """Test Linear issue description formatting."""

    def test_includes_proposal_details(self) -> None:
        """Test that Linear description includes proposal details."""
        entry = CaptainLogEntry(
            entry_id="CL-test-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Test",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Use tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
                seen_count=5,
                first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            supporting_metrics=["cpu: 45%", "duration: 3.2s"],
        )
        desc = _format_linear_description(entry)
        assert "Add retry logic" in desc
        assert "Improves reliability" in desc
        assert "Use tenacity" in desc
        assert "reliability" in desc
        assert "llm_client" in desc
        assert "5" in desc
        assert "cpu: 45%" in desc

    def test_handles_no_proposed_change(self) -> None:
        """Test that Linear description handles entries without proposed_change."""
        entry = CaptainLogEntry(
            entry_id="CL-test-001",
            type=CaptainLogEntryType.OBSERVATION,
            title="Test",
            rationale="Test",
        )
        assert _format_linear_description(entry) == ""


def _write_entry(
    log_dir: pathlib.Path,
    entry_id: str = "CL-20260220-120000-001",
    what: str = "Add retry logic",
    seen_count: int = 5,
    first_seen: datetime | None = None,
    status: CaptainLogStatus = CaptainLogStatus.AWAITING_APPROVAL,
    category: ChangeCategory = ChangeCategory.RELIABILITY,
    scope: ChangeScope = ChangeScope.LLM_CLIENT,
    linear_issue_id: str | None = None,
) -> pathlib.Path:
    """Helper to write a CL entry JSON file to disk."""
    if first_seen is None:
        first_seen = datetime.now(timezone.utc) - timedelta(days=14)

    fp = compute_proposal_fingerprint(category, scope, what)
    data = {
        "entry_id": entry_id,
        "timestamp": "2026-02-20T12:00:00+00:00",
        "type": "reflection",
        "title": f"Task: {what[:30]}",
        "rationale": "Test rationale",
        "proposed_change": {
            "what": what,
            "why": "Test reason",
            "how": "Test method",
            "category": category.value,
            "scope": scope.value,
            "fingerprint": fp,
            "seen_count": seen_count,
            "first_seen": first_seen.isoformat(),
            "related_entry_ids": [],
        },
        "supporting_metrics": ["cpu: 45%"],
        "status": status.value,
        "linear_issue_id": linear_issue_id,
    }
    file_path = log_dir / f"{entry_id}-test.json"
    file_path.write_text(json.dumps(data, indent=2))
    return file_path


class TestScanPromotableEntries:
    """Test the scanning phase of the promotion pipeline."""

    def test_finds_qualifying_entries(self, tmp_path: pathlib.Path) -> None:
        """Test that scan finds qualifying entries."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, seen_count=5)

        pipeline = PromotionPipeline(log_dir=log_dir)
        entries = pipeline.scan_promotable_entries()
        assert len(entries) == 1
        assert entries[0].proposed_change is not None
        assert entries[0].proposed_change.seen_count == 5

    def test_skips_below_min_seen_count(self, tmp_path: pathlib.Path) -> None:
        """Test that scan skips entries below min seen_count."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, seen_count=1)

        pipeline = PromotionPipeline(log_dir=log_dir)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_skips_too_recent(self, tmp_path: pathlib.Path) -> None:
        """Test that scan skips entries that are too recent."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, seen_count=5, first_seen=datetime.now(timezone.utc))

        pipeline = PromotionPipeline(log_dir=log_dir)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_skips_already_approved(self, tmp_path: pathlib.Path) -> None:
        """Test that scan skips already approved entries."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, status=CaptainLogStatus.APPROVED)

        pipeline = PromotionPipeline(log_dir=log_dir)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_skips_already_promoted(self, tmp_path: pathlib.Path) -> None:
        """Test that scan skips already promoted entries."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, linear_issue_id="FRE-200")

        pipeline = PromotionPipeline(log_dir=log_dir)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_skips_excluded_categories(self, tmp_path: pathlib.Path) -> None:
        """Test that scan skips excluded categories."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, category=ChangeCategory.SAFETY)

        criteria = PromotionCriteria(excluded_categories=[ChangeCategory.SAFETY])
        pipeline = PromotionPipeline(log_dir=log_dir, criteria=criteria)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_custom_criteria(self, tmp_path: pathlib.Path) -> None:
        """Test that scan respects custom criteria."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, seen_count=2)

        criteria = PromotionCriteria(min_seen_count=2, min_age_days=0)
        pipeline = PromotionPipeline(log_dir=log_dir, criteria=criteria)
        assert len(pipeline.scan_promotable_entries()) == 1


class TestPromotionPipelineRun:
    """Test the full promotion pipeline execution."""

    @pytest.mark.asyncio
    async def test_dry_run_no_create_fn(self, tmp_path: pathlib.Path) -> None:
        """Without create_issue_fn, pipeline logs but doesn't promote."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, entry_id="CL-20260220-120000-001")

        pipeline = PromotionPipeline(log_dir=log_dir)
        promoted = await pipeline.run()
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_promotes_with_create_fn(self, tmp_path: pathlib.Path) -> None:
        """Test that pipeline promotes entries when create_issue_fn is provided."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        fp = _write_entry(log_dir, entry_id="CL-20260220-120000-001")

        async def mock_create(
            title: str,
            team: str,
            desc: str,
            priority: int,
            labels: list[str],
            state: str,
            project: str,
        ) -> str | None:
            return "FRE-999"

        pipeline = PromotionPipeline(log_dir=log_dir, create_issue_fn=mock_create)
        promoted = await pipeline.run()

        assert len(promoted) == 1
        assert promoted[0]["linear_issue_id"] == "FRE-999"

        data = json.loads(fp.read_text())
        assert data["status"] == "approved"
        assert data["linear_issue_id"] == "FRE-999"

    @pytest.mark.asyncio
    async def test_respects_max_issues_cap(self, tmp_path: pathlib.Path) -> None:
        """Test that pipeline respects max_issues cap."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        for i in range(5):
            _write_entry(
                log_dir,
                entry_id=f"CL-20260220-12000{i}-001",
                what=f"Proposal {i}",
                scope=ChangeScope(list(ChangeScope)[i % len(list(ChangeScope))].value),
            )

        async def mock_create(*args: object) -> str | None:
            return "FRE-X"

        criteria = PromotionCriteria(max_existing_linear_issues=2)
        pipeline = PromotionPipeline(
            log_dir=log_dir, criteria=criteria, create_issue_fn=mock_create
        )
        promoted = await pipeline.run()
        assert len(promoted) == 2

    @pytest.mark.asyncio
    async def test_handles_create_fn_failure(self, tmp_path: pathlib.Path) -> None:
        """Test that pipeline handles create_issue_fn failure gracefully."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir)

        async def failing_create(*args: object) -> str | None:
            raise RuntimeError("API down")

        pipeline = PromotionPipeline(log_dir=log_dir, create_issue_fn=failing_create)
        promoted = await pipeline.run()
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_no_entries_returns_empty(self, tmp_path: pathlib.Path) -> None:
        """Test that pipeline returns empty when no promotable entries exist."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()

        pipeline = PromotionPipeline(log_dir=log_dir)
        promoted = await pipeline.run()
        assert promoted == []


class TestIntegrationDedupToPromotion:
    """Integration test: 5 similar proposals -> 1 entry with seen_count 5 -> 1 Linear issue."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self, tmp_path: pathlib.Path) -> None:
        """Test full dedup-to-promotion pipeline integration."""
        from unittest.mock import patch

        from personal_agent.captains_log.manager import CaptainLogManager

        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        cat = ChangeCategory.CONCURRENCY
        scope = ChangeScope.ORCHESTRATOR
        fp = compute_proposal_fingerprint(cat, scope, "Add concurrency control")
        old_first_seen = datetime.now(timezone.utc) - timedelta(days=14)

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            for i in range(5):
                entry = CaptainLogEntry(
                    entry_id=f"CL-integ-{i:03d}",
                    type=CaptainLogEntryType.REFLECTION,
                    title="Task: concurrency",
                    rationale=f"Observation #{i}",
                    proposed_change=ProposedChange(
                        what="Add concurrency control",
                        why="Prevent inference contention",
                        how="Use semaphore",
                        category=cat,
                        scope=scope,
                        fingerprint=fp,
                        first_seen=old_first_seen,
                    ),
                    status=CaptainLogStatus.AWAITING_APPROVAL,
                )
                manager.save_entry(entry)

        files = list(log_dir.glob("CL-*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["proposed_change"]["seen_count"] == 5

        created_issues: list[str] = []

        async def mock_create(*args: object) -> str | None:
            created_issues.append("FRE-NEW")
            return "FRE-NEW"

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=mock_create,
        )
        promoted = await pipeline.run()

        assert len(promoted) == 1
        assert promoted[0]["linear_issue_id"] == "FRE-NEW"
        assert len(created_issues) == 1

        final_data = json.loads(files[0].read_text())
        assert final_data["status"] == "approved"
        assert final_data["linear_issue_id"] == "FRE-NEW"
