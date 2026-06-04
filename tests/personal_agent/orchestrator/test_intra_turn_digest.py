"""Tests for the PR-B intra-turn digest pass + expand tool (ADR-0085, FRE-475).

Keep-window-deferred semantics (owner decision): the per-round pass digests
oversized/eligible/unpinned tool messages that are OLDER than the most-recent
``tool_result_digest_keep`` results; the current batch stays verbatim.
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


def _ctx(messages: list[dict[str, object]], *, round_: int = 1, pins=None) -> SimpleNamespace:
    return SimpleNamespace(
        messages=messages,
        tool_iteration_count=round_,
        tool_result_pins=pins if pins is not None else {},
        session_id=_SID,
        trace_id="trace1",
    )


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


@pytest.mark.asyncio
async def test_keep_window_protects_current_batch_digests_older() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        _tool_msg("t_old", "bash", _big_bash("old")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        _tool_msg("t_new", "bash", _big_bash("new")),
    ]
    ctx = _ctx(messages)
    store = _FakeStore()
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=store)

    assert _is_digest(messages[3])  # older tool result digested
    assert not _is_digest(messages[5])  # current batch (within keep) stays verbatim
    assert len(store.puts) == 1
    # adjacency: digested message keeps its tool-pair fields, same position
    assert messages[3]["tool_call_id"] == "t_old"
    assert messages[3]["role"] == "tool"
    assert messages[3]["name"] == "bash"


@pytest.mark.asyncio
async def test_pinned_read_not_digested_until_write_releases() -> None:
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("r1", "read", _big_bash("read")),  # the pinned read (oldest)
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        _tool_msg("k1", "bash", _big_bash("keep")),  # keep-window protector
    ]
    pins = {"r1": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(messages, round_=1, pins=pins)
    store = _FakeStore()
    # No write this round → pin holds → read not digested.
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=store)
    assert not _is_digest(messages[1])

    # A successful prior-round write to /a.py releases the pin → read digested.
    ctx.tool_iteration_count = 2
    sidecar = {"w1": {"tool_name": "write", "success": True, "arguments": {"path": "/a.py"}}}
    await apply_intra_turn_digest(ctx, sidecar, trace_ctx=None, store=store)
    assert "r1" not in ctx.tool_result_pins
    assert _is_digest(messages[1])


@pytest.mark.asyncio
async def test_same_batch_read_and_write_does_not_release() -> None:
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("r1", "read", _big_bash("read")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        _tool_msg("k1", "bash", _big_bash("keep")),
    ]
    pins = {"r1": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(messages, round_=2, pins=pins)
    sidecar = {
        "r2": {"tool_name": "read", "success": True, "arguments": {"path": "/a.py"}},
        "w2": {"tool_name": "write", "success": True, "arguments": {"path": "/a.py"}},
    }
    await apply_intra_turn_digest(ctx, sidecar, trace_ctx=None, store=_FakeStore())
    assert "r1" in ctx.tool_result_pins  # same-batch hazard → deferred
    assert not _is_digest(messages[1])


@pytest.mark.asyncio
async def test_failed_write_does_not_release_pin() -> None:
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("r1", "read", _big_bash("read")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        _tool_msg("k1", "bash", _big_bash("keep")),
    ]
    pins = {"r1": ToolResultPin(path="/a.py", round_pinned=1)}
    ctx = _ctx(messages, round_=2, pins=pins)
    sidecar = {"w1": {"tool_name": "write", "success": False, "arguments": {"path": "/a.py"}}}
    await apply_intra_turn_digest(ctx, sidecar, trace_ctx=None, store=_FakeStore())
    assert "r1" in ctx.tool_result_pins


@pytest.mark.asyncio
async def test_ttl_abandonment_releases_pin() -> None:
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("r1", "read", _big_bash("read")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        _tool_msg("k1", "bash", _big_bash("keep")),
    ]
    pins = {"r1": ToolResultPin(path="/a.py", round_pinned=0)}
    ctx = _ctx(messages, round_=4, pins=pins)  # ttl=4 → released
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=_FakeStore())
    assert "r1" not in ctx.tool_result_pins
    assert _is_digest(messages[1])


@pytest.mark.asyncio
async def test_put_timeout_leaves_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_put_timeout_ms", 20)
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("t_old", "bash", _big_bash("old")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        _tool_msg("t_new", "bash", _big_bash("new")),
    ]
    ctx = _ctx(messages)
    store = _FakeStore(delay=0.5)  # exceeds the 20ms ceiling
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=store)
    assert not _is_digest(messages[1])  # left verbatim on timeout


@pytest.mark.asyncio
async def test_already_digested_is_idempotent() -> None:
    messages = [
        {"role": "user", "content": "u"},
        _tool_msg("t_old", "bash", _big_bash("old")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        _tool_msg("t_new", "bash", _big_bash("new")),
    ]
    ctx = _ctx(messages)
    store = _FakeStore()
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=store)
    first = messages[1]["content"]
    # second pass: the already-digested message is not re-digested / re-put.
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=store)
    assert messages[1]["content"] == first
    assert len(store.puts) == 1


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
    """A digested role="tool" message keeps its tool_call_id, so the send-time
    pair sanitiser never orphans it and leaves it byte-identical (ADR-0085 D6).
    """
    from personal_agent.orchestrator.context_window import _sanitize_tool_pairs

    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t_old"}]},
        _tool_msg("t_old", "bash", _big_bash("old")),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t_new"}]},
        _tool_msg("t_new", "bash", _big_bash("new")),
    ]
    ctx = _ctx(messages)
    await apply_intra_turn_digest(ctx, {}, trace_ctx=None, store=_FakeStore())
    assert _is_digest(messages[2])  # t_old digested
    digested_content = messages[2]["content"]

    sanitized = _sanitize_tool_pairs(messages)
    survivor = next(m for m in sanitized if m.get("tool_call_id") == "t_old")
    assert survivor["content"] == digested_content  # byte-identical, not orphaned
