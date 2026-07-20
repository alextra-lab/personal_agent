"""LLM client factory — dispatches to LocalLLMClient or LiteLLMClient based on provider placement.

Two-path dispatch (ADR-0033):
  placement == local  →  LocalLLMClient (GPU-aware concurrency, thinking budget, tools)
  placement == cloud  →  LiteLLMClient (all cloud providers via litellm.acompletion())

Usage:
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.types import ModelRole

    client = get_llm_client(role_name="primary")
    response = await client.respond(role=ModelRole.PRIMARY, messages=[...])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from personal_agent.config import load_model_config
from personal_agent.llm_client.models import ModelConfig, ModelDefinition, Placement

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


def _build_client(
    model_key: str, model_def: ModelDefinition | None, budget_role: str, config: ModelConfig
) -> Any:
    """Construct the client for a resolved deployment key + explicit budget lane.

    The single dispatch door (ADR-0121 §6): every path — role resolution and the
    key-bypass helper — resolves to a key, then enters here with an explicit
    ``budget_role``. ``local`` placement → :class:`LocalLLMClient`; any cloud
    placement → :class:`LiteLLMClient`.

    Args:
        model_key: The resolved catalog deployment key (drives placement).
        model_def: The effective :class:`ModelDefinition` for the call.
        budget_role: The cost-gate budget lane to bill against.
        config: The loaded :class:`ModelConfig`.

    Returns:
        A client whose placement matches the deployment's provider.
    """
    if model_def is not None and config.placement_of(model_key) is not Placement.LOCAL:
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        return LiteLLMClient(
            model_id=model_def.id,
            provider=model_def.provider or "anthropic",
            max_tokens=model_def.max_tokens or 8192,
            budget_role=budget_role,
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()


def get_llm_client(role_name: str = "primary", *, selection_key: str | None = None) -> Any:
    """Return the appropriate LLM client for a role, honouring a session selection.

    Resolution order (ADR-0121 §4/§6):

    1. **Selection** — an advisory key from ``selection_key`` or, when that is
       ``None``, the per-turn selection context
       (:func:`~personal_agent.config.selection.get_current_selection`, set once
       per turn from the server-authoritative selection store). It is passed
       through the fail-closed guardrail
       (:func:`~personal_agent.config.model_loader.resolve_selected_deployment`):
       honoured only for an ``open`` role naming a valid, kind-compatible key;
       otherwise the role's binding default wins. A user's model choice therefore
       never reaches a pinned writer role, by any route.
    2. **Profile fallback** — when no selection is carried, the active
       ExecutionProfile's redirect still applies (cloud ``sub_agent`` →
       ``claude_haiku``), unchanged, until Path is removed in ADR-0121 T5. In a
       chat turn ``primary`` always carries a selection, so its resolution is
       store-authoritative and does not consult the profile.

    ``local`` placement → :class:`LocalLLMClient`; any cloud placement →
    :class:`LiteLLMClient`.

    ``role_name`` must be a literal factory role name (``"primary"``,
    ``"captains_log"``, etc.) — ``budget_role_for(role_name)`` derives the
    cost-gate budget lane from it and only recognizes those names. A caller
    holding an already-resolved model key from **trusted config** (e.g.
    :func:`~personal_agent.config.model_loader.resolve_role_model_key`, the
    ADR-0099 ``model_roles.yaml`` matrix) should use :func:`get_llm_client_for_key`
    instead, which takes the budget role explicitly and fails loudly on an
    unknown key (FRE-869). A **user- or model-proposed** key must instead be
    passed as ``selection_key`` here so the §6 guardrail applies.

    Args:
        role_name: The factory role name (default: ``"primary"``).
        selection_key: An advisory selected deployment key for this role, applied
            through the guardrail. ``None`` falls back to the per-turn selection
            context, then the profile redirect.

    Returns:
        An LLM client instance matching the resolved deployment's provider placement.
    """
    config = load_model_config()

    from personal_agent.config.model_loader import (
        resolve_role_target,
        resolve_selected_deployment,
    )
    from personal_agent.config.selection import get_current_selection

    selection = selection_key if selection_key is not None else get_current_selection(role_name)

    model_key: str | None
    if selection is not None:
        # Guardrailed: honoured only for an open role + valid kind-compatible key,
        # else the role's binding default (fail-closed, ADR-0121 §6 / AC-4c).
        model_key = resolve_selected_deployment(role_name, selection, config)
    else:
        # No selection carried — the ExecutionProfile redirect still governs the
        # open roles it always has (sub_agent/artifact_builder), until T5 removes
        # Path. Only pass a key when a profile actually redirects the role.
        from personal_agent.config.profile import resolve_profile_redirect

        model_key = resolve_profile_redirect(role_name)

    resolved_key, model_def = resolve_role_target(role_name, model_key=model_key, config=config)

    from personal_agent.cost_gate import budget_role_for

    return _build_client(resolved_key, model_def, budget_role_for(role_name), config)


def get_llm_client_for_key(model_key: str, budget_role: str = "skill_routing") -> Any:
    """Return an LLM client for a specific model key from **trusted config**.

    This is the trusted-config door, not a user-selection door. Every call site
    passes a key resolved from configuration — the ADR-0099 ``model_roles.yaml``
    matrix (:func:`~personal_agent.config.model_loader.resolve_role_model_key`)
    or a ``settings.*_model_key`` — never a request-, user-, or model-proposed
    key. It deliberately does **not** apply the ADR-0121 §6 selection guardrail
    (``open``/``kind`` intersection): its keys are already config-validated, so
    it fails loudly on an unknown key rather than falling back. A user- or
    model-proposed key must instead go through :func:`get_llm_client`'s
    ``selection_key`` so the guardrail applies — keeping the guardrail's one door
    (the risk row's "second door" closed by construction). The future sub-agent
    ADR, where a *model* proposes a sub-agent key, routes that key through the
    guarded path for the same reason.

    Used for components that need a *specific* model regardless of the active
    ExecutionProfile — e.g. the Phase C skill router, which must use a remote
    model even when the primary agent runs locally (the local SLM server is
    currently single-threaded; running routing on it would serialize calls).
    Passing such a config-resolved key to :func:`get_llm_client` instead would
    silently mis-bill spend, since ``budget_role_for`` cannot map a resolved
    model key back to its budget lane (FRE-869).

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

    return _build_client(model_key, model_def, budget_role, config)
