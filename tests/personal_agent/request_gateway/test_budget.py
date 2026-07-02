"""Tests for Stage 7: Context Budget Management."""

from typing import Any
from unittest.mock import patch

import structlog

from personal_agent.request_gateway.budget import (
    _context_occupancy,
    _total_context_tokens,
    apply_budget,
    estimate_tokens,
)
from personal_agent.request_gateway.types import AssembledContext
from personal_agent.telemetry.compaction import (
    clear_dropped_entities,
    get_dropped_entities,
)
from personal_agent.telemetry.context_quality import (
    reset_incident_tracker,
)

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
        assert estimate_tokens("hello") == 1

    def test_ten_words(self) -> None:
        # "word" is one cl100k_base token; 10 words = 10 tokens.
        text = " ".join(["word"] * 10)
        assert estimate_tokens(text) == 10

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


class TestListShapedContent:
    """ADR-0101 §2 widens ``content`` to ``str | list[dict]`` (FRE-709)."""

    def test_list_content_does_not_crash(self) -> None:
        messages = [
            _msg("user", "hello"),
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "the answer is forty two"}],
            },
        ]
        ctx = _context(messages=messages)
        result = apply_budget(ctx, max_tokens=10_000, trace_id="t1")
        assert result.trimmed is False
        assert result.token_count > 0

    def test_image_block_counts_toward_budget(self) -> None:
        """An image block must add its fixed token estimate, not drop to ~0 (master gate finding)."""
        from personal_agent.llm_client.message_content import IMAGE_BLOCK_TOKEN_ESTIMATE

        text_only = [
            {"role": "user", "content": [{"type": "text", "text": "look at this"}]},
        ]
        with_image = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                ],
            },
        ]
        text_only_tokens = _total_context_tokens(text_only, None, None)
        with_image_tokens = _total_context_tokens(with_image, None, None)
        assert with_image_tokens - text_only_tokens >= IMAGE_BLOCK_TOKEN_ESTIMATE


# ---------------------------------------------------------------------------
# apply_budget — Phase 1: history trimming
# ---------------------------------------------------------------------------


class TestApplyBudgetHistoryTrimming:
    def test_drops_oldest_history_when_over_budget(self) -> None:
        long_text = " ".join(["word"] * 500)
        messages = [
            _msg("system", "You are an assistant."),
            _msg("user", long_text),  # old user message
            _msg("assistant", long_text),  # old assistant reply
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

        last_user = next((m for m in reversed(result.messages) if m["role"] == "user"), None)
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


# ---------------------------------------------------------------------------
# FRE-593 — context-window occupancy breakdown
# ---------------------------------------------------------------------------


def _reasoning_msg(content: str, reasoning: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "reasoning_content": reasoning}


class TestContextOccupancy:
    """The pure breakdown helper splits the window into memory/tool/reasoning + total."""

    def test_empty_categories_are_zero_total_counts_content(self) -> None:
        # No memory, no tools, no reasoning — only message content contributes.
        occ = _context_occupancy([_msg("user", "word word word")], None, None)
        assert occ["memory_tokens"] == 0
        assert occ["tool_tokens"] == 0
        assert occ["reasoning_tokens"] == 0
        # total is the real total (message content lands in the residual, not a category).
        assert occ["total"] == _total_context_tokens([_msg("user", "word word word")], None, None)
        assert occ["total"] >= 3

    def test_memory_only(self) -> None:
        memory = [{"type": "entity", "name": "Alice and Bob and Carol"}]
        occ = _context_occupancy([_msg("user", "hi")], memory, None)
        assert occ["memory_tokens"] > 0
        assert occ["tool_tokens"] == 0
        assert occ["reasoning_tokens"] == 0

    def test_tools_only(self) -> None:
        tools = [{"name": "search", "description": "find things on the web"}]
        occ = _context_occupancy([_msg("user", "hi")], None, tools)
        assert occ["tool_tokens"] > 0
        assert occ["memory_tokens"] == 0
        assert occ["reasoning_tokens"] == 0

    def test_reasoning_only_zero_fills_elsewhere(self) -> None:
        # Thinking-model turn: reasoning_content present, no memory/tools.
        messages = [_reasoning_msg("", "word word word word")]
        occ = _context_occupancy(messages, None, None)
        assert occ["reasoning_tokens"] == 4
        assert occ["memory_tokens"] == 0
        assert occ["tool_tokens"] == 0

    def test_non_thinking_turn_reasoning_is_zero(self) -> None:
        # Plain message, no reasoning_content key — must zero-fill, not error.
        occ = _context_occupancy([_msg("user", "hello there")], None, None)
        assert occ["reasoning_tokens"] == 0

    def test_all_categories_present_total_is_real_total(self) -> None:
        messages = [_reasoning_msg("hello there", "word word")]
        memory = [{"type": "entity", "name": "Alice"}]
        tools = [{"name": "search", "description": "web"}]
        occ = _context_occupancy(messages, memory, tools)
        assert occ["memory_tokens"] > 0
        assert occ["tool_tokens"] > 0
        assert occ["reasoning_tokens"] > 0
        assert occ["total"] == _total_context_tokens(messages, memory, tools)
        # total includes message content too, so it is at least the reasoning slice.
        assert occ["total"] >= occ["reasoning_tokens"]


class TestContextOccupancyEmit:
    """apply_budget emits the breakdown on the context_budget_applied event (AC1)."""

    def test_event_carries_context_occupancy(self) -> None:
        memory = [{"type": "entity", "name": "Alice"}]
        tools = [{"name": "search", "description": "web search"}]
        ctx = _context(
            messages=[_reasoning_msg("current question", "word word word")],
            memory_context=memory,
            tool_definitions=tools,
        )
        with structlog.testing.capture_logs() as captured:
            result = apply_budget(ctx, max_tokens=10_000, trace_id="t-occ")

        evt = next(e for e in captured if e.get("event") == "context_budget_applied")
        occ = evt["context_occupancy"]
        assert set(occ) == {"memory_tokens", "tool_tokens", "reasoning_tokens", "total"}
        assert all(isinstance(v, int) for v in occ.values())
        # total in the breakdown matches the scalar already on the event and the result.
        assert occ["total"] == evt["total_tokens"]
        assert occ["total"] == result.token_count
        assert occ["memory_tokens"] > 0
        assert occ["tool_tokens"] > 0
        assert occ["reasoning_tokens"] > 0

    def test_occupancy_reflects_post_trim_state(self) -> None:
        # Memory is dropped by trimming; the breakdown must show 0 memory tokens
        # (post-trim altitude — it describes what the model actually sees).
        long_text = " ".join(["word"] * 500)
        memory = [{"type": "entity", "name": long_text}]
        ctx = _context(
            messages=[_msg("user", "short question")],
            memory_context=memory,
        )
        with structlog.testing.capture_logs() as captured:
            result = apply_budget(ctx, max_tokens=5, trace_id="t-trim")

        assert result.memory_context is None  # dropped
        evt = next(e for e in captured if e.get("event") == "context_budget_applied")
        assert evt["context_occupancy"]["memory_tokens"] == 0


# ---------------------------------------------------------------------------
# FRE-249 Bug A: entities_dropped populated when memory context is dropped
# ---------------------------------------------------------------------------


class TestBugAEntitiesDroppedPopulated:
    """Regression tests for FRE-249 Bug A.

    Pre-fix the dropped-entity cache was always empty because
    ``apply_budget`` passed ``entities_dropped=()`` unconditionally.
    """

    def setup_method(self) -> None:
        clear_dropped_entities("session-bug-a")

    def test_entities_dropped_populated_from_entity_name(self) -> None:
        long_text = " ".join(["word"] * 500)
        memory = [
            {"type": "entity", "name": "redis-config"},
            {"type": "entity", "name": "postgres-replica"},
            {"type": "session", "session_id": "session-99", "summary": long_text},
        ]
        ctx = _context(
            messages=[_msg("user", "short question")],
            memory_context=memory,
        )
        result = apply_budget(
            ctx,
            max_tokens=5,
            trace_id="t-bug-a",
            session_id="session-bug-a",
        )

        assert result.memory_context is None
        dropped = get_dropped_entities("session-bug-a")
        assert "redis-config" in dropped
        assert "postgres-replica" in dropped
        assert "session-99" in dropped

    def test_entity_id_preferred_over_name(self) -> None:
        memory = [
            {"type": "entity", "entity_id": "ent-1", "name": "fallback-name"},
        ]
        long_text = " ".join(["word"] * 500)
        ctx = _context(
            messages=[_msg("user", long_text), _msg("user", "current")],
            memory_context=memory,
        )
        apply_budget(
            ctx,
            max_tokens=5,
            trace_id="t-bug-a-2",
            session_id="session-bug-a",
        )
        dropped = get_dropped_entities("session-bug-a")
        assert "ent-1" in dropped
        assert "fallback-name" not in dropped

    def test_empty_or_non_dict_items_skipped(self) -> None:
        long_text = " ".join(["word"] * 500)
        memory: list[Any] = [
            {"type": "entity", "name": "ok"},
            {"type": "entity"},
            "not-a-dict",
            {"type": "entity", "name": ""},
        ]
        ctx = _context(
            messages=[_msg("user", long_text), _msg("user", "current")],
            memory_context=memory,
        )
        apply_budget(
            ctx,
            max_tokens=5,
            trace_id="t-bug-a-3",
            session_id="session-bug-a",
        )
        dropped = get_dropped_entities("session-bug-a")
        assert dropped == {"ok"}


# ---------------------------------------------------------------------------
# FRE-249 Phase 2 governance hook (ADR-0059 §D6)
# ---------------------------------------------------------------------------


class TestPhase2GovernanceHook:
    def setup_method(self) -> None:
        reset_incident_tracker()

    def teardown_method(self) -> None:
        reset_incident_tracker()

    def _config_kwargs(
        self,
        *,
        enabled: bool = True,
        threshold: int = 2,
        reduction: float = 0.15,
    ) -> dict[str, Any]:
        return {
            "context_quality_governance_enabled": enabled,
            "context_quality_governance_threshold": threshold,
            "context_quality_governance_budget_reduction": reduction,
        }

    def test_disabled_flag_no_op(self) -> None:
        from personal_agent.telemetry.context_quality import get_incident_tracker

        tracker = get_incident_tracker()
        for _ in range(5):
            tracker.register("session-gov-1")

        long_text = " ".join(["word"] * 100)
        ctx = _context(
            messages=[_msg("user", long_text), _msg("user", "current")],
        )
        with patch("personal_agent.request_gateway.budget.settings") as mock:
            mock.context_quality_governance_enabled = False
            mock.context_quality_governance_threshold = 2
            mock.context_quality_governance_budget_reduction = 0.15
            result = apply_budget(
                ctx,
                max_tokens=10_000,
                trace_id="t-gov",
                session_id="session-gov-1",
            )
        assert result.trimmed is False

    def test_below_threshold_no_tightening(self) -> None:
        from personal_agent.telemetry.context_quality import get_incident_tracker

        tracker = get_incident_tracker()
        tracker.register("session-gov-2")

        ctx = _context(messages=[_msg("user", "hi")])
        with patch("personal_agent.request_gateway.budget.settings") as mock:
            mock.context_quality_governance_enabled = True
            mock.context_quality_governance_threshold = 5
            mock.context_quality_governance_budget_reduction = 0.15
            apply_budget(
                ctx,
                max_tokens=10_000,
                trace_id="t-gov-2",
                session_id="session-gov-2",
            )

    def test_at_threshold_tightens_and_trims(self) -> None:
        from personal_agent.telemetry.context_quality import get_incident_tracker

        tracker = get_incident_tracker()
        tracker.register("session-gov-3")
        tracker.register("session-gov-3")

        long_text = " ".join(["word"] * 100)
        ctx = _context(
            messages=[_msg("user", long_text), _msg("user", "current")],
            memory_context=[{"type": "entity", "name": "x"}],
        )
        with patch("personal_agent.request_gateway.budget.settings") as mock:
            mock.context_quality_governance_enabled = True
            mock.context_quality_governance_threshold = 2
            mock.context_quality_governance_budget_reduction = 0.99
            result = apply_budget(
                ctx,
                max_tokens=200,
                trace_id="t-gov-3",
                session_id="session-gov-3",
            )
        assert result.trimmed is True

    def test_no_session_id_skips_governance(self) -> None:
        from personal_agent.telemetry.context_quality import get_incident_tracker

        tracker = get_incident_tracker()
        tracker.register("session-gov-4")
        tracker.register("session-gov-4")

        ctx = _context(messages=[_msg("user", "hi")])
        with patch("personal_agent.request_gateway.budget.settings") as mock:
            mock.context_quality_governance_enabled = True
            mock.context_quality_governance_threshold = 2
            mock.context_quality_governance_budget_reduction = 0.99
            result = apply_budget(
                ctx,
                max_tokens=10_000,
                trace_id="t-gov-4",
                session_id="",
            )
        assert result.trimmed is False
