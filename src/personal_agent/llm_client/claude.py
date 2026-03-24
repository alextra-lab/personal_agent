"""Claude API client for Anthropic's Claude models.

Model identity (model ID, max_tokens) is injected at construction time from a
ModelDefinition loaded out of config/models.yaml (ADR-0031). Only secrets
(api_key) and operational controls (budget) come from settings.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole, ToolCall
    from personal_agent.telemetry.trace import TraceContext

from anthropic import AsyncAnthropic

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()

# Approximate pricing per million tokens for cost estimation when the API
# does not return pricing metadata.  Update this table when model pricing changes.
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    # model_id_prefix: (input_usd_per_mtok, output_usd_per_mtok)
    "claude-haiku": (0.80, 4.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus": (15.0, 75.0),
}

_DEFAULT_INPUT_PRICE = 3.0
_DEFAULT_OUTPUT_PRICE = 15.0


def _estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD using known pricing tiers.

    Args:
        model_id: Full model identifier string.
        input_tokens: Number of prompt tokens consumed.
        output_tokens: Number of completion tokens generated.

    Returns:
        Estimated cost in USD.
    """
    for prefix, (in_price, out_price) in _ANTHROPIC_PRICING.items():
        if prefix in model_id:
            return (input_tokens / 1_000_000 * in_price) + (output_tokens / 1_000_000 * out_price)
    return (input_tokens / 1_000_000 * _DEFAULT_INPUT_PRICE) + (
        output_tokens / 1_000_000 * _DEFAULT_OUTPUT_PRICE
    )


class ClaudeClient:
    """Client for Anthropic Claude API.

    Model identity comes from a ModelDefinition (config/models.yaml) injected at
    construction time; secrets and budgets come from settings (ADR-0031).

    Usage:
        from personal_agent.config import load_model_config

        model_config = load_model_config()
        model_def = model_config.models[model_config.entity_extraction_role]
        client = ClaudeClient(model_id=model_def.id, max_tokens=model_def.max_tokens)
        response = await client.chat_completion(messages=[...])
    """

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 4096,
        cost_tracker: CostTrackerService | None = None,
    ) -> None:
        """Initialize Claude client.

        Args:
            model_id: Anthropic model identifier (e.g. "claude-sonnet-4-5-20250514").
                      Comes from ModelDefinition.id in config/models.yaml.
            max_tokens: Maximum output tokens per request. Comes from
                        ModelDefinition.max_tokens in config/models.yaml.
            cost_tracker: Optional cost tracking service (creates one if not provided).

        Raises:
            ValueError: If AGENT_ANTHROPIC_API_KEY is not configured.
        """
        api_key = settings.anthropic_api_key
        if not api_key:
            raise ValueError(
                "Anthropic API key not configured. Set AGENT_ANTHROPIC_API_KEY environment variable."
            )

        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model_id
        self.max_tokens = max_tokens
        self.weekly_budget_usd = settings.cloud_weekly_budget_usd

        # Cost tracking (persisted to PostgreSQL)
        self.cost_tracker = cost_tracker or CostTrackerService()

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int | None = None,
        trace_id: UUID | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Make a chat completion request to Claude.

        Args:
            messages: List of messages (OpenAI format: role, content)
            system: Optional system message
            max_tokens: Optional max tokens (defaults to settings value)
            trace_id: Optional trace ID for request tracking
            purpose: Optional purpose for cost tracking

        Returns:
            Response dict with content, usage, and cost information

        Raises:
            ValueError: If API key not configured or weekly budget exceeded
            Exception: If API call fails
        """
        # Check weekly budget
        await self._check_weekly_budget()

        # Convert messages to Claude format
        claude_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")

            # Claude uses "user" and "assistant" roles
            if role in ("user", "assistant"):
                claude_messages.append({"role": role, "content": content})

        # Ensure cost tracker is connected (lazy connect — __init__ is sync)
        if self.cost_tracker.pool is None:
            await self.cost_tracker.connect()

        # Make API call
        try:
            # Build create parameters
            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens or self.max_tokens,
                "messages": claude_messages,
            }
            if system:
                create_params["system"] = system

            t0 = time.monotonic()
            response = await self.client.messages.create(**create_params)
            latency_ms = int((time.monotonic() - t0) * 1000)

            # Extract response content
            content = ""
            if response.content:
                for block in response.content:
                    if hasattr(block, "text"):
                        content += block.text

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_usd = _estimate_cost(self.model, input_tokens, output_tokens)

            # Record cost + latency to database
            await self.cost_tracker.record_api_call(
                provider="anthropic",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                trace_id=trace_id,
                purpose=purpose,
                latency_ms=latency_ms,
            )

            log.info(
                "claude_api_call_completed",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                trace_id=trace_id,
                purpose=purpose,
                component="claude_client",
            )

            return {
                "content": content,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "model": self.model,
            }

        except Exception as e:
            log.error(
                "claude_api_call_failed",
                model=self.model,
                error=str(e),
                exc_info=True,
                component="claude_client",
            )
            raise

    async def _check_weekly_budget(self) -> None:
        """Check if the shared cloud weekly budget has been exceeded.

        Raises:
            ValueError: If weekly budget exceeded.
        """
        weekly_cost_usd = await self.cost_tracker.get_weekly_cost(provider=None)

        if weekly_cost_usd >= self.weekly_budget_usd:
            raise ValueError(
                f"Weekly cloud API budget exceeded: "
                f"${weekly_cost_usd:.2f} / ${self.weekly_budget_usd:.2f}"
            )

    async def get_cost_summary(self) -> dict[str, Any]:
        """Get cost tracking summary from database.

        Returns:
            Dict with total and weekly costs
        """
        summary = await self.cost_tracker.get_cost_summary(provider="anthropic")
        summary["weekly_budget_usd"] = self.weekly_budget_usd
        summary["budget_remaining_usd"] = max(
            0.0, self.weekly_budget_usd - summary["weekly_cost_usd"]
        )
        return summary

    @property
    def model_configs(self) -> dict[str, Any]:
        """Expose model configs for executor compatibility.

        Returns:
            Dict mapping role names to ModelDefinition objects.
        """
        from personal_agent.config import load_model_config

        config = load_model_config()
        return config.models

    @staticmethod
    def _convert_tools_to_anthropic(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool definitions to Anthropic format.

        Args:
            tools: OpenAI-format tool definitions.

        Returns:
            Anthropic-format tool definitions.
        """
        anthropic_tools: list[dict[str, Any]] = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return anthropic_tools

    @staticmethod
    def _convert_messages_to_anthropic(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic format.

        Extracts system message (separate parameter in Anthropic API) and
        converts tool-result messages to Anthropic's tool_result content blocks.

        Args:
            messages: OpenAI-format message list.

        Returns:
            Tuple of (system_text, anthropic_messages).
        """
        system_text: str | None = None
        anthropic_msgs: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_text = content
                continue

            if role == "tool":
                anthropic_msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": content or "",
                            }
                        ],
                    }
                )
                continue

            if role == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", str(uuid4())),
                            "name": func.get("name", ""),
                            "input": args,
                        }
                    )
                anthropic_msgs.append({"role": "assistant", "content": blocks})
                continue

            if role in ("user", "assistant"):
                anthropic_msgs.append({"role": role, "content": content or ""})

        return system_text, anthropic_msgs

    async def respond(
        self,
        role: ModelRole,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,  # noqa: ARG002
        max_retries: int | None = None,  # noqa: ARG002
        reasoning_effort: str | None = None,  # noqa: ARG002
        trace_ctx: TraceContext | None = None,
        previous_response_id: str | None = None,  # noqa: ARG002
        priority: Any = None,  # noqa: ARG002
        priority_timeout: float | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        """Make an LLM call via Anthropic API, matching LocalLLMClient interface.

        Converts OpenAI-format messages/tools to Anthropic format, calls the API,
        and returns a normalized LLMResponse.

        Args:
            role: Model role (unused — ClaudeClient always uses self.model).
            messages: OpenAI-format message list.
            tools: Optional OpenAI-format tool definitions.
            tool_choice: Tool choice ("auto", "none", "any", or specific).
            response_format: Unused (Anthropic uses a different mechanism).
            system_prompt: Optional system prompt (merged with system message).
            max_tokens: Max output tokens.
            temperature: Sampling temperature.
            timeout_s: Unused (Anthropic SDK handles its own timeouts).
            max_retries: Unused (Anthropic SDK has built-in retry logic).
            reasoning_effort: Unused (not supported by Anthropic API).
            trace_ctx: Trace context for telemetry.
            previous_response_id: Unused (not supported by Anthropic API).
            priority: Unused (no local concurrency control needed).
            priority_timeout: Unused.

        Returns:
            LLMResponse with normalized structure.
        """
        from personal_agent.llm_client.types import LLMResponse

        del role  # unused — ClaudeClient always uses self.model

        await self._check_weekly_budget()

        system_from_msgs, anthropic_msgs = self._convert_messages_to_anthropic(messages)
        system = system_prompt or system_from_msgs

        create_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": anthropic_msgs,
        }
        if system:
            create_params["system"] = system
        if temperature is not None:
            create_params["temperature"] = temperature
        if tools:
            create_params["tools"] = self._convert_tools_to_anthropic(tools)
            if tool_choice == "none":
                pass  # Anthropic has no "none" equivalent — omit tool_choice
            elif tool_choice in ("auto", None):
                create_params["tool_choice"] = {"type": "auto"}
            elif tool_choice == "any":
                create_params["tool_choice"] = {"type": "any"}
            elif isinstance(tool_choice, dict) and "function" in tool_choice:
                create_params["tool_choice"] = {
                    "type": "tool",
                    "name": tool_choice["function"]["name"],
                }

        if self.cost_tracker.pool is None:
            await self.cost_tracker.connect()

        trace_uuid: UUID | None = None
        if trace_ctx is not None:
            try:
                trace_uuid = UUID(str(trace_ctx.trace_id))
            except (ValueError, AttributeError):
                pass

        t0 = time.monotonic()
        response = await self.client.messages.create(**create_params)
        latency_ms = int((time.monotonic() - t0) * 1000)

        content_text = ""
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                from personal_agent.llm_client.types import ToolCall as ToolCallType

                tool_calls.append(
                    ToolCallType(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input),
                    )
                )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost_usd = _estimate_cost(self.model, input_tokens, output_tokens)

        await self.cost_tracker.record_api_call(
            provider="anthropic",
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            trace_id=trace_uuid,
            purpose="orchestrator",
            latency_ms=latency_ms,
        )

        log.info(
            "claude_respond_completed",
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            tool_calls_count=len(tool_calls),
            trace_id=str(trace_uuid) if trace_uuid else None,
            component="claude_client",
        )

        return LLMResponse(
            role="assistant",
            content=content_text,
            tool_calls=tool_calls,
            reasoning_trace=None,
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_id=response.id,
            raw={"stop_reason": response.stop_reason},
        )
