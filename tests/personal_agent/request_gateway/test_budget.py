"""Tests for Stage 7: Context Budget Management."""

from typing import Any

import pytest

from personal_agent.request_gateway.budget import apply_budget, estimate_tokens
from personal_agent.request_gateway.types import AssembledContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def _context(
    messages: list[dict[str, Any]] | None = None,
    memory_context: list[dict[str, Any]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    token_count: int = 0,
) -> AssembledContext:
    return AssembledContext(
        messages=messages or [_msg("user", "hello")],
        memory_context=memory_context,
        tool_definitions=tool_definitions,
        token_count=token_count,
    )


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_single_word(self) -> None:
        # 1 word * 1.3 = int(1.3) = 1
        assert estimate_tokens("hello") == 1

    def test_ten_words(self) -> None:
        text = " ".join(["word"] * 10)
        assert estimate_tokens(text) == int(10 * 1.3)

    def test_proportional_to_word_count(self) -> None:
        short = estimate_tokens("one two three")
        long = estimate_tokens("one two three four five six")
        assert long > short


# ---------------------------------------------------------------------------
# apply_budget — under budget
# ---------------------------------------------------------------------------

class TestApplyBudgetUnderLimit:
    def test_under_budget_returns_unchanged(self) -> None:
        ctx = _context(messages=[_msg("user", "hi")])
        result = apply_budget(ctx, max_tokens=10_000, trace_id="t1")
        assert result.trimmed is False
        assert result.overflow_action is None
        assert result.messages == ctx.messages

    def test_under_budget_token_count_updated(self) -> None:
        ctx = _context(messages=[_msg("user", "hi")])
        result = apply_budget(ctx, max_tokens=10_000, trace_id="t1")
        # Token count should be recalculated (not just passed through)
        assert result.token_count >= 0


# ---------------------------------------------------------------------------
# apply_budget — Phase 1: history trimming
# ---------------------------------------------------------------------------

class TestApplyBudgetHistoryTrimming:
    def test_drops_oldest_history_when_over_budget(self) -> None:
        long_text = " ".join(["word"] * 500)
        messages = [
            _msg("system", "You are an assistant."),
            _msg("user", long_text),      # old user message
            _msg("assistant", long_text), # old assistant reply
            _msg("user", "current question"),  # keep this
        ]
        ctx = _context(messages=messages)
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")

        assert result.trimmed is True
        assert result.overflow_action == "dropped_oldest_history"

    def test_system_message_preserved(self) -> None:
        long_text = " ".join(["word"] * 500)
        messages = [
            _msg("system", "System prompt."),
            _msg("user", long_text),
            _msg("assistant", long_text),
            _msg("user", "current question"),
        ]
        ctx = _context(messages=messages)
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")

        roles = [m["role"] for m in result.messages]
        assert "system" in roles

    def test_last_user_message_preserved(self) -> None:
        long_text = " ".join(["word"] * 500)
        messages = [
            _msg("user", long_text),
            _msg("assistant", long_text),
            _msg("user", "current question"),
        ]
        ctx = _context(messages=messages)
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")

        last_user = next(
            (m for m in reversed(result.messages) if m["role"] == "user"), None
        )
        assert last_user is not None
        assert last_user["content"] == "current question"


# ---------------------------------------------------------------------------
# apply_budget — Phase 2: memory context dropped
# ---------------------------------------------------------------------------

class TestApplyBudgetMemoryDrop:
    def test_drops_memory_when_history_trim_not_enough(self) -> None:
        # Single user message (can't trim history) + large memory
        long_text = " ".join(["word"] * 500)
        memory = [{"type": "entity", "name": long_text}]
        ctx = _context(
            messages=[_msg("user", "short question")],
            memory_context=memory,
        )
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")

        assert result.memory_context is None
        assert "memory" in (result.overflow_action or "")

    def test_memory_preserved_when_under_budget(self) -> None:
        memory = [{"type": "entity", "name": "Alice"}]
        ctx = _context(
            messages=[_msg("user", "hi")],
            memory_context=memory,
        )
        result = apply_budget(ctx, max_tokens=10_000, trace_id="t1")
        assert result.memory_context == memory


# ---------------------------------------------------------------------------
# apply_budget — Phase 3: tool definitions dropped
# ---------------------------------------------------------------------------

class TestApplyBudgetToolDrop:
    def test_drops_tools_as_last_resort(self) -> None:
        long_text = " ".join(["word"] * 500)
        tools = [{"name": "search", "description": long_text}]
        ctx = _context(
            messages=[_msg("user", "short")],
            tool_definitions=tools,
        )
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")

        assert result.tool_definitions is None
        assert result.overflow_action == "dropped_tool_definitions"

    def test_tools_preserved_when_under_budget(self) -> None:
        tools = [{"name": "search"}]
        ctx = _context(
            messages=[_msg("user", "hi")],
            tool_definitions=tools,
        )
        result = apply_budget(ctx, max_tokens=10_000, trace_id="t1")
        assert result.tool_definitions == tools


# ---------------------------------------------------------------------------
# Non-message fields preserved
# ---------------------------------------------------------------------------

class TestNonMessageFieldsPreserved:
    def test_skills_and_delegation_context_preserved(self) -> None:
        skills = [{"name": "code-review"}]
        delegation = {"target": "claude-code"}
        long_text = " ".join(["word"] * 500)
        ctx = AssembledContext(
            messages=[_msg("user", long_text), _msg("user", "current")],
            memory_context=None,
            tool_definitions=None,
            skills=skills,
            delegation_context=delegation,
        )
        result = apply_budget(ctx, max_tokens=5, trace_id="t1")
        assert result.skills == skills
        assert result.delegation_context == delegation
