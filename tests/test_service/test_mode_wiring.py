"""Integration test: app.py reads live mode from ModeManager, not hardcoded NORMAL.

FRE-246 final wiring — verifies all four Mode.NORMAL hardcodes have been removed
and replaced with get_current_mode() calls.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.service import app as app_module


class TestModeNotHardcoded:
    """Source-level guard: Mode.NORMAL must not appear as a call argument."""

    def test_app_does_not_hardcode_mode_normal_in_chat_handlers(self) -> None:
        """app.py chat handlers must not contain Mode.NORMAL as a kwarg value.

        The four sites that previously hardcoded Mode.NORMAL must now call
        get_current_mode() so the live brainstem mode propagates to the gateway
        and orchestrator session manager.
        """
        source = inspect.getsource(app_module)
        # These patterns must not appear in the module source after FRE-246.
        assert "mode=Mode.NORMAL" not in source, (
            "Found 'mode=Mode.NORMAL' — replace with get_current_mode()"
        )
        assert "Mode.NORMAL, Channel.CHAT" not in source, (
            "Found 'Mode.NORMAL, Channel.CHAT' — replace with get_current_mode()"
        )

    def test_app_imports_get_current_mode(self) -> None:
        """app.py must import get_current_mode from personal_agent.brainstem."""
        assert hasattr(app_module, "get_current_mode"), (
            "get_current_mode not imported into app module"
        )


class TestGetCurrentModeCalledInChat:
    """Functional guard: patching get_current_mode propagates to gateway call."""

    @pytest.mark.asyncio
    @patch("personal_agent.orchestrator.Orchestrator")
    @patch("personal_agent.service.app.SessionRepository")
    @patch("personal_agent.service.app.run_gateway_pipeline", new_callable=AsyncMock)
    @patch("personal_agent.brainstem.expansion.compute_expansion_budget")
    @patch("personal_agent.brainstem.sensors.poll_system_metrics")
    @patch("personal_agent.service.app.get_current_mode")
    async def test_chat_passes_live_mode_to_gateway(
        self,
        mock_get_mode: MagicMock,
        mock_poll: MagicMock,
        mock_compute: MagicMock,
        mock_pipeline: AsyncMock,
        mock_repo_cls: MagicMock,
        mock_orchestrator_cls: MagicMock,
    ) -> None:
        """When get_current_mode returns ALERT the gateway receives ALERT, not NORMAL."""
        from personal_agent.service.app import chat

        mock_get_mode.return_value = Mode.ALERT
        mock_poll.return_value = {"cpu_percent": 20.0, "memory_percent": 40.0}
        mock_compute.return_value = 0
        mock_pipeline.return_value = None

        session = SimpleNamespace(session_id=uuid4(), messages=[])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=session)
        repo.create = AsyncMock(return_value=session)
        repo.append_message = AsyncMock(return_value=None)
        mock_repo_cls.return_value = repo

        session_manager = MagicMock()
        session_manager.get_session.return_value = None
        orchestrator = MagicMock()
        orchestrator.session_manager = session_manager
        orchestrator.handle_user_request = AsyncMock(
            return_value={"reply": "ok", "trace_id": "t-1"}
        )
        mock_orchestrator_cls.return_value = orchestrator

        await chat(message="hello", session_id=None, db=AsyncMock())

        # get_current_mode must have been called (not Mode.NORMAL literal).
        assert mock_get_mode.call_count >= 1, "get_current_mode was never called"

        # Gateway must have received Mode.ALERT, not Mode.NORMAL.
        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args.kwargs
        assert call_kwargs.get("mode") is Mode.ALERT, (
            f"Expected Mode.ALERT but got {call_kwargs.get('mode')!r}"
        )
