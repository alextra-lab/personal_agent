"""Unit regressions for evaluation harness runner behavior."""

from __future__ import annotations

from types import TracebackType
from unittest.mock import AsyncMock

import pytest

from tests.evaluation.harness.models import ConversationPath, SessionSpec
from tests.evaluation.harness.runner import EvaluationRunner


@pytest.mark.asyncio
async def test_inference_probe_does_not_send_invalid_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe should not include an invalid non-UUID session_id query parameter."""
    captured_params: dict[str, str | None] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, timeout: float) -> None:
            self._timeout = timeout

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

        async def post(self, url: str, params: dict[str, str]) -> _FakeResponse:
            _ = url
            captured_params.update(params)
            return _FakeResponse()

    monkeypatch.setattr("tests.evaluation.harness.runner.httpx.AsyncClient", _FakeAsyncClient)

    runner = EvaluationRunner()
    responsive = await runner.check_inference_responsive()

    assert responsive is True
    assert captured_params.get("message") == "ping"
    assert "session_id" not in captured_params


@pytest.mark.asyncio
async def test_single_session_path_awaits_post_path_assertions() -> None:
    """Single-session path must await async post-path assertion finalization."""
    runner = EvaluationRunner()
    runner.create_session = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")  # type: ignore[method-assign]
    runner._run_post_path_assertions = AsyncMock()  # type: ignore[method-assign]

    path = ConversationPath(
        path_id="CP-T1",
        name="Single session await test",
        category="Harness Unit",
        objective="Verify await usage",
        turns=(),
    )

    await runner._run_single_session_path(path)

    runner._run_post_path_assertions.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_session_path_awaits_post_path_assertions() -> None:
    """Multi-session path must await async post-path assertion finalization."""
    runner = EvaluationRunner()
    runner.create_session = AsyncMock(return_value="00000000-0000-0000-0000-000000000002")  # type: ignore[method-assign]
    runner._run_post_path_assertions = AsyncMock()  # type: ignore[method-assign]

    path = ConversationPath(
        path_id="CP-T2",
        name="Multi session await test",
        category="Harness Unit",
        objective="Verify await usage",
        sessions=(SessionSpec(turns=()),),
    )

    await runner._run_multi_session_path(path)

    runner._run_post_path_assertions.assert_awaited_once()
