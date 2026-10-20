"""The egress-guarded Responses API route for ``LiteLLMClient`` (FRE-1627).

litellm bridges some chat requests to the Responses API (today: ``gpt-6.1-sol``
with function tools). Its async bridge does not forward ``client=`` (litellm
1.106.0 and 1.107.0rc2, ``completion_extras/litellm_responses_transformation/
handler.py``), so a bridged request leaves without the ADR-0141 D2 layer-2 hook.
This module does the bridge's job with the guarded client: the assembled chat
kwargs become ``litellm.aresponses`` kwargs, and the Responses reply becomes a
chat ``ModelResponse``, so the rest of ``LiteLLMClient.respond`` reads it
unchanged.

The request map is explicit. A key it does not map raises rather than being
dropped, for the reason ``drop_params`` is off: a silently narrowed request is a
quietly-succeeding wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import litellm
from litellm.completion_extras.litellm_responses_transformation.transformation import (
    LiteLLMResponsesTransformationHandler,
)
from litellm.main import responses_api_bridge_check
from litellm.responses.utils import ResponseAPILoggingUtils
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    Message,
    ModelResponse,
)

from personal_agent.llm_client.types import LLMClientError

if TYPE_CHECKING:
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

#: Chat kwargs that keep their name on the Responses API.
_PASSTHROUGH_KEYS = frozenset(
    {"model", "api_key", "api_base", "temperature", "top_p", "timeout", "num_retries"}
)
#: Chat kwargs this module translates; ``client`` is replaced by the guarded one.
_TRANSLATED_KEYS = frozenset(
    {
        "messages",
        "max_tokens",
        "reasoning_effort",
        "tools",
        "tool_choice",
        "response_format",
        "client",
    }
)

_INCOMPLETE_FINISH_REASONS: Mapping[str | None, str] = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}

_transformation = LiteLLMResponsesTransformationHandler()  # type: ignore[no-untyped-call]


def routes_to_responses(request: Mapping[str, Any]) -> bool:
    """Whether litellm would bridge this chat request to the Responses API.

    Args:
        request: The assembled litellm chat kwargs (``model`` is ``provider/id``).

    Returns:
        True when litellm's own bridge check classifies the request as ``responses``.
    """
    provider, _, model = str(request["model"]).partition("/")
    model_info, _ = responses_api_bridge_check(
        model=model,
        custom_llm_provider=provider,
        tools=request.get("tools"),
        reasoning_effort=request.get("reasoning_effort"),
        api_base=request.get("api_base"),
    )
    return model_info.get("mode") == "responses"


def _function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a chat function tool to the Responses shape; pass any other tool through."""
    function = tool.get("function")
    if tool.get("type") != "function" or not isinstance(function, Mapping):
        return dict(tool)
    # `strict` defaults to false on Chat Completions and to true on the Responses
    # API (openai SDK FunctionToolParam). A strict schema needs
    # additionalProperties: false and every property required, which the tool
    # registry does not promise, so an undeclared value keeps the chat meaning.
    flat: dict[str, Any] = {
        "type": "function",
        "name": function["name"],
        "strict": bool(function.get("strict", False)),
    }
    for key in ("description", "parameters"):
        if function.get(key) is not None:
            flat[key] = function[key]
    return flat


def _tool_choice(choice: str | Mapping[str, Any]) -> str | dict[str, Any]:
    """Flatten a named chat tool choice; the string choices mean the same on both APIs."""
    if isinstance(choice, str):
        return choice
    function = choice.get("function")
    if choice.get("type") == "function" and isinstance(function, Mapping):
        return {"type": "function", "name": function["name"]}
    return dict(choice)


def to_responses_request(request: Mapping[str, Any], *, client: AsyncHTTPHandler) -> dict[str, Any]:
    """Translate assembled chat kwargs into ``litellm.aresponses`` kwargs.

    Args:
        request: The assembled litellm chat kwargs from ``LiteLLMClient.respond``.
        client: The egress-guarded handler that must carry the request.

    Returns:
        Keyword arguments for ``litellm.aresponses``.

    Raises:
        LLMClientError: The request carries a key this route does not map.
    """
    unmapped = sorted(set(request) - _PASSTHROUGH_KEYS - _TRANSLATED_KEYS)
    if unmapped:
        raise LLMClientError(
            f"{unmapped} have no mapping on the guarded Responses route (FRE-1627); "
            "refusing to drop them."
        )

    out: dict[str, Any] = {key: request[key] for key in _PASSTHROUGH_KEYS if key in request}
    input_items, instructions = _transformation.convert_chat_completion_messages_to_responses_api(
        list(request["messages"])
    )
    if not input_items and instructions is not None:
        # The API rejects an empty input. litellm's bridge carries a system-only
        # request as a system input item; this does the same.
        input_items = [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": instructions}],
            }
        ]
        instructions = None
    out["input"] = input_items
    if instructions is not None:
        out["instructions"] = instructions
    if request.get("max_tokens") is not None:
        out["max_output_tokens"] = request["max_tokens"]
    if request.get("reasoning_effort") is not None:
        out["reasoning"] = {"effort": request["reasoning_effort"]}
    if request.get("tools"):
        out["tools"] = [_function_tool(tool) for tool in request["tools"]]
    if request.get("tool_choice") is not None:
        out["tool_choice"] = _tool_choice(request["tool_choice"])
    if request.get("response_format") is not None:
        out["text"] = _transformation.transform_response_format_to_text_format(
            request["response_format"]
        )
    # Chat Completions does not store a request by default; the Responses API
    # does. This client is stateless (no previous_response_id), so storage has
    # no use here.
    out["store"] = False
    out["client"] = client
    return out


def _item_fields(item: object) -> Mapping[str, Any]:
    """An output item as a mapping, whether litellm parsed it to a model or kept a dict."""
    if isinstance(item, Mapping):
        return item
    dump = getattr(item, "model_dump", None)
    return dump() if callable(dump) else {}


def to_chat_response(response: ResponsesAPIResponse) -> ModelResponse:
    """Translate a Responses API reply into a chat ``ModelResponse``.

    Args:
        response: The reply ``litellm.aresponses`` returned.

    Returns:
        A one-choice chat response: text from ``output_text`` parts, the refusal
        from ``refusal`` parts, tool calls from ``function_call`` items
        (id = ``call_id``), and chat-shaped usage.

    Raises:
        LLMClientError: The reply carries an error.
    """
    if response.error is not None:
        raise LLMClientError(f"Responses API error: {response.error}")

    texts: list[str] = []
    refusals: list[str] = []
    tool_calls: list[ChatCompletionMessageToolCall] = []
    for item in response.output:
        fields = _item_fields(item)
        match fields.get("type"):
            case "message":
                for part in fields.get("content") or ():
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
                    elif part.get("type") == "refusal":
                        # ADR-0150 D1: respond() surfaces message.refusal.
                        refusals.append(part["refusal"])
            case "function_call":
                tool_calls.append(
                    ChatCompletionMessageToolCall(
                        id=fields["call_id"],
                        type="function",
                        function=Function(name=fields["name"], arguments=fields["arguments"]),
                    )
                )
            case _:
                # Reasoning items carry no chat content; the chat history keeps none.
                pass

    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        finish_reason = _INCOMPLETE_FINISH_REASONS.get(reason, "length")
    else:
        finish_reason = "tool_calls" if tool_calls else "stop"

    return ModelResponse(
        id=response.id,
        model=response.model,
        choices=[
            Choices(
                index=0,
                finish_reason=finish_reason,
                message=Message(
                    role="assistant",
                    content="".join(texts) or None,
                    tool_calls=tool_calls or None,
                    refusal="".join(refusals) or None,
                ),
            )
        ],
        usage=ResponseAPILoggingUtils.transform_response_api_usage_to_chat_usage(response.usage),
    )


async def aresponses_as_chat(responses_request: Mapping[str, Any]) -> ModelResponse:
    """Dispatch a translated request on the Responses API.

    Args:
        responses_request: The output of :func:`to_responses_request`, which
            carries the guarded client.

    Returns:
        The reply as a chat ``ModelResponse``.

    Raises:
        LLMClientError: An error in the reply, or a streamed reply.
    """
    reply = await litellm.aresponses(**responses_request)
    if not isinstance(reply, ResponsesAPIResponse):
        # The route never asks to stream, so a stream here is a litellm change.
        raise LLMClientError(f"expected a non-streamed Responses reply, got {type(reply).__name__}")
    return to_chat_response(reply)
