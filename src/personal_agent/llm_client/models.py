"""Pydantic models for LLM client configuration.

This module defines the schema for model configuration loaded from config/models.yaml.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolCallingStrategy(str, Enum):
    """How the agent presents tools to a given model.

    NATIVE:          Pass tools in the OpenAI ``tools`` array and expect
                     structured ``tool_calls`` in the response.  Works only when
                     the model's chat template renders tools correctly *and* the
                     model was fine-tuned on that format (e.g. Qwen3.5 via
                     LM Studio).
    PROMPT_INJECTED: Render tool definitions as text inside the system prompt
                     and parse tool invocations from the model's free-text
                     output.  Use this for models whose chat template does not
                     support tools or whose native tool output is unreliable.
    DISABLED:        No tool calling at all (e.g. the router model).
    """

    NATIVE = "native"
    PROMPT_INJECTED = "prompt"
    DISABLED = "disabled"


class Placement(str, Enum):
    """Where a provider's inference physically runs (ADR-0121 Layer 1).

    LOCAL: owner-controlled hardware reached over the network (the SLM tunnel).
           Subject to strict concurrency control — the GPU is a scarce resource.
    CLOUD: a third-party API. Concurrency is the provider's problem, not ours.

    Replaced ``ModelDefinition.provider_type`` and the retired
    ``concurrency.infer_provider_type``'s
    URL string-parsing: placement is now declared once on the provider rather
    than reconstructed per model entry.
    """

    LOCAL = "local"
    CLOUD = "cloud"


class ModelKind(str, Enum):
    """What a deployment *is* (ADR-0121 Layer 2).

    The typing hole this closes: before ADR-0121 nothing said whether a catalog
    entry was a chat model, an embedding model, or a reranker, so nothing
    structurally prevented the ``embedding`` role binding to a chat model.
    Half of the authorization rule in ADR-0121 §6 — ``kind`` compatibility is
    intrinsic to the model; ``open`` is policy on the role; both are required.
    """

    LLM = "llm"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


#: Which deployment kinds each role may legally bind to (ADR-0121 §6, AC-2).
#: A role absent from this map accepts LLM only — the fail-closed default, so a
#: role added later cannot silently accept an arbitrary kind.
ROLE_KIND_REQUIREMENTS: dict[str, ModelKind] = {
    "embedding": ModelKind.EMBEDDING,
    "reranker": ModelKind.RERANKER,
    "reranker_fallback": ModelKind.RERANKER,
}


class ProviderDefinition(BaseModel):
    """A backend we talk to — one entry per provider, not per model (ADR-0121 Layer 1).

    Owns endpoint, authentication, placement, and total concurrency capacity.
    Before ADR-0121 there was no provider entity at all: ``endpoint`` was
    copy-pasted onto 5 of 12 model entries and the provider was reconstructed at
    runtime by parsing the URL.

    Attributes:
        base_url: Base URL for this provider's OpenAI-compatible API. ``None``
            for providers dispatched through a vendor SDK (Anthropic, OpenAI),
            which carry their own endpoints.
        auth_env: Name of the ``AppConfig`` field holding this provider's
            credential. ``None`` means no authentication (the local SLM tunnel,
            which is gated by Cloudflare Access headers rather than a key).
        placement: Where this provider runs — drives concurrency control.
        max_concurrency: Total in-flight requests permitted across **all** of
            this provider's deployments. The laptop-contention ceiling lives
            here rather than being frozen into a role binding.
        summary: One line describing the provider, for the config read API.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str | None = Field(None, description="Base URL; None for SDK-dispatched providers")
    auth_env: str | None = Field(None, description="AppConfig field holding the credential")
    placement: Placement = Field(..., description="local | cloud")
    max_concurrency: int = Field(..., ge=1, description="Total in-flight cap across deployments")
    summary: str = Field("", description="One-line description for the config read API")


class RoleBinding(BaseModel):
    """Which deployment a role uses, plus its per-use parameters (ADR-0121 Layer 3).

    Decoding parameters and effort live here, not on the deployment, because
    they are per-*use*. This is what dissolves the ``primary``/``sub_agent``
    duplication: they stop being two "models" and become two bindings of one
    model at different effort — which is what they always were.

    Attributes:
        deployment: Key into the deployment catalog. Validated to exist and to
            be ``kind``-compatible with this role at config load (AC-2).
        open: Whether a user may select this role's model. ``False`` (the
            default) means pinned — the fail-closed half of ADR-0121 §6's
            guardrail, so a role added later is never selectable by omission.
        max_tokens: Per-use output cap, overriding the deployment default.
        temperature: Per-use sampling temperature, overriding the deployment default.
        disable_thinking: Per-use hard disable of thinking for Qwen3.5+ models.
        reasoning_effort: Per-use reasoning-effort hint for reasoning models.
        default_timeout: Per-use request timeout, overriding the deployment default.
    """

    model_config = ConfigDict(frozen=True)

    deployment: str = Field(..., description="Deployment catalog key this role binds to")
    open: bool = Field(False, description="User-selectable? Pinned by default (fail closed)")
    max_tokens: int | None = Field(None, ge=1, description="Per-use output cap override")
    temperature: float | None = Field(None, ge=0.0, le=2.0, description="Per-use temperature")
    disable_thinking: bool | None = Field(None, description="Per-use thinking disable")
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = Field(
        None, description="Per-use reasoning-effort hint"
    )
    default_timeout: int | None = Field(None, ge=1, description="Per-use timeout override")


class ModelDefinition(BaseModel):
    """Configuration for a single model.

    Applies to both local models (LM Studio, vLLM, Ollama) and cloud models
    (Anthropic Claude, OpenAI). Cloud-specific fields (provider, max_tokens) are
    optional and ignored for local models; local-specific fields (quantization,
    endpoint) are optional and ignored for cloud models (ADR-0031).

    Attributes:
        id: Model identifier. For local models this is the LM Studio slug
            (e.g., "qwen3.5-35b-a3b"). For cloud models this is the provider's
            model name (e.g., "claude-sonnet-4-5-20250514", "o4-mini").
        provider: Cloud provider name. "anthropic", "openai", etc. dispatch to LiteLLMClient.
            None means local model via LocalLLMClient.
        max_tokens: Maximum output tokens for this model. Primarily useful for
            cloud models where output length is billed per token. None = provider
            default / LocalLLMClient call-site default.
        endpoint: Optional base URL override for this model. If None, uses
            settings.llm_base_url. Not used for cloud models (they use provider SDK).
            "local" = single-GPU servers (strict concurrency), "managed" = self-hosted
            multi-GPU clusters (moderate control), "cloud" = OpenAI/Anthropic/etc
            (pass-through). Auto-detected from endpoint if omitted.
        context_length: Maximum context length for this model.
        quantization: Quantization level (e.g., "8bit", "4bit", "5bit"). None for
            cloud models where quantization is managed by the provider.
        max_concurrency: Maximum concurrent requests for this model.
        default_timeout: Default timeout in seconds for requests to this model.
        temperature: Default sampling temperature (None uses backend default).
        top_p: Top-p nucleus sampling probability (None uses backend default).
        top_k: Top-k sampling — number of highest-probability tokens to keep. Not in the
            standard OpenAI spec; passed via extra_body for vLLM/LM Studio backends.
        presence_penalty: Presence penalty to reduce repetition. Positive values discourage
            token reuse. Passed in the top-level payload (standard OpenAI field).
        supports_function_calling: Whether model/backend supports OpenAI-style function calling.
            If False, tools are not passed to the model. Defaults to True.
        disable_thinking: If True, inject chat_template_kwargs enable_thinking=False via
            extra_body on every request. Hard-disables thinking for Qwen3.5+ models.
            Mutually exclusive with thinking_budget_tokens.
        thinking_budget_tokens: Cap on the number of thinking tokens the model may generate.
            Passed as thinking_budget in extra_body. None means unlimited.
            Mutually exclusive with disable_thinking.
        supports_vision: Whether this model/deployment accepts image content blocks
            (ADR-0101 §5). A deployment property, not inferred — set explicitly per
            model definition. Defaults to False.
        supports_pdf_document: Whether this model/deployment accepts a provider-side
            native PDF document block (ADR-0102 §3). A deployment property, not
            inferred — set explicitly per model definition. Composes with
            supports_vision: a model may be vision-capable (rasterized image blocks)
            without being PDF-document-capable (the local SLM case), and vice versa.
            Defaults to False.
    """

    id: str = Field(..., description="Model identifier")
    provider: str | None = Field(
        None,
        description=(
            "Provider reference — a key in the catalog's `providers:` mapping "
            "(ADR-0121 Layer 1). Placement, endpoint, auth, and the concurrency "
            "ceiling are read from that provider rather than repeated here. "
            "Legacy: before ADR-0121 this held a bare LiteLLM provider name and "
            "None meant 'local'."
        ),
    )
    kind: ModelKind = Field(
        ModelKind.LLM,
        description=(
            "What this deployment is (ADR-0121 Layer 2). Validated against the "
            "binding role's requirement at config load — the typing hole that "
            "let the embedding role bind to a chat model (AC-2)."
        ),
    )
    dimensions: int | None = Field(
        None,
        ge=1,
        description=(
            "Embedding vector width. Only meaningful for kind=embedding; the "
            "runtime truncates to settings.embedding_dimensions (Matryoshka)."
        ),
    )
    summary: str = Field(
        "",
        description=(
            "One line of intended use, for the model picker and for machine "
            "selection. DECLARED facts only (ADR-0121 §2) — observed behaviour "
            "such as latency or truncation belongs in telemetry, never here, "
            "where it silently goes stale (the FRE-880 rot this line prevents)."
        ),
    )
    status: Literal["active", "preview", "deprecated"] = Field(
        "active", description="Lifecycle status, surfaced by the picker."
    )
    max_tokens: int | None = Field(
        None,
        ge=1,
        description=(
            "Maximum output tokens. Primarily used for cloud models where output length "
            "is billed per token. None = provider default."
        ),
    )
    endpoint: str | None = Field(None, description="Optional base URL override (local models)")
    context_length: int = Field(..., ge=1, description="Maximum context length")
    quantization: str | None = Field(
        None,
        description=(
            "Quantization level (e.g., '8bit', '4bit'). "
            "None for cloud models where quantization is provider-managed."
        ),
    )
    max_concurrency: int = Field(..., ge=1, description="Maximum concurrent requests")
    min_concurrency: int = Field(
        default=1,
        ge=1,
        description=(
            "Floor for adaptive concurrency control (ADR-0033). "
            "Brainstem cannot reduce effective concurrency below this value. "
            "Must be <= max_concurrency."
        ),
    )
    default_timeout: int = Field(..., ge=1, description="Default timeout in seconds")
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Default sampling temperature for this model (None uses backend default).",
    )
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = Field(
        default=None,
        description=(
            "FRE-766: discrete reasoning-effort hint for reasoning models (GPT-5 family). "
            "One of low/medium/high/xhigh; None uses the provider default (medium for GPT-5). "
            "Forwarded to litellm.acompletion by LiteLLMClient. Claude uses adaptive thinking, "
            "not this hint — leave None for Anthropic models."
        ),
    )
    top_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Top-p nucleus sampling probability (None uses backend default).",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="Top-k sampling — passed via extra_body (not standard OpenAI).",
    )
    presence_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="Presence penalty to reduce token repetition.",
    )
    min_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Min-p sampling — passed via extra_body (llama.cpp / vLLM extension).",
    )
    repetition_penalty: float | None = Field(
        default=None,
        ge=0.0,
        description="Repetition penalty — passed via extra_body (llama.cpp / vLLM extension).",
    )
    supports_function_calling: bool = Field(
        True,
        description=(
            "DEPRECATED — use tool_calling_strategy instead.  Kept for backward "
            "compatibility; ignored when tool_calling_strategy is set explicitly."
        ),
    )
    supports_vision: bool = Field(
        False,
        description=(
            "Whether this model/deployment accepts image content blocks (ADR-0101 §5). "
            "A deployment property, not inferred — set explicitly per model definition."
        ),
    )
    supports_pdf_document: bool = Field(
        False,
        description=(
            "Whether this model/deployment accepts a provider-side native PDF document "
            "block (ADR-0102 §3). A deployment property, not inferred — set explicitly "
            "per model definition. Composes with supports_vision (rasterized image "
            "blocks); the two flags are independent."
        ),
    )
    input_cost_per_token: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost per input token (ADR-0101 §8b / FRE-691). Config-owned pricing "
            "registered into litellm.model_cost at startup so cloud cost is deterministic "
            "and non-zero, independent of litellm's shipped registry. None = rely on the "
            "litellm registry (local/free models leave this unset). Image (vision) tokens "
            "are billed as ordinary input tokens on this rate."
        ),
    )
    output_cost_per_token: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost per output token (ADR-0101 §8b / FRE-691). See input_cost_per_token."
        ),
    )
    tool_calling_strategy: ToolCallingStrategy | None = Field(
        default=None,
        description=(
            "How to present tools to this model.  'native' = OpenAI tools array, "
            "'prompt' = inject tools into the system prompt as text, "
            "'disabled' = no tool calling.  When None the strategy is derived "
            "from supports_function_calling for backward compatibility."
        ),
    )
    parallel_tool_calls: bool = Field(
        default=True,
        description=(
            "Include parallel_tool_calls=True in the chat completions payload, "
            "allowing the model to emit multiple tool calls in a single response turn. "
            "Only active when tool_calling_strategy=NATIVE. Requires llama.cpp >= build "
            "with QwenLM/#1831 Qwen3.x template fixes (FRE-232). "
            "Set False for models whose chat template does not handle parallel calls."
        ),
    )
    disable_thinking: bool = Field(
        default=False,
        description=(
            "If True, inject chat_template_kwargs enable_thinking=False via extra_body. "
            "Hard-disables thinking for Qwen3.5+ models. "
            "Mutually exclusive with thinking_budget_tokens."
        ),
    )
    thinking_budget_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Cap on thinking tokens; passed as thinking_budget in extra_body. "
            "None = unlimited. Mutually exclusive with disable_thinking."
        ),
    )

    @model_validator(mode="after")
    def _thinking_fields_exclusive(self) -> "ModelDefinition":
        """Ensure disable_thinking and thinking_budget_tokens are not both set."""
        if self.disable_thinking and self.thinking_budget_tokens is not None:
            raise ValueError(
                "disable_thinking and thinking_budget_tokens are mutually exclusive: "
                "a model cannot have thinking both disabled and budgeted."
            )
        return self

    @model_validator(mode="after")
    def _min_max_concurrency(self) -> "ModelDefinition":
        """Ensure min_concurrency does not exceed max_concurrency."""
        if self.min_concurrency > self.max_concurrency:
            raise ValueError(
                f"min_concurrency ({self.min_concurrency}) must be <= "
                f"max_concurrency ({self.max_concurrency})"
            )
        return self

    @model_validator(mode="after")
    def _derive_tool_calling_strategy(self) -> "ModelDefinition":
        """Derive tool_calling_strategy from supports_function_calling when not set."""
        if self.tool_calling_strategy is None:
            self.tool_calling_strategy = (
                ToolCallingStrategy.NATIVE
                if self.supports_function_calling
                else ToolCallingStrategy.DISABLED
            )
        return self

    @property
    def effective_tool_strategy(self) -> ToolCallingStrategy:
        """Return the resolved tool calling strategy (never None)."""
        if self.tool_calling_strategy is not None:
            return self.tool_calling_strategy
        return (
            ToolCallingStrategy.NATIVE
            if self.supports_function_calling
            else ToolCallingStrategy.DISABLED
        )


class ModelConfig(BaseModel):
    """Complete model configuration.

    This represents the structure of config/models.yaml after loading and validation.
    All model identity and call parameters live here (ADR-0031). Only secrets (API keys)
    and operational controls (budgets, feature flags) belong in settings.py / .env.

    Cognitive-pipeline role assignment (entity extraction, Captain's Log, insights,
    compressor, embedding, reranker) lives ONLY in config/model_roles.yaml (ADR-0099
    D1 stage 2, FRE-650) — resolved via
    :func:`personal_agent.config.model_loader.resolve_role_model_key`, not a field
    on this model. There is no fallback: an absent matrix or undeclared role raises.

    Attributes:
        providers: Layer 1 — the backends, keyed by provider name.
        models: Layer 2 — the deployment catalog, keyed by a stable alias naming
            the **model**, never the role. Two deployments of the same weights
            are separate entries: they are separately servable, separately
            sized, and separately available (the two-qwen case).
        roles: Layer 3 — role bindings. Which deployment each role uses, plus
            its per-use parameters.
    """

    providers: dict[str, ProviderDefinition] = Field(
        default_factory=dict, description="Layer 1 — backends by provider name"
    )
    models: dict[str, ModelDefinition] = Field(..., description="Layer 2 — deployment catalog")
    roles: dict[str, RoleBinding] = Field(
        default_factory=dict, description="Layer 3 — role bindings"
    )

    @model_validator(mode="after")
    def _deployments_reference_known_providers(self) -> "ModelConfig":
        """Every deployment's provider must exist (ADR-0121 §8 replacement check)."""
        if not self.providers:
            return self
        for key, definition in self.models.items():
            # A deployment with no provider is a not-yet-migrated legacy entry
            # carrying its own `endpoint`. The invariant enforced here is "no
            # DANGLING provider reference", not "everything has migrated" —
            # otherwise the three layers cannot be introduced additively.
            # Tighten to require `provider` once every entry declares one.
            if definition.provider is None:
                continue
            if definition.provider not in self.providers:
                raise ValueError(
                    f"deployment {key!r} references unknown provider "
                    f"{definition.provider!r}; known providers: {sorted(self.providers)}"
                )
        return self

    @model_validator(mode="after")
    def _bindings_are_valid_and_kind_compatible(self) -> "ModelConfig":
        """Every role binding must resolve to an existing, kind-compatible deployment.

        This is AC-2. The dangling-reference half survives from ADR-0099's
        retired divergence guard; the kind half is new, and is what makes
        "a writer role bound to an embedding model" unrepresentable rather
        than merely unconventional.
        """
        for role, binding in self.roles.items():
            definition = self.models.get(binding.deployment)
            if definition is None:
                raise ValueError(
                    f"role {role!r} binds to deployment {binding.deployment!r}, which is "
                    f"not defined under models:; known deployments: {sorted(self.models)}"
                )
            required = ROLE_KIND_REQUIREMENTS.get(role, ModelKind.LLM)
            if definition.kind is not required:
                raise ValueError(
                    f"role {role!r} requires a {required.value!r} deployment but "
                    f"{binding.deployment!r} is kind {definition.kind.value!r} "
                    "(ADR-0121 §6 / AC-2 — kind compatibility is not a convention)"
                )
        return self

    def placement_of(self, deployment_key: str) -> Placement:
        """Return where a deployment runs, via its provider.

        Replaces reading ``provider_type`` off the model entry: placement is a
        provider fact, declared once.

        Args:
            deployment_key: Key into ``models``.

        Returns:
            The deployment's provider placement. Defaults to
            :attr:`Placement.LOCAL` when the deployment or its provider is
            unknown — matching the pre-ADR-0121 fallback, where an unresolved
            ``provider_type`` meant local.
        """
        definition = self.models.get(deployment_key)
        if definition is None or definition.provider is None:
            return Placement.LOCAL
        provider = self.providers.get(definition.provider)
        return Placement.LOCAL if provider is None else provider.placement
