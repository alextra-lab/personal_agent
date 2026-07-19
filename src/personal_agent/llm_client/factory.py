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
        *,
        trace_ctx: TraceContext,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        reasoning_effort: str | None = None,
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

    ``role_name`` must be a literal factory role name (``"primary"``,
    ``"captains_log"``, etc.) — ``budget_role_for(role_name)`` derives the cost-gate
    budget lane from it and only recognizes those names. A caller holding an
    already-resolved model key (e.g. from
    :func:`~personal_agent.config.model_loader.resolve_role_model_key`, the
    ADR-0099 ``model_roles.yaml`` matrix) should use
    :func:`get_llm_client_for_key` instead, which takes the budget role
    explicitly and fails loudly on an unknown key rather than silently
    defaulting the budget lane to ``"main_inference"`` (FRE-869).

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
    config = load_model_config()

    # Resolve the model key: profile overrides role_name when profile is active.
    from personal_agent.config.model_loader import resolve_role_target

    # Effective definition — deployment plus this role's binding overrides.
    # max_tokens below is per-use and may live on the binding.
    # Only pass a key when a profile actually redirects the role; otherwise let
    # the binding decide. Passing the bare role name would miss once deployments
    # are keyed by model alias rather than by role.
    from personal_agent.config.profile import resolve_profile_redirect

    profile_key = resolve_profile_redirect(role_name)
    resolved_key, model_def = resolve_role_target(role_name, model_key=profile_key, config=config)

    if model_def and model_def.provider_type != "local":
        from personal_agent.cost_gate import budget_role_for
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        return LiteLLMClient(
            model_id=model_def.id,
            provider=model_def.provider or "anthropic",
            max_tokens=model_def.max_tokens or 8192,
            budget_role=budget_role_for(role_name),
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()


def get_llm_client_for_key(model_key: str, budget_role: str = "skill_routing") -> Any:
    """Return an LLM client for a specific model key, bypassing profile resolution.

    Used for components that need a *specific* model regardless of the active
    ExecutionProfile — e.g. the Phase C skill router, which must use a remote
    model even when the primary agent runs locally (the local SLM server is
    currently single-threaded; running routing on it would serialize calls).
    Also the correct call for a caller holding a model key already resolved via
    :func:`~personal_agent.config.model_loader.resolve_role_model_key` (the
    ADR-0099 ``model_roles.yaml`` matrix, which already accounts for the active
    profile) — passing that key to :func:`get_llm_client` instead would silently
    mis-bill spend, since ``budget_role_for`` cannot map a resolved model key
    back to its budget lane (FRE-869).

    Args:
        model_key: Key in ``models.yaml`` (e.g. ``"claude_haiku"``,
            ``"qwen3.5-35b-a3b"``).
        budget_role: Cost-gate budget role for this client (default
            ``"skill_routing"``). Distinct budget category isolates routing
            spend from primary inference.

    Returns:
        LiteLLMClient for cloud provider models; LocalLLMClient for local.

    Raises:
        ValueError: If ``model_key`` is not registered in ``models.yaml``.
    """
    config = load_model_config()
    model_def = config.models.get(model_key)
    if model_def is None:
        raise ValueError(
            f"Unknown model key '{model_key}'. Available: {sorted(config.models.keys())}"
        )

    if model_def.provider_type != "local":
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        return LiteLLMClient(
            model_id=model_def.id,
            provider=model_def.provider or "anthropic",
            max_tokens=model_def.max_tokens or 8192,
            budget_role=budget_role,
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()
