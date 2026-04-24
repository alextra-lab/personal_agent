"""Tests for parsing text-based tool calls."""

import json

from personal_agent.llm_client.tool_call_parser import parse_text_tool_calls


# ── Strategy 1: [TOOL_REQUEST] ──────────────────────────────────────────────


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


# ── Strategy 2: <tool_call> JSON ─────────────────────────────────────────────


def test_parse_tool_call_tag_json() -> None:
    """Parses <tool_call>{json}</tool_call> blocks."""
    content = '<tool_call>{"name":"read_file","arguments":{"path":"/tmp/a.txt"}}</tool_call>'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "/tmp/a.txt"}


# ── Strategy 2b: <tool_call> Qwen XML variant ───────────────────────────────


def test_parse_qwen_xml_single_param() -> None:
    """Parses Qwen XML-parameter variant with a JSON array value."""
    content = (
        "<tool_call>\n"
        "<function=mcp_perplexity_ask>\n"
        '<parameter=messages>[{"role": "user", "content": "weather in Paris"}]</parameter>\n'
        "</function>\n"
        "</tool_call>"
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "mcp_perplexity_ask"
    args = json.loads(calls[0]["arguments"])
    assert args["messages"] == [{"role": "user", "content": "weather in Paris"}]


def test_parse_qwen_xml_multiple_params() -> None:
    """Parses Qwen XML variant with multiple parameters."""
    content = (
        "<tool_call>\n"
        "<function=list_directory>\n"
        "<parameter=path>/home/user</parameter>\n"
        "<parameter=include_details>true</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_directory"
    args = json.loads(calls[0]["arguments"])
    assert args["path"] == "/home/user"
    assert args["include_details"] is True


def test_parse_qwen_xml_string_value_not_json() -> None:
    """Qwen XML: plain string values that aren't valid JSON stay as strings."""
    content = (
        "<tool_call>\n"
        "<function=search_tool>\n"
        "<parameter=query>how to use asyncio</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    args = json.loads(calls[0]["arguments"])
    assert args["query"] == "how to use asyncio"


def test_parse_qwen_xml_malformed_no_function_tag() -> None:
    """Qwen XML: <tool_call> without <function=...> falls through as warning."""
    content = "<tool_call>\nsome random text without function tags\n</tool_call>"
    calls = parse_text_tool_calls(content)
    assert len(calls) == 0


def test_parse_qwen_xml_does_not_conflict_with_json() -> None:
    """JSON variant still works when both formats exist in same output."""
    content = (
        '<tool_call>{"name":"read_file","arguments":{"path":"/a"}}</tool_call>\n'
        "<tool_call>\n"
        "<function=list_directory>\n"
        "<parameter=path>/b</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 2
    assert calls[0]["name"] == "read_file"
    assert calls[1]["name"] == "list_directory"


# ── Strategy 4: bracket fallback ─────────────────────────────────────────────


def test_parse_bracket_fallback_tool_call() -> None:
    """Parses [tool_name, {...}] fallback format."""
    content = '[mcp_perplexity_ask, {"messages":[{"role":"user","content":"What is 2+2?"}]}]'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "mcp_perplexity_ask"
    assert json.loads(calls[0]["arguments"]) == {
        "messages": [{"role": "user", "content": "What is 2+2?"}]
    }


def test_parse_bracket_fallback_with_trailing_noise() -> None:
    """Parses bracket fallback with extra trailing braces/brackets."""
    content = '[mcp_perplexity_ask, {"messages":[{"role":"user","content":"OpenAI pricing?"}]}}]'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "mcp_perplexity_ask"
    assert json.loads(calls[0]["arguments"])["messages"][0]["content"] == "OpenAI pricing?"


# ── Strategy 5: <tool_code> Gemini-style print(fn(...)) ──────────────────────


def test_parse_tool_code_positional_string() -> None:
    """Parses `<tool_code>print(fn("x"))</tool_code>` with a single string arg."""
    content = '<tool_code>\nprint(self_telemetry_query("ERROR", limit=5))\n</tool_code>'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "self_telemetry_query"
    args = json.loads(calls[0]["arguments"])
    # Positional "ERROR" is threaded to the first-arg-like slot; kwargs preserved.
    assert args.get("limit") == 5


def test_parse_tool_code_no_args() -> None:
    """Parses `<tool_code>print(fn())</tool_code>` with no arguments."""
    content = "<tool_code>\nprint(infra_health())\n</tool_code>"
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "infra_health"
    assert json.loads(calls[0]["arguments"]) == {}


def test_parse_tool_code_kwargs_only() -> None:
    """Parses `<tool_code>print(fn(key=value))</tool_code>` with kwargs."""
    content = (
        "<tool_code>\n"
        'print(self_telemetry_query(query_type="errors", limit=10))\n'
        "</tool_code>"
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "self_telemetry_query"
    args = json.loads(calls[0]["arguments"])
    assert args == {"query_type": "errors", "limit": 10}


def test_parse_tool_code_multiple_blocks() -> None:
    """Parses multiple `<tool_code>` blocks in one response."""
    content = (
        "<tool_code>\nprint(infra_health())\n</tool_code>\n"
        '<tool_code>\nprint(self_telemetry_query(query_type="errors", limit=5))\n</tool_code>'
    )
    calls = parse_text_tool_calls(content)
    assert len(calls) == 2
    assert calls[0]["name"] == "infra_health"
    assert calls[1]["name"] == "self_telemetry_query"


def test_parse_tool_code_bare_call_no_print() -> None:
    """Parses `<tool_code>fn(arg=value)</tool_code>` without the `print(...)` wrapper."""
    content = '<tool_code>\nself_telemetry_query(query_type="errors")\n</tool_code>'
    calls = parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "self_telemetry_query"
    assert json.loads(calls[0]["arguments"]) == {"query_type": "errors"}
