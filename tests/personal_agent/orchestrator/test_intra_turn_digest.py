"""Tests for the birth-time intra-turn digest pass + expand tool (ADR-0085, FRE-475).

Birth-time / case-(a) semantics (redesign after PR-B's keep-deferred A/B failure):
`apply_intra_turn_digest` operates on the FRESH `tool_results` list BEFORE
`ctx.messages.extend(...)`, digesting non-pinned oversized results in place so the
verbatim bytes never enter `ctx.messages` (no cached-prefix invalidation). Reads are
pinned (kept verbatim — the model may edit against them); released-pin deferred
digestion is out of scope (FRE-485).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from personal_agent.config import settings
from personal_agent.orchestrator.tool_result_digest import (
    apply_intra_turn_digest,
    compute_content_hash,
)
from personal_agent.orchestrator.types import ToolResultPin

_SID = str(uuid4())


def _tool_msg(tool_call_id: str, name: str, content: str) -> dict[str, object]:
    return {"tool_call_id": tool_call_id, "role": "tool", "name": name, "content": content}


def _big_bash(seed: str = "x") -> str:
    return json.dumps(
        {"stdout": "\n".join(f"{seed} {i}" for i in range(2000)), "exit_code": 0, "command": "c"}
    )


def _ctx(*, round_: int = 1, pins=None, messages=None) -> SimpleNamespace:
    return SimpleNamespace(
        messages=messages if messages is not None else [],
        tool_iteration_count=round_,
        tool_result_pins=pins if pins is not None else {},
        session_id=_SID,
        trace_id="trace1",
    )


def _sidecar(**entries: dict[str, object]) -> dict[str, dict[str, object]]:
    return dict(entries)


class _FakeStore:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.puts: list[str] = []
        self.objects: dict[str, bytes] = {}

    async def put(self, *, r2_key, content, content_type, metadata=None, trace_id=None) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            from personal_agent.storage.artifact_store import ArtifactStoreError

            raise ArtifactStoreError("boom")
        self.puts.append(r2_key)
        self.objects[r2_key] = content

    async def get(self, r2_key, *, trace_id=None) -> bytes:
        return self.objects[r2_key]


def _is_digest(msg: dict[str, object]) -> bool:
    try:
        return json.loads(str(msg["content"])).get("_digest") is True
    except (TypeError, ValueError):
        return False


@pytest.fixture(autouse=True)
def _digest_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", 50)
    monkeypatch.setattr(settings, "tool_result_digest_min_savings_tokens", 10)
    monkeypatch.setattr(settings, "tool_result_digest_keep", 1)
    monkeypatch.setattr(settings, "tool_result_digest_pin_ttl_turns", 4)
    monkeypatch.setattr(settings, "tool_result_digest_put_timeout_ms", 2000)
    monkeypatch.setattr(settings, "tool_result_digest_exclude_tools", [])


# ---------------------------------------------------------------------------
# Birth-time / case-(a) invariant — the fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_birth_time_digests_bash_before_extend() -> None:
    """The fresh bash result is digested in place; the verbatim bytes never
    enter ctx.messages on the subsequent extend (case-(a), zero invalidation).
    """
    tool_results = [_tool_msg("t1", "bash", _big_bash("out"))]
    sidecar = _sidecar(t1={"tool_name": "bash", "success": True, "arguments": {"command": "c"}})
    ctx = _ctx()
    store = _FakeStore()

    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=store)
    assert _is_digest(tool_results[0])  # digested in the fresh batch
    assert len(store.puts) == 1
    # adjacency fields preserved
    assert tool_results[0]["tool_call_id"] == "t1"
    assert tool_results[0]["role"] == "tool"
    assert tool_results[0]["name"] == "bash"

    # Simulate the executor's extend: only the digest enters ctx.messages.
    ctx.messages.extend(tool_results)
    assert _is_digest(ctx.messages[-1])
    assert "out 1000" not in str(ctx.messages[-1]["content"])  # bulk never entered the prefix


@pytest.mark.asyncio
async def test_read_stays_verbatim_pinned() -> None:
    tool_results = [_tool_msg("r1", "read", _big_bash("src"))]
    sidecar = _sidecar(r1={"tool_name": "read", "success": True, "arguments": {"path": "/a.py"}})
    ctx = _ctx()
    store = _FakeStore()

    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=store)
    assert not _is_digest(tool_results[0])  # read pinned → verbatim
    assert "r1" in ctx.tool_result_pins
    assert len(store.puts) == 0


@pytest.mark.asyncio
async def test_mixed_batch_digests_bash_keeps_read() -> None:
    tool_results = [
        _tool_msg("r1", "read", _big_bash("src")),
        _tool_msg("b1", "bash", _big_bash("out")),
    ]
    sidecar = _sidecar(
        r1={"tool_name": "read", "success": True, "arguments": {"path": "/a.py"}},
        b1={"tool_name": "bash", "success": True, "arguments": {"command": "c"}},
    )
    ctx = _ctx()
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=_FakeStore())
    assert not _is_digest(tool_results[0])  # read verbatim
    assert _is_digest(tool_results[1])  # bash digested


@pytest.mark.asyncio
async def test_error_payload_and_small_and_excluded_stay_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_exclude_tools", ["web"])
    err = json.dumps({"status": "error", "hint": "x " * 5000})
    tool_results = [
        _tool_msg("e1", "bash", err),  # error payload → verbatim
        _tool_msg("s1", "bash", "tiny"),  # below threshold → verbatim
        _tool_msg("w1", "web", _big_bash("page")),  # excluded tool → verbatim
    ]
    sidecar = _sidecar(
        e1={"tool_name": "bash", "success": False, "arguments": {}},
        s1={"tool_name": "bash", "success": True, "arguments": {}},
        w1={"tool_name": "web", "success": True, "arguments": {}},
    )
    ctx = _ctx()
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=_FakeStore())
    assert not any(_is_digest(r) for r in tool_results)


@pytest.mark.asyncio
async def test_put_timeout_leaves_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_put_timeout_ms", 20)
    tool_results = [_tool_msg("b1", "bash", _big_bash("out"))]
    sidecar = _sidecar(b1={"tool_name": "bash", "success": True, "arguments": {}})
    ctx = _ctx()
    store = _FakeStore(delay=0.5)  # exceeds 20ms ceiling
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=store)
    assert not _is_digest(tool_results[0])


@pytest.mark.asyncio
async def test_already_digested_is_idempotent() -> None:
    tool_results = [_tool_msg("b1", "bash", _big_bash("out"))]
    sidecar = _sidecar(b1={"tool_name": "bash", "success": True, "arguments": {}})
    ctx = _ctx()
    store = _FakeStore()
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=store)
    first = tool_results[0]["content"]
    # Re-running the pass over the now-digested batch is a no-op (no second put).
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=store)
    assert tool_results[0]["content"] == first
    assert len(store.puts) == 1


# ---------------------------------------------------------------------------
# D4 pin bookkeeping (release semantics — state only; no deferred digestion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_released_on_prior_round_write() -> None:
    pins = {"r0": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(round_=2, pins=pins)
    sidecar = _sidecar(w1={"tool_name": "write", "success": True, "arguments": {"path": "/a.py"}})
    await apply_intra_turn_digest(ctx, [], sidecar, trace_ctx=None, store=_FakeStore())
    assert "r0" not in ctx.tool_result_pins  # released


@pytest.mark.asyncio
async def test_failed_write_does_not_release_pin() -> None:
    pins = {"r0": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(round_=2, pins=pins)
    sidecar = _sidecar(w1={"tool_name": "write", "success": False, "arguments": {"path": "/a.py"}})
    await apply_intra_turn_digest(ctx, [], sidecar, trace_ctx=None, store=_FakeStore())
    assert "r0" in ctx.tool_result_pins


@pytest.mark.asyncio
async def test_same_batch_read_write_defers_release() -> None:
    pins = {"r0": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(round_=2, pins=pins)
    sidecar = _sidecar(
        r1={"tool_name": "read", "success": True, "arguments": {"path": "/a.py"}},
        w1={"tool_name": "write", "success": True, "arguments": {"path": "/a.py"}},
    )
    await apply_intra_turn_digest(ctx, [], sidecar, trace_ctx=None, store=_FakeStore())
    assert "r0" in ctx.tool_result_pins  # same-batch hazard → deferred


@pytest.mark.asyncio
async def test_ttl_abandonment_releases_pin() -> None:
    pins = {"r0": ToolResultPin(path="/a.py", round_pinned=0)}
    ctx = _ctx(round_=4, pins=pins)  # ttl=4
    await apply_intra_turn_digest(ctx, [], {}, trace_ctx=None, store=_FakeStore())
    assert "r0" not in ctx.tool_result_pins


# ---------------------------------------------------------------------------
# expand_tool_result tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_tool_result_happy_and_hash_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.tools import tool_result_expand as mod

    full = "verbatim-payload-" + "y" * 200
    key = "tool-results/s/t/c1"
    store = _FakeStore()
    store.objects[key] = full.encode("utf-8")
    monkeypatch.setattr(mod, "get_artifact_store", lambda: store)

    ok = await mod.expand_tool_result_executor(key=key, content_hash=compute_content_hash(full))
    assert ok["success"] is True
    assert ok["content"] == full

    bad = await mod.expand_tool_result_executor(key=key, content_hash="deadbeef")
    assert bad["success"] is False
    assert "hash" in str(bad).lower()


@pytest.mark.asyncio
async def test_expand_tool_result_ranged_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.tools import tool_result_expand as mod

    full = "\n".join(f"line {i}" for i in range(100))
    key = "tool-results/s/t/c2"
    store = _FakeStore()
    store.objects[key] = full.encode("utf-8")
    monkeypatch.setattr(mod, "get_artifact_store", lambda: store)

    ranged = await mod.expand_tool_result_executor(
        key=key, content_hash=compute_content_hash(full), offset=0, limit=5
    )
    assert ranged["success"] is True
    assert ranged["content"].count("\n") <= 5


@pytest.mark.asyncio
async def test_expand_tool_result_store_unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.tools import tool_result_expand as mod

    monkeypatch.setattr(mod, "get_artifact_store", lambda: None)
    res = await mod.expand_tool_result_executor(key="tool-results/s/t/c", content_hash="x")
    assert res["success"] is False


# ---------------------------------------------------------------------------
# D6 — send-time transcript stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digested_message_survives_sanitize_tool_pairs() -> None:
    """A birth-digested role="tool" entry keeps its tool_call_id, so once extended
    the send-time pair sanitiser leaves it byte-identical (ADR-0085 D6).
    """
    from personal_agent.orchestrator.context_window import _sanitize_tool_pairs

    tool_results = [_tool_msg("t_old", "bash", _big_bash("old"))]
    sidecar = _sidecar(t_old={"tool_name": "bash", "success": True, "arguments": {}})
    ctx = _ctx()
    await apply_intra_turn_digest(ctx, tool_results, sidecar, trace_ctx=None, store=_FakeStore())
    assert _is_digest(tool_results[0])

    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t_old"}]},
        tool_results[0],
    ]
    digested_content = tool_results[0]["content"]
    sanitized = _sanitize_tool_pairs(messages)
    survivor = next(m for m in sanitized if m.get("tool_call_id") == "t_old")
    assert survivor["content"] == digested_content
