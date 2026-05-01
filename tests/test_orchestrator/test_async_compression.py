"""Tests for async background compression manager (ADR-0038 + ADR-0061)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator import compression_manager
from personal_agent.telemetry.within_session_compression import (
    WithinSessionCompressionRecord,
)


def _msg(role: str, size: int) -> dict[str, Any]:
    return {"role": role, "content": "x" * size}


def _record(*, summariser_called: bool) -> WithinSessionCompressionRecord:
    from datetime import datetime, timezone

    return WithinSessionCompressionRecord(
        trace_id="t1",
        session_id="s1",
        trigger="soft",
        head_tokens=10,
        middle_tokens_in=5000,
        middle_tokens_out=200 if summariser_called else 4500,
        tail_tokens=400,
        pre_pass_replacements=2,
        summariser_called=summariser_called,
        summariser_duration_ms=1000 if summariser_called else 0,
        compressed_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Ensure clean compression state between tests."""
    compression_manager.clear_all()
    yield  # type: ignore[misc]
    compression_manager.clear_all()


class TestGetSummary:
    def test_returns_none_when_no_summary(self) -> None:
        assert compression_manager.get_summary("session-1") is None

    @pytest.mark.asyncio
    async def test_returns_summary_from_completed_task(self) -> None:
        summary_text = "## Conversation Summary\n- Decisions: foo"
        compressed = [
            {"role": "system", "content": "sys"},
            {"role": "system", "content": summary_text},
            {"role": "user", "content": "tail"},
        ]

        async def fake_compress(*args: Any, **kwargs: Any):
            return compressed, _record(summariser_called=True)

        with patch(
            "personal_agent.orchestrator.within_session_compression.compress_in_place",
            side_effect=fake_compress,
        ):
            task = asyncio.create_task(
                compression_manager._run_compression(
                    "s1", [_msg("user", 100)], "t1", None
                )
            )
            compression_manager._pending_tasks["s1"] = task
            await task

        result = compression_manager.get_summary("s1")
        assert result == summary_text

    def test_consumes_summary_on_read(self) -> None:
        compression_manager._summaries["s1"] = "some summary"
        assert compression_manager.get_summary("s1") == "some summary"
        assert compression_manager.get_summary("s1") is None


class TestMaybeTriggerCompression:
    def test_does_not_trigger_when_disabled(self) -> None:
        messages = [_msg("system", 40)] + [_msg("user", 4000) for _ in range(5)]
        with patch.object(
            compression_manager.settings,
            "context_compression_enabled",
            False,
        ):
            compression_manager.maybe_trigger_compression("s1", messages, "t1")

        assert "s1" not in compression_manager._pending_tasks

    def test_does_not_trigger_when_within_session_disabled(self) -> None:
        """ADR-0061 master kill switch."""
        messages = [_msg("system", 40)] + [_msg("user", 4000) for _ in range(10)]
        with (
            patch.object(compression_manager.settings, "context_window_max_tokens", 100),
            patch.object(
                compression_manager.settings,
                "within_session_compression_enabled",
                False,
            ),
        ):
            compression_manager.maybe_trigger_compression("s1", messages, "t1")
        assert "s1" not in compression_manager._pending_tasks

    def test_does_not_trigger_below_threshold(self) -> None:
        messages = [_msg("system", 40), _msg("user", 40)]
        compression_manager.maybe_trigger_compression("s1", messages, "t1")
        assert "s1" not in compression_manager._pending_tasks

    @pytest.mark.asyncio
    async def test_triggers_above_threshold(self) -> None:
        max_tokens = 2048
        threshold_ratio = 0.65
        target = int(max_tokens * threshold_ratio)

        per_msg_chars = 200
        msg_count = (target * 4 // per_msg_chars) + 5
        messages = [_msg("system", 40)] + [
            _msg("user" if i % 2 == 0 else "assistant", per_msg_chars)
            for i in range(msg_count)
        ]

        fake_compress = AsyncMock(
            return_value=(
                [{"role": "system", "content": "## Conversation Summary\nx"}],
                _record(summariser_called=True),
            )
        )
        with (
            patch.object(compression_manager.settings, "context_window_max_tokens", max_tokens),
            patch.object(
                compression_manager.settings,
                "context_compression_threshold_ratio",
                threshold_ratio,
            ),
            patch(
                "personal_agent.orchestrator.within_session_compression.compress_in_place",
                fake_compress,
            ),
        ):
            compression_manager.maybe_trigger_compression("s1", messages, "t1")
            assert "s1" in compression_manager._pending_tasks

            await compression_manager._pending_tasks["s1"]

        assert "## Conversation Summary" in compression_manager._summaries["s1"]
        assert compression_manager._last_compressed_at_msgcount["s1"] == len(messages)

    @pytest.mark.asyncio
    async def test_does_not_trigger_if_already_pending(self) -> None:
        never_done: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        fake_task = asyncio.ensure_future(never_done)
        compression_manager._pending_tasks["s1"] = fake_task

        messages = [_msg("system", 40)] + [_msg("user", 4000) for _ in range(10)]
        with patch.object(compression_manager.settings, "context_window_max_tokens", 100):
            compression_manager.maybe_trigger_compression("s1", messages, "t1")

        assert compression_manager._pending_tasks["s1"] is fake_task
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass

    def test_refire_cursor_blocks_immediate_retrigger(self) -> None:
        """ADR-0061 §D1 — second compression must wait for refire_after new messages."""
        messages = [_msg("system", 40)] + [_msg("user", 4000) for _ in range(10)]
        compression_manager._last_compressed_at_msgcount["s1"] = len(messages)
        with (
            patch.object(compression_manager.settings, "context_window_max_tokens", 100),
            patch.object(
                compression_manager.settings,
                "within_session_compression_refire_after_messages",
                4,
            ),
        ):
            # Add 1 new message — below the floor.
            messages.append(_msg("user", 4000))
            compression_manager.maybe_trigger_compression("s1", messages, "t1")
        assert "s1" not in compression_manager._pending_tasks

    @pytest.mark.asyncio
    async def test_refire_cursor_allows_retrigger_past_floor(self) -> None:
        """ADR-0061 §D1 — once enough messages have been added, soft fires again."""
        messages = [_msg("system", 40)] + [_msg("user", 4000) for _ in range(10)]
        compression_manager._last_compressed_at_msgcount["s1"] = len(messages)

        fake_compress = AsyncMock(
            return_value=(
                [{"role": "system", "content": "## Conversation Summary\nx"}],
                _record(summariser_called=True),
            )
        )
        with (
            patch.object(compression_manager.settings, "context_window_max_tokens", 100),
            patch.object(
                compression_manager.settings,
                "within_session_compression_refire_after_messages",
                4,
            ),
            patch(
                "personal_agent.orchestrator.within_session_compression.compress_in_place",
                fake_compress,
            ),
        ):
            messages.extend([_msg("user", 4000) for _ in range(4)])
            compression_manager.maybe_trigger_compression("s1", messages, "t1")
            assert "s1" in compression_manager._pending_tasks
            await compression_manager._pending_tasks["s1"]

        assert compression_manager._last_compressed_at_msgcount["s1"] == len(messages)


class TestCleanup:
    def test_cleanup_session(self) -> None:
        compression_manager._summaries["s1"] = "old"
        compression_manager._last_compressed_at_msgcount["s1"] = 10
        compression_manager.cleanup_session("s1")
        assert "s1" not in compression_manager._summaries
        assert "s1" not in compression_manager._last_compressed_at_msgcount

    def test_clear_all(self) -> None:
        compression_manager._summaries["s1"] = "a"
        compression_manager._summaries["s2"] = "b"
        compression_manager._last_compressed_at_msgcount["s1"] = 1
        compression_manager.clear_all()
        assert len(compression_manager._summaries) == 0
        assert len(compression_manager._pending_tasks) == 0
        assert len(compression_manager._last_compressed_at_msgcount) == 0
