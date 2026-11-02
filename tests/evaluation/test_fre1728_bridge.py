"""FRE-1728 AC-2 and AC-4: the bridge's wipe sequence and its spend stop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from scripts.eval import eval_isolation
from scripts.eval.eval_isolation import ArmTurnResult
from scripts.eval.fre1728 import bridge as bridge_module
from scripts.eval.fre1728.bridge import BridgeDeps, create_app
from scripts.eval.fre1728.spend import SpendGuard, assert_eval_postgres_dsn


@dataclass
class _Runner:
    """A fake runner over a fake graph."""

    events: list[str]
    graph: set[str]
    turns: list[str] = field(default_factory=list)

    async def run_turn(self, _http: Any, _es: Any, message: str, **_kwargs: Any) -> ArmTurnResult:
        self.turns.append(message)
        return ArmTurnResult(
            session_id="s1",
            trace_id=f"t{len(self.turns)}",
            arm="stub",
            extraction_settled=None,
            response=f"reply to {message}",
        )

    async def wipe(self, _es: Any) -> None:
        self.events.append("wipe_graph")
        self.graph.clear()


@dataclass
class _Ops:
    events: list[str]

    async def clear_captures(self) -> None:
        self.events.append("clear_captures")

    async def stop(self) -> None:
        self.events.append("stop_gateway")


def _client(*, gateway_spend: float = 0.0, cap: float = 45.0) -> tuple[httpx.AsyncClient, Any]:
    events: list[str] = []
    graph = {"fact"}
    runner = _Runner(events, graph)

    async def read_spend() -> float:
        return state.gateway_spend

    async def read_memories() -> list[dict[str, str]]:
        return [{"kind": "entity", "content": fact} for fact in sorted(graph)]

    async def find_turns(trace_ids: Sequence[str]) -> list[str]:
        return [t for t in trace_ids if t != "t-missing"]

    async def count_denied() -> int:
        return 2

    deps = BridgeDeps(
        runner=runner,  # type: ignore[arg-type]
        http=AsyncMock(),
        es=AsyncMock(),
        guard=SpendGuard(read_gateway_spend=read_spend, cap_usd=cap),
        ops=_Ops(events),
        arm="stub",
        read_memories=read_memories,
        find_turns=find_turns,
        count_budget_denied=count_denied,
    )

    @dataclass
    class _State:
        gateway_spend: float
        runner: _Runner
        events: list[str]
        graph: set[str]

    state = _State(gateway_spend, runner, events, graph)
    transport = httpx.ASGITransport(app=create_app(deps))
    return httpx.AsyncClient(transport=transport, base_url="http://bridge"), state


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "wait_for_gateway_idle", AsyncMock(return_value=1.5))
    monkeypatch.setattr(eval_isolation, "wait_for_gateway_idle", AsyncMock(return_value=0.0))


@pytest.mark.asyncio
async def test_turn_returns_the_reply_text() -> None:
    client, state = _client()
    response = await client.post("/turn", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["response"] == "reply to hello"
    assert state.runner.turns == ["hello"]


@pytest.mark.asyncio
async def test_wipe_deletes_captures_before_the_graph() -> None:
    """A graph wipe alone is undone: consolidation extracts every capture it finds again."""
    client, state = _client()
    response = await client.post("/wipe")

    assert response.status_code == 200
    assert state.events == ["clear_captures", "wipe_graph"]
    assert response.json()["residual_memories"] == []


@pytest.mark.asyncio
async def test_settle_reports_missing_turns_and_budget_denials() -> None:
    client, _state = _client()
    response = await client.post("/settle", json={"trace_ids": ["t1", "t-missing"]})

    assert response.json() == {
        "waited_s": 1.5,
        "missing_turns": ["t-missing"],
        "budget_denied": 2,
    }


@pytest.mark.asyncio
async def test_memories_reads_the_graph() -> None:
    client, _state = _client()
    response = await client.get("/memories")

    assert response.json() == [{"kind": "entity", "content": "fact"}]


@pytest.mark.asyncio
async def test_ac4_turn_at_the_cap_is_refused_and_the_gateway_stops() -> None:
    client, state = _client(gateway_spend=44.0, cap=45.0)

    first = await client.post("/turn", json={"message": "one"})
    state.gateway_spend = 45.0
    second = await client.post("/turn", json={"message": "two"})
    third = await client.post("/turn", json={"message": "three"})

    assert first.status_code == 200
    assert second.status_code == 402
    assert third.status_code == 402
    assert state.runner.turns == ["one"]
    assert state.events.count("stop_gateway") == 1


@pytest.mark.asyncio
async def test_ac4_simulator_usage_counts_toward_the_cap() -> None:
    client, state = _client(gateway_spend=44.0, cap=45.0)

    usage = await client.post("/usage", json={"prompt_tokens": 2_000_000, "completion_tokens": 0})
    spend = await client.get("/spend")
    turn = await client.post("/turn", json={"message": "one"})

    assert usage.status_code == 200
    assert spend.json() == {"spent_usd": 45.5, "cap_usd": 45.0, "stop": True}
    assert turn.status_code == 402
    assert state.runner.turns == []


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://agent:pw@localhost:5432/personal_agent",
        "postgresql://agent:pw@postgres:5434/personal_agent",
    ],
)
def test_spend_reader_refuses_any_postgres_but_eval(dsn: str) -> None:
    with pytest.raises(ValueError, match="eval Postgres"):
        assert_eval_postgres_dsn(dsn)


def test_paid_arm_needs_the_owners_recorded_go_and_the_stub_does_not() -> None:
    from scripts.eval.fre1728.run import refuse_paid_run_without_go

    refuse_paid_run_without_go("stub", None)
    refuse_paid_run_without_go("control", '"go" (owner, 2026-11-03)')
    for empty in (None, "  "):
        with pytest.raises(SystemExit, match="paid calls"):
            refuse_paid_run_without_go("control", empty)


def test_stub_driver_env_reaches_only_the_stub_and_holds_no_secret() -> None:
    from scripts.eval.fre1728.run import driver_env

    env = driver_env("stub", {"PATH": "/bin", "HOME": "/h", "AGENT_OPENAI_API_KEY": "real"})

    assert env["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
    assert env["OPENAI_API_KEY"] != "real"
    assert set(env) == {"PATH", "HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL"}


@pytest.mark.asyncio
async def test_ac4_a_failed_gateway_stop_is_tried_again() -> None:
    """A stop that failed once must not count as done: the gateway keeps spending."""
    client, state = _client(gateway_spend=45.0, cap=45.0)
    deps_ops = _FlakyOps(state.events)
    client._transport.app.state.deps.ops = deps_ops  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError):
        await client.get("/spend")
    await client.get("/spend")

    assert deps_ops.attempts == 2


class _FlakyOps:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.attempts = 0

    async def clear_captures(self) -> None:
        return None

    async def stop(self) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("docker stop failed")
