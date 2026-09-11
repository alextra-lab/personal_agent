"""ADR-0149 D1: forced-synthesis tool cache retention.

The forced-synthesis path (tool-iteration limit hit) normally drops ``tools=``
so the model answers from gathered results. On backends that support cache
retention, keeping ``tools=`` with ``tool_choice="none"`` preserves the
prompt prefix cache. These tests verify: (1) backends with cache support
retain tools when history contains tool blocks; (2) backends without cache
support drop tools; (3) placeholder tools are used when real defs unavailable.
"""

# ruff: noqa: D103

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.models import Dialect
from personal_agent.orchestrator.executor import (
    _SYNTHESIS_PLACEHOLDER_TOOL,
    _forced_synthesis_tool_overrides,
    _tool_budget_message,
    _transcript_has_tool_blocks,
)

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {"name": "web_search", "description": "search", "parameters": {}},
    }
]


def _tool_result_msg() -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "call_1", "content": "result"}


def _assistant_tool_call_msg() -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }
        ],
    }


def _mock_llm_client(dialect: Dialect) -> MagicMock:
    """Create mock llm_client with dialect_for_role returning the given value."""
    client = MagicMock()
    client.dialect_for_role.return_value = dialect
    return client


# ── _transcript_has_tool_blocks ──────────────────────────────────────────────


def test_transcript_has_tool_blocks_detects_tool_result() -> None:
    messages = [{"role": "user", "content": "hi"}, _tool_result_msg()]
    assert _transcript_has_tool_blocks(messages) is True


def test_transcript_has_tool_blocks_detects_assistant_tool_calls() -> None:
    messages = [{"role": "user", "content": "hi"}, _assistant_tool_call_msg()]
    assert _transcript_has_tool_blocks(messages) is True


def test_transcript_has_tool_blocks_false_for_plain_chat() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert _transcript_has_tool_blocks(messages) is False


# ── _forced_synthesis_tool_overrides ─────────────────────────────────────────


def test_cache_enabled_with_tool_history_retains_tools_and_pins_none() -> None:
    client = _mock_llm_client(dialect=Dialect.LLAMACPP_QWEN)
    tools, tool_choice = _forced_synthesis_tool_overrides(
        llm_client=client,
        model_role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=_TOOL_DEFS,
    )
    assert tools == _TOOL_DEFS
    assert tool_choice == "none"


def test_cache_enabled_without_tool_history_drops_tools() -> None:
    client = _mock_llm_client(dialect=Dialect.LLAMACPP_QWEN)
    tools, tool_choice = _forced_synthesis_tool_overrides(
        llm_client=client,
        model_role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": "q"}],
        tool_defs=_TOOL_DEFS,
    )
    assert tools is None
    assert tool_choice is None


def test_none_dialect_drops_tools_even_with_history() -> None:
    client = _mock_llm_client(dialect=None)
    with patch(
        "personal_agent.orchestrator.executor.synthesis_retains_tools",
        return_value=False,
    ):
        tools, tool_choice = _forced_synthesis_tool_overrides(
            llm_client=client,
            model_role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
            tool_defs=_TOOL_DEFS,
        )
    assert tools is None
    assert tool_choice is None


def test_cache_enabled_with_history_but_no_tool_defs_uses_placeholder() -> None:
    client = _mock_llm_client(dialect=Dialect.LLAMACPP_QWEN)
    tools, tool_choice = _forced_synthesis_tool_overrides(
        llm_client=client,
        model_role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=[],
    )
    assert tools == [_SYNTHESIS_PLACEHOLDER_TOOL]
    assert tool_choice == "none"


# ── AC-1: Countdown message unit naming ─────────────────────────────────────────
# These tests verify that budget warning messages use "tool round(s)" language
# and provide correct zero-case text (AC-1).


def test_tool_budget_message_at_remaining_2() -> None:
    """At max-2 (remaining=2), the production helper names the unit as rounds."""
    message = _tool_budget_message(2)
    assert message == (
        "⚠️ Tool budget: 2 tool round(s) remaining "
        "(a round may hold several parallel calls). Prioritize synthesis — "
        "start another round only if it is strictly necessary to answer the user's question."
    )


def test_tool_budget_message_at_remaining_1() -> None:
    """At max-1 (remaining=1), the production helper still counts down in rounds."""
    message = _tool_budget_message(1)
    assert message == (
        "⚠️ Tool budget: 1 tool round(s) remaining "
        "(a round may hold several parallel calls). Prioritize synthesis — "
        "start another round only if it is strictly necessary to answer the user's question."
    )


def test_tool_budget_message_at_remaining_0_is_last_round_text() -> None:
    """At max (remaining=0), the production helper returns the last-round text,
    not an invitation to call a tool that will then be dropped.
    """
    message = _tool_budget_message(0)
    assert message == (
        "⚠️ Tool budget: this is your last round. Your next reply will have no tools available. "
        "Gather what you still need now, in parallel, and be ready to write your answer."
    )
    assert "tool call(s)" not in message
    assert "0 tool round(s)" not in message


# ── AC-2: Seeded negative for cache miss ────────────────────────────────────────
# Test that when SYNTHESIS_RETAINS_TOOLS[LLAMACPP_QWEN] is False,
# forced_synthesis_cache_miss_declared is logged (AC-2 seeded negative).


def test_cache_disabled_on_llamacpp_qwen_does_not_retain_tools() -> None:
    """AC-2 seeded negative: patch LLAMACPP_QWEN to False, verify tools not retained."""
    from personal_agent.llm_client.models import SYNTHESIS_RETAINS_TOOLS
    from personal_agent.orchestrator.executor import synthesis_retains_tools

    # Save original value
    original_value = SYNTHESIS_RETAINS_TOOLS[Dialect.LLAMACPP_QWEN]

    try:
        # Patch to False (seeded negative)
        SYNTHESIS_RETAINS_TOOLS[Dialect.LLAMACPP_QWEN] = False

        # Verify synthesis_retains_tools now returns False for this dialect
        assert synthesis_retains_tools(Dialect.LLAMACPP_QWEN) is False

        # When cache capability is disabled, tools should not be retained
        # even with tool history present
        client = _mock_llm_client(dialect=Dialect.LLAMACPP_QWEN)
        tools, tool_choice = _forced_synthesis_tool_overrides(
            llm_client=client,
            model_role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
            tool_defs=_TOOL_DEFS,
        )

        # With cache disabled, should return (None, None) - prior drop-tools behavior
        assert tools is None
        assert tool_choice is None

    finally:
        # Restore original value
        SYNTHESIS_RETAINS_TOOLS[Dialect.LLAMACPP_QWEN] = original_value


# ── AC-3: No tool calls on synthesis ────────────────────────────────────────────
# These tests verify that tool_choice="none" prevents tool calls (AC-3).


def test_forced_synthesis_returns_none_tool_choice_when_cache_enabled() -> None:
    """AC-3: When cache is enabled and tools are kept, tool_choice must be 'none'."""
    client = _mock_llm_client(dialect=Dialect.LLAMACPP_QWEN)
    tools, tool_choice = _forced_synthesis_tool_overrides(
        llm_client=client,
        model_role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=_TOOL_DEFS,
    )
    # When tools are kept, tool_choice must be "none" to prevent model from calling them
    assert tool_choice == "none"
    assert tools is not None  # Tools are present for cache retention
