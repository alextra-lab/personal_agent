"""AC-4 (FRE-1219) — the governed vocabulary validator agrees with real fixed emit sites.

Runs against documents *this test suite generates* by calling the real
production emit functions and replaying their actual ``log.info``/``log.warning``
kwargs through the real ``ElasticsearchLogger.log_event()`` -> ``validate_document()``
assembly boundary — not a hand-typed fixture dict, and not historical production
documents (which the fix cannot retroactively change, and which AC-4 explicitly
excludes as proof).

Design note (codex plan review): a raw ``structlog.testing.capture_logs()`` dict
still carries the un-processed ``event`` key, which is itself a retired spelling
(``event`` -> ``event_type``) that the real assembly path strips/renames before
``validate_document()`` ever sees the document. Passing that raw dict straight to
``validate_document()`` would fail for the wrong reason and prove nothing about
this ticket's four names. Instead: capture the real kwargs the production
function passes to ``log.<method>(event_type, **kwargs)`` (via a mocked ``log``),
then hand ``event_type``/``kwargs`` to ``ElasticsearchLogger.log_event()``
directly -- the same assembly `ElasticsearchHandler` reaches in production,
just without the stdlib logging round-trip.

Two representative sites (per the plan: the rename + at least 2 removal sites):
the rename (``cost_estimator``, covered together with AC-2 in
``test_cost_estimator.py``) is exercised again here for the AC-4 angle, plus two
removal sites covering different failure shapes: an inline-computed field
(``feedback.py``) and a fan-out/fusion recall path (``memory/service.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry.es_logger import ElasticsearchLogger


async def _assert_document_passes_validator(event_type: str, data: dict) -> None:
    """Replay a real emit's kwargs through the real log_event() assembly.

    ``validate_document`` raises outside production (ADR-0133 D4) — the test
    environment is TEST, so any violation surfaces as an exception here.

    A mocked *connected* client is required: ``log_event``'s not-connected
    branch has a pre-existing, unrelated bug (``log.warning(..., event=...)``
    collides with structlog's reserved ``event`` parameter) that would mask
    the assertion this test exists to make. Not this ticket's bug to fix —
    worth a separate follow-up, noted in the PR.
    """
    logger = ElasticsearchLogger()
    logger.client = AsyncMock()
    logger.client.index = AsyncMock(return_value={"_id": "doc-1"})
    await logger.log_event(event_type, data)


def _service() -> MemoryService:
    service = MemoryService()  # fre-375-allow: arms/rerank mocked; no substrate touched
    service.connected = True
    service.driver = object()
    return service


class TestRenameSite:
    @pytest.mark.asyncio
    async def test_actual_cost_fallback_priced_passes_validator(self, monkeypatch) -> None:
        import litellm
        from litellm import ModelResponse

        from personal_agent.llm_client.cost_estimator import actual_cost_for_response

        fake_model = "fre-1219-test/unregistered-model-ac4"
        monkeypatch.setitem(
            litellm.model_cost,
            fake_model,
            {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
        )
        response = ModelResponse(
            id="msg_test_fre1219_ac4",
            choices=[
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"content": "ok", "role": "assistant", "tool_calls": None},
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model=fake_model,
            object="chat.completion",
        )

        with patch("personal_agent.llm_client.cost_estimator.log") as mock_log:
            actual_cost_for_response(response=response, model=fake_model, trace_id="t")

        events = [
            call
            for call in mock_log.info.call_args_list
            if call.args and call.args[0] == "actual_cost_fallback_priced"
        ]
        assert events, "actual_cost_fallback_priced event was not emitted"
        event_type, data = events[0].args[0], events[0].kwargs
        await _assert_document_passes_validator(event_type, data)


class TestRemovalSites:
    @pytest.mark.asyncio
    async def test_feedback_event_processed_defer_branch_passes_validator(self) -> None:
        from datetime import datetime, timezone

        from personal_agent.captains_log.feedback import FeedbackEvent, FeedbackPoller

        poller = FeedbackPoller(linear_client=AsyncMock(), state_path=None)
        event = FeedbackEvent(
            issue_id="issue-1",
            issue_identifier="FRE-0000",
            label="Defer",
            issue_title="test",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        with (
            patch("personal_agent.captains_log.feedback.log") as mock_log,
            patch("personal_agent.captains_log.feedback._load_poller_state") as mock_load,
            patch("personal_agent.captains_log.feedback._save_poller_state"),
        ):
            from personal_agent.captains_log.feedback import _PollerState

            mock_load.return_value = _PollerState()
            poller._client.count_open_issues = AsyncMock(return_value=0)
            await poller.process_feedback([event])

        events = [
            call
            for call in mock_log.info.call_args_list
            if call.args and call.args[0] == "feedback_event_processed"
        ]
        assert events, "feedback_event_processed event was not emitted"
        event_type, data = events[0].args[0], events[0].kwargs
        await _assert_document_passes_validator(event_type, data)

    @pytest.mark.asyncio
    async def test_multipath_recall_passes_validator(self, monkeypatch) -> None:
        s = get_settings()
        monkeypatch.setattr(s, "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
        monkeypatch.setattr(s, "structural_arm_enabled", False, raising=False)
        monkeypatch.setattr(s, "reranker_enabled", False, raising=False)

        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        service.lexical_recall_arm = AsyncMock(return_value=[RankedResult("t1", 1, kind="turn")])
        with patch("personal_agent.memory.service.log") as mock_log:
            await service._multipath_fused_recall("vision", path="broad", trace_id="t")

        events = [
            call
            for call in mock_log.info.call_args_list
            if call.args and call.args[0] == "multipath_recall"
        ]
        assert events, "multipath_recall event was not emitted"
        event_type, data = events[0].args[0], events[0].kwargs
        await _assert_document_passes_validator(event_type, data)
