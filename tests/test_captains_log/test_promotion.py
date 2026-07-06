"""Tests for Captain's Log promotion pipeline (ADR-0030)."""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.captains_log.promotion as promotion_module
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
from personal_agent.captains_log.promotion import (
    PromotionCriteria,
    PromotionPipeline,
    _format_linear_description,
    _map_seen_count_to_priority,
)
from personal_agent.sysgraph.repository import SignalValue


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

    def test_includes_fingerprint_markers_when_present(self) -> None:
        """ADR-0040: description embeds fingerprint for Linear dedup and rejection handling."""
        fp = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        entry = CaptainLogEntry(
            entry_id="CL-test-fp",
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Test",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Reliability",
                how="Tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
                fingerprint=fp,
                seen_count=3,
            ),
        )
        desc = _format_linear_description(entry)
        assert fp in desc
        assert "<!-- fingerprint:" in desc

    def test_default_promotion_cap_is_five(self) -> None:
        """ADR-0040 default max issues per run."""
        assert PromotionCriteria().max_existing_linear_issues == 5

    def test_default_excludes_knowledge_quality(self) -> None:
        """FRE-620 promotion floor: only RELIABILITY/high-severity auto-promotes by default."""
        assert PromotionCriteria().excluded_categories == [ChangeCategory.KNOWLEDGE_QUALITY]

    def test_handles_no_proposed_change(self) -> None:
        """Test that Linear description handles entries without proposed_change."""
        entry = CaptainLogEntry(
            entry_id="CL-test-001",
            type=CaptainLogEntryType.OBSERVATION,
            title="Test",
            rationale="Test",
        )
        assert _format_linear_description(entry) == ""


class TestVerbatimSubstanceCarryThrough:
    """ADR-0105 D5/AC-4: promoted tickets carry full substance, not a thin summary."""

    def test_rationale_appears_verbatim(self) -> None:
        """entry.rationale (always present) appears in full in the description."""
        rationale = (
            "This is a long multi-sentence rationale explaining exactly why the "
            "reflection pipeline flagged this pattern, with specific detail that "
            "must survive into the ticket body unabridged."
        )
        entry = CaptainLogEntry(
            entry_id="CL-test-rationale",
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale=rationale,
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Use tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
            ),
        )
        desc = _format_linear_description(entry)
        assert rationale in desc

    def test_experiment_design_items_appear_verbatim(self) -> None:
        """Each experiment_design list item appears in full, not joined/summarized."""
        steps = [
            "Step one: instrument the retry path with a counter metric.",
            "Step two: run for 7 days and compare failure rate against baseline.",
        ]
        entry = CaptainLogEntry(
            entry_id="CL-test-experiment",
            type=CaptainLogEntryType.HYPOTHESIS,
            title="Test",
            rationale="Test rationale",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Use tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
            ),
            experiment_design=steps,
        )
        desc = _format_linear_description(entry)
        for step in steps:
            assert step in desc

    def test_expected_outcome_and_potential_implementation_appear_verbatim(self) -> None:
        """expected_outcome (str) and potential_implementation (list[str]) carry through."""
        expected_outcome = "Failure rate drops below 2% within the 7-day window."
        implementation_steps = [
            "Wrap the LLM client call in a tenacity retry decorator.",
            "Cap retries at 3 with exponential backoff.",
        ]
        entry = CaptainLogEntry(
            entry_id="CL-test-outcome",
            type=CaptainLogEntryType.IDEA,
            title="Test",
            rationale="Test rationale",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Use tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
            ),
            expected_outcome=expected_outcome,
            potential_implementation=implementation_steps,
        )
        desc = _format_linear_description(entry)
        assert expected_outcome in desc
        for step in implementation_steps:
            assert step in desc

    def test_absent_optional_fields_produce_no_empty_sections(self) -> None:
        """When experiment_design/expected_outcome/potential_implementation are unset,
        no stray section headers are added (only rationale is unconditional).
        """
        entry = CaptainLogEntry(
            entry_id="CL-test-minimal",
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Just a rationale.",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Use tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
            ),
        )
        desc = _format_linear_description(entry)
        assert "Just a rationale." in desc
        assert "## Experiment Design" not in desc
        assert "## Expected Outcome" not in desc
        assert "## Potential Implementation" not in desc


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
    eval_mode: bool = False,
    source: ProposalSource | None = None,
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
            "source": source.value if source else None,
            "fingerprint": fp,
            "seen_count": seen_count,
            "first_seen": first_seen.isoformat(),
            "related_entry_ids": [],
        },
        "supporting_metrics": ["cpu: 45%"],
        "status": status.value,
        "linear_issue_id": linear_issue_id,
        "eval_mode": eval_mode,
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

    def test_skips_eval_derived_entries(self, tmp_path: pathlib.Path) -> None:
        """FRE-523: eval-derived entries are never promoted (no Linear leak)."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        # An otherwise-qualifying entry that originated from an eval run.
        _write_entry(log_dir, entry_id="CL-20260220-120000-001", seen_count=5, eval_mode=True)

        pipeline = PromotionPipeline(log_dir=log_dir)
        assert len(pipeline.scan_promotable_entries()) == 0

    def test_promotes_non_eval_alongside_eval(self, tmp_path: pathlib.Path) -> None:
        """A real (non-eval) entry is still promoted while the eval one is skipped."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, entry_id="CL-20260220-120000-001", seen_count=5, eval_mode=False)
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-002",
            what="Eval-derived change",
            seen_count=5,
            eval_mode=True,
        )

        pipeline = PromotionPipeline(log_dir=log_dir)
        entries = pipeline.scan_promotable_entries()
        assert len(entries) == 1
        assert entries[0].entry_id == "CL-20260220-120000-001"

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

    def test_default_criteria_skips_knowledge_quality_promotes_reliability(
        self, tmp_path: pathlib.Path
    ) -> None:
        """FRE-620: under the default promotion floor, a medium KNOWLEDGE_QUALITY entry
        (e.g. a graph-quality anomaly) does not reach Linear, but a high RELIABILITY one does.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-kq",
            what="Knowledge quality anomaly",
            category=ChangeCategory.KNOWLEDGE_QUALITY,
        )
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-rel",
            what="Reliability anomaly",
            category=ChangeCategory.RELIABILITY,
        )

        pipeline = PromotionPipeline(log_dir=log_dir)
        promotable = {entry.entry_id for entry in pipeline.scan_promotable_entries()}

        assert "CL-20260220-120000-kq" not in promotable
        assert "CL-20260220-120000-rel" in promotable

    def test_custom_criteria(self, tmp_path: pathlib.Path) -> None:
        """Test that scan respects custom criteria."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, seen_count=2)

        criteria = PromotionCriteria(min_seen_count=2, min_age_days=0)
        pipeline = PromotionPipeline(log_dir=log_dir, criteria=criteria)
        assert len(pipeline.scan_promotable_entries()) == 1


class TestAdr0105SourceDiscriminatorConvergence:
    """ADR-0105 D1 / FRE-715 AC-1: one entrypoint, both source values, none missing."""

    def test_one_scan_returns_both_source_values(self, tmp_path: pathlib.Path) -> None:
        """Both producers persist via one CaptainLogManager; one PromotionPipeline
        scan over that shared directory returns both, each tagged by source.
        """
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)
        old_enough = datetime.now(timezone.utc) - timedelta(days=14)

        statistical_entry = CaptainLogEntry(
            entry_id="",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title="Task: statistical",
            rationale="From the statistical detector",
            proposed_change=ProposedChange(
                what="Address insight pattern: cost spike",
                why="Cost spike detected",
                how="Investigate and mitigate",
                category=ChangeCategory.COST,
                scope=ChangeScope.LLM_CLIENT,
                source=ProposalSource.STATISTICAL_DETECTOR,
                fingerprint=compute_proposal_fingerprint(
                    ChangeCategory.COST,
                    ChangeScope.LLM_CLIENT,
                    "Address insight pattern: cost spike",
                ),
                seen_count=5,
                first_seen=old_enough,
            ),
        )
        reflection_entry = CaptainLogEntry(
            entry_id="",
            type=CaptainLogEntryType.REFLECTION,
            title="Task: reflection",
            rationale="From the LLM reflector",
            proposed_change=ProposedChange(
                what="Add retry logic",
                why="Improves reliability",
                how="Wrap calls in tenacity",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
                source=ProposalSource.REFLECTION,
                fingerprint=compute_proposal_fingerprint(
                    ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
                ),
                seen_count=5,
                first_seen=old_enough,
            ),
        )

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            manager.save_entry(statistical_entry)
            manager.save_entry(reflection_entry)

        # One PromotionPipeline, one scan, over the directory both producers wrote to.
        pipeline = PromotionPipeline(log_dir=log_dir)
        entries = pipeline.scan_promotable_entries()

        sources = {e.proposed_change.source for e in entries if e.proposed_change}
        assert sources == {ProposalSource.STATISTICAL_DETECTOR, ProposalSource.REFLECTION}
        # No produced proposal lacks a source.
        assert all(e.proposed_change and e.proposed_change.source is not None for e in entries)

    def test_scan_promotable_entries_defined_exactly_once(self) -> None:
        """Structural check: only PromotionPipeline scans CL proposals for promotion.

        Other CL-*.json scanners exist (ES backfill replay, retention/archival)
        but they serve different purposes and use different method names; only
        one function in the codebase is named ``scan_promotable_entries``.
        """
        import ast

        src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "personal_agent"
        defining_files = []
        for py_file in src_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "scan_promotable_entries":
                    defining_files.append(py_file)

        assert defining_files == [src_root / "captains_log" / "promotion.py"]

    def test_producers_never_call_linear_directly(self) -> None:
        """Neither producer talks to Linear directly — both reach it only
        through the single PromotionPipeline entrypoint.
        """
        src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "personal_agent"
        producer_files = [
            src_root / "insights" / "engine.py",
            src_root / "captains_log" / "reflection.py",
            src_root / "captains_log" / "reflection_dspy.py",
        ]
        for f in producer_files:
            text = f.read_text(encoding="utf-8")
            assert "LinearClient" not in text, f"{f} must not construct a Linear client"
            assert "create_issue" not in text, f"{f} must not call Linear directly"


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
    async def test_issue_budget_pauses_promotion(self, tmp_path: pathlib.Path) -> None:
        """ADR-0040: when non-archived Linear count exceeds threshold, skip creating issues."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir)

        mock_create = AsyncMock(return_value="FF-1")
        lc = MagicMock()
        lc.count_open_issues = AsyncMock(return_value=201)
        lc.list_issues = AsyncMock(return_value=[])

        with patch.object(promotion_module.settings, "issue_budget_threshold", 200):
            pipeline = PromotionPipeline(
                log_dir=log_dir,
                criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
                create_issue_fn=mock_create,
                linear_client=lc,
            )
            promoted = await pipeline.run()
        assert promoted == []
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_linear_duplicate_links_existing_issue(self, tmp_path: pathlib.Path) -> None:
        """ADR-0040: fingerprint match in Linear links CL entry without new save_issue."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        fp_path = _write_entry(log_dir)
        data = json.loads(fp_path.read_text())
        fp = data["proposed_change"]["fingerprint"]

        mock_create = AsyncMock(side_effect=AssertionError("save_issue should not be called"))

        lc = MagicMock()
        lc.count_open_issues = AsyncMock(return_value=50)
        lc.list_issues = AsyncMock(
            return_value=[
                {
                    "id": "other-uuid",
                    "identifier": "FF-EXISTING",
                    "description": f"Prior\n<!-- fingerprint: {fp} -->\n",
                }
            ]
        )

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=mock_create,
            linear_client=lc,
        )
        promoted = await pipeline.run()
        assert len(promoted) == 1
        assert promoted[0]["linear_issue_id"] == "FF-EXISTING"
        mock_create.assert_not_called()

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


class TestAdr0105BidirectionalLinkage:
    """ADR-0105 D4/AC-3: promotion writes sysgraph linkage + stamps ES insight docs."""

    @pytest.mark.asyncio
    async def test_run_records_sysgraph_linkage_when_repo_provided(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A fresh promotion calls sysgraph_repo.record_promotion with the proposal + ticket."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, source=ProposalSource.REFLECTION)

        sysgraph_repo = MagicMock()
        sysgraph_repo.record_promotion = AsyncMock()
        sysgraph_repo.get_signal = AsyncMock(return_value=SignalValue(0.0, 0, False))

        async def mock_create(*args: object) -> str | None:
            return "FRE-NEW"

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, sysgraph_repo=sysgraph_repo
        )
        promoted = await pipeline.run()

        assert len(promoted) == 1
        sysgraph_repo.record_promotion.assert_awaited_once()
        _, kwargs = sysgraph_repo.record_promotion.call_args
        assert kwargs["linear_issue_id"] == "FRE-NEW"

    @pytest.mark.asyncio
    async def test_run_records_sysgraph_linkage_on_dedup_linked_branch_too(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The dedup-linked-to-existing-issue branch also writes the graph linkage."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        fp_path = _write_entry(log_dir, source=ProposalSource.STATISTICAL_DETECTOR)
        data = json.loads(fp_path.read_text())
        fp = data["proposed_change"]["fingerprint"]

        lc = MagicMock()
        lc.count_open_issues = AsyncMock(return_value=50)
        lc.list_issues = AsyncMock(
            return_value=[
                {
                    "id": "other-uuid",
                    "identifier": "FF-EXISTING",
                    "description": f"Prior\n<!-- fingerprint: {fp} -->\n",
                }
            ]
        )
        mock_create = AsyncMock(side_effect=AssertionError("should not be called"))
        sysgraph_repo = MagicMock()
        sysgraph_repo.record_promotion = AsyncMock()
        sysgraph_repo.get_signal = AsyncMock(return_value=SignalValue(0.0, 0, False))

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=mock_create,
            linear_client=lc,
            sysgraph_repo=sysgraph_repo,
        )
        promoted = await pipeline.run()

        assert promoted[0]["linear_issue_id"] == "FF-EXISTING"
        sysgraph_repo.record_promotion.assert_awaited_once()
        _, kwargs = sysgraph_repo.record_promotion.call_args
        assert kwargs["linear_issue_id"] == "FF-EXISTING"

    @pytest.mark.asyncio
    async def test_run_skips_sysgraph_linkage_when_source_is_none(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Legacy entries with no source discriminator are skipped, not fabricated."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, source=None)

        sysgraph_repo = MagicMock()
        sysgraph_repo.record_promotion = AsyncMock()
        sysgraph_repo.get_signal = AsyncMock(return_value=SignalValue(0.0, 0, False))

        async def mock_create(*args: object) -> str | None:
            return "FRE-NEW"

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, sysgraph_repo=sysgraph_repo
        )
        promoted = await pipeline.run()

        assert len(promoted) == 1
        sysgraph_repo.record_promotion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_stamps_es_insight_linkage_only_for_statistical_detector(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The ES agent-insights-* stamp fires only for STATISTICAL_DETECTOR-sourced entries."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        fp_path = _write_entry(
            log_dir, entry_id="CL-stat-001", source=ProposalSource.STATISTICAL_DETECTOR
        )
        data = json.loads(fp_path.read_text())
        fp = data["proposed_change"]["fingerprint"]

        es_handler = MagicMock()
        es_handler._connected = True
        es_handler.es_logger.update_by_query = AsyncMock(return_value=1)

        async def mock_create(*args: object) -> str | None:
            return "FRE-NEW"

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, es_handler=es_handler
        )
        await pipeline.run()

        es_handler.es_logger.update_by_query.assert_awaited_once()
        args, _ = es_handler.es_logger.update_by_query.call_args
        assert args[0] == "agent-insights-*"
        assert args[1] == {"term": {"fingerprint": fp}}
        assert args[3] == {"linear_issue_id": "FRE-NEW"}

    @pytest.mark.asyncio
    async def test_run_does_not_stamp_es_for_reflection_source(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Reflection-sourced entries have no ES insights doc to stamp."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(log_dir, source=ProposalSource.REFLECTION)

        es_handler = MagicMock()
        es_handler._connected = True
        es_handler.es_logger.update_by_query = AsyncMock(return_value=1)

        async def mock_create(*args: object) -> str | None:
            return "FRE-NEW"

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, es_handler=es_handler
        )
        await pipeline.run()

        es_handler.es_logger.update_by_query.assert_not_awaited()


class TestAdr0105SignalReadInPromotion:
    """ADR-0105 D7/AC-6: promotion reads the realized-value signal before capping."""

    @pytest.mark.asyncio
    async def test_no_sysgraph_repo_leaves_order_unchanged(self, tmp_path: pathlib.Path) -> None:
        """sysgraph_repo=None fails open -- identical to today's un-ranked behavior."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-001",
            what="Proposal A",
            source=ProposalSource.REFLECTION,
        )

        created: list[str] = []

        async def mock_create(*args: object) -> str | None:
            created.append(str(args[0]))
            return "FRE-A"

        pipeline = PromotionPipeline(log_dir=log_dir, create_issue_fn=mock_create)
        promoted = await pipeline.run()

        assert len(promoted) == 1

    @pytest.mark.asyncio
    async def test_suppressed_entry_is_excluded(self, tmp_path: pathlib.Path) -> None:
        """A suppressed (source, category) entry never reaches _create_linear_issue."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-001",
            what="Suppressed proposal",
            category=ChangeCategory.RELIABILITY,
            source=ProposalSource.REFLECTION,
        )

        sysgraph_repo = MagicMock()
        sysgraph_repo.get_signal = AsyncMock(
            return_value=SignalValue(value=-0.6, n=6, suppressed=True)
        )
        mock_create = AsyncMock(return_value="FRE-SHOULD-NOT-CREATE")

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, sysgraph_repo=sysgraph_repo
        )
        promoted = await pipeline.run()

        assert promoted == []
        mock_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ranks_by_realized_value_before_capping(self, tmp_path: pathlib.Path) -> None:
        """Two same-seen_count candidates: higher-v category is promoted when the cap admits one."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-001",
            what="Low-value proposal",
            seen_count=5,
            category=ChangeCategory.RELIABILITY,
            source=ProposalSource.REFLECTION,
        )
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-002",
            what="High-value proposal",
            seen_count=5,
            category=ChangeCategory.COST,
            source=ProposalSource.REFLECTION,
        )

        def _signal_for(source: str, category: str) -> SignalValue:
            if category == "cost":
                return SignalValue(value=0.4, n=3, suppressed=False)
            return SignalValue(value=-0.1, n=2, suppressed=False)

        sysgraph_repo = MagicMock()
        sysgraph_repo.get_signal = AsyncMock(side_effect=_signal_for)
        created_titles: list[str] = []

        async def mock_create(title: str, *args: object) -> str | None:
            created_titles.append(title)
            return "FRE-WINNER"

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(max_existing_linear_issues=1),
            create_issue_fn=mock_create,
            sysgraph_repo=sysgraph_repo,
        )
        promoted = await pipeline.run()

        assert len(promoted) == 1
        assert "High-value proposal" in created_titles[0]

    @pytest.mark.asyncio
    async def test_signal_read_failure_degrades_to_unmodulated_score(
        self, tmp_path: pathlib.Path
    ) -> None:
        """get_signal raising for one entry does not block the run (fail open)."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write_entry(
            log_dir,
            entry_id="CL-20260220-120000-001",
            what="Errors on signal read",
            source=ProposalSource.REFLECTION,
        )

        sysgraph_repo = MagicMock()
        sysgraph_repo.get_signal = AsyncMock(side_effect=RuntimeError("db down"))

        async def mock_create(*args: object) -> str | None:
            return "FRE-OK"

        pipeline = PromotionPipeline(
            log_dir=log_dir, create_issue_fn=mock_create, sysgraph_repo=sysgraph_repo
        )
        promoted = await pipeline.run()

        assert len(promoted) == 1
