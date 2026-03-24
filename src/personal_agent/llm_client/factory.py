"""LLM client factory — dispatches to LocalLLMClient or ClaudeClient based on provider.

The factory reads model configuration from models.yaml and returns the
appropriate client for the given role's provider field (ADR-0031).

Usage:
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.types import ModelRole

    client = get_llm_client(role_name="standard")
    response = await client.respond(role=ModelRole.STANDARD, messages=[...])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from personal_agent.config import load_model_config

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole
    from personal_agent.telemetry.trace import TraceContext


class LLMClient(Protocol):
    """Structural protocol for LLM clients (LocalLLMClient and ClaudeClient).

    Both clients must implement respond() with this signature so the executor
    can use either interchangeably. Extra client-specific params (priority,
    priority_timeout) are absorbed by **kwargs.
    """

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
        **kwargs: Any,
    ) -> LLMResponse:
        """Make a single-turn LLM call and return a normalized LLMResponse."""
        ...


def get_llm_client(role_name: str = "standard") -> Any:
    """Return the appropriate LLM client for a given role's provider.

    Reads the provider field from models.yaml for the specified role.
    If provider is "anthropic", returns a ClaudeClient configured with
    the role's model definition. Otherwise returns a LocalLLMClient.

    Args:
        role_name: The model role name to look up (default: "standard").

    Returns:
        An LLM client instance matching the role's provider.

    Examples:
        >>> # With local models.yaml (provider: null)
        >>> client = get_llm_client("standard")
        >>> type(client).__name__
        'LocalLLMClient'

        >>> # With baseline models.yaml (provider: "anthropic")
        >>> client = get_llm_client("standard")
        >>> type(client).__name__
        'ClaudeClient'
    """
    config = load_model_config()
    model_def = config.models.get(role_name)

    if model_def and model_def.provider == "anthropic":
        from personal_agent.llm_client.claude import ClaudeClient

        return ClaudeClient(
            model_id=model_def.id,
            max_tokens=model_def.max_tokens or 8192,
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()
