"""Tests for the ProposedChange source discriminator (ADR-0105 D1, ADR-0125 D1).

Covers:
- ProposalSource enum values.
- source round-trips through JSON serialization.
- ADR-0125 D1: source is non-nullable — a write that omits it is rejected, not
  defaulted, and a stored legacy payload without it must be migrated (see
  scripts/migrate_fre1001_captains_log_source_backfill.py) before it validates.
- ADR-0125 D1: the producer -> dimension mapping is total and build-enforced.
"""

import json
import pathlib

import pytest
from pydantic import ValidationError

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ChangeCategory,
    ChangeScope,
    Dimension,
    ProposalSource,
    ProposedChange,
    producer_dimension,
)


class TestProposalSourceEnum:
    """Test the ProposalSource discriminator values."""

    def test_has_statistical_detector_and_reflection(self) -> None:
        """ADR-0105 D1 names two producers; both values must exist."""
        assert ProposalSource.STATISTICAL_DETECTOR.value == "statistical_detector"
        assert ProposalSource.REFLECTION.value == "reflection"

    def test_has_legacy_unattributable_sentinel(self) -> None:
        """ADR-0125 D1: the migration-only sentinel for un-attributable legacy rows."""
        assert ProposalSource.LEGACY_UNATTRIBUTABLE.value == "legacy_unattributable"

    def test_legacy_unattributable_never_constructed_by_a_real_producer(self) -> None:
        """The sentinel is a migration artifact, not a producer.

        No ``ProposedChange(...)`` call in ``src/`` may pass
        ``source=ProposalSource.LEGACY_UNATTRIBUTABLE``. AST-based (not a
        blunt string search) so a legitimate *guard* against the
        sentinel -- e.g. promotion.py's `pc.source is ProposalSource.
        LEGACY_UNATTRIBUTABLE` skip check -- is not mistaken for a producer
        emitting it. Guards against a future producer copy-pasting the sentinel
        as a lazy default instead of setting its own real source.
        """
        import ast

        src_root = pathlib.Path(__file__).parent.parent.parent / "src" / "personal_agent"

        offenders = []
        for py_file in src_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "ProposedChange":
                    continue
                for kw in node.keywords:
                    if kw.arg == "source" and (
                        isinstance(kw.value, ast.Attribute)
                        and kw.value.attr == "LEGACY_UNATTRIBUTABLE"
                    ):
                        offenders.append(f"{py_file}:{node.lineno}")

        assert offenders == [], f"a producer must never construct with the sentinel: {offenders}"


class TestProposedChangeSourceField:
    """Test the source field on ProposedChange (ADR-0125 D1: non-nullable)."""

    def test_omitted_source_is_rejected(self) -> None:
        """A ProposedChange built without source is rejected, not defaulted (AC-1 check 1)."""
        with pytest.raises(ValidationError):
            ProposedChange(what="x", why="y", how="z")

    def test_write_boundary_never_creates_a_file_when_source_omitted(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The rejection happens at construction -- before any manager write path runs."""
        with pytest.raises(ValidationError):
            ProposedChange(what="x", why="y", how="z")
        assert list(tmp_path.glob("*.json")) == []

    def test_explicit_none_source_is_rejected(self) -> None:
        """Passing source=None explicitly is rejected the same as omitting it."""
        with pytest.raises(ValidationError):
            ProposedChange(what="x", why="y", how="z", source=None)

    def test_accepts_statistical_detector(self) -> None:
        """Source accepts ProposalSource.STATISTICAL_DETECTOR."""
        pc = ProposedChange(what="x", why="y", how="z", source=ProposalSource.STATISTICAL_DETECTOR)
        assert pc.source == ProposalSource.STATISTICAL_DETECTOR

    def test_accepts_reflection(self) -> None:
        """Source accepts ProposalSource.REFLECTION."""
        pc = ProposedChange(what="x", why="y", how="z", source=ProposalSource.REFLECTION)
        assert pc.source == ProposalSource.REFLECTION

    def test_round_trips_through_json(self) -> None:
        """Source survives a model_dump_json / model_validate_json round trip."""
        entry = CaptainLogEntry(
            entry_id="CL-test-source-001",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title="Test",
            rationale="Test",
            proposed_change=ProposedChange(
                what="x",
                why="y",
                how="z",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
                source=ProposalSource.REFLECTION,
            ),
        )
        raw = entry.model_dump_json()
        restored = CaptainLogEntry.model_validate(json.loads(raw))
        assert restored.proposed_change is not None
        assert restored.proposed_change.source == ProposalSource.REFLECTION

    def test_legacy_payload_without_source_key_is_rejected(self) -> None:
        """ADR-0125 D1: a pre-migration on-disk entry (no 'source' key at all) is rejected.

        Not silently accepted. This is the behavior that makes AC-1's
        "no durable derived artifact can be written by a producer whose dimension
        is undeclared" hold for reads of the historical corpus too: a stored
        legacy entry must go through
        scripts/migrate_fre1001_captains_log_source_backfill.py (which stamps an
        explicit ProposalSource.LEGACY_UNATTRIBUTABLE) before it validates again.
        PromotionPipeline.scan_promotable_entries and CaptainLogManager's dedup
        merge path already catch this exception and skip the entry rather than
        crash -- see test_promotion.py's
        test_run_skips_unmigrated_legacy_entries_entirely.
        """
        legacy_data = {
            "entry_id": "CL-legacy-001",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "type": "reflection",
            "title": "Legacy entry",
            "rationale": "Written before ADR-0105",
            "proposed_change": {
                "what": "Old proposal",
                "why": "Old reason",
                "how": "Old method",
                "category": "reliability",
                "scope": "llm_client",
                "seen_count": 1,
            },
        }
        with pytest.raises(ValidationError):
            CaptainLogEntry.model_validate(legacy_data)


class TestProducerDimensionMapping:
    """ADR-0125 D1: the producer -> dimension mapping is total and build-enforced."""

    def test_statistical_detector_is_harness_health(self) -> None:
        assert producer_dimension(ProposalSource.STATISTICAL_DETECTOR) is Dimension.HARNESS_HEALTH

    def test_reflection_is_harness_health(self) -> None:
        assert producer_dimension(ProposalSource.REFLECTION) is Dimension.HARNESS_HEALTH

    def test_legacy_unattributable_is_harness_health(self) -> None:
        """Conservative quarantine, not recovered provenance.

        See ADR-0125 D2: dimension-1 output can never reach user-facing context, so classifying
        un-attributable legacy material as dimension-1 is the safe default. This
        does NOT assert the historical entries were actually produced by a
        dimension-1 producer -- only that treating them as such is never unsafe.
        """
        assert producer_dimension(ProposalSource.LEGACY_UNATTRIBUTABLE) is Dimension.HARNESS_HEALTH

    def test_every_current_producer_is_mapped(self) -> None:
        """Totality, as a permanent regression test (AC-1 check 3, automated form).

        Iterates the live ProposalSource vocabulary and calls producer_dimension
        on every member. A future member added without a matching match/case
        branch raises via the assert_never fallback -- this loop fails the
        instant that happens, with no code change required to catch it. (mypy's
        static exhaustiveness check on the same match/assert_never construct is
        the second, independent enforcement layer -- see the manual proof cited
        in the FRE-1001 ticket close-out comment.)
        """
        for source in ProposalSource:
            dimension = producer_dimension(source)
            assert isinstance(dimension, Dimension)
