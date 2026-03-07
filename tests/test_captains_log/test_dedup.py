"""Tests for Captain's Log dedup fingerprinting and merge logic (ADR-0030)."""

import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import patch

from personal_agent.captains_log.dedup import (
    _normalize_text,
    compute_proposal_fingerprint,
)
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposedChange,
)


class TestNormalizeText:
    """Test text normalization for fingerprinting."""

    def test_lowercases(self) -> None:
        """Test that normalization lowercases text."""
        assert _normalize_text("Add Retry Logic") == _normalize_text("add retry logic")

    def test_removes_stopwords(self) -> None:
        """Test that normalization removes stopwords."""
        result = _normalize_text("add the retry logic to the client")
        assert "the" not in result.split()
        assert "to" not in result.split()

    def test_strips_punctuation(self) -> None:
        """Test that normalization strips punctuation."""
        result = _normalize_text("add retry-logic!!")
        assert "add" in result
        assert "logic" in result

    def test_order_independent(self) -> None:
        """Test that normalization is order-independent."""
        a = _normalize_text("add retry logic to client")
        b = _normalize_text("client retry logic add")
        assert a == b

    def test_deduplicates_tokens(self) -> None:
        """Test that normalization deduplicates tokens."""
        result = _normalize_text("retry retry retry logic")
        assert result.count("retry") == 1


class TestComputeFingerprint:
    """Test fingerprint computation."""

    def test_same_inputs_same_fingerprint(self) -> None:
        """Test that same inputs produce the same fingerprint."""
        fp1 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        fp2 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        assert fp1 == fp2

    def test_different_category_different_fingerprint(self) -> None:
        """Test that different category produces different fingerprint."""
        fp1 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        fp2 = compute_proposal_fingerprint(
            ChangeCategory.PERFORMANCE, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        assert fp1 != fp2

    def test_different_scope_different_fingerprint(self) -> None:
        """Test that different scope produces different fingerprint."""
        fp1 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "Add retry logic"
        )
        fp2 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.TOOLS, "Add retry logic"
        )
        assert fp1 != fp2

    def test_word_order_invariant(self) -> None:
        """Test that fingerprint is invariant to word order."""
        fp1 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "add retry logic"
        )
        fp2 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY, ChangeScope.LLM_CLIENT, "logic retry add"
        )
        assert fp1 == fp2

    def test_fingerprint_is_16_hex_chars(self) -> None:
        """Test that fingerprint is 16 hex characters."""
        fp = compute_proposal_fingerprint(
            ChangeCategory.COST, ChangeScope.CONFIG, "Reduce token usage"
        )
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_stopword_invariant(self) -> None:
        """Test that fingerprint is invariant to stopwords."""
        fp1 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY,
            ChangeScope.LLM_CLIENT,
            "Add retry logic to the LLM client",
        )
        fp2 = compute_proposal_fingerprint(
            ChangeCategory.RELIABILITY,
            ChangeScope.LLM_CLIENT,
            "add retry logic llm client",
        )
        assert fp1 == fp2


class TestDedupOnWrite:
    """Test that CaptainLogManager.save_entry() deduplicates on fingerprint."""

    def _make_entry(
        self,
        what: str = "Add retry logic",
        entry_id: str = "",
        fingerprint: str | None = None,
    ) -> CaptainLogEntry:
        cat = ChangeCategory.RELIABILITY
        scope = ChangeScope.LLM_CLIENT
        if fingerprint is None:
            fingerprint = compute_proposal_fingerprint(cat, scope, what)
        return CaptainLogEntry(
            entry_id=entry_id,
            type=CaptainLogEntryType.REFLECTION,
            title="Task: test",
            rationale="Test rationale",
            proposed_change=ProposedChange(
                what=what,
                why="Improves reliability",
                how="Wrap calls in tenacity",
                category=cat,
                scope=scope,
                fingerprint=fingerprint,
                first_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
            status=CaptainLogStatus.AWAITING_APPROVAL,
        )

    def test_first_write_creates_file(self, tmp_path: pathlib.Path) -> None:
        """Test that first write creates a file."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)
        entry = self._make_entry()

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            path = manager.save_entry(entry)

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["proposed_change"]["seen_count"] == 1
        assert data["proposed_change"]["fingerprint"] is not None

    def test_duplicate_merges_instead_of_new_file(self, tmp_path: pathlib.Path) -> None:
        """Test that duplicate merges instead of creating new file."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            path1 = manager.save_entry(self._make_entry())
            path2 = manager.save_entry(self._make_entry(entry_id="CL-dup-001"))

        assert path1 == path2
        data = json.loads(path1.read_text())
        assert data["proposed_change"]["seen_count"] == 2
        assert "CL-dup-001" in data["proposed_change"]["related_entry_ids"]

    def test_five_duplicates_produce_seen_count_5(self, tmp_path: pathlib.Path) -> None:
        """Integration-style: 5 similar proposals -> 1 entry with seen_count 5."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            for i in range(5):
                manager.save_entry(self._make_entry(entry_id=f"CL-batch-{i:03d}"))

        files = list(log_dir.glob("CL-*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["proposed_change"]["seen_count"] == 5
        assert len(data["proposed_change"]["related_entry_ids"]) == 4

    def test_different_fingerprints_create_separate_files(self, tmp_path: pathlib.Path) -> None:
        """Test that different fingerprints create separate files."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry_a = self._make_entry(what="Add retry logic")
        entry_b = CaptainLogEntry(
            entry_id="",
            type=CaptainLogEntryType.REFLECTION,
            title="Task: other",
            rationale="Different",
            proposed_change=ProposedChange(
                what="Reduce token usage",
                why="Cost savings",
                how="Cache embeddings",
                category=ChangeCategory.COST,
                scope=ChangeScope.LLM_CLIENT,
                fingerprint=compute_proposal_fingerprint(
                    ChangeCategory.COST, ChangeScope.LLM_CLIENT, "Reduce token usage"
                ),
                first_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
            status=CaptainLogStatus.AWAITING_APPROVAL,
        )

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            manager.save_entry(entry_a)
            manager.save_entry(entry_b)

        files = list(log_dir.glob("CL-*.json"))
        assert len(files) == 2

    def test_no_fingerprint_always_creates_new_file(self, tmp_path: pathlib.Path) -> None:
        """Test that entries without fingerprint always create new file."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry1 = CaptainLogEntry(
            entry_id="CL-nofp-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Task: no fingerprint",
            rationale="Old-style entry",
            proposed_change=ProposedChange(
                what="Something",
                why="Because",
                how="Somehow",
            ),
            status=CaptainLogStatus.AWAITING_APPROVAL,
        )
        entry2 = CaptainLogEntry(
            entry_id="CL-nofp-002",
            type=CaptainLogEntryType.REFLECTION,
            title="Task: no fingerprint",
            rationale="Old-style entry",
            proposed_change=ProposedChange(
                what="Something",
                why="Because",
                how="Somehow",
            ),
            status=CaptainLogStatus.AWAITING_APPROVAL,
        )

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            p1 = manager.save_entry(entry1)
            p2 = manager.save_entry(entry2)

        assert p1 != p2
        files = list(log_dir.glob("CL-*.json"))
        assert len(files) == 2

    def test_approved_entry_not_merged_into(self, tmp_path: pathlib.Path) -> None:
        """Dedup only matches AWAITING_APPROVAL entries; APPROVED ones are skipped."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = self._make_entry(entry_id="CL-approved-001")
        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            path1 = manager.save_entry(entry)

        data = json.loads(path1.read_text())
        data["status"] = CaptainLogStatus.APPROVED.value
        path1.write_text(json.dumps(data, indent=2, default=str))

        new_entry = self._make_entry(entry_id="CL-approved-002")
        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            path2 = manager.save_entry(new_entry)

        assert path1 != path2
        files = list(log_dir.glob("CL-*.json"))
        assert len(files) == 2


class TestBackwardCompatibility:
    """Test that old entries without ADR-0030 fields load correctly."""

    def test_old_entry_without_new_fields_loads(self, tmp_path: pathlib.Path) -> None:
        """Test that old entry without new fields loads correctly."""
        old_data = {
            "entry_id": "CL-20260101-000000-001",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "type": "reflection",
            "title": "Old entry",
            "rationale": "Legacy",
            "proposed_change": {
                "what": "Something",
                "why": "Reason",
                "how": "Method",
            },
            "supporting_metrics": [],
            "status": "awaiting_approval",
        }
        file_path = tmp_path / "old_entry.json"
        file_path.write_text(json.dumps(old_data))

        entry = CaptainLogEntry.model_validate(json.loads(file_path.read_text()))
        assert entry.proposed_change is not None
        assert entry.proposed_change.category is None
        assert entry.proposed_change.scope is None
        assert entry.proposed_change.fingerprint is None
        assert entry.proposed_change.seen_count == 1
        assert entry.proposed_change.first_seen is None
        assert entry.proposed_change.related_entry_ids == []
        assert entry.linear_issue_id is None

    def test_old_entry_without_proposed_change_loads(self) -> None:
        """Test that old entry without proposed_change loads correctly."""
        entry = CaptainLogEntry(
            entry_id="CL-old-001",
            type=CaptainLogEntryType.OBSERVATION,
            title="Observation",
            rationale="Just an observation",
        )
        assert entry.proposed_change is None
        assert entry.linear_issue_id is None
