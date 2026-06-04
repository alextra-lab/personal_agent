"""FRE-484: forced-synthesis tool overrides for the Anthropic path.

The forced-synthesis path (tool-iteration limit hit) normally drops ``tools=``
so the model answers from gathered results. On Anthropic, a transcript that
already contains ``tool_use``/``tool_result`` blocks makes LiteLLM reject the
call with ``UnsupportedParamsError`` unless ``tools=`` is present. These tests
pin the decision helpers that keep a non-empty tool list and force
``tool_choice="none"`` on that path only — leaving every other path
(local SLM, no tool history) on the prior drop-tools behavior.
"""

# ruff: noqa: D103

from __future__ import annotations

from typing import Any

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


def test_anthropic_with_tool_history_retains_tools_and_pins_none() -> None:
    tools, tool_choice = _forced_synthesis_tool_overrides(
        provider="anthropic",
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=_TOOL_DEFS,
    )
    assert tools == _TOOL_DEFS
    assert tool_choice == "none"


def test_anthropic_without_tool_history_drops_tools() -> None:
    tools, tool_choice = _forced_synthesis_tool_overrides(
        provider="anthropic",
        messages=[{"role": "user", "content": "q"}],
        tool_defs=_TOOL_DEFS,
    )
    assert tools is None
    assert tool_choice is None


def test_local_provider_none_drops_tools_even_with_history() -> None:
    tools, tool_choice = _forced_synthesis_tool_overrides(
        provider=None,
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=_TOOL_DEFS,
    )
    assert tools is None
    assert tool_choice is None


def test_anthropic_with_history_but_no_tool_defs_uses_placeholder() -> None:
    # Codex gap #4: empty tool_defs must still yield a non-empty tools= so the
    # Anthropic LiteLLM raise is avoided.
    tools, tool_choice = _forced_synthesis_tool_overrides(
        provider="anthropic",
        messages=[{"role": "user", "content": "q"}, _tool_result_msg()],
        tool_defs=[],
    )
    assert tools == [_SYNTHESIS_PLACEHOLDER_TOOL]
    assert tool_choice == "none"
