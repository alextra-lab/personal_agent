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
from personal_agent.orchestrator.executor import _validate_and_fix_conversation_roles


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
        msg = _msg(
            "assistant",
            "thought",
            tool_calls=[{"id": "x", "function": {"name": "f", "arguments": big_args}}],
        )
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
        # "x" * 4000 = 500 tokens (tiktoken cl100k_base). 4 messages × 500 = 2000
        # tokens cross the 2000-token floor; min_turns=2 is satisfied within that set.
        messages = [_msg("user", "x" * 4000) for _ in range(6)]
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=2000, min_turns=2)
        assert len(tail) == 4

    def test_tool_pair_kept_via_contiguity_not_repair(self) -> None:
        # FRE-942 deleted the backward tool-pair repair: a user-anchored contiguous
        # suffix keeps a tool message together with its assistant because the walk
        # extends back to the user turn that opened them, never by reaching across
        # the middle for a non-contiguous assistant.
        messages = [
            _msg("user", "ask"),
            _msg(
                "assistant",
                "",
                tool_calls=[{"id": "tc-1", "function": {"name": "f", "arguments": "{}"}}],
            ),
            _msg("tool", "result", tool_call_id="tc-1"),
        ]
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=1, min_turns=3)
        assert tail == messages
        assert tail[0]["role"] == "user"

    def test_tail_does_not_cross_head_boundary(self) -> None:
        messages = [_msg("system", "head")] + [_msg("user", "x" * 50) for _ in range(5)]
        tail = wsc._extract_tail(messages, head_len=1, min_tokens=10000, min_turns=10)
        # Even though floors are huge, tail can never include the head msg.
        assert all(m["role"] != "system" for m in tail)
        assert len(tail) == 5


class TestExtractTailContract:
    """FRE-942 — the bounded contiguous-suffix contract.

    The tail band used to have two floors and no ceiling, which let it grow without
    bound: a real production compaction preserved a 254,071-token tail verbatim inside
    a 96,000-token window (see ``scripts/audit/fre942_compaction_census.py``). One
    clause per numbered rule in the ADR-0061 §D3 amendment; precedence is
    contiguity → user-alignment → ceiling → floors.
    """

    def _tokens(self, messages: list[dict[str, Any]]) -> int:
        return estimate_messages_tokens(messages)

    def test_result_is_always_a_contiguous_suffix(self) -> None:
        """Rule 1 — callers derive the middle boundary as ``len(messages) - len(tail)``
        (``within_session_compression.py``), which is only sound for a contiguous
        suffix. The old tool-pair repair could insert an arbitrarily distant earlier
        index, making the same message appear in *both* the middle and the tail.
        """
        messages = [
            _msg("user", "ask"),
            _msg(
                "assistant",
                "",
                tool_calls=[{"id": "tc-far", "function": {"name": "f", "arguments": "{}"}}],
            ),
        ]
        for i in range(6):
            messages.append(_msg("user", f"u{i}"))
            messages.append(_msg("assistant", f"a{i}"))
        messages.append(_msg("tool", "X" * 400, tool_call_id="tc-far"))

        tail = wsc._extract_tail(messages, head_len=0, min_tokens=10, min_turns=2)

        assert tail == messages[len(messages) - len(tail) :]

    def test_returns_empty_when_no_user_turn_is_available(self) -> None:
        """Rule 2 — a tail that cannot start on a user turn is dropped entirely rather
        than handed to the assembler with a dangling assistant/tool prefix.
        """
        messages = [
            _msg("user", "ask"),
            _msg("assistant", "reply"),
            _msg("assistant", "trailing"),
        ]
        tail = wsc._extract_tail(messages, head_len=2, min_tokens=1, min_turns=1)
        assert tail == []

    def test_ceiling_bounds_the_accumulated_tail(self) -> None:
        """Rule 3 — the production failure shape: several large trailing results whose
        sum dwarfs the window. The ceiling must stop the walk; before FRE-942 the
        ``min_turns`` floor forced all of them in.
        """
        messages = [_msg("user", "ask")]
        for i in range(4):
            messages.append(_msg("user", f"step {i}"))
            messages.append(_msg("tool", "x" * 40_000, tool_call_id=f"tc-{i}"))

        tail = wsc._extract_tail(
            messages, head_len=0, min_tokens=2_000, min_turns=4, max_tokens=12_000
        )

        assert self._tokens(tail) <= 12_000
        assert len(tail) < len(messages)

    def test_single_message_is_exempt_from_the_ceiling(self) -> None:
        """Rule 3's exception — one oversized message may exceed the ceiling, so the
        bound can never delete the most recent message outright. The exemption is one
        *message*, not one semantic turn.
        """
        messages = [_msg("user", "ask"), _msg("user", "x" * 80_000)]
        tail = wsc._extract_tail(messages, head_len=0, min_tokens=1, min_turns=1, max_tokens=100)
        assert tail == messages[-1:]
        assert self._tokens(tail) > 100

    def test_user_alignment_outranks_both_floors(self) -> None:
        """Rule 4 — floors bound the *walk*, not the returned value. Forward alignment
        runs afterwards and may drop messages that satisfied them.
        """
        messages = [
            _msg("user", "ask"),
            _msg("user", "x" * 4_000),
            _msg("assistant", "y" * 4_000),
            _msg("assistant", "z" * 4_000),
        ]
        tail = wsc._extract_tail(messages, head_len=1, min_tokens=1, min_turns=3)
        # The walk satisfied min_turns=3; alignment keeps only the user-anchored run.
        assert tail == messages[1:]
        assert all(m["role"] != "system" for m in tail)

    def test_ceiling_never_strands_a_tool_message_from_its_assistant(self) -> None:
        """Rule 6 — the backing assistant must PRECEDE its tool message in the returned
        list. Asserting order, not merely that the id appears somewhere: both
        ``_sanitize_tool_pairs`` and the wire sanitiser match ids globally, so an
        id-presence check would pass on a mis-ordered band.
        """
        messages = [_msg("user", "ask")]
        for i in range(5):
            messages.append(_msg("user", f"step {i}"))
            messages.append(
                _msg(
                    "assistant",
                    "",
                    tool_calls=[{"id": f"tc-{i}", "function": {"name": "f", "arguments": "{}"}}],
                )
            )
            messages.append(_msg("tool", "x" * 8_000, tool_call_id=f"tc-{i}"))

        tail = wsc._extract_tail(
            messages, head_len=0, min_tokens=1_000, min_turns=2, max_tokens=6_000
        )

        seen_call_ids: set[str] = set()
        for msg in tail:
            if msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    seen_call_ids.add(str(call["id"]))
            if msg.get("role") == "tool":
                assert str(msg["tool_call_id"]) in seen_call_ids, (
                    "tool message appears before the assistant that issued it"
                )

    @pytest.mark.parametrize(
        ("max_tokens", "min_turns"),
        [(0, 4), (-1, 4), (10, 0)],
        ids=["zero_ceiling", "negative_ceiling", "zero_turn_floor"],
    )
    def test_degenerate_inputs_still_return_a_valid_suffix(
        self, max_tokens: int, min_turns: int
    ) -> None:
        """Rule 5 — the settings validator makes these unreachable from configuration,
        but the pure helper must stay total rather than raise or loop.
        """
        messages = [_msg("user", "ask"), _msg("user", "x" * 2_000)]
        tail = wsc._extract_tail(
            messages, head_len=0, min_tokens=50, min_turns=min_turns, max_tokens=max_tokens
        )
        assert tail == messages[len(messages) - len(tail) :]

    def test_ceiling_below_the_floor_lets_the_ceiling_win(self) -> None:
        """Rule 5 — ``max_tokens < min_tokens`` is accepted; the bound simply wins."""
        messages = [_msg("user", f"u{i}") for i in range(8)] + [_msg("user", "x" * 4_000)]
        tail = wsc._extract_tail(
            messages, head_len=0, min_tokens=10_000, min_turns=8, max_tokens=600
        )
        assert self._tokens(tail) <= 600 or len(tail) == 1


# ---------------------------------------------------------------------------
# Trigger predicate
# ---------------------------------------------------------------------------


class TestNeedsHardCompression:
    def test_below_threshold_returns_false(self) -> None:
        messages = [_msg("user", "small")]
        assert wsc.needs_hard_compression(messages, max_tokens=1000) is False

    def test_above_threshold_returns_true(self) -> None:
        # "x" * 4000 = 500 tokens (tiktoken cl100k_base). max_tokens=500 puts us
        # at 100% utilisation which exceeds any compression threshold ratio.
        messages = [_msg("user", "x" * 4000)]
        assert wsc.needs_hard_compression(messages, max_tokens=500) is True

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
                    tool_calls=[{"id": f"tc-{i}", "function": {"name": "es", "arguments": "{}"}}],
                )
            )
            messages.append(_msg("tool", "x" * 8000, tool_call_id=f"tc-{i}"))
        messages.append(_msg("user", "wrap it up"))

        prefix_before = compute_prefix_hash(messages[0])

        async def fake_compress_turns(
            msgs: list, trace_id: str = "", session_id: str | None = None
        ) -> str:
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
        # Summary marker is right after the head; role must be "assistant" so
        # it survives _validate_and_fix_conversation_roles (FRE-576 F2).
        assert compressed[2]["role"] == "assistant"
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
                    tool_calls=[{"id": f"tc-{i}", "function": {"name": "f", "arguments": "{}"}}],
                )
            )
            messages.append(_msg("tool", "z" * 8000, tool_call_id=f"tc-{i}"))
        messages.append(_msg("user", "follow up"))

        async def fake_compress_turns(
            msgs: list, trace_id: str = "", session_id: str | None = None
        ) -> str:
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
            isinstance(m.get("content"), str) and m["content"].startswith("## Conversation Summary")
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


# ---------------------------------------------------------------------------
# F2 regression — summary role must survive _validate_and_fix_conversation_roles
# ---------------------------------------------------------------------------


class TestSummaryRoleSurvivesRoleFixer:
    """FRE-576 F2: the within-session summary must not be dropped by the role-fixer.

    _validate_and_fix_conversation_roles keeps only the first system message
    and silently discards later ones.  If SUMMARY_ROLE is "system" the recap
    inserted mid-list is deleted before the LLM sees it.  After the fix
    (SUMMARY_ROLE = "assistant") the recap survives.
    """

    @pytest.mark.asyncio
    async def test_summary_role_survives_role_fixer(self) -> None:
        messages: list[dict[str, Any]] = [
            _msg("system", "persona block"),
            _msg("user", "start task"),
        ]
        for i in range(5):
            messages.append(
                _msg(
                    "assistant",
                    f"step {i}",
                    tool_calls=[{"id": f"tc-{i}", "function": {"name": "es", "arguments": "{}"}}],
                )
            )
            messages.append(_msg("tool", "r" * 6000, tool_call_id=f"tc-{i}"))
        messages.append(_msg("user", "final question"))

        summary_text = "## Conversation Summary\n- Decisions: ran 5 es queries"

        async def fake_compress(
            msgs: list, trace_id: str = "", session_id: str | None = None
        ) -> str:
            return summary_text

        with patch(
            "personal_agent.orchestrator.context_compressor.compress_turns",
            side_effect=fake_compress,
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t1",
                session_id="s1",
                trigger="hard",
                bus=None,
                pre_pass_threshold_tokens=200,
                min_tail_tokens=20,
                min_tail_turns=2,
            )

        assert record.summariser_called is True

        # Summary must survive the role-fixer applied before every LLM dispatch.
        fixed = _validate_and_fix_conversation_roles(compressed)
        summary_contents = [
            m["content"]
            for m in fixed
            if isinstance(m.get("content"), str) and "## Conversation Summary" in m["content"]
        ]
        assert summary_contents, (
            "within-session summary was dropped by _validate_and_fix_conversation_roles — "
            f"SUMMARY_ROLE={wsc.SUMMARY_ROLE!r} is being stripped"
        )
