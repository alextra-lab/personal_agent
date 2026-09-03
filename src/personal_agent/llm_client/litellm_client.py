"""LiteLLM-backed client for all cloud LLM providers.

Uses litellm.acompletion() to transparently handle message/tool format
conversion across Anthropic, OpenAI, Google, Mistral, and other providers.

Replaces ClaudeClient for all cloud providers (ADR-0033). Two clients, clear
boundary: LocalLLMClient for local inference, LiteLLMClient for cloud.

Our wrapper adds: cost tracking via CostTrackerService, budget enforcement,
and telemetry (structlog). LiteLLM handles provider format conversion and retries.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import litellm
import structlog
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from openai import AsyncOpenAI
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai

from personal_agent.llm_client.prompt_identity import (
    PromptIdentity,
    derive_fallback_prompt_identity,
)
from personal_agent.llm_client.telemetry import (
    emit_model_call_completed,
    emit_model_call_started,
)
from personal_agent.security import (
    check_egress_or_raise,
    create_guarded_http_client,
    get_domain_guard,
    guard_event_hooks,
)
from personal_agent.telemetry.spans import model_call_span

if TYPE_CHECKING:
    from personal_agent.llm_client.models import ProviderDefinition
    from personal_agent.llm_client.types import LLMResponse, ModelRole, ToolCall
    from personal_agent.security import DomainGuard
    from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

# Suppress litellm verbose startup logging
litellm.suppress_debug_info = True

# ── Egress guard route classification (ADR-0141 D2.1/D2.2) ────────────────
# Verified against litellm 1.98.0 / openai SDK: which providers dispatch
# through which transport mechanism decides which client= shape the guard
# must be injected as. A provider in neither set fails closed in
# _build_guarded_client rather than dispatching unguarded.
_OPENAI_SDK_ROUTE_PROVIDERS = frozenset({"openai"})
# ovhcloud's OVHCloudChatConfig rides litellm's generic base_llm_http_handler,
# which is AsyncHTTPHandler-based like Anthropic's own dedicated handler — not
# a third mechanism.
_ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS = frozenset({"anthropic", "ovhcloud"})
# Layer-1 fallback for a provider with no catalog-declared base_url override
# (anthropic, openai) — host-only; DomainGuard.check_url only extracts the
# hostname, so this need not match litellm's own undocumented default URL
# byte-for-byte, and is never fed into litellm_kwargs["api_base"].
_KNOWN_DEFAULT_HOSTS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}

# Layer-2 client caches. Cache only what litellm never mutates in place.
# The OpenAI-SDK route caches the guarded httpx.AsyncClient (the connection
# pool) and builds a fresh AsyncOpenAI wrapper per call, because litellm
# mutates .max_retries/.organization on a passed-in AsyncOpenAI object on
# every call (openai.py::_set_dynamic_params_on_client) — sharing that
# object across concurrent calls races. AsyncHTTPHandler has no equivalent
# per-call mutation (verified against litellm 1.98.0), so it caches whole,
# and is provider-independent (carries no auth/base_url state) so anthropic
# and ovhcloud share one entry per guard. Keyed including id(guard) — not
# just provider/endpoint — so a test constructing a fresh DomainGuard per
# test never receives a stale client cached against a different guard; in
# production the guard is the process singleton, so the cache is fully
# effective there.
_guarded_httpx_clients: dict[tuple[str | None, int], httpx.AsyncClient] = {}
_guarded_async_http_handlers: dict[int, AsyncHTTPHandler] = {}


def _build_guarded_client(
    provider: str,
    provider_def: ProviderDefinition,
    api_key: str | None,
    guard: DomainGuard,
) -> AsyncOpenAI | AsyncHTTPHandler:
    """Build the layer-2 guarded client object for litellm's ``client=`` kwarg.

    Args:
        provider: The catalog provider name (litellm dispatch prefix).
        provider_def: The provider's catalog entry (for ``base_url``).
        api_key: Resolved credential, or None.
        guard: The DomainGuard to inject.

    Returns:
        A guarded ``AsyncOpenAI`` (OpenAI-SDK route) or ``AsyncHTTPHandler``
        (AsyncHTTPHandler route) instance.

    Raises:
        LLMClientError: If ``provider`` is not classified into either route
            mechanism — fails closed rather than dispatching unguarded
            (ADR-0141 D2: "that is a security regression and is not
            accepted").
    """
    from personal_agent.llm_client.types import LLMClientError

    if provider in _OPENAI_SDK_ROUTE_PROVIDERS:
        cache_key = (provider_def.base_url, id(guard))
        pooled = _guarded_httpx_clients.get(cache_key)
        if pooled is None:
            pooled = create_guarded_http_client(guard=guard)
            _guarded_httpx_clients[cache_key] = pooled
        return AsyncOpenAI(
            api_key=api_key or "unused",
            base_url=provider_def.base_url,
            http_client=pooled,
        )

    if provider in _ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS:
        handler = _guarded_async_http_handlers.get(id(guard))
        if handler is None:
            handler = AsyncHTTPHandler(event_hooks=guard_event_hooks(guard=guard))
            _guarded_async_http_handlers[id(guard)] = handler
        return handler

    raise LLMClientError(
        f"provider {provider!r} is not classified into an egress-guard route "
        "mechanism (ADR-0141 D2) — refusing to dispatch unguarded. Add it to "
        "_OPENAI_SDK_ROUTE_PROVIDERS or _ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS "
        "in litellm_client.py once its litellm route mechanism is verified."
    )


def _mark_message_cache_control(message: dict[str, Any]) -> bool:
    """Attach an ephemeral cache_control breakpoint to a message's content.

    Anthropic caches the prefix up to and including the last marked block. The
    block must live inside a content list, so a string content is promoted to a
    single text block first. Idempotent: a message already carrying a marker is
    left unchanged.

    Args:
        message: A chat message dict (modified in-place).

    Returns:
        True if a marker was applied (or already present and markable), False
        when the content shape cannot carry a marker (e.g. empty content).
    """
    content = message.get("content")
    if isinstance(content, str) and content:
        message["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return True
    if isinstance(content, list) and content:
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block.setdefault("cache_control", {"type": "ephemeral"})
            return True
    return False


def _strip_cache_control(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> None:
    """Remove every ``cache_control`` marker from messages and tools in-place.

    The executor passes a shallow copy of its working message list each round
    (``api_messages = list(messages)``), so the underlying message dicts are
    shared and mutated in place. Without clearing first, re-marking across the
    in-turn tool loop accumulates breakpoints and eventually exceeds Anthropic's
    4-block cap (FRE-468). Stripping makes re-application idempotent.

    Args:
        messages: api_messages list (modified in-place).
        tools: OpenAI-format tool definitions list, or None (modified in-place).
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    for tool in tools or []:
        if isinstance(tool, dict):
            tool.pop("cache_control", None)


def _enforce_cache_control_cap(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, cap: int = 4
) -> None:
    """Defensively clamp the number of ``cache_control`` breakpoints to ``cap``.

    Anthropic rejects any request with more than 4 ``cache_control`` blocks. By
    construction this function's callers place at most 3 (system + history-end +
    last tool), but this guard ensures a future regression degrades to a warning
    and a dropped breakpoint rather than a hard API 400 for the whole turn. The
    static anchors (system message, tool definitions) are preserved; the earliest
    history-end markers are dropped first, keeping the newest intended history-end
    breakpoint (the frozen prefix nearest the current user turn).

    Args:
        messages: api_messages list (modified in-place).
        tools: OpenAI-format tool definitions list, or None.
        cap: Maximum allowed cache_control blocks (Anthropic's hard limit is 4).
    """
    static = 0
    if messages:
        sys_content = messages[0].get("content")
        if isinstance(sys_content, list):
            static += sum(1 for b in sys_content if isinstance(b, dict) and "cache_control" in b)
    for tool in tools or []:
        if isinstance(tool, dict) and "cache_control" in tool:
            static += 1

    history_blocks: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if idx == 0:  # system anchor — never dropped
            continue
        content = msg.get("content")
        if isinstance(content, list):
            history_blocks.extend(
                b for b in content if isinstance(b, dict) and "cache_control" in b
            )

    total = static + len(history_blocks)
    if total <= cap:
        return

    allowed_history = max(0, cap - static)
    drop_count = len(history_blocks) - allowed_history
    for block in history_blocks[:drop_count]:
        block.pop("cache_control", None)
    log.warning(
        "cache_control_cap_enforced",
        total=total,
        cap=cap,
        dropped=drop_count,
    )


def _decorated_anthropic_copy(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    frozen_layout: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Return cache-decorated **deep copies** of ``messages``/``tools``; inputs untouched.

    Anthropic prompt-cache markers (``cache_control``) are request-local metadata.
    Decorating in place would scribble onto caller-owned dicts that the executor
    persists into session history (`session.messages`), leaking provider-specific
    metadata (and a `str`→`list` system-content promotion) into the saved
    conversation — which could then ride into a later, possibly non-Anthropic,
    request (FRE-473). This builder isolates the decoration to a request-local copy
    so the persisted history stays provider-neutral.

    Args:
        messages: Caller-owned message list (not modified).
        tools: Caller-owned OpenAI-format tool list, or None (not modified).
        frozen_layout: When True, also place the ADR-0081 §D2 history-end breakpoint.

    Returns:
        ``(wire_messages, wire_tools)`` — decorated deep copies safe to send to
        LiteLLM. ``wire_tools`` is None iff ``tools`` is None.
    """
    wire_messages = copy.deepcopy(messages)
    wire_tools = copy.deepcopy(tools) if tools is not None else None
    _apply_anthropic_cache_control(wire_messages, wire_tools, frozen_layout=frozen_layout)
    return wire_messages, wire_tools


def _apply_anthropic_cache_control(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    frozen_layout: bool = False,
) -> None:
    """Attach Anthropic cache_control markers in-place for prompt caching.

    Marks the system message and the last tool definition as cache breakpoints.
    Anthropic caches the prefix up to the last marked block, so marking both
    the static system prompt and the static tool list eliminates re-processing
    of ~4,200 tokens on every turn after the first.

    Under the ADR-0081 §D2 frozen append-only layout (``frozen_layout=True``,
    FRE-434), a third breakpoint is added on the **last frozen message before the
    current user turn** (whose content carries this turn's volatile tail). Without
    it the cloud backend re-reads the whole conversation history past the tool
    block every turn and the frozen history is never banked as a cached prefix —
    so this is what makes the cloud reuse asymmetry (cheap reset) actually hold.

    LiteLLM forwards cache_control blocks through to the Anthropic API
    transparently when present in message content.

    Args:
        messages: api_messages list (modified in-place). Must be pre-sanitised.
        tools: OpenAI-format tool definitions list, or None.
        frozen_layout: When True, also mark the history-end breakpoint
            (ADR-0081 §D2 point 4).
    """
    # FRE-468: clear markers left by a prior pass over these (shared) dicts so
    # re-marking across the in-turn tool loop is idempotent and never accumulates
    # past Anthropic's 4-block cap.
    _strip_cache_control(messages, tools)

    # Mark system message
    if messages and messages[0].get("role") == "system":
        sys_content = messages[0].get("content", "")
        if isinstance(sys_content, str):
            messages[0]["content"] = [
                {"type": "text", "text": sys_content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(sys_content, list) and sys_content:
            last_block = sys_content[-1]
            if isinstance(last_block, dict) and "cache_control" not in last_block:
                last_block["cache_control"] = {"type": "ephemeral"}

    # ADR-0081 §D2 point 4: history-end breakpoint (frozen layout only). The
    # current user turn is the last user message (it carries the volatile tail);
    # everything before it is frozen history. Mark the nearest markable message
    # immediately preceding that turn so the whole history is a cached prefix.
    if frozen_layout and messages:
        last_user_idx: int | None = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is not None:
            for j in range(last_user_idx - 1, -1, -1):
                if messages[j].get("role") == "system":
                    break
                if _mark_message_cache_control(messages[j]):
                    break

    # Mark last tool definition (caches the whole tool list prefix)
    if tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" not in last_tool:
            last_tool["cache_control"] = {"type": "ephemeral"}

    # FRE-468: defensive backstop — never let the request exceed Anthropic's cap.
    _enforce_cache_control_cap(messages, tools, cap=4)


class LiteLLMClient:
    """Cloud LLM client backed by LiteLLM.

    Handles all cloud providers (Anthropic, OpenAI, Google, Mistral, etc.)
    through a single interface. LiteLLM manages message format conversion,
    tool calling translation, and provider-specific API differences.

    Our wrapper adds:
    - Cost tracking via CostTrackerService (record_api_call to PostgreSQL)
    - Weekly budget enforcement (AGENT_CLOUD_WEEKLY_BUDGET_USD)
    - Telemetry emission via structlog

    The factory selects this client when the deployment's provider placement is not
    local (ADR-0033; placement replaced provider_type in ADR-0121).
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
        *,
        budget_role: str,
        reasoning_effort: str | None = None,
        egress_guard: DomainGuard | None = None,
    ) -> None:
        """Initialize LiteLLMClient with model and provider configuration.

        Args:
            model_id: Provider model identifier (e.g., ``claude-sonnet-4-6``).
            provider: Provider name for LiteLLM dispatch.
            max_tokens: Default maximum output tokens.
            reasoning_effort: The deployment's **declared** reasoning depth
                (FRE-1007), applied to every call this client makes unless a
                call site overrides it. Carried on the client rather than left
                to call sites because the producers that most needed it passed
                nothing: the digest and insights both dispatch through
                ``get_llm_client_for_key`` and never named an effort, so the
                declaration sat in ``config/models.yaml`` and never reached the
                request. ``None`` means the deployment declared none — which
                ``config_guard.check_reasoning_declaration`` refuses for any
                role-bound deployment.
            budget_role: Cost-gate role this client reserves against
                (``main_inference``, ``entity_extraction``, etc.). **Required
                and keyword-only** — it has no default by design (FRE-989
                finding three). Three entry points each carried their own
                default and they disagreed: this constructor said
                ``main_inference`` ($10 daily), ``get_llm_client_for_key`` said
                ``skill_routing`` ($0.10 daily), and ``budget_role_for`` fell
                back to ``main_inference``. A call that omitted the lane
                therefore landed in a different budget depending on which door
                it came through. Reconciling the three to *zero* defaults makes
                the omission a ``TypeError`` at construction instead of a
                plausible wrong answer at billing time.
            egress_guard: DomainGuard to inject for the egress guard
                (ADR-0141 D2). Test seam — ``None`` (the production default)
                resolves the process singleton (``get_domain_guard()``) at
                call time.
        """
        self.model_id = model_id
        self.provider = provider
        self.max_tokens = max_tokens
        self.budget_role = budget_role
        self.reasoning_effort = reasoning_effort
        self._egress_guard = egress_guard
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
        priority: Any = None,
        priority_timeout: float | None = None,
        prompt_identity: PromptIdentity | None = None,
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
            prompt_identity: Identity of the prompt sent on this call (ADR-0078
                D1/D4). When None, a fallback is derived so the emitted
                ``model_call_completed`` always carries prompt identity fields.
            **kwargs: Additional provider-specific parameters passed to litellm.

        Returns:
            Normalized LLMResponse.

        Raises:
            ValueError: If weekly cloud budget is exceeded.
            TypeError: If ``role`` is not a ``ModelRole`` member (FRE-1037 —
                fail closed rather than surfacing an incidental ``AttributeError``
                on ``role.value`` deeper in the call).
            LLMClientError: On API failure after retries.
        """
        from personal_agent.llm_client.types import ModelRole as _ModelRole

        if not isinstance(role, _ModelRole):
            raise TypeError(f"role must be a ModelRole, got {type(role).__name__}: {role!r}")

        from personal_agent.llm_client.types import LLMClientError
        from personal_agent.llm_client.types import LLMResponse as LLMResponseType
        from personal_agent.llm_client.types import ToolCall as ToolCallType

        effective_max_tokens = max_tokens or self.max_tokens
        trace_id = str(trace_ctx.trace_id)

        # ── Settings + cost tracking ──────────────────────────────────────
        # FRE-988: the shared singleton holds one pooled connection for the
        # process's lifetime; connect() is idempotent, so this is a cheap
        # no-op once the pool exists rather than a fresh asyncpg pool per call.
        from personal_agent.config.settings import get_settings
        from personal_agent.llm_client.cost_tracker import get_cost_tracker_service

        _settings = get_settings()
        cost_tracker = get_cost_tracker_service()
        await cost_tracker.connect()

        # Prepend system prompt as a system message if provided
        api_messages = list(messages)
        if system_prompt:
            api_messages = [{"role": "system", "content": system_prompt}, *api_messages]

        # Sanitise tool_call / tool_result consistency before dispatch (FRE-237).
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        api_messages, _ = sanitise_messages(api_messages, trace_id=trace_id)

        # Resolve credential + base URL from the provider's catalog entry
        # (ADR-0121 Layer 1) instead of a provider-name branch (FRE-1155): the
        # old if/elif covered only anthropic and openai, so every other
        # provider dispatched with no credential and no declared base_url.
        # OVH's anonymous-access allowance (2 req/min vs 400 authenticated)
        # let that through invisibly until a rate limit surfaced it live.
        from personal_agent.config import load_model_config  # noqa: PLC0415

        provider_def = load_model_config().providers.get(self.provider)

        # Fails closed rather than dispatching unauthenticated (FRE-1155
        # decision) — both when the provider isn't in the catalog at all (no
        # way to know its auth policy; reachable via
        # second_brain.entity_extraction.ExtractionModelOverride, which
        # doesn't validate its provider string against the catalog) and when
        # a declared auth_env resolves empty. Mirrors how
        # is_provider_available() already treats a missing credential as
        # unselectable, and embeddings.py's identical guard for the same
        # catalog field (memory/embeddings.py:_get_embedding_config).
        if provider_def is None:
            raise LLMClientError(
                f"provider {self.provider!r} is not declared in the model "
                "catalog (config/models.yaml); refusing to dispatch without "
                "a known auth policy for it."
            )

        # ── Egress guard, layer 1 (ADR-0141 D2.1) ─────────────────────────
        # Route-independent pre-dispatch check, owns the exception type: some
        # litellm provider routes wrap a hook exception without `from e`
        # (verified for the Anthropic handler against litellm 1.98.0), making
        # the cause chain unreliable, so the type guarantee never depends on
        # unwrapping. Checked against a declared base_url when the catalog
        # overrides one, else a known default host — never fed into
        # litellm_kwargs["api_base"] (would risk a byte-mismatch against
        # litellm's own undocumented per-provider default URL and silently
        # break real dispatch). Layer 2 below still blocks the connection
        # regardless of which host litellm actually resolves to.

        resolved_guard = self._egress_guard or get_domain_guard()
        _egress_check_url = provider_def.base_url or _KNOWN_DEFAULT_HOSTS.get(self.provider)
        if _egress_check_url is not None:
            check_egress_or_raise(_egress_check_url, guard=resolved_guard)

        api_key: str | None = None
        if provider_def.auth_env:
            resolved_key = getattr(_settings, provider_def.auth_env, None)
            if not resolved_key:
                raise LLMClientError(
                    f"provider {self.provider!r} declares auth_env "
                    f"{provider_def.auth_env!r}, which is unset. Refusing to "
                    "dispatch unauthenticated."
                )
            api_key = str(resolved_key)

        # Build litellm call kwargs
        litellm_kwargs: dict[str, Any] = {
            "model": self._litellm_model,
            "messages": api_messages,
            "max_tokens": effective_max_tokens,
        }
        if api_key:
            litellm_kwargs["api_key"] = api_key

        # ── Egress guard, layer 2 (ADR-0141 D2.1/D2.2) ────────────────────
        # Per-route injected hook, owns no-connection depth: covers URLs the
        # SDK/handler constructs internally and redirect hops. Two mechanisms
        # in scope today (verified against litellm 1.98.0): the OpenAI-SDK
        # route (litellm's client= kwarg must be an AsyncOpenAI instance —
        # anything else is used directly if passed, so building our own with
        # a guarded http_client= is sufficient) and the AsyncHTTPHandler
        # route (anthropic's dedicated handler, and ovhcloud's generic
        # base_llm_http_handler — both isinstance-check client= against
        # AsyncHTTPHandler and silently drop anything else). A provider in
        # neither set fails closed rather than dispatching unguarded.
        litellm_kwargs["client"] = _build_guarded_client(
            self.provider, provider_def, api_key, resolved_guard
        )
        if provider_def.base_url:
            litellm_kwargs["api_base"] = provider_def.base_url
        if tools:
            litellm_kwargs["tools"] = tools
        if tool_choice is not None:
            litellm_kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            litellm_kwargs["response_format"] = response_format
        if temperature is not None:
            litellm_kwargs["temperature"] = temperature
        # FRE-766 wired the per-call hint; FRE-1007 added the deployment's declared
        # value beneath it, because the producers that most needed one passed nothing.
        # Per-call still wins — an explicit argument is a deliberate override of the
        # declaration, not a competitor to it. drop_params is left off so a model that
        # rejects the value surfaces an error rather than masking it; the config guard
        # is what keeps a rejected combination from ever being declared.
        effective_reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        )
        if effective_reasoning_effort is not None:
            from personal_agent.llm_client.reasoning import provider_reasoning_support

            if provider_reasoning_support(self.model_id, self.provider) is None:
                # litellm holds no capability record for this model, so it would
                # reject EVERY reasoning parameter — including `thinking`. That is
                # an infrastructure condition, not a configuration error: the map
                # is fetched from GitHub at import, and a host whose egress reaches
                # the provider but not GitHub lands here with a config file that
                # was verified in CI and never changed. Dropping every background
                # producer's call over it is disproportionate, so the declared
                # depth is omitted and the omission is made loud instead of silent
                # — which is the property FRE-1007 actually cares about.
                log.error(
                    "reasoning_declaration_undeliverable",
                    model=self._litellm_model,
                    declared_reasoning_effort=effective_reasoning_effort,
                    reason="litellm_has_no_capability_record_for_model",
                    remedy="ensure the litellm model cost map is reachable at import",
                    role=role.value,
                    trace_id=trace_id,
                    component="litellm_client",
                )
            else:
                litellm_kwargs["reasoning_effort"] = effective_reasoning_effort
        if timeout_s is not None:
            litellm_kwargs["timeout"] = timeout_s
        if max_retries is not None:
            litellm_kwargs["num_retries"] = max_retries

        # Anthropic prompt caching — eliminates re-processing of static system prompt
        # and tool list on every turn after the first (cache write: ~$0.30/MTok,
        # cache hit: ~$0.03/MTok vs $3.00/MTok uncached).
        if self.provider == "anthropic":
            litellm_kwargs.setdefault("extra_headers", {})["anthropic-beta"] = (
                "prompt-caching-2024-07-31"
            )
            # FRE-473: decorate request-local copies so cache_control markers never
            # mutate caller-owned messages/tools (which the executor persists into
            # session history). The wire payload carries the markers; the saved
            # conversation stays provider-neutral.
            wire_messages, wire_tools = _decorated_anthropic_copy(
                api_messages,
                litellm_kwargs.get("tools"),
                frozen_layout=True,  # sole layout since FRE-941 retired the A/B flag (ADR-0081 §D2)
            )
            litellm_kwargs["messages"] = wire_messages
            if wire_tools is not None:
                litellm_kwargs["tools"] = wire_tools

        # ── Cost Check Gate (ADR-0065 D1) ─────────────────────────────────
        # Atomic reservation in front of every paid call. Replaces the
        # advisory weekly check that produced the 2026-04-30 cap-overshoot
        # incident: the gate's SELECT … FOR UPDATE serialises concurrent
        # reservers, raises BudgetDenied with a structured payload when any
        # cap would be exceeded, and is reconciled to the actual cost via
        # commit/refund below.
        from personal_agent.cost_gate import (  # noqa: PLC0415 — lazy to avoid cycle
            BudgetDenied,
            get_default_gate,
            load_budget_config,
        )
        from personal_agent.llm_client.cost_estimator import (  # noqa: PLC0415
            estimate_reservation_for_call,
        )

        gate = get_default_gate()
        budget_config = load_budget_config()
        reservation_amount = estimate_reservation_for_call(
            role=self.budget_role,
            model=self._litellm_model,
            messages=api_messages,
            max_tokens=effective_max_tokens,
            config=budget_config,
            trace_id=trace_ctx.trace_id,
        )
        try:
            reservation_id = await gate.reserve(
                role=self.budget_role,
                amount=reservation_amount,
                trace_id=UUID(trace_ctx.trace_id),
                session_id=UUID(trace_ctx.session_id) if trace_ctx.session_id else None,
                # Turn-level call — no sub-agent task_id reaches this layer
                # (mirrors the route_traces convention: task_id NULL = turn-level).
                task_id=None,
            )
        except BudgetDenied:
            log.warning(
                "litellm_request_budget_denied",
                model=self._litellm_model,
                trace_id=trace_id,
                role=role.value,
                budget_role=self.budget_role,
                reservation_amount_usd=float(reservation_amount),
            )
            raise

        start_time = time.monotonic()

        # ADR-0129 D3 / FRE-1067: the model-call span wraps ONLY the provider
        # call + response parsing (through usage extraction) — deliberately
        # narrower than the full function, matching the pre-existing
        # start_time/elapsed stopwatch boundary exactly. Budget-gate
        # settlement and the durable cost_tracker write below are real I/O
        # unrelated to model-call duration; including them would make the
        # span's duration diverge from api_costs.latency_ms beyond ADR-0129
        # AC-4's 10% reconciliation tolerance. span_id is captured before the
        # `with` exits so emit_model_call_completed can use it further down.
        with model_call_span(
            role=role.value, model=self._litellm_model, provider=self.provider
        ) as _model_span:
            span_id = format(_model_span.get_span_context().span_id, "016x")
            # ADR-0074 §I2: canonical model_call_started emission (parity with
            # LocalLLMClient).
            emit_model_call_started(
                log=log,
                role=role.value,
                model=self._litellm_model,
                endpoint=self.provider,
                provider=self.provider,
                trace_ctx=trace_ctx,
                span_id=span_id,
                extra={
                    "budget_role": self.budget_role,
                    "reservation_amount_usd": float(reservation_amount),
                    "max_tokens": effective_max_tokens,
                },
            )
            try:
                response = await litellm.acompletion(**litellm_kwargs)
            except asyncio.CancelledError:
                # FRE-973: a turn-level wall-clock deadline (asyncio.wait_for in the
                # executor's step_llm_call) cancels this call directly. CancelledError
                # is a BaseException, not an Exception, so it would otherwise skip the
                # except Exception below entirely and leak this reservation until the
                # cost-gate reaper sweeps it at TTL. Resolve it the same as any other
                # failed exit, then propagate the cancellation — never swallow it.
                try:
                    await gate.refund(reservation_id, trace_id=trace_id)
                except Exception as refund_exc:  # noqa: BLE001
                    log.error(
                        "litellm_refund_after_cancel_failed",
                        trace_id=trace_id,
                        session_id=trace_ctx.session_id,
                        reservation_id=str(reservation_id),
                        error=str(refund_exc),
                    )
                raise
            except Exception as e:
                # Refund the reservation so the counter doesn't leak headroom.
                try:
                    await gate.refund(reservation_id, trace_id=trace_id)
                except Exception as refund_exc:  # noqa: BLE001
                    log.error(
                        "litellm_refund_after_failure_failed",
                        trace_id=trace_id,
                        session_id=trace_ctx.session_id,
                        reservation_id=str(reservation_id),
                        error=str(refund_exc),
                    )
                log.error(
                    "litellm_request_failed",
                    model=self._litellm_model,
                    trace_id=trace_id,
                    session_id=trace_ctx.session_id,
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

            # Usage — extract base tokens plus provider-specific cache fields
            usage: dict[str, Any] = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

                # Anthropic: explicit cache_control headers → cache_creation / cache_read fields
                cache_read = getattr(response.usage, "cache_read_input_tokens", None)
                cache_write = getattr(response.usage, "cache_creation_input_tokens", None)
                if cache_read is not None:
                    usage["cache_read_input_tokens"] = cache_read
                if cache_write is not None:
                    usage["cache_creation_input_tokens"] = cache_write

                # OpenAI: automatic server-side caching → prompt_tokens_details.cached_tokens
                # (gpt-4o, gpt-4o-mini, o1, and newer models; no client headers needed)
                prompt_details = getattr(response.usage, "prompt_tokens_details", None)
                if prompt_details is not None:
                    openai_cached = getattr(prompt_details, "cached_tokens", None)
                    if openai_cached is not None and openai_cached > 0:
                        # Use the same field so the log line is uniform across providers
                        usage["cache_read_input_tokens"] = (
                            usage.get("cache_read_input_tokens", 0) + openai_cached
                        )

                # FRE-766: reasoning-token count from the reasoning models (GPT-5 family
                # via litellm). Defensive across shapes — completion_tokens_details may be a
                # provider object (attribute) or a dict; left absent (never 0/None-forced)
                # when the provider omits it (e.g. Claude adaptive thinking) so the eval
                # never treats "missing" as "zero reasoning".
                completion_details = getattr(response.usage, "completion_tokens_details", None)
                if completion_details is not None:
                    reasoning_tokens = (
                        completion_details.get("reasoning_tokens")
                        if isinstance(completion_details, dict)
                        else getattr(completion_details, "reasoning_tokens", None)
                    )
                    if reasoning_tokens is not None:
                        usage["reasoning_tokens"] = reasoning_tokens

            _model_span.set_attribute(
                gen_ai.GEN_AI_USAGE_INPUT_TOKENS, usage.get("prompt_tokens", 0)
            )
            _model_span.set_attribute(
                gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, usage.get("completion_tokens", 0)
            )

        # Cost tracking — reconcile via litellm.completion_cost(), guarded so an
        # unmapped dated response id can't silently commit $0 (ADR-0101 §8b AC-11).
        from personal_agent.llm_client.cost_estimator import actual_cost_for_response

        cost = float(
            actual_cost_for_response(
                response=response, model=self._litellm_model, trace_id=trace_id
            )
        )

        # Settle the reservation against the actual cost. Always commit (even
        # at $0) so the reservation row transitions out of `active` and the
        # reaper doesn't refund it.
        from decimal import Decimal as _Decimal  # noqa: PLC0415 — local alias

        try:
            await gate.commit(
                reservation_id,
                _Decimal(str(cost)),
                trace_id=trace_id,
                session_id=trace_ctx.session_id,
            )
        except Exception as commit_exc:  # noqa: BLE001
            # If the commit fails (DB hiccup), we'd rather log loudly than
            # silently lose the actual-cost adjustment. The reaper will sweep
            # the reservation when its TTL expires.
            log.error(
                "litellm_commit_failed",
                trace_id=trace_id,
                session_id=trace_ctx.session_id,
                reservation_id=str(reservation_id),
                cost=cost,
                error=str(commit_exc),
            )

        if cost > 0:
            # ADR-0074 / FRE-376 Phase 4: trace_ctx is non-optional and
            # carries a guaranteed UUID trace_id. session_id is still
            # nullable for system-tagged paths (probes, scheduler ticks
            # via SystemTraceContext) — in that case we log loudly and
            # skip cost recording rather than raise, so the chat turn
            # keeps working with degraded attribution.
            session_id_str = trace_ctx.session_id
            if session_id_str is None:
                log.error(
                    "cost_record_missing_identity",
                    model=self._litellm_model,
                    trace_id=trace_id,
                    trace_kind=trace_ctx.kind,
                    reason="trace_ctx_missing_session_id",
                )
            else:
                try:
                    await cost_tracker.record_api_call(
                        provider=self.provider,
                        # Match the canonical model_call_completed string
                        # (ADR-0121 T4 / AC-8) rather than the bare model id —
                        # a cost row and its telemetry counterpart must name
                        # the same model for the same call.
                        model=self._litellm_model,
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        cost_usd=cost,
                        trace_id=UUID(trace_id),
                        session_id=UUID(session_id_str),
                        purpose=self.budget_role,
                        latency_ms=latency_ms,
                        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                    )
                except Exception as record_exc:  # noqa: BLE001
                    # Non-fatal — but the failure should not be silent any
                    # more. ADR-0074 makes identity load-bearing; anything
                    # that prevents the row from landing is operationally
                    # interesting and worth a log line.
                    log.error(
                        "cost_record_failed",
                        model=self._litellm_model,
                        trace_id=trace_id,
                        error=str(record_exc),
                    )

        response_id: str | None = getattr(response, "id", None)

        # ADR-0074 §I2: canonical model_call_completed emission (parity with
        # LocalLLMClient). Reuses span_id minted at started emit so a single
        # span threads through the call.
        _cost_usd: float | None = round(cost, 6) if cost else None
        _input_tokens = usage.get("prompt_tokens")
        _output_tokens = usage.get("completion_tokens")
        _total_tokens = usage.get("total_tokens")
        _cache_read = usage.get("cache_read_input_tokens")
        _cache_creation = usage.get("cache_creation_input_tokens")
        # FRE-1008: hashes the actual api_messages/tools sent above, not
        # system_prompt twice — see prompt_identity.py.
        _identity = prompt_identity or derive_fallback_prompt_identity(
            f"role.{role.value}",
            system_prompt=system_prompt,
            request_messages=api_messages,
            tools=tools,
        )
        emit_model_call_completed(
            log=log,
            role=role.value,
            model=self._litellm_model,
            endpoint=self.provider,
            provider=self.provider,
            trace_ctx=trace_ctx,
            span_id=span_id,
            input_tokens=_input_tokens,
            output_tokens=_output_tokens,
            prompt_identity=_identity,
            total_tokens=_total_tokens,
            cache_read_tokens=_cache_read,
            extra={
                "cost_usd": _cost_usd,
                "tool_calls": len(tool_calls),
                "cache_creation_input_tokens": _cache_creation,
                # FRE-989: parity with model_call_started, which has carried the
                # budget lane since the gate landed. A completed event that
                # reports cost_usd without naming the lane it was billed to
                # cannot answer a per-role cost question on its own.
                "budget_role": self.budget_role,
            },
        )

        return LLMResponseType(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_trace=None,
            usage=usage,
            response_id=response_id,
            cost_usd=_cost_usd or 0.0,
            # Surfaced so a caller can tell a reply cut off at the ceiling from a
            # complete one (FRE-996). Reachable via `raw` before this, which meant in
            # practice nobody read it and truncation arrived disguised as a parse error.
            finish_reason=choice.finish_reason,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )
