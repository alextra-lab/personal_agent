"""Tests for expansion budget wiring in the service layer.

Verifies that the chat endpoint computes an expansion budget from
brainstem sensors and passes it into run_gateway_pipeline() before
each request.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.service.app import chat
from personal_agent.service.auth import RequestUser

_TEST_REQUEST_USER = RequestUser(user_id=uuid4(), email="test@example.com")


def _make_session(messages: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(session_id=uuid4(), messages=messages or [])


def _make_mock_repo(session: SimpleNamespace) -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.create = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    return repo


def _make_mock_orchestrator() -> tuple[MagicMock, MagicMock]:
    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(
        return_value={"reply": "ok", "trace_id": "t-1"}
    )
    return orchestrator, session_manager


class TestExpansionBudgetWiring:
    """Expansion budget must be computed from sensors and passed to pipeline."""

    @pytest.mark.asyncio
    @patch("personal_agent.orchestrator.Orchestrator")
    @patch("personal_agent.service.app.SessionRepository")
    @patch("personal_agent.service.app.run_gateway_pipeline", new_callable=AsyncMock)
    @patch("personal_agent.brainstem.expansion.compute_expansion_budget")
    @patch("personal_agent.brainstem.sensors.poll_system_metrics")
    async def test_expansion_budget_computed_and_passed_to_pipeline(
        self,
        mock_poll: MagicMock,
        mock_compute: MagicMock,
        mock_pipeline: AsyncMock,
        mock_repo_cls: MagicMock,
        mock_orchestrator_cls: MagicMock,
    ) -> None:
        """run_gateway_pipeline receives expansion_budget from brainstem sensors."""
        mock_poll.return_value = {
            "cpu_percent": 20.0,
            "memory_percent": 40.0,
            "active_inference_count": 0,
        }
        mock_compute.return_value = 2
        mock_pipeline.return_value = None

        session = _make_session()
        mock_repo_cls.return_value = _make_mock_repo(session)
        orchestrator, _ = _make_mock_orchestrator()
        mock_orchestrator_cls.return_value = orchestrator

        await chat(message="hello", session_id=None, request_user=_TEST_REQUEST_USER, db=AsyncMock())

        # Sensors polled
        mock_poll.assert_called_once()
        # Budget computed from sensor output
        mock_compute.assert_called_once()
        sensor_arg = mock_compute.call_args[0][0]
        assert sensor_arg["cpu_percent"] == 20.0

        # Budget passed to pipeline
        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args.kwargs
        assert call_kwargs.get("expansion_budget") == 2

    @pytest.mark.asyncio
    @patch("personal_agent.orchestrator.Orchestrator")
    @patch("personal_agent.service.app.SessionRepository")
    @patch("personal_agent.service.app.run_gateway_pipeline", new_callable=AsyncMock)
    @patch("personal_agent.brainstem.expansion.compute_expansion_budget")
    @patch("personal_agent.brainstem.sensors.poll_system_metrics")
    async def test_sensor_failure_defaults_budget_to_zero(
        self,
        mock_poll: MagicMock,
        mock_compute: MagicMock,
        mock_pipeline: AsyncMock,
        mock_repo_cls: MagicMock,
        mock_orchestrator_cls: MagicMock,
    ) -> None:
        """When sensor polling raises, expansion_budget defaults to 0 (safe)."""
        mock_poll.side_effect = RuntimeError("sensor unavailable")
        mock_pipeline.return_value = None

        session = _make_session()
        mock_repo_cls.return_value = _make_mock_repo(session)
        orchestrator, _ = _make_mock_orchestrator()
        mock_orchestrator_cls.return_value = orchestrator

        await chat(message="hello", session_id=None, request_user=_TEST_REQUEST_USER, db=AsyncMock())

        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args.kwargs
        assert call_kwargs.get("expansion_budget") == 0

    @pytest.mark.asyncio
    @patch("personal_agent.orchestrator.Orchestrator")
    @patch("personal_agent.service.app.SessionRepository")
    @patch("personal_agent.service.app.run_gateway_pipeline", new_callable=AsyncMock)
    @patch("personal_agent.brainstem.expansion.compute_expansion_budget")
    @patch("personal_agent.brainstem.sensors.poll_system_metrics")
    async def test_high_load_reduces_budget(
        self,
        mock_poll: MagicMock,
        mock_compute: MagicMock,
        mock_pipeline: AsyncMock,
        mock_repo_cls: MagicMock,
        mock_orchestrator_cls: MagicMock,
    ) -> None:
        """Under load, compute_expansion_budget returns reduced budget."""
        mock_poll.return_value = {
            "cpu_percent": 92.0,
            "memory_percent": 40.0,
            "active_inference_count": 0,
        }
        mock_compute.return_value = 0  # Simulating critical CPU pressure
        mock_pipeline.return_value = None

        session = _make_session()
        mock_repo_cls.return_value = _make_mock_repo(session)
        orchestrator, _ = _make_mock_orchestrator()
        mock_orchestrator_cls.return_value = orchestrator

        await chat(message="hello", session_id=None, request_user=_TEST_REQUEST_USER, db=AsyncMock())

        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args.kwargs
        assert call_kwargs.get("expansion_budget") == 0
