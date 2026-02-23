"""Adapters for different LLM API formats.

This module provides adapters to normalize different backend API formats
(e.g., responses vs chat_completions) into a unified LLMResponse structure.

Per LOCAL_LLM_CLIENT_SPEC_v0.1.md Section 3.2, we prefer the responses API
when available, with chat_completions as a fallback.
"""

from typing import Any

from personal_agent.llm_client.tool_call_parser import parse_text_tool_calls
from personal_agent.llm_client.types import LLMInvalidResponse, LLMResponse, ToolCall


def adapt_responses_response(response_data: dict[str, Any]) -> LLMResponse:
    """Adapt responses API response to LLMResponse.

    The responses API uses an 'output' array with different structure.
    This adapter handles the /v1/responses endpoint format (preferred per spec).
    Also supports simplified format for testing (direct fields).

    Args:
        response_data: Raw response from responses API.

    Returns:
        Normalized LLMResponse structure.

    Raises:
        LLMInvalidResponse: If response format is invalid or unexpected.
    """
    try:
        # Responses API format: has 'output' array with items of type 'message' or 'reasoning'
        content = ""
        reasoning_trace = None
        tool_calls: list[ToolCall] = []

        output = response_data.get("output", [])
        if isinstance(output, list) and len(output) > 0:
            # Full responses API format with output array
            for item in output:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type", "")

                # Extract reasoning trace from reasoning items
                if item_type == "reasoning":
                    reasoning_content = item.get("content", [])
                    if isinstance(reasoning_content, list):
                        reasoning_parts = []
                        for rc in reasoning_content:
                            if isinstance(rc, dict) and rc.get("type") == "reasoning_text":
                                reasoning_parts.append(rc.get("text", ""))
                        if reasoning_parts:
                            reasoning_trace = "\n".join(reasoning_parts)

                # Extract message content from message items
                elif item_type == "message":
                    message_content = item.get("content", [])
                    if isinstance(message_content, list):
                        content_parts = []
                        for mc in message_content:
                            if isinstance(mc, dict) and mc.get("type") == "output_text":
                                content_parts.append(mc.get("text", ""))
                        if content_parts:
                            content = "\n".join(content_parts)

                    # Extract tool calls if present in message item
                    raw_tool_calls = item.get("tool_calls", [])
                    if raw_tool_calls:
                        for tc in raw_tool_calls:
                            if isinstance(tc, dict):
                                tool_calls.append(
                                    ToolCall(
                                        id=tc.get("id", ""),
                                        name=tc.get("name", ""),
                                        arguments=tc.get("arguments", "{}"),
                                    )
                                )
                    # If no structured tool calls, check for text-based tool calls
                    # (reasoning models often generate tool calls as text)
                    elif content:
                        text_tool_calls = parse_text_tool_calls(content, trace_id=None)
                        tool_calls.extend(text_tool_calls)

                # Extract function calls from function_call items (responses API format for tool calls)
                elif item_type == "function_call":
                    # Responses API returns function calls as separate items in output array
                    call_id = item.get("call_id") or item.get("id") or ""
                    function_name = item.get("name") or ""
                    function_arguments = item.get("arguments") or "{}"

                    # If arguments is a string, it should be JSON
                    # Validate it's valid JSON, but keep as string for ToolCall
                    if isinstance(function_arguments, str):
                        try:
                            import json as json_module

                            json_module.loads(function_arguments)
                        except (json_module.JSONDecodeError, ValueError):
                            function_arguments = "{}"

                    tool_calls.append(
                        ToolCall(
                            id=call_id,
                            name=function_name,
                            arguments=function_arguments,
                        )
                    )
        else:
            # Simplified format (for testing or direct field access)
            # Try direct content field
            raw_content = response_data.get("content")
            if isinstance(raw_content, dict):
                content = raw_content.get("text", "") or ""
            else:
                content = raw_content or ""

            # Extract reasoning trace if present
            reasoning_trace = response_data.get("reasoning_trace") or response_data.get("thinking")

            # Extract tool calls if present
            raw_tool_calls = response_data.get("tool_calls", [])
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    if isinstance(tc, dict):
                        tool_calls.append(
                            ToolCall(
                                id=tc.get("id", ""),
                                name=tc.get("name", ""),
                                arguments=tc.get("arguments", "{}"),
                            )
                        )

        # Extract response_id (for stateful conversation tracking)
        response_id = response_data.get("id")

        # Extract usage information
        usage = response_data.get("usage", {})
        if usage:
            # Responses API uses input_tokens/output_tokens, normalize to prompt_tokens/completion_tokens
            usage = {
                "prompt_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "completion_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                "total_tokens": usage.get("total_tokens", 0),
            }
        else:
            # Try to extract from response metadata
            usage = {
                "prompt_tokens": response_data.get(
                    "input_tokens", response_data.get("prompt_tokens", 0)
                ),
                "completion_tokens": response_data.get(
                    "output_tokens", response_data.get("completion_tokens", 0)
                ),
                "total_tokens": response_data.get("total_tokens", 0),
            }

        return LLMResponse(
            role=response_data.get("role", "assistant"),
            content=content,
            tool_calls=tool_calls,
            reasoning_trace=reasoning_trace,
            usage=usage,
            response_id=response_id,
            raw=response_data,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise LLMInvalidResponse(f"Invalid response format: {e}") from e


def adapt_chat_completions_response(response_data: dict[str, Any]) -> LLMResponse:
    """Adapt OpenAI-style chat_completions response to LLMResponse.

    This adapter handles the /v1/chat/completions endpoint format used by
    LM Studio and other OpenAI-compatible servers.

    Args:
        response_data: Raw response from chat_completions API.

    Returns:
        Normalized LLMResponse structure.

    Raises:
        LLMInvalidResponse: If response format is invalid or unexpected.
    """
    try:
        # Extract the first choice (most common case)
        choices = response_data.get("choices", [])
        if not choices:
            raise LLMInvalidResponse("Response has no choices")

        choice = choices[0]
        message = choice.get("message", {})

        # Extract content
        content = message.get("content", "") or ""

        # Extract tool calls if present (native or text-based)
        tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls", [])
        if raw_tool_calls:
            # Native function calls (structured format)
            for tc in raw_tool_calls:
                if isinstance(tc, dict):
                    tool_calls.append(
                        ToolCall(
                            id=tc.get("id", ""),
                            name=tc.get("function", {}).get("name", ""),
                            arguments=tc.get("function", {}).get("arguments", "{}"),
                        )
                    )
        elif content:
            # If no structured tool calls, check for text-based tool calls
            # (reasoning models without native function calling support generate tool calls as text)
            text_tool_calls = parse_text_tool_calls(content, trace_id=None)
            tool_calls.extend(text_tool_calls)

        # Extract usage information
        usage = response_data.get("usage", {})
        if not usage:
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }

        # No reasoning trace in chat_completions format
        reasoning_trace = None

        # No response_id in chat_completions format (stateless)
        response_id = None

        return LLMResponse(
            role=message.get("role", "assistant"),
            content=content,
            tool_calls=tool_calls,
            reasoning_trace=reasoning_trace,
            usage=usage,
            response_id=response_id,
            raw=response_data,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise LLMInvalidResponse(f"Invalid response format: {e}") from e


def build_responses_request(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    previous_response_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build a responses API request payload.

    The responses API uses 'input' (string or array of input items) NOT 'messages'.
    For tool results, we send function_call_output items with previous_response_id.

    Args:
        messages: List of message dicts with role and content.
        model: Model identifier.
        tools: Optional list of tool definitions for function calling.
        tool_choice: Tool choice parameter ("auto", "none", or specific tool).
        max_tokens: Maximum tokens to generate.
        temperature: Sampling temperature.
        previous_response_id: ID from previous response (for stateful conversation).
        reasoning_effort: Optional reasoning effort level. LM Studio /v1/responses API
            supports "minimal", "low", "medium", "high". Warnings about model support are harmless.

    Returns:
        Request payload dictionary.
    """
    payload: dict[str, Any] = {"model": model}

    # LM Studio's /responses endpoint is OpenAI-compatible, but in practice it's safest to use:
    # - input as a simple string for normal turns
    # - input as function_call_output items for tool-result turns (stateful via previous_response_id)
    tool_result_messages = [msg for msg in messages if msg.get("role") == "tool"]

    if tool_result_messages:
        # Convert tool role messages to function_call_output items
        input_items: list[dict[str, Any]] = []
        for msg in tool_result_messages:
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", "{}"),
                }
            )
        payload["input"] = input_items
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
    else:
        # No tool results: format as a single string input
        # Extract content from messages (skip assistant messages with tool_calls)
        content_parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Skip assistant messages that have tool_calls (they're not text content)
            if role == "assistant" and msg.get("tool_calls"):
                continue

            if content:
                if role == "system":
                    content_parts.append(content)
                elif role == "user":
                    content_parts.append(f"User: {content}")
                elif role == "assistant":
                    content_parts.append(f"Assistant: {content}")

        payload["input"] = "\n".join(content_parts) if content_parts else ""

        # Tools are only included on the tool-request turn (not on tool-result follow-up)
        if tools:
            # LM Studio's /responses validator requires tools[i].name for function tools.
            tools_with_name: list[dict[str, Any]] = []
            for tool in tools:
                tool_copy = tool.copy()
                if tool_copy.get("type") == "function" and isinstance(
                    tool_copy.get("function"), dict
                ):
                    tool_copy["name"] = tool_copy["function"].get("name", "")
                tools_with_name.append(tool_copy)
            payload["tools"] = tools_with_name
            payload["tool_choice"] = tool_choice if tool_choice else "auto"

    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    if temperature is not None:
        payload["temperature"] = temperature

    return payload


def build_chat_completions_request(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
    previous_response_id: str | None = None,  # Ignored for chat/completions (stateless)
    reasoning_effort: str | None = None,  # Ignored for chat/completions (responses-only)
) -> dict[str, Any]:
    """Build a chat_completions API request payload.

    This is a fallback adapter for when only chat_completions is available.

    Args:
        messages: List of message dicts with role and content.
        model: Model identifier.
        tools: Optional list of tool definitions for function calling.
        tool_choice: Tool choice parameter ("auto", "none", or specific tool).
        max_tokens: Maximum tokens to generate.
        temperature: Sampling temperature.
        response_format: Optional structured output constraints (OpenAI-compatible).
        previous_response_id: Ignored (chat/completions is stateless, included for signature consistency).
        reasoning_effort: Ignored (chat/completions doesn't support reasoning effort, included for signature consistency).

    Returns:
        Request payload dictionary.
    """
    # Normalize messages: handle tool_calls format for different backends
    # Some backends (mlx-openai-server) require 'index' field, others (llama-cpp-python) may not
    # Some backends may not handle assistant messages with tool_calls in conversation history
    normalized_messages: list[dict[str, Any]] = []
    for msg in messages:
        msg_copy = msg.copy()
        role = msg_copy.get("role", "")

        # Handle assistant messages with tool_calls
        if role == "assistant" and "tool_calls" in msg_copy:
            tool_calls = msg_copy["tool_calls"]
            if isinstance(tool_calls, list):
                normalized_tool_calls = []
                for idx, tc in enumerate(tool_calls):
                    tc_copy = tc.copy() if isinstance(tc, dict) else {}
                    # Ensure index is present (some backends require it for validation)
                    # If index already exists, preserve it; otherwise add it
                    if "index" not in tc_copy:
                        tc_copy["index"] = idx
                    normalized_tool_calls.append(tc_copy)
                msg_copy["tool_calls"] = normalized_tool_calls
        normalized_messages.append(msg_copy)

    payload: dict[str, Any] = {
        "model": model,
        "messages": normalized_messages,
    }

    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        else:
            # Default to "auto" if tools are provided
            payload["tool_choice"] = "auto"

    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    if temperature is not None:
        payload["temperature"] = temperature

    if response_format is not None:
        payload["response_format"] = response_format

    return payload
