"""FRE-1728 AC-2: memory survives a new session, and the wipe runs once per user and run."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from scripts.eval import eval_isolation
from scripts.eval.eval_isolation import IsolatedArmRunner
from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS


class _Graph:
    """A fake eval graph: facts stay until a wipe removes them."""

    def __init__(self) -> None:
        self.facts: set[str] = set()
        self.wipes = 0

    async def wipe(self, _driver: Any, *, uri: str) -> None:
        self.wipes += 1
        self.facts.clear()


class _Http:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def post(self, url: str, **_kwargs: Any) -> Any:
        self.urls.append(url)
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: {"session_id": "s", "trace_id": "t"}
        return response


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> _Graph:
    fake = _Graph()
    monkeypatch.setattr(eval_isolation, "wipe_eval_graph", fake.wipe)
    monkeypatch.setattr(eval_isolation, "wait_for_gateway_idle", AsyncMock(return_value=0.0))
    monkeypatch.setattr(eval_isolation, "wait_for_local_model_calls", AsyncMock(return_value=0.0))
    monkeypatch.setattr(eval_isolation, "wait_for_event_settle", AsyncMock(return_value=True))
    return fake


@pytest.mark.asyncio
async def test_a_fact_survives_the_next_session_when_the_caller_owns_the_wipe(
    graph: _Graph,
) -> None:
    runner = IsolatedArmRunner(driver=object(), wipe_on_new_session=False)
    graph.facts.add("fact")

    await runner.run_turn(_Http(), AsyncMock(), "next session", wait_settle=False)  # type: ignore[arg-type]

    assert graph.facts == {"fact"}
    assert graph.wipes == 0


@pytest.mark.asyncio
async def test_seeded_negative_the_default_wipe_loses_the_fact(graph: _Graph) -> None:
    """The same check fails on the old behaviour, so the check can detect a loss."""
    runner = IsolatedArmRunner(driver=object())
    graph.facts.add("fact")

    await runner.run_turn(_Http(), AsyncMock(), "next session", wait_settle=False)  # type: ignore[arg-type]

    assert graph.facts == set()
    assert graph.wipes == 1


@pytest.mark.asyncio
async def test_wipe_removes_the_fact_and_reseeds(graph: _Graph) -> None:
    reseed = AsyncMock()
    runner = IsolatedArmRunner(driver=object(), reseed=reseed, wipe_on_new_session=False)
    graph.facts.add("fact")

    await runner.wipe(AsyncMock())

    assert graph.facts == set()
    reseed.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_session_without_a_wipe_still_waits_for_local_model_calls(graph: _Graph) -> None:
    runner = IsolatedArmRunner(driver=object(), wipe_on_new_session=False)

    await runner.run_turn(_Http(), AsyncMock(), "next session", wait_settle=False)  # type: ignore[arg-type]

    eval_isolation.wait_for_local_model_calls.assert_awaited_once()  # type: ignore[attr-defined]


def test_stub_arm_is_a_loopback_eval_url() -> None:
    assert EVAL_ARMS["stub"] == "http://localhost:9014"
