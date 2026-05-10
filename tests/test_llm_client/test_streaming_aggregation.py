"""Tests for `_aggregate_streaming_chunks` — reassembling SSE chat-completion chunks.

The streaming protocol arrives as `data: {...}` JSON dicts; this aggregator
must produce a single response dict whose shape is identical to what the
non-streaming `adapt_chat_completions_response` consumes. The agent depends on
this equivalence — `_do_request` calls the aggregator, then passes its output
straight into the existing adapter.
"""

import pytest

from personal_agent.llm_client.adapters import (
    _aggregate_streaming_chunks,
    adapt_chat_completions_response,
)
from personal_agent.llm_client.types import LLMInvalidResponse


def _make_chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> dict:
    """Build one OpenAI-style chunk dict around a delta."""
    chunk: dict = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def test_empty_chunks_raises() -> None:
    """No chunks at all is an invalid stream."""
    with pytest.raises(LLMInvalidResponse):
        _aggregate_streaming_chunks([])


def test_plain_text_response_aggregates() -> None:
    """Multiple content deltas concatenate in order."""
    chunks = [
        _make_chunk({"role": "assistant", "content": "Hello"}),
        _make_chunk({"content": ", "}),
        _make_chunk({"content": "world!"}),
        _make_chunk({}, finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}),
    ]
    result = _aggregate_streaming_chunks(chunks)
    msg = result["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Hello, world!"
    assert "tool_calls" not in msg
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 8


def test_aggregated_shape_round_trips_through_response_adapter() -> None:
    """The aggregator's output must be valid input to `adapt_chat_completions_response`.

    This is the contract `_do_request` relies on: chunks → aggregate → adapt → LLMResponse.
    """
    chunks = [
        _make_chunk({"role": "assistant", "content": "answer"}),
        _make_chunk({}, finish_reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
    ]
    aggregated = _aggregate_streaming_chunks(chunks)
    llm_response = adapt_chat_completions_response(aggregated)
    assert llm_response["content"] == "answer"
    assert llm_response["tool_calls"] == []
    assert llm_response["usage"]["total_tokens"] == 2


def test_single_tool_call_concatenates_argument_fragments() -> None:
    """Tool call arguments arrive as JSON fragments and must be joined in order."""
    chunks = [
        _make_chunk({"role": "assistant"}),
        _make_chunk({
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "bash", "arguments": ""},
                }
            ]
        }),
        _make_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"comm'}}]}),
        _make_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}]}),
        _make_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}),
    ]
    result = _aggregate_streaming_chunks(chunks)
    tcs = result["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_abc"
    assert tcs[0]["function"]["name"] == "bash"
    assert tcs[0]["function"]["arguments"] == '{"command": "ls"}'
    assert result["choices"][0]["finish_reason"] == "tool_calls"


def test_parallel_tool_calls_indexed_separately() -> None:
    """Multiple tool calls identified by `index` accumulate independently even when interleaved."""
    chunks = [
        _make_chunk({"role": "assistant"}),
        _make_chunk({"tool_calls": [
            {"index": 0, "id": "call_0", "type": "function", "function": {"name": "bash", "arguments": ""}},
            {"index": 1, "id": "call_1", "type": "function", "function": {"name": "bash", "arguments": ""}},
        ]}),
        # Interleaved fragments
        _make_chunk({"tool_calls": [{"index": 1, "function": {"arguments": '{"x":1}'}}]}),
        _make_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"y":'}}]}),
        _make_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]}),
        _make_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22}),
    ]
    result = _aggregate_streaming_chunks(chunks)
    tcs = result["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 2
    # Sorted by index — first call_0, then call_1
    assert tcs[0]["id"] == "call_0"
    assert tcs[0]["function"]["arguments"] == '{"y":2}'
    assert tcs[1]["id"] == "call_1"
    assert tcs[1]["function"]["arguments"] == '{"x":1}'


def test_reasoning_content_accumulates() -> None:
    """`reasoning_content` deltas (from llama-server's reasoning_parser) accumulate too."""
    chunks = [
        _make_chunk({"role": "assistant", "reasoning_content": "Let me think. "}),
        _make_chunk({"reasoning_content": "Step 1. "}),
        _make_chunk({"reasoning_content": "Step 2."}),
        _make_chunk({"content": "Done."}),
        _make_chunk({}, finish_reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5}),
    ]
    result = _aggregate_streaming_chunks(chunks)
    msg = result["choices"][0]["message"]
    assert msg["reasoning_content"] == "Let me think. Step 1. Step 2."
    assert msg["content"] == "Done."


def test_usage_only_final_chunk_is_captured() -> None:
    """vLLM/llama-server emit usage in a final usage-only chunk with no choices."""
    chunks = [
        _make_chunk({"role": "assistant", "content": "hi"}),
        _make_chunk({}, finish_reason="stop"),
        # Final usage-only chunk; some servers send choices=[] alongside usage.
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        },
    ]
    result = _aggregate_streaming_chunks(chunks)
    assert result["usage"]["total_tokens"] == 12


def test_error_chunk_raises() -> None:
    """A chunk with a non-null `error` field is a server-side failure mid-stream."""
    chunks = [
        _make_chunk({"role": "assistant", "content": "partial"}),
        {"error": {"message": "context length exceeded", "type": "invalid_request_error"}},
    ]
    with pytest.raises(LLMInvalidResponse):
        _aggregate_streaming_chunks(chunks)


def test_tool_call_without_index_field_falls_back_to_zero() -> None:
    """Some servers omit the `index` for single-tool responses; treat as index 0."""
    chunks = [
        _make_chunk({"role": "assistant"}),
        _make_chunk({"tool_calls": [
            {"id": "call_solo", "type": "function", "function": {"name": "fn", "arguments": "{}"}},
        ]}),
        _make_chunk({}, finish_reason="tool_calls"),
    ]
    result = _aggregate_streaming_chunks(chunks)
    tcs = result["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_solo"
    assert tcs[0]["function"]["name"] == "fn"


def test_aggregator_default_usage_when_missing() -> None:
    """If no usage chunk arrives, the aggregator returns a zeroed usage block.

    The downstream telemetry path tolerates zeros; what we cannot tolerate is a
    KeyError when reading `usage.prompt_tokens`.
    """
    chunks = [
        _make_chunk({"role": "assistant", "content": "x"}),
        _make_chunk({}, finish_reason="stop"),
    ]
    result = _aggregate_streaming_chunks(chunks)
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
