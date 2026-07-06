"""Tests that freshness-review proposals tag source=STATISTICAL_DETECTOR (ADR-0105 D1).

Both dormant-entity and dormant-relationship builders are rule-based/threshold
detectors, not the LLM reflection path, so they fall under the same
discriminator value as the InsightsEngine per D1's coarse two-value model.
"""

from personal_agent.brainstem.jobs.freshness_review import (
    _build_entity_dormant_proposal,
    _build_relationship_dormant_proposal,
)
from personal_agent.captains_log.models import ProposalSource
from personal_agent.config.settings import get_settings
from personal_agent.memory.freshness_aggregate import GraphStalenessSummary, StalenessTierCounts


class TestEntityDormantProposalTagsSource:
    def test_tags_statistical_detector_source(self) -> None:
        cfg = get_settings()
        summary = GraphStalenessSummary(
            entities=StalenessTierCounts(
                dormant=cfg.freshness_dormant_entity_proposal_threshold + 5
            ),
        )

        entry = _build_entity_dormant_proposal(summary, trace_id="trace-1", cfg=cfg)

        assert entry is not None
        assert entry.proposed_change is not None
        assert entry.proposed_change.source == ProposalSource.STATISTICAL_DETECTOR

    def test_below_threshold_returns_none(self) -> None:
        cfg = get_settings()
        summary = GraphStalenessSummary(entities=StalenessTierCounts(dormant=0))

        assert _build_entity_dormant_proposal(summary, trace_id="trace-1", cfg=cfg) is None


class TestRelationshipDormantProposalTagsSource:
    def test_tags_statistical_detector_source(self) -> None:
        cfg = get_settings()
        summary = GraphStalenessSummary(
            relationships=StalenessTierCounts(
                dormant=cfg.freshness_dormant_relationship_proposal_threshold + 5
            ),
        )

        entry = _build_relationship_dormant_proposal(summary, trace_id="trace-1", cfg=cfg)

        assert entry is not None
        assert entry.proposed_change is not None
        assert entry.proposed_change.source == ProposalSource.STATISTICAL_DETECTOR

    def test_below_threshold_returns_none(self) -> None:
        cfg = get_settings()
        summary = GraphStalenessSummary(relationships=StalenessTierCounts(dormant=0))

        assert _build_relationship_dormant_proposal(summary, trace_id="trace-1", cfg=cfg) is None
