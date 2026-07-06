"""Tests for the ProposedChange source discriminator (ADR-0105 D1).

Covers:
- ProposalSource enum values.
- source round-trips through JSON serialization.
- Backward compatibility: legacy payloads without a "source" key still
  validate (default None), so pre-existing on-disk Captain's Log entries
  are not broken by this field's introduction.
"""

import json

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)


class TestProposalSourceEnum:
    """Test the ProposalSource discriminator values."""

    def test_has_statistical_detector_and_reflection(self) -> None:
        """ADR-0105 D1 names two producers; both values must exist."""
        assert ProposalSource.STATISTICAL_DETECTOR.value == "statistical_detector"
        assert ProposalSource.REFLECTION.value == "reflection"


class TestProposedChangeSourceField:
    """Test the source field on ProposedChange."""

    def test_defaults_to_none(self) -> None:
        """A ProposedChange built without source defaults to None."""
        pc = ProposedChange(what="x", why="y", how="z")
        assert pc.source is None

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

    def test_legacy_payload_without_source_key_still_validates(self) -> None:
        """A pre-migration on-disk entry (no 'source' key at all) must still parse.

        Historical Captain's Log entries predate this field. If source were
        required, CaptainLogEntry.model_validate() would start raising on the
        existing backlog inside PromotionPipeline.scan_promotable_entries().
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
        entry = CaptainLogEntry.model_validate(legacy_data)
        assert entry.proposed_change is not None
        assert entry.proposed_change.source is None
