"""Claude API client for Anthropic's Claude models (Phase 2.2).

This module provides integration with Anthropic's Claude API for deep reasoning
tasks like entity extraction and second brain consolidation.
"""

from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


class ClaudeClient:
    """Client for Anthropic Claude API.

    Usage:
        client = ClaudeClient()
        response = await client.chat_completion(
            messages=[{"role": "user", "content": "Extract entities from this text..."}]
        )
    """

    def __init__(self, cost_tracker: CostTrackerService | None = None) -> None:  # noqa: D107
        """Initialize Claude client with API key from settings.

        Args:
            cost_tracker: Optional cost tracking service (will create one if not provided)
        """
        api_key = settings.anthropic_api_key
        if not api_key:
            raise ValueError(
                "Anthropic API key not configured. Set AGENT_ANTHROPIC_API_KEY environment variable."
            )

        self.client = AsyncAnthropic(api_key=api_key)
        self.model = settings.claude_model
        self.max_tokens = settings.claude_max_tokens
        self.weekly_budget_usd = settings.claude_weekly_budget_usd

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

            response = await self.client.messages.create(**create_params)

            # Extract response content
            content = ""
            if response.content:
                for block in response.content:
                    if hasattr(block, "text"):
                        content += block.text

            # Calculate cost (approximate, based on Claude pricing)
            # Claude Sonnet 4.5: $3/1M input tokens, $15/1M output tokens
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_usd = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)

            # Record cost to database
            await self.cost_tracker.record_api_call(
                provider="anthropic",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                trace_id=trace_id,
                purpose=purpose,
            )

            log.info(
                "claude_api_call_completed",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
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
        """Check if weekly budget has been exceeded.

        Raises:
            ValueError: If weekly budget exceeded
        """
        # Get weekly cost from database
        weekly_cost_usd = await self.cost_tracker.get_weekly_cost(provider="anthropic")

        if weekly_cost_usd >= self.weekly_budget_usd:
            raise ValueError(
                f"Weekly Claude API budget exceeded: ${weekly_cost_usd:.2f} / ${self.weekly_budget_usd:.2f}"
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
