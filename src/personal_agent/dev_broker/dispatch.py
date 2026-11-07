"""The broker's one dispatch path (ADR-0159 D5.2, ADR-0141 D1-D2; FRE-1683).

Every model call goes through ``LiteLLMClient`` under the budget role ``dev_agent``. The client
reserves the call on the cost gate and applies the egress guard. The broker process sets that guard
to allow the Scaleway host only (see :func:`personal_agent.dev_broker.server.check_egress_guard`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from personal_agent.llm_client.types import LLMResponse, ModelRole
from personal_agent.telemetry.trace import TraceContext

#: The JSON values that a chat request carries.
JsonObject = Mapping[str, object]


@dataclass(frozen=True)
class ModelRequest:
    """The fields of an agent's request that the broker forwards.

    Attributes:
        model_key: The catalog key of the model.
        messages: OpenAI-format messages.
        tools: OpenAI-format tool definitions, or ``None``.
        tool_choice: The tool choice, or ``None``.
        temperature: The temperature, or ``None``.
        max_tokens: The clamped output-token limit.
    """

    model_key: str
    messages: Sequence[JsonObject]
    tools: Sequence[JsonObject] | None
    tool_choice: str | JsonObject | None
    temperature: float | None
    max_tokens: int


class Dispatcher(Protocol):
    """Sends one model call."""

    async def dispatch(self, request: ModelRequest, trace_ctx: TraceContext) -> LLMResponse:
        """Send ``request`` and return the provider's answer.

        Args:
            request: The forwarded request.
            trace_ctx: The job's trace context.

        Returns:
            The normalized response.
        """
        ...


class LiteLLMDispatcher:
    """Dispatch through ``LiteLLMClient`` with the ``dev_agent`` budget role."""

    async def dispatch(self, request: ModelRequest, trace_ctx: TraceContext) -> LLMResponse:
        """Send ``request`` through the model catalog's client for its key.

        Args:
            request: The forwarded request.
            trace_ctx: The job's trace context.

        Returns:
            The normalized response.
        """
        from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

        client = get_llm_client_for_key(request.model_key, budget_role="dev_agent")
        response: LLMResponse = await client.respond(
            role=ModelRole.DEV_AGENT,
            messages=[dict(message) for message in request.messages],
            trace_ctx=trace_ctx,
            tools=None if request.tools is None else [dict(tool) for tool in request.tools],
            tool_choice=(
                dict(request.tool_choice)
                if isinstance(request.tool_choice, Mapping)
                else request.tool_choice
            ),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        return response
