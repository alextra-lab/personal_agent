"""Tests for within-session compression (ADR-0061)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator import within_session_compression as wsc
from personal_agent.orchestrator.context_compressor import (
    FALLBACK_MARKER,
    _content_is_error_payload,
    _pre_pass_tool_outputs,
    _shape_descriptor,
)
from personal_agent.orchestrator.context_window import (
    compute_prefix_hash,
    estimate_messages_tokens,
)


def _msg(role: str, content: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": role, "content": content}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Pre-pass helper
# ---------------------------------------------------------------------------


class TestPrePassToolOutputs:
    def test_replaces_large_tool_message(self) -> None:
        big_body = json.dumps({"results": [{"id": i} for i in range(2000)]})
        middle = [
            _msg("tool", big_body, tool_call_id="call-1"),
        ]
        out, count = _pre_pass_tool_outputs(middle, threshold_tokens=100)
        assert count == 1
        replacement = json.loads(out[0]["content"])
        assert replacement["_replaced"] is True
        assert replacement["tool_call_id"] == "call-1"
        assert replacement["size_chars"] == len(big_body)
        assert "results" in replacement["shape"]

    def test_skips_small_tool_message(self) -> None:
        small = _msg("tool", '{"ok": true}', tool_call_id="call-2")
        out, count = _pre_pass_tool_outputs([small], threshold_tokens=100)
        assert count == 0
        assert out[0] is small

    def test_preserves_assistant_messages(self) -> None:
        big_args = "x" * 5000
        msg = _msg("assistant", "thought", tool_calls=[{"id": "x", "function": {"name": "f", "arguments": big_args}}])
        out, count = _pre_pass_tool_outputs([msg], threshold_tokens=100)
        assert count == 0
        assert out[0] is msg

    def test_skips_error_payload(self) -> None:
        big_error = '{"status": "error", "message": "' + "y" * 5000 + '"}'
        msg = _msg("tool", big_error, tool_call_id="call-3")
        out, count = _pre_pass_tool_outputs([msg], threshold_tokens=10)
        assert count == 0
        assert out[0] is msg

    def test_preserves_tool_call_id_for_pair_sanitization(self) -> None:
        big = "z" * 5000
        msg = _msg("tool", big, tool_call_id="call-4")
        out, _ = _pre_pass_tool_outputs([msg], threshold_tokens=100)
        descriptor = json.loads(out[0]["content"])
        # Sanitiser keys on out[0]["tool_call_id"], not the descriptor body.
        assert out[0]["tool_call_id"] == "call-4"
        assert descriptor["tool_call_id"] == "call-4"

    def test_shape_descriptor_dict(self) -> None:
        result = _shape_descriptor('{"results": [], "next": null, "took": 5}')
        assert "results" in result and "next" in result and "took" in result

    def test_shape_descriptor_list(self) -> None:
        result = _shape_descriptor("[1, 2, 3, 4, 5]")
        assert result == "list[5]"

    def test_shape_descriptor_unparseable(self) -> None:
        result = _shape_descriptor("plain text\nwith newlines that should be flattened")
        assert "plain text" in result and "\n" not in result

    def test_error_payload_detector(self) -> None:
        assert _content_is_error_payload('{"error": "boom"}') is True
        assert _content_is_error_payload('{"status": "error"}') is True
        assert _content_is_error_payload('{"status":"error"}') is True
        assert _content_is_error_payload('{"ok": true}') is False


# ---------------------------------------------------------------------------
# Head / tail extraction
# ---------------------------------------------------------------------------


class TestExtractHead:
    def test_keeps_system_and_first_user(self) -> None:
        messages = [
            _msg("system", "deploy"),
            _msg("system", "skill"),
            _msg("user", "task"),
            _msg("assistant", "ok"),
            _msg("user", "follow-up"),
        ]
        head = wsc._extract_head(messages)
        assert [m["role"] for m in head] == ["system", "system", "user"]
        assert head[2]["content"] == "task"

    def test_no_user_message_yields_system_only(self) -> None:
        messages = [_msg("system", "a"), _msg("assistant", "no user yet")]
        head = wsc._extract_head(messages)
        assert [m["role"] for m in head] == ["system"]

    def test_assistant_before_first_user_does_not_steal_head(self) -> None:
        messages = [
            _msg("system", "a"),
            _msg("assistant", "weird"),  # ends head walk
            _msg("user", "task"),
        ]
        head = wsc._extract_head(messages)
        assert [m["role"] for m in head] == ["system"]


class TestExtractTail:
    def test_dynamic_token_floor(self) -> None:
        # 8 messages of ~50 chars each (=~12 tokens). min_tokens=24 should
        # pull in at least 2; min_turns=4 forces 4.
        messages = [_msg("user", "x" * 50) for _ in range(8)]
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=24, min_turns=4)
        assert len(tail) == 4
        # Tail must be the last 4 messages, in order.
        assert tail == messages[-4:]

    def test_token_floor_dominates_when_messages_are_large(self) -> None:
        messages = [_msg("user", "x" * 4000) for _ in range(6)]  # ~1000 tokens each
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=2000, min_turns=2)
        # Walking back, two messages already cross the floor.
        assert len(tail) == 2

    def test_pulls_in_assistant_for_tool_pair(self) -> None:
        messages = [
            _msg("user", "ask"),
            _msg("assistant", "", tool_calls=[{"id": "tc-1", "function": {"name": "f", "arguments": "{}"}}]),
            _msg("tool", "result", tool_call_id="tc-1"),
        ]
        # Tail floor of 1 token + 1 turn would normally only pull the tool msg.
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=1, min_turns=1)
        roles = [m["role"] for m in tail]
        assert "assistant" in roles
        assert "tool" in roles

    def test_tail_does_not_cross_head_boundary(self) -> None:
        messages = [_msg("system", "head")] + [_msg("user", "x" * 50) for _ in range(5)]
        tail = wsc._extract_tail(messages, head_len=1, min_tokens=10000, min_turns=10)
        # Even though floors are huge, tail can never include the head msg.
        assert all(m["role"] != "system" for m in tail)
        assert len(tail) == 5


# ---------------------------------------------------------------------------
# Trigger predicate
# ---------------------------------------------------------------------------


class TestNeedsHardCompression:
    def test_below_threshold_returns_false(self) -> None:
        messages = [_msg("user", "small")]
        assert wsc.needs_hard_compression(messages, max_tokens=1000) is False

    def test_above_threshold_returns_true(self) -> None:
        messages = [_msg("user", "x" * 4000)]  # ~1000 tokens
        assert wsc.needs_hard_compression(messages, max_tokens=1000) is True

    def test_disabled_flag_returns_false(self) -> None:
        messages = [_msg("user", "x" * 4000)]
        with patch.object(wsc.settings, "within_session_compression_enabled", False):
            assert wsc.needs_hard_compression(messages, max_tokens=1000) is False

    def test_zero_max_tokens_returns_false(self) -> None:
        assert wsc.needs_hard_compression([_msg("user", "x")], max_tokens=0) is False


# ---------------------------------------------------------------------------
# compress_in_place — integration of the parts above
# ---------------------------------------------------------------------------


class TestCompressInPlace:
    @pytest.mark.asyncio
    async def test_summary_path_replaces_middle(self) -> None:
        # 7 turns of (user/assistant/tool) past the head — tail of 3 turns
        # leaves 8+ middle messages for pre-pass and summarisation.
        messages: list[dict[str, Any]] = [
            _msg("system", "skill block"),
            _msg("user", "task: gather logs"),
        ]
        for i in range(7):
            messages.append(
                _msg(
                    "assistant",
                    f"step {i}",
                    tool_calls=[
                        {"id": f"tc-{i}", "function": {"name": "es", "arguments": "{}"}}
                    ],
                )
            )
            messages.append(_msg("tool", "x" * 8000, tool_call_id=f"tc-{i}"))
        messages.append(_msg("user", "wrap it up"))

        prefix_before = compute_prefix_hash(messages[0])

        async def fake_compress_turns(msgs: list, trace_id: str = "") -> str:
            return "## Conversation Summary\n- Decisions: ran multiple es queries"

        with patch(
            "personal_agent.orchestrator.context_compressor.compress_turns",
            side_effect=fake_compress_turns,
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t1",
                session_id="s1",
                trigger="hard",
                bus=None,
                pre_pass_threshold_tokens=200,
                min_tail_tokens=30,
                min_tail_turns=2,
            )

        # Head: system + first user preserved verbatim
        assert compressed[0] == messages[0]
        assert compressed[1] == messages[1]
        # KV cache prefix invariant
        assert compute_prefix_hash(compressed[0]) == prefix_before
        # Summary marker is right after the head
        assert compressed[2]["role"] == "system"
        assert compressed[2]["content"].startswith("## Conversation Summary")
        # Tail keeps the last user message verbatim
        assert messages[-1] in compressed
        # Record fields populated
        assert record.summariser_called is True
        assert record.pre_pass_replacements >= 2
        assert record.middle_tokens_in > record.middle_tokens_out
        assert record.tokens_saved > 0
        assert record.trigger == "hard"

    @pytest.mark.asyncio
    async def test_fallback_marker_keeps_pre_passed_middle(self) -> None:
        # Long enough that even after tail extraction the middle still
        # contains tool messages the pre-pass can replace.
        messages: list[dict[str, Any]] = [
            _msg("system", "skill"),
            _msg("user", "task"),
        ]
        for i in range(5):
            messages.append(
                _msg(
                    "assistant",
                    f"step {i}",
                    tool_calls=[
                        {"id": f"tc-{i}", "function": {"name": "f", "arguments": "{}"}}
                    ],
                )
            )
            messages.append(_msg("tool", "z" * 8000, tool_call_id=f"tc-{i}"))
        messages.append(_msg("user", "follow up"))

        async def fake_compress_turns(msgs: list, trace_id: str = "") -> str:
            return FALLBACK_MARKER

        with patch(
            "personal_agent.orchestrator.context_compressor.compress_turns",
            side_effect=fake_compress_turns,
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t",
                session_id="s",
                trigger="soft",
                bus=None,
                pre_pass_threshold_tokens=200,
                min_tail_tokens=10,
                min_tail_turns=1,
            )

        # No summary inserted; pre-passed middle survives instead.
        assert not any(
            isinstance(m.get("content"), str)
            and m["content"].startswith("## Conversation Summary")
            for m in compressed
        )
        assert record.summariser_called is False
        assert record.pre_pass_replacements >= 1
        # Token sum after compression must be smaller than before.
        assert estimate_messages_tokens(compressed) < estimate_messages_tokens(messages)

    @pytest.mark.asyncio
    async def test_durable_write_failure_does_not_propagate(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0061 — telemetry failure must not abort the orchestrator turn."""
        messages = [
            _msg("system", "h"),
            _msg("user", "u"),
            _msg("user", "x" * 4000),
            _msg("user", "tail"),
        ]

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        with (
            patch(
                "personal_agent.orchestrator.within_session_compression.record_compression",
                side_effect=boom,
            ),
            patch(
                "personal_agent.orchestrator.context_compressor.compress_turns",
                AsyncMock(return_value=FALLBACK_MARKER),
            ),
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t",
                session_id="s",
                trigger="soft",
                bus=None,
                pre_pass_threshold_tokens=200,
                min_tail_tokens=10,
                min_tail_turns=1,
            )

        assert isinstance(compressed, list)
        assert record.trigger == "soft"
