"""Tests for outcome ingestion + realized-value signal wiring (ADR-0105 D7, FRE-717)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.brainstem.jobs.outcome_ingestion import (
    _classify_outcome,
    run_outcome_ingestion,
)
from personal_agent.sysgraph.repository import SignalValue


def _issue(state_name: str, labels: list[str] | None = None) -> dict:
    return {
        "state": {"name": state_name},
        "labels": [{"name": name} for name in (labels or [])],
    }


class TestClassifyOutcome:
    def test_done_is_shipped(self) -> None:
        assert _classify_outcome(_issue("Done")) == "shipped"

    def test_canceled_with_rejected_label_is_owner_rejected(self) -> None:
        assert _classify_outcome(_issue("Canceled", ["Rejected"])) == "owner-rejected"

    def test_canceled_without_rejected_label_is_canceled_as_noise(self) -> None:
        assert _classify_outcome(_issue("Canceled")) == "canceled-as-noise"

    def test_duplicate_with_rejected_label_is_owner_rejected(self) -> None:
        assert _classify_outcome(_issue("Duplicate", ["Rejected"])) == "owner-rejected"

    def test_duplicate_without_rejected_label_is_canceled_as_noise(self) -> None:
        assert _classify_outcome(_issue("Duplicate")) == "canceled-as-noise"

    @pytest.mark.parametrize(
        "state_name", ["Approved", "In Progress", "In Review", "Awaiting Deploy", "Verify Failed"]
    )
    def test_open_states_are_not_decided(self, state_name: str) -> None:
        assert _classify_outcome(_issue(state_name)) is None

    def test_missing_state_is_not_decided(self) -> None:
        assert _classify_outcome({}) is None


@pytest.mark.asyncio
class TestRunOutcomeIngestion:
    async def test_ingests_shipped_ticket_and_updates_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-123"])
        repo.ticket_source_kind = AsyncMock(return_value=("reflection", "reliability"))
        repo.get_signal = AsyncMock(return_value=SignalValue(value=0.0, n=0, suppressed=False))
        repo.record_outcome = AsyncMock(return_value=True)
        repo.compute_and_apply_signal = AsyncMock(
            return_value=SignalValue(value=0.333, n=1, suppressed=False)
        )
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("Done"))

        await run_outcome_ingestion(client, trace_id="test-trace")

        repo.record_outcome.assert_awaited_once_with("FRE-123", "shipped")
        repo.compute_and_apply_signal.assert_awaited_once_with("reflection", "reliability")
        repo.disconnect.assert_awaited_once()

    async def test_still_open_ticket_is_not_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-456"])
        repo.record_outcome = AsyncMock()
        repo.compute_and_apply_signal = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("In Progress"))

        await run_outcome_ingestion(client, trace_id="test-trace")

        repo.record_outcome.assert_not_awaited()
        repo.compute_and_apply_signal.assert_not_awaited()

    async def test_disabled_skips_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "outcome_ingestion_enabled", False)
        repo_ctor = MagicMock()
        monkeypatch.setattr("personal_agent.sysgraph.SysgraphRepository", repo_ctor)

        client = MagicMock()
        client.get_issue = AsyncMock()

        await run_outcome_ingestion(client, trace_id="test-trace")

        repo_ctor.assert_not_called()
        client.get_issue.assert_not_awaited()

    async def test_sysgraph_connect_failure_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = MagicMock()
        repo.connect = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock()

        await run_outcome_ingestion(client, trace_id="test-trace")

        client.get_issue.assert_not_awaited()

    async def test_recorded_outcome_stamps_ticket_outcome_onto_es_doc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-719 (ADR-0105 D6): a recorded outcome is stamped onto the source ES doc.

        Without this, the promoted proposal document never carries a queryable
        shipped/canceled signal -- the funnel dashboard has nothing to facet on.
        """
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-123"])
        repo.ticket_source_kind = AsyncMock(return_value=("reflection", "reliability"))
        repo.get_signal = AsyncMock(return_value=SignalValue(value=0.0, n=0, suppressed=False))
        repo.record_outcome = AsyncMock(return_value=True)
        repo.compute_and_apply_signal = AsyncMock(
            return_value=SignalValue(value=0.333, n=1, suppressed=False)
        )
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("Done"))

        es_handler = MagicMock()
        es_handler._connected = True
        es_handler.es_logger.update_by_query = AsyncMock(return_value=1)

        await run_outcome_ingestion(client, trace_id="test-trace", es_handler=es_handler)

        es_handler.es_logger.update_by_query.assert_awaited_once_with(
            "agent-captains-reflections-*",
            {"term": {"linear_issue_id": "FRE-123"}},
            "ctx._source.ticket_outcome = params.ticket_outcome",
            {"ticket_outcome": "shipped"},
        )

    async def test_not_recorded_outcome_does_not_stamp_es(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-recorded (not newly-recorded) outcome does not re-stamp ES."""
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-789"])
        repo.ticket_source_kind = AsyncMock(return_value=("reflection", "reliability"))
        repo.get_signal = AsyncMock(return_value=SignalValue(value=0.0, n=1, suppressed=False))
        repo.record_outcome = AsyncMock(return_value=False)  # already recorded
        repo.compute_and_apply_signal = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("Done"))

        es_handler = MagicMock()
        es_handler._connected = True
        es_handler.es_logger.update_by_query = AsyncMock(return_value=1)

        await run_outcome_ingestion(client, trace_id="test-trace", es_handler=es_handler)

        es_handler.es_logger.update_by_query.assert_not_awaited()

    async def test_es_stamp_failure_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing ES stamp is fail-open, matching every other sysgraph call in this file."""
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-123"])
        repo.ticket_source_kind = AsyncMock(return_value=("reflection", "reliability"))
        repo.get_signal = AsyncMock(return_value=SignalValue(value=0.0, n=0, suppressed=False))
        repo.record_outcome = AsyncMock(return_value=True)
        repo.compute_and_apply_signal = AsyncMock(
            return_value=SignalValue(value=0.333, n=1, suppressed=False)
        )
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("Done"))

        es_handler = MagicMock()
        es_handler._connected = True
        es_handler.es_logger.update_by_query = AsyncMock(side_effect=RuntimeError("boom"))

        await run_outcome_ingestion(client, trace_id="test-trace", es_handler=es_handler)

        repo.disconnect.assert_awaited_once()

    async def test_already_recorded_does_not_recompute_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.tickets_awaiting_outcome = AsyncMock(return_value=["FRE-789"])
        repo.ticket_source_kind = AsyncMock(return_value=("reflection", "reliability"))
        repo.get_signal = AsyncMock(return_value=SignalValue(value=0.0, n=1, suppressed=False))
        repo.record_outcome = AsyncMock(return_value=False)  # already recorded
        repo.compute_and_apply_signal = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        client = MagicMock()
        client.get_issue = AsyncMock(return_value=_issue("Done"))

        await run_outcome_ingestion(client, trace_id="test-trace")

        repo.compute_and_apply_signal.assert_not_awaited()
