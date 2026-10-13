"""ADR-0156 A5.3–A5.5 (FRE-1595 AC-1): a skill-provided tool, revealed by read_skill.

The tests use a test tool (``probe_tool``, skill-provided) and two seeded skills that provide it.
No production tool is skill-provided yet (T-C2 makes the telemetry tools so).

The check order for a primary call to a skill-provided tool (A5.5): registry and arguments →
the reveal → the mode → the loop gate → the D8 rules and the card → execution. A call that fails
the reveal or the mode creates no card (``execute_tool`` never runs) and no loop-gate count.

The spike (FRE-1594) chose the simple form: a revealed tool joins the ``tools`` array of the
primary request for the rest of the session.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import text

import personal_agent.orchestrator.executor as ex
import personal_agent.orchestrator.skills as skills_module
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.read_skill import build_read_skill_tool, read_skill_executor
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult

pytestmark = pytest.mark.asyncio

_TOOL = "probe_tool"


# ── fixtures ─────────────────────────────────────────────────────────────────────


def _write_skill(root: Path, name: str, provides: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Covers the {name} case.\n"
        f"metadata:\n  provides-tools: {provides}\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


def _probe_tool(modes: tuple[str, ...] = ("NORMAL",)) -> ToolDefinition:
    return ToolDefinition(
        name=_TOOL,
        description="Measures a probe value.",
        category="read_only",
        parameters=[
            ToolParameter(name="window", type="string", description="A window.", required=False)
        ],
        risk_level="low",
        allowed_modes=list(modes),
        skill_provided=True,
    )


def _plain_tool() -> ToolDefinition:
    return ToolDefinition(
        name="plain_tool",
        description="A plain tool.",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL", "ALERT"],
    )


class _FakeLayer:
    """The registry plus an ``execute_tool`` that records each call that reaches it.

    ``execute_tool`` is where the D8 rules and the approval card run, so a call that never
    reaches it never shows a card.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.governance_config = MagicMock()
        self.executed: list[str] = []

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any], trace_ctx: TraceContext, **_kw: Any
    ) -> ToolResult:
        self.executed.append(tool_name)
        if tool_name == "read_skill":
            output = await read_skill_executor(ctx=trace_ctx, **arguments)
        else:
            output = {"value": 42}
        return ToolResult(tool_name=tool_name, success=True, output=output, latency_ms=1.0)


@pytest.fixture
def layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeLayer:
    root = tmp_path / "skills"
    _write_skill(root, "finds-probes", _TOOL)
    _write_skill(root, "explains-probes", _TOOL)
    monkeypatch.setattr(skills_module, "SKILLS_DIR", root)
    monkeypatch.setattr(skills_module, "_cache", None)

    registry = ToolRegistry()
    registry.register(build_read_skill_tool(), read_skill_executor)
    registry.register(_probe_tool(), lambda **_: {})
    registry.register(_plain_tool(), lambda **_: {})
    fake = _FakeLayer(registry)

    monkeypatch.setattr(ex, "_tool_registry", registry)
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: fake)
    monkeypatch.setattr(ex, "get_default_registry", lambda: registry)
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)
    monkeypatch.setattr(ex, "_report_turn_progress", AsyncMock())
    monkeypatch.setattr(ex, "get_mode_manager", lambda: _Modes(Mode.NORMAL))
    return fake


class _Modes:
    def __init__(self, mode: Mode) -> None:
        self.mode = mode

    def get_current_mode(self) -> Mode:
        return self.mode


def _ctx(**kwargs: Any) -> ExecutionContext:
    return ExecutionContext(
        session_id=kwargs.pop("session_id", "session-fre1595"),
        trace_id="trace-fre1595",
        user_message="Measure the probe.",
        mode=kwargs.pop("mode", Mode.NORMAL),
        channel=Channel.CHAT,
        **kwargs,
    )


def _call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def _respond(ctx: ExecutionContext, *calls: dict[str, Any]) -> None:
    ctx.messages.append({"role": "assistant", "content": "", "tool_calls": list(calls)})


def _result(ctx: ExecutionContext, call_id: str) -> dict[str, Any]:
    for msg in ctx.messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            parsed: dict[str, Any] = json.loads(msg["content"])
            return parsed
    raise AssertionError(f"no result for {call_id}")


async def _round(ctx: ExecutionContext) -> TaskState:
    return await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id=ctx.trace_id))


def _listed(ctx: ExecutionContext) -> set[str]:
    return {d["function"]["name"] for d in ex._primary_tool_definitions(ctx)}


def _assert_refused_unrevealed(result: dict[str, Any]) -> None:
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_revealed"
    assert "explains-probes, finds-probes" in result["error"]["message"]


# ── AC-1: the order of the checks ──────────────────────────────────────────────────


async def test_a_call_before_the_reveal_is_refused(layer: _FakeLayer) -> None:
    ctx = _ctx()
    assert _TOOL not in _listed(ctx)
    _respond(ctx, _call("p1", _TOOL))

    assert await _round(ctx) == TaskState.LLM_CALL

    _assert_refused_unrevealed(_result(ctx, "p1"))
    assert layer.executed == []  # no card: the D8 rules never ran
    assert _TOOL not in ctx.loop_gate._fsms  # no loop-gate count


@pytest.mark.parametrize("read_first", [True, False], ids=["read_skill_first", "tool_first"])
async def test_a_call_beside_the_revealing_read_skill_is_refused(
    layer: _FakeLayer, read_first: bool
) -> None:
    ctx = _ctx()
    read = _call("r1", "read_skill", {"name": "finds-probes"})
    probe = _call("p1", _TOOL)
    _respond(ctx, *((read, probe) if read_first else (probe, read)))

    await _round(ctx)

    _assert_refused_unrevealed(_result(ctx, "p1"))
    assert _result(ctx, "r1")["ok"] is True
    assert layer.executed == ["read_skill"]
    assert _TOOL not in ctx.loop_gate._fsms
    assert ctx.revealed_tools == {_TOOL}


async def test_a_call_in_the_next_round_runs(layer: _FakeLayer) -> None:
    ctx = _ctx()
    _respond(ctx, _call("r1", "read_skill", {"name": "explains-probes"}))
    await _round(ctx)
    assert _TOOL in _listed(ctx)

    _respond(ctx, _call("p2", _TOOL, {"window": "1h"}))
    await _round(ctx)

    assert _result(ctx, "p2") == {"ok": True, "value": 42}
    assert layer.executed == ["read_skill", _TOOL]
    assert ctx.loop_gate._fsms[_TOOL].total_calls == 1


async def test_a_file_read_reveals_nothing(layer: _FakeLayer) -> None:
    """Only a body read loads the skill; a file read of it does not reveal its tools."""
    ctx = _ctx()
    _respond(ctx, _call("r1", "read_skill", {"name": "finds-probes", "path": "x.md"}))
    await _round(ctx)
    assert ctx.revealed_tools == set()


async def test_a_later_turn_lists_and_runs_the_tool(layer: _FakeLayer) -> None:
    """A new turn whose context loaded the record needs no new read_skill."""
    ctx = _ctx()
    ctx.revealed_tools = {_TOOL}
    assert _TOOL in _listed(ctx)
    _respond(ctx, _call("p1", _TOOL))

    await _round(ctx)

    assert _result(ctx, "p1")["ok"] is True
    assert layer.executed == [_TOOL]


async def test_a_mode_that_refuses_the_tool_hides_and_refuses_it(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(mode=Mode.ALERT)
    ctx.revealed_tools = {_TOOL}
    monkeypatch.setattr(ex, "get_mode_manager", lambda: _Modes(Mode.ALERT))
    persist = AsyncMock()
    monkeypatch.setattr(ex, "_persist_revealed_tools", persist)

    assert _TOOL not in _listed(ctx)
    assert "plain_tool" in _listed(ctx)
    _respond(ctx, _call("p1", _TOOL))
    await _round(ctx)

    result = _result(ctx, "p1")
    assert result["ok"] is False
    assert result["error"]["code"] == "mode_refused"
    assert layer.executed == []
    assert _TOOL not in ctx.loop_gate._fsms
    # The record is kept: a later turn in a mode that allows the tool sees it again.
    assert ctx.revealed_tools == {_TOOL}
    persist.assert_not_awaited()


async def test_a_mode_change_inside_the_turn_refuses_a_listed_tool(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is checked on every round, against the live mode, not the turn's start mode."""
    ctx = _ctx()
    ctx.revealed_tools = {_TOOL}
    monkeypatch.setattr(ex, "get_mode_manager", lambda: _Modes(Mode.ALERT))
    _respond(ctx, _call("p1", _TOOL))
    await _round(ctx)
    assert _result(ctx, "p1")["error"]["code"] == "mode_refused"
    assert layer.executed == []


# ── the request: the simple form ───────────────────────────────────────────────────


def _llm_client(tool_name: str | None) -> MagicMock:
    from personal_agent.llm_client.models import Dialect

    calls = [{"id": "c1", "name": tool_name, "arguments": "{}"}] if tool_name else []
    client = MagicMock()
    client.model_configs = {}
    client.dialect_for_role.return_value = Dialect.LLAMACPP_QWEN
    client.respond = AsyncMock(
        return_value={
            "content": "" if calls else "Done.",
            "tool_calls": calls,
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    return client


async def _sent_tools(ctx: ExecutionContext) -> set[str]:
    client = _llm_client("read_skill")
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])
    with patch("personal_agent.llm_client.factory.get_llm_client", return_value=client):
        await ex.step_llm_call(ctx, session, TraceContext.new_trace())
    tools = client.respond.call_args.kwargs["tools"] or []
    return {t["function"]["name"] for t in tools}


async def test_the_request_adds_the_tool_after_the_reveal(layer: _FakeLayer) -> None:
    ctx = _ctx(messages=[{"role": "user", "content": "Measure the probe."}])
    ctx.turn_started_at = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    before = await _sent_tools(ctx)
    assert _TOOL not in before
    assert {"read_skill", "plain_tool"} <= before

    _respond(ctx, _call("r1", "read_skill", {"name": "finds-probes"}))
    await _round(ctx)
    after = await _sent_tools(ctx)

    assert after == before | {_TOOL}


# ── the record: load and add ───────────────────────────────────────────────────────


class _NoDb:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _owned_ctx() -> ExecutionContext:
    return _ctx(session_id=str(uuid4()), user_id=uuid4())


async def test_a_failed_load_gives_an_empty_set(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("personal_agent.service.database.AsyncSessionLocal", _NoDb)
    monkeypatch.setattr(
        "personal_agent.service.repositories.session_repository.SessionRepository.load_revealed_tools",
        AsyncMock(side_effect=RuntimeError("database down")),
    )
    with structlog.testing.capture_logs() as logs:
        loaded = await ex._load_revealed_tools(_owned_ctx())
    assert loaded == set()
    assert [e["event"] for e in logs if e["event"] == "revealed_tools_load_failed"] == [
        "revealed_tools_load_failed"
    ]


async def test_a_name_that_is_no_longer_skill_provided_is_ignored(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("personal_agent.service.database.AsyncSessionLocal", _NoDb)
    monkeypatch.setattr(
        "personal_agent.service.repositories.session_repository.SessionRepository.load_revealed_tools",
        AsyncMock(return_value=frozenset({_TOOL, "renamed_tool", "plain_tool"})),
    )
    with structlog.testing.capture_logs() as logs:
        loaded = await ex._load_revealed_tools(_owned_ctx())
    assert loaded == {_TOOL}
    stale = [e for e in logs if e["event"] == "revealed_tools_stale_names_ignored"]
    assert stale[0]["names"] == ["plain_tool", "renamed_tool"]


@pytest.mark.parametrize(
    ("add", "event"),
    [
        (AsyncMock(side_effect=RuntimeError("database down")), "revealed_tools_add_failed"),
        (AsyncMock(return_value=0), "revealed_tools_add_no_row"),
    ],
    ids=["raises", "no_row"],
)
async def test_a_failed_add_keeps_the_reveal_for_the_turn(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch, add: AsyncMock, event: str
) -> None:
    monkeypatch.setattr("personal_agent.service.database.AsyncSessionLocal", _NoDb)
    monkeypatch.setattr(
        "personal_agent.service.repositories.session_repository.SessionRepository.add_revealed_tools",
        add,
    )
    ctx = _owned_ctx()
    _respond(ctx, _call("r1", "read_skill", {"name": "finds-probes"}))
    with structlog.testing.capture_logs() as logs:
        await _round(ctx)

    add.assert_awaited_once()
    assert event in [e["event"] for e in logs]
    assert ctx.revealed_tools == {_TOOL}
    _respond(ctx, _call("p2", _TOOL))
    await _round(ctx)
    assert _result(ctx, "p2")["ok"] is True


async def test_a_failed_load_lets_the_turn_run(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = AsyncMock(side_effect=RuntimeError("database down"))
    monkeypatch.setattr("personal_agent.service.database.AsyncSessionLocal", _NoDb)
    monkeypatch.setattr(
        "personal_agent.service.repositories.session_repository.SessionRepository.load_revealed_tools",
        load,
    )
    ctx, sm = _init_ctx(session_id=str(uuid4()), user_id=uuid4())
    ctx.revealed_tools = {"left_over"}
    with structlog.testing.capture_logs() as logs:
        assert await ex.step_init(ctx, sm, TraceContext.new_trace()) == TaskState.LLM_CALL
    load.assert_awaited_once()
    assert ctx.revealed_tools == set()
    assert "revealed_tools_load_failed" in [e["event"] for e in logs]


# ── the dispatch boundary and the worker ───────────────────────────────────────────


async def test_a_direct_primary_dispatch_is_refused(layer: _FakeLayer) -> None:
    async def dispatch(**kwargs: Any) -> dict[str, Any]:
        return await dispatch_tool_call(
            tool_call_id="d1",
            tool_name=_TOOL,
            arguments={},
            tool_layer=layer,  # type: ignore[arg-type]
            trace_ctx=TraceContext(trace_id="t"),
            trace_id="t",
            session_id=None,
            loaded_skills=set(),
            **kwargs,
        )

    refused = await dispatch()
    _assert_refused_unrevealed(json.loads(refused["content"]))
    assert layer.executed == []

    assert (await dispatch(revealed_tools={_TOOL}))["success"] is True
    assert (await dispatch(principal="sub_agent"))["success"] is True
    assert layer.executed == [_TOOL, _TOOL]


async def test_a_worker_lists_a_skill_provided_tool_that_it_holds(
    layer: _FakeLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_agent.orchestrator import sub_agent

    monkeypatch.setattr(sub_agent, "get_shared_tool_execution_layer", lambda: layer)
    defs = sub_agent._build_tool_defs([_TOOL, "plain_tool"])
    assert {d["function"]["name"] for d in defs or []} == {_TOOL, "plain_tool"}


# ── a new gateway process reads the record from Postgres ───────────────────────────


def _init_ctx(**kwargs: Any) -> tuple[ExecutionContext, Any]:
    from personal_agent.orchestrator.session import SessionManager
    from personal_agent.request_gateway.types import (
        AssembledContext,
        Complexity,
        DecompositionResult,
        DecompositionStrategy,
        GatewayOutput,
        GovernanceContext,
        IntentResult,
        TaskType,
    )

    sm = SessionManager()
    ctx = _ctx(eval_mode=True, **kwargs)
    ctx.gateway_output = GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.TOOL_USE, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="test"),
        context=AssembledContext(messages=[], memory_context=None, tool_definitions=None),
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
    )
    return ctx, sm


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


async def test_a_new_context_reads_the_reveal_from_postgres(layer: _FakeLayer) -> None:
    """Turn 1 reveals and persists; a fresh context (a new process) loads it in step_init."""
    if not _postgres_available():
        pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")
    from personal_agent.service.database import AsyncSessionLocal, engine

    await engine.dispose()
    user_id, session_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
            {"uid": user_id, "email": f"fre1595-{user_id}@test.invalid"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at)"
                " VALUES (:sid, :uid, :now, :now)"
            ),
            {"sid": session_id, "uid": user_id, "now": now},
        )
        await db.commit()
    try:
        first = _ctx(session_id=str(session_id), user_id=user_id)
        _respond(first, _call("r1", "read_skill", {"name": "finds-probes"}))
        await _round(first)
        assert first.revealed_tools == {_TOOL}

        second, sm = _init_ctx(session_id=str(session_id), user_id=user_id)
        assert second.revealed_tools == set()
        assert await ex.step_init(second, sm, TraceContext.new_trace()) == TaskState.LLM_CALL
        assert second.revealed_tools == {_TOOL}
        assert _TOOL in _listed(second)

        stranger, sm = _init_ctx(session_id=str(session_id), user_id=uuid4())
        await ex.step_init(stranger, sm, TraceContext.new_trace())
        assert stranger.revealed_tools == set()
    finally:
        async with AsyncSessionLocal() as db:
            # step_init emits transport events for the session (FK to sessions).
            for table in ("session_events", "sessions"):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE session_id = :sid"), {"sid": session_id}
                )
            await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
            await db.commit()
        await engine.dispose()
