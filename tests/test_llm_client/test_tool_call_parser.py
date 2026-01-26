"""Tests for parsing text-based tool calls."""

import json

from personal_agent.llm_client.tool_call_parser import parse_text_tool_calls


def test_parse_tool_request_end_tool_request() -> None:
    """Parses [TOOL_REQUEST]... [END_TOOL_REQUEST] blocks."""
    content = (
        '[TOOL_REQUEST]{"name":"list_directory","arguments":{"path":"/tmp"}}[END_TOOL_REQUEST]'
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_directory"
    assert json.loads(calls[0]["arguments"]) == {"path": "/tmp"}


def test_parse_tool_request_end_tool_result_is_accepted() -> None:
    """Parses [TOOL_REQUEST]... [END_TOOL_RESULT] blocks (model typo tolerance)."""
    content = '[TOOL_REQUEST]{"name":"list_directory","arguments":{"path":"/tmp"}}[END_TOOL_RESULT]'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_directory"
    assert json.loads(calls[0]["arguments"]) == {"path": "/tmp"}
