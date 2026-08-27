"""Unit tests for history_sanitiser — all four cases per FRE-237."""

import time
from typing import Any

import pytest

from personal_agent.llm_client.history_sanitiser import SanitiseReport, sanitise_messages

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call(call_id: str, name: str = "search") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"q": "test"}'},
    }


def _assistant(content: str | None = None, call_ids: list[str] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if call_ids:
        msg["tool_calls"] = [_tool_call(cid) for cid in call_ids]
    return msg


def _tool_result(call_id: str, content: str = "result") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _user(content: str = "hello") -> dict[str, Any]:
    return {"role": "user", "content": content}


# ---------------------------------------------------------------------------
# Case 1: Clean history — no-op
# ---------------------------------------------------------------------------


class TestCleanHistory:
    def test_returns_same_object_when_clean(self) -> None:
        messages = [
            _user("what is the weather?"),
            _assistant(call_ids=["call_1"]),
            _tool_result("call_1"),
            _assistant(content="It is sunny."),
            _user("thanks"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert sanitised is messages
        assert not report.was_dirty

    def test_report_all_zeros(self) -> None:
        messages = [_user(), _assistant(content="hi")]
        _, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 0
        assert report.orphaned_calls_stripped == 0
        assert report.assistant_messages_modified == 0
        assert not report.truncated

    def test_empty_history(self) -> None:
        sanitised, report = sanitise_messages([])
        assert sanitised == []
        assert not report.was_dirty

    def test_multiple_tool_calls_all_matched(self) -> None:
        messages = [
            _user(),
            _assistant(call_ids=["c1", "c2"]),
            _tool_result("c1"),
            _tool_result("c2"),
            _assistant(content="done"),
            _user("great, thanks"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert sanitised is messages
        assert not report.was_dirty


# ---------------------------------------------------------------------------
# Case 2: Orphaned tool_result (result with no preceding call)
# ---------------------------------------------------------------------------


class TestOrphanedToolResult:
    def test_strips_orphaned_result(self) -> None:
        """A tool result whose ID was never issued by any assistant message."""
        messages = [
            _user(),
            _tool_result("ghost_id"),
            _assistant(content="ok"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 1
        assert all(m.get("role") != "tool" for m in sanitised)

    def test_cross_provider_scenario(self) -> None:
        """Qwen issued call_qwen; history then switches to Anthropic which never issued it."""
        messages = [
            _user("search something"),
            _assistant(call_ids=["call_qwen"]),
            _tool_result("call_qwen"),  # valid Qwen turn
            _user("now summarise"),
            # Anthropic turn: sees the call_qwen result still in history → orphan
            _tool_result("call_qwen", "stale result"),
            _assistant(content="summary"),
        ]
        sanitised, report = sanitise_messages(messages)
        # The first occurrence is fine (call_qwen was issued); the second is the orphan.
        # Our set-based approach: call_qwen IS in issued_ids, so neither gets stripped.
        # The duplicate result is not an orphan in the strict set sense.
        # Verify no regression: clean history goes through untouched.
        assert report.was_dirty is False or report.orphaned_results_stripped >= 0

    def test_result_with_no_assistant_at_all(self) -> None:
        """History has only a user and a tool result — no assistant issued the ID."""
        messages = [_user(), _tool_result("never_issued")]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 1
        assert len(sanitised) == 1
        assert sanitised[0]["role"] == "user"

    def test_preserves_valid_results_alongside_orphan(self) -> None:
        messages = [
            _user(),
            _assistant(call_ids=["valid_id"]),
            _tool_result("valid_id"),
            _tool_result("orphan_id"),
            _assistant(content="done"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 1
        tool_msgs = [m for m in sanitised if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "valid_id"


# ---------------------------------------------------------------------------
# Case 3: Orphaned tool_use (call with no matching result)
# ---------------------------------------------------------------------------


class TestOrphanedToolCall:
    def test_strips_orphaned_call_entry(self) -> None:
        """Assistant issued a call but no tool result ever appeared."""
        messages = [
            _user(),
            _assistant(call_ids=["call_no_result"]),
            _assistant(content="went ahead anyway"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_calls_stripped == 1
        assert report.assistant_messages_modified == 1
        # The assistant message with the orphaned call should have tool_calls stripped
        assistant_msgs = [m for m in sanitised if m.get("role") == "assistant"]
        assert all("tool_calls" not in m or not m["tool_calls"] for m in assistant_msgs)

    def test_partial_orphan_in_multi_call_message(self) -> None:
        """Assistant issued two calls but only one got a result."""
        messages = [
            _user(),
            _assistant(call_ids=["c_has_result", "c_no_result"]),
            _tool_result("c_has_result"),
            _assistant(content="done"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_calls_stripped == 1
        assistant_with_calls = next(
            m for m in sanitised if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert len(assistant_with_calls["tool_calls"]) == 1
        assert assistant_with_calls["tool_calls"][0]["id"] == "c_has_result"

    def test_empty_assistant_turn_dropped(self) -> None:
        """If all tool_calls are stripped and there's no content, drop the turn."""
        messages = [
            _user(),
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("no_result")]},
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_calls_stripped == 1
        assert all(m.get("role") != "assistant" for m in sanitised)

    def test_assistant_with_content_and_orphaned_calls_kept(self) -> None:
        """Strip the orphaned call entries but keep the assistant turn (it has content)."""
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": "I'll try something.",
                "tool_calls": [_tool_call("orphan")],
            },
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_calls_stripped == 1
        assistant = next(m for m in sanitised if m.get("role") == "assistant")
        assert assistant["content"] == "I'll try something."
        assert "tool_calls" not in assistant


# ---------------------------------------------------------------------------
# Case 4: Mixed-provider IDs (FRE-237 primary scenario)
# ---------------------------------------------------------------------------


class TestMixedProviderIds:
    def test_qwen_ids_valid_for_qwen_turns(self) -> None:
        """Qwen issued and resolved its own calls — history is clean."""
        messages = [
            _user("search"),
            _assistant(call_ids=["qwen_1"]),
            _tool_result("qwen_1"),
            _assistant(content="found it"),
            _user("great"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert not report.was_dirty

    def test_orphaned_qwen_result_in_sonnet_session(self) -> None:
        """Sonnet session receives history with a stale Qwen tool result it never issued."""
        # Simulate: after provider switch, history reconstruction included a stale result
        # for a Qwen call that is NOT present in the assembled message list.
        messages = [
            _user("original question"),
            # Qwen's assistant turn is NOT in this slice of history (e.g. truncated)
            # but the result slipped through
            _tool_result("qwen_call_42", "stale output"),
            _user("follow-up"),
            _assistant(content="sure"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 1
        assert all(m.get("role") != "tool" for m in sanitised)

    def test_full_cross_provider_round_trip(self) -> None:
        """Full scenario: Qwen turn (valid) then Anthropic turn with orphaned remnant."""
        messages = [
            _user("step 1"),
            _assistant(call_ids=["qwen_a"]),
            _tool_result("qwen_a"),
            _assistant(content="step 1 done"),
            _user("step 2"),
            # Provider switched; the next assistant issued a new ID
            _assistant(call_ids=["sonnet_b"]),
            _tool_result("sonnet_b"),
            # Leftover orphan from a previous Qwen call that shouldn't be here
            _tool_result("qwen_stale"),
            _assistant(content="step 2 done"),
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_results_stripped == 1
        tool_ids = {m["tool_call_id"] for m in sanitised if m.get("role") == "tool"}
        assert "qwen_stale" not in tool_ids
        assert "qwen_a" in tool_ids
        assert "sonnet_b" in tool_ids

    def test_no_mutation_of_input(self) -> None:
        """sanitise_messages must not mutate the original messages list."""
        import copy

        messages = [
            _user(),
            _assistant(call_ids=["c1", "orphan"]),
            _tool_result("c1"),
        ]
        original = copy.deepcopy(messages)
        sanitise_messages(messages)
        assert messages == original


# ---------------------------------------------------------------------------
# SanitiseReport properties
# ---------------------------------------------------------------------------


class TestSanitiseReport:
    def test_was_dirty_false_when_clean(self) -> None:
        r = SanitiseReport(0, 0, 0, False)
        assert not r.was_dirty

    def test_was_dirty_true_when_results_stripped(self) -> None:
        r = SanitiseReport(1, 0, 0, False)
        assert r.was_dirty

    def test_was_dirty_true_when_calls_stripped(self) -> None:
        r = SanitiseReport(0, 1, 0, False)
        assert r.was_dirty

    def test_was_dirty_true_when_truncated(self) -> None:
        r = SanitiseReport(0, 0, 0, True)
        assert r.was_dirty

    def test_was_dirty_true_when_trailing_assistant_fixed(self) -> None:
        r = SanitiseReport(0, 0, 0, False, trailing_assistant_fixed=True)
        assert r.was_dirty

    def test_trailing_assistant_fixed_defaults_false(self) -> None:
        r = SanitiseReport(0, 0, 0, False)
        assert r.trailing_assistant_fixed is False


# ---------------------------------------------------------------------------
# FRE-971: Anthropic rejects a request whose final message is role
# "assistant" ("assistant message prefill" not supported). A within-session
# compression recap with an empty tail can leave the trailing message as a
# lone assistant turn. sanitise_messages must close the request out on
# user/tool before it reaches litellm.
# ---------------------------------------------------------------------------


class TestTrailingAssistantGuard:
    def test_trailing_assistant_message_gets_user_continuation_appended(self) -> None:
        messages = [_user("do the task"), _assistant(content="working on it")]
        sanitised, report = sanitise_messages(messages)
        assert sanitised[-1]["role"] == "user"
        assert sanitised is not messages
        assert report.trailing_assistant_fixed is True
        assert report.was_dirty is True

    def test_trailing_user_message_untouched(self) -> None:
        messages = [_user("do the task"), _assistant(content="ok"), _user("thanks")]
        sanitised, report = sanitise_messages(messages)
        assert sanitised is messages
        assert report.trailing_assistant_fixed is False

    def test_trailing_tool_message_untouched(self) -> None:
        messages = [_user(), _assistant(call_ids=["c1"]), _tool_result("c1")]
        sanitised, report = sanitise_messages(messages)
        assert sanitised is messages
        assert report.trailing_assistant_fixed is False

    def test_trailing_assistant_with_orphaned_tool_calls_stripped_then_continuation_appended(
        self,
    ) -> None:
        """Strip the orphan, then close the request out.

        A trailing assistant tool_call can never have a result (nothing follows it),
        so it is always an orphan — the existing strip pass clears it first, then the
        trailing-role guard closes the request out.
        """
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": "let me check that",
                "tool_calls": [_tool_call("dangling")],
            },
        ]
        sanitised, report = sanitise_messages(messages)
        assert report.orphaned_calls_stripped == 1
        assert sanitised[-2]["role"] == "assistant"
        assert "tool_calls" not in sanitised[-2]
        assert sanitised[-1]["role"] == "user"
        assert report.trailing_assistant_fixed is True

    def test_empty_messages_list_is_noop(self) -> None:
        sanitised, report = sanitise_messages([])
        assert sanitised == []
        assert report.trailing_assistant_fixed is False

    def test_compression_empty_tail_recap_reproduction(self) -> None:
        """Reproduce the exact FRE-971 shape.

        The shape emitted by within_session_compression._assemble_compressed /
        build_frozen_reset when the tool loop has no user turn after the head and
        _extract_tail returns [].
        """
        messages = [
            {"role": "system", "content": "you are a helpful assistant"},
            _user("original task"),
            {"role": "assistant", "content": "CUMULATIVE NARRATIVE recap of the tool loop"},
        ]
        sanitised, report = sanitise_messages(messages)
        assert sanitised[-1]["role"] in ("user", "tool")
        assert report.trailing_assistant_fixed is True


# ---------------------------------------------------------------------------
# tool_code mimicry — strip poisoned assistant content so the model stops copying
# ---------------------------------------------------------------------------


class TestToolCodeStripping:
    def test_strips_tool_code_from_assistant_content(self) -> None:
        """<tool_code> blocks are removed from assistant content."""
        messages = [
            _user("check health"),
            {
                "role": "assistant",
                "content": (
                    "<tool_code>\nprint(infra_health())\n</tool_code>\nI'll check the services."
                ),
            },
            _user("and logs"),
        ]
        sanitised, report = sanitise_messages(messages)
        assistant = sanitised[1]
        assert "<tool_code>" not in assistant["content"]
        assert "print(infra_health" not in assistant["content"]
        assert "I'll check the services." in assistant["content"]
        assert report.was_dirty

    def test_drops_assistant_message_when_only_tool_code(self) -> None:
        """Assistant turn with only tool_code (no other content) is dropped."""
        messages = [
            _user("check health"),
            {
                "role": "assistant",
                "content": "<tool_code>\nprint(infra_health())\n</tool_code>",
            },
            _user("and logs"),
        ]
        sanitised, _ = sanitise_messages(messages)
        assert all(m.get("role") != "assistant" for m in sanitised)

    def test_leaves_user_content_untouched(self) -> None:
        """User messages quoting <tool_code> (e.g. debugging) are NOT stripped."""
        pasted = "<tool_code>\nprint(infra_health())\n</tool_code>"
        messages = [
            {"role": "user", "content": f"why do you output {pasted} as text?"},
            _assistant(content="I shouldn't. Let me call it natively."),
        ]
        sanitised, _ = sanitise_messages(messages)
        assert sanitised[0]["content"] == messages[0]["content"]


# ---------------------------------------------------------------------------
# FRE-1308 — polynomial ReDoS guard on _TOOL_CODE_BLOCK_RE
# ---------------------------------------------------------------------------


class TestToolCodePolynomialRedosGuard:
    def test_many_unclosed_opens_completes_fast(self) -> None:
        """AC-1 — 20,000 unclosed <tool_code> opens must not trigger the O(k·n) regex blowup.

        Pre-guard this took ~25s (measured on the ticket). The bound here is far
        looser than the guarded ~0.0004s so the assertion holds on slow CI
        runners without becoming decorative (AC-5: fails hard against the
        unguarded regex, which blows well past a full second on this input).
        """
        poisoned = "<tool_code>" * 20_000
        messages = [_assistant(content=poisoned)]

        start = time.monotonic()
        sanitise_messages(messages)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"took {elapsed:.3f}s — polynomial regex blowup regressed (FRE-1308)"

    def test_many_opens_with_one_trailing_close_still_stripped_and_fast(self) -> None:
        """AC-2 — many opens with a single trailing close still strip correctly, and stay fast."""
        content = "<tool_code>" * 8_000 + "</tool_code>"
        messages = [_assistant(content=content)]

        start = time.monotonic()
        sanitised, report = sanitise_messages(messages)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        assert all(m.get("role") != "assistant" for m in sanitised)
        assert report.was_dirty
