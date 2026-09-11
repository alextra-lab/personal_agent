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
