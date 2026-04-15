"""LiteLLM-backed client for all cloud LLM providers.

Uses litellm.acompletion() to transparently handle message/tool format
conversion across Anthropic, OpenAI, Google, Mistral, and other providers.

Replaces ClaudeClient for all cloud providers (ADR-0033). Two clients, clear
boundary: LocalLLMClient for local inference, LiteLLMClient for cloud.

Our wrapper adds: cost tracking via CostTrackerService, budget enforcement,
and telemetry (structlog). LiteLLM handles provider format conversion and retries.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import litellm
import structlog

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole, ToolCall
    from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

# Suppress litellm verbose startup logging
litellm.suppress_debug_info = True


class LiteLLMClient:
    """Cloud LLM client backed by LiteLLM.

    Handles all cloud providers (Anthropic, OpenAI, Google, Mistral, etc.)
    through a single interface. LiteLLM manages message format conversion,
    tool calling translation, and provider-specific API differences.

    Our wrapper adds:
    - Cost tracking via CostTrackerService (record_api_call to PostgreSQL)
    - Weekly budget enforcement (AGENT_CLOUD_WEEKLY_BUDGET_USD)
    - Telemetry emission via structlog

    The factory selects this client when provider_type is not "local" (ADR-0033).
    LiteLLM model string format: "{provider}/{model_id}" e.g. "anthropic/claude-sonnet-4-6".

    Args:
        model_id: Provider model identifier (e.g., "claude-sonnet-4-6").
        provider: Provider name for LiteLLM dispatch (e.g., "anthropic", "openai", "google").
        max_tokens: Default maximum output tokens.

    Raises:
        ValueError: If weekly cloud budget is exceeded before the call.
    """

    def __init__(
        self,
        model_id: str,
        provider: str = "anthropic",
        max_tokens: int = 8192,
    ) -> None:
        """Initialize LiteLLMClient with model and provider configuration."""
        self.model_id = model_id
        self.provider = provider
        self.max_tokens = max_tokens
        # LiteLLM model string: "provider/model_id"
        self._litellm_model = f"{provider}/{model_id}"

    @property
    def model_configs(self) -> dict[str, Any]:
        """Expose model configs dict for executor compatibility (model_configs.get(role))."""
        from personal_agent.config import load_model_config

        return load_model_config().models

    async def respond(
        self,
        role: ModelRole,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        reasoning_effort: str | None = None,
        trace_ctx: TraceContext | None = None,
        previous_response_id: str | None = None,
        priority: Any = None,
        priority_timeout: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make an LLM call via LiteLLM to any cloud provider.

        LiteLLM handles message/tool format conversion transparently.
        This method adds budget checking, cost recording, and telemetry.

        Args:
            role: Model role (used for telemetry; model is fixed at construction).
            messages: OpenAI-format messages (LiteLLM converts to provider format).
            tools: OpenAI-format tool definitions (LiteLLM converts as needed).
            tool_choice: Tool selection strategy.
            response_format: Response format constraint (JSON mode, etc.).
            system_prompt: System prompt prepended as a system message.
            max_tokens: Max output tokens override (defaults to self.max_tokens).
            temperature: Temperature override.
            timeout_s: Request timeout in seconds.
            max_retries: Number of retries on transient errors.
            reasoning_effort: Reasoning effort hint (provider-specific, passed through).
            trace_ctx: Trace context for telemetry.
            previous_response_id: Ignored for cloud providers (stateless API).
            priority: Ignored for cloud providers.
            priority_timeout: Ignored for cloud providers.
            **kwargs: Additional provider-specific parameters passed to litellm.

        Returns:
            Normalized LLMResponse.

        Raises:
            ValueError: If weekly cloud budget is exceeded.
            LLMClientError: On API failure after retries.
        """
        from personal_agent.llm_client.types import LLMClientError
        from personal_agent.llm_client.types import LLMResponse as LLMResponseType
        from personal_agent.llm_client.types import ToolCall as ToolCallType

        effective_max_tokens = max_tokens or self.max_tokens
        trace_id = str(trace_ctx.trace_id) if trace_ctx else str(uuid4())

        # Budget enforcement — mirror ClaudeClient._check_weekly_budget pattern
        from personal_agent.config.settings import get_settings
        from personal_agent.llm_client.cost_tracker import CostTrackerService

        _settings = get_settings()
        cost_tracker = CostTrackerService()
        await cost_tracker.connect()
        try:
            weekly_cost = await cost_tracker.get_weekly_cost(provider=None)
            if weekly_cost >= _settings.cloud_weekly_budget_usd:
                raise ValueError(
                    f"Weekly cloud API budget exceeded: "
                    f"${weekly_cost:.2f} >= ${_settings.cloud_weekly_budget_usd:.2f}"
                )
        except ValueError:
            raise
        except Exception:
            pass  # DB unavailable — allow call, cost tracking degraded

        # Prepend system prompt as a system message if provided
        api_messages = list(messages)
        if system_prompt:
            api_messages = [{"role": "system", "content": system_prompt}, *api_messages]

        # Resolve provider API key from AGENT_-prefixed settings so LiteLLM
        # doesn't have to find a bare ANTHROPIC_API_KEY / OPENAI_API_KEY env var.
        api_key: str | None = None
        if self.provider == "anthropic":
            api_key = _settings.anthropic_api_key or None
        elif self.provider == "openai":
            api_key = _settings.openai_api_key or None

        # Build litellm call kwargs
        litellm_kwargs: dict[str, Any] = {
            "model": self._litellm_model,
            "messages": api_messages,
            "max_tokens": effective_max_tokens,
        }
        if api_key:
            litellm_kwargs["api_key"] = api_key
        if tools:
            litellm_kwargs["tools"] = tools
        if tool_choice is not None:
            litellm_kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            litellm_kwargs["response_format"] = response_format
        if temperature is not None:
            litellm_kwargs["temperature"] = temperature
        if timeout_s is not None:
            litellm_kwargs["timeout"] = timeout_s
        if max_retries is not None:
            litellm_kwargs["num_retries"] = max_retries

        start_time = time.monotonic()
        log.info(
            "litellm_request_start",
            model=self._litellm_model,
            trace_id=trace_id,
            role=role.value,
            max_tokens=effective_max_tokens,
        )

        try:
            response = await litellm.acompletion(**litellm_kwargs)
        except Exception as e:
            log.error(
                "litellm_request_failed",
                model=self._litellm_model,
                trace_id=trace_id,
                error=str(e),
                exc_info=True,
            )
            raise LLMClientError(f"LiteLLM call failed: {e}") from e

        elapsed = time.monotonic() - start_time
        latency_ms = int(elapsed * 1000)

        # Extract response data (litellm returns OpenAI-format ModelResponse)
        choice = response.choices[0]
        message = choice.message
        content: str = message.content or ""

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCallType(
                        id=tc.id or str(uuid4()),
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        # Usage
        usage: dict[str, Any] = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Cost tracking — use litellm.completion_cost(), record to DB
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            cost = 0.0

        if cost > 0:
            try:
                await cost_tracker.record_api_call(
                    provider=self.provider,
                    model=self.model_id,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    cost_usd=cost,
                    latency_ms=latency_ms,
                )
            except Exception:
                pass  # Non-fatal — degraded cost tracking

        response_id: str | None = getattr(response, "id", None)

        log.info(
            "litellm_request_complete",
            model=self._litellm_model,
            trace_id=trace_id,
            role=role.value,
            elapsed_s=round(elapsed, 2),
            tokens=usage.get("total_tokens"),
            cost_usd=round(cost, 6) if cost else None,
            tool_calls=len(tool_calls),
        )

        await cost_tracker.disconnect()

        return LLMResponseType(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_trace=None,
            usage=usage,
            response_id=response_id,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )
