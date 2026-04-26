"""LLM client factory — dispatches to LocalLLMClient or LiteLLMClient based on provider_type.

Two-path dispatch (ADR-0033):
  provider_type == "local"  →  LocalLLMClient (GPU-aware concurrency, thinking budget, tools)
  provider_type == "cloud"  →  LiteLLMClient (all cloud providers via litellm.acompletion())

Usage:
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.types import ModelRole

    client = get_llm_client(role_name="primary")
    response = await client.respond(role=ModelRole.PRIMARY, messages=[...])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from personal_agent.config import load_model_config

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole
    from personal_agent.telemetry.trace import TraceContext


class LLMClient(Protocol):
    """Structural protocol for LLM clients (LocalLLMClient and LiteLLMClient).

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


def get_llm_client(role_name: str = "primary") -> Any:
    """Return the appropriate LLM client for a given role's provider_type.

    When an ExecutionProfile is active (set via
    :func:`~personal_agent.config.profile.set_current_profile`), the profile's
    model key for the requested role overrides the ``models.yaml`` default.
    This is the primary mechanism for cloud-profile dispatch (ADR-0044 D1).

    Decision order:
    1. If a profile is active AND the profile defines a model for this role →
       look up that model key in ``models.yaml`` and dispatch accordingly.
    2. Otherwise → look up ``role_name`` directly in ``models.yaml`` (existing
       behaviour, unchanged for local/default execution).

    ``"local"`` ``provider_type`` → :class:`LocalLLMClient` (GPU-aware concurrency,
    thinking budget, tool filtering). Any other value → :class:`LiteLLMClient`
    (all cloud providers via ``litellm.acompletion()``).

    Args:
        role_name: The model role name to look up in ``models.yaml``
            (default: ``"primary"``).

    Returns:
        An LLM client instance matching the resolved role's ``provider_type``.

    Examples:
        >>> # Default: reads 'primary' from models.yaml
        >>> client = get_llm_client("primary")

        >>> # With cloud profile active: reads cloud profile's primary_model key
        >>> from personal_agent.config.profile import load_profile, set_current_profile
        >>> set_current_profile(load_profile("cloud"))
        >>> client = get_llm_client("primary")  # → LiteLLMClient(claude-sonnet-4-6)
    """
    from personal_agent.config.profile import resolve_model_key

    config = load_model_config()

    # Resolve the model key: profile overrides role_name when profile is active.
    resolved_key = resolve_model_key(role_name)

    model_def = config.models.get(resolved_key)

    if model_def and model_def.provider_type != "local":
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        return LiteLLMClient(
            model_id=model_def.id,
            provider=model_def.provider or "anthropic",
            max_tokens=model_def.max_tokens or 8192,
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()
