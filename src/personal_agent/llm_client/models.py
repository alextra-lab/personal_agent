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


class Dialect(str, Enum):
    """A named wire vocabulary: the thinking lever, plus the accepted sampling set (ADR-0145 D3a).

    Declared once on a :class:`ProviderDefinition` and overridable on a
    :class:`ModelDefinition` that disagrees with its provider — the Anthropic case,
    where Sonnet 5 and Haiku 4.5 share a provider entry but speak different
    dialects (FRE-1430 F6). Five dialects cover every declared chat model measured
    by FRE-1430: no two of the five agree on what they accept.
    """

    LLAMACPP_QWEN = "llamacpp_qwen"
    OVH_QWEN = "ovh_qwen"
    OPENAI_GPT5 = "openai_gpt5"
    ANTHROPIC_ADAPTIVE = "anthropic_adaptive"
    ANTHROPIC_BUDGET = "anthropic_budget"


class ModeSpec(BaseModel):
    """A named sampler/thinking preset, written in a model's dialect vocabulary (ADR-0145 D3a).

    Every field here is optional; a mode sets only the ones its dialect accepts,
    and the loader rejects the rest (:data:`DIALECT_FIELDS`). The three
    thinking-lever fields (``enable_thinking``, ``reasoning_effort``, ``effort``,
    ``budget_tokens``) are named per the dialect that owns them rather than
    unified into one, because the dialects disagree not just on the wire shape
    but on the value domain (FRE-1430 F3, F6): OVH's ``reasoning_effort`` admits
    ``{none, low, medium}``, OpenAI's the full five-rung ladder, and Anthropic's
    two models don't share a lever at all.

    Attributes:
        enable_thinking: llamacpp_qwen's thinking lever
            (``chat_template_kwargs.enable_thinking``). ``reasoning_effort``
            validates on this dialect but changes nothing (FRE-1430 F16), so it
            is deliberately absent from this dialect's accepted fields.
        temperature: Sampling temperature, accepted by every dialect except
            anthropic_adaptive (Sonnet 5 rejects it as deprecated, F1/F6).
        top_p: Nucleus sampling probability.
        top_k: Top-k sampling (llamacpp_qwen and anthropic_budget only).
        min_p: Min-p sampling (llamacpp_qwen only; llama.cpp/vLLM extension).
        presence_penalty: Presence penalty (llamacpp_qwen and ovh_qwen only).
        frequency_penalty: Frequency penalty.
        repeat_penalty: llama.cpp's own wire name for repetition penalty — NOT
            ``repetition_penalty``, which is accepted and silently ignored on
            that server (FRE-1430 F2; the rename itself is FRE-1438).
        seed: Sampling seed.
        reasoning_effort: ovh_qwen / openai_gpt5's thinking lever. Value domain
            is further restricted per dialect by :data:`DIALECT_VALUE_DOMAINS` —
            OVH's gateway 400s on ``high`` and 422s on ``xhigh`` (FRE-1430 F3).
        effort: anthropic_adaptive's thinking lever (native
            ``thinking: {type: adaptive}`` + ``output_config.effort``).
        budget_tokens: anthropic_budget's thinking lever (native
            ``thinking: {type: enabled, budget_tokens}``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enable_thinking: bool | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    top_p: float | None = Field(None, ge=0.0, le=1.0)
    top_k: int | None = Field(None, ge=1)
    min_p: float | None = Field(None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(None, ge=-2.0, le=2.0)
    repeat_penalty: float | None = Field(None, ge=0.0)
    seed: int | None = None
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None
    effort: Literal["low", "medium", "high"] | None = None
    budget_tokens: int | None = Field(None, ge=1)

    @property
    def resolved_reasoning_effort(self) -> str | None:
        """The single wire-facing reasoning-depth value, whichever dialect field carries it.

        Callers that forward a reasoning depth to litellm (which accepts one
        ``reasoning_effort`` kwarg regardless of provider and transforms it
        per model, FRE-1430 F5) do not need to know which lever field their
        model's dialect uses.
        """
        return self.reasoning_effort if self.reasoning_effort is not None else self.effort


#: Fields each dialect's mode bodies may set — the lever plus the accepted
#: sampling set (ADR-0145 D3a table). A field absent here is either rejected by
#: the wire (OVH, OpenAI, Anthropic all 400 on an unknown field, FRE-1430 F1) or
#: silently inert (llama.cpp's ``reasoning_effort``, F16) — either way, not part
#: of the dialect's real vocabulary.
DIALECT_FIELDS: dict[Dialect, frozenset[str]] = {
    Dialect.LLAMACPP_QWEN: frozenset(
        {
            "enable_thinking",
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "frequency_penalty",
            "repeat_penalty",
            "seed",
        }
    ),
    Dialect.OVH_QWEN: frozenset(
        {
            "reasoning_effort",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
        }
    ),
    Dialect.OPENAI_GPT5: frozenset(
        {"reasoning_effort", "temperature", "top_p", "frequency_penalty", "seed"}
    ),
    Dialect.ANTHROPIC_ADAPTIVE: frozenset({"effort"}),
    Dialect.ANTHROPIC_BUDGET: frozenset({"budget_tokens", "temperature", "top_p", "top_k"}),
}

#: Per-dialect value restrictions narrower than the field's own type (ADR-0145
#: D3a). ``reasoning_effort`` is a five-value ``Literal`` on :class:`ModeSpec`
#: because the value is shared type-wise across dialects, but no single dialect
#: accepts all five (FRE-1430 F3): OVH's gateway 400s on ``high`` and 422s on
#: ``xhigh``, and only the 3-rung intersection reaches the model at all.
DIALECT_VALUE_DOMAINS: dict[Dialect, dict[str, frozenset[str]]] = {
    Dialect.OVH_QWEN: {"reasoning_effort": frozenset({"none", "low", "medium"})},
    Dialect.OPENAI_GPT5: {
        "reasoning_effort": frozenset({"none", "low", "medium", "high", "xhigh"})
    },
}


def dialect_accepts(dialect: Dialect, param: str) -> bool:
    """Whether a **call-site** parameter name is in this dialect's vocabulary (ADR-0145 D2/D3a).

    :data:`DIALECT_FIELDS` names the *configuration* fields a mode may set. One
    of them is spelled differently at the call site: ``anthropic_adaptive``
    declares ``effort`` in the catalog and takes ``reasoning_effort`` as the
    kwarg, because litellm's transformation is the wire there and
    ``reasoning_effort`` is the kwarg it transforms. The alias is one-way on
    purpose — ``anthropic_budget``'s lever is ``budget_tokens``, so a call site
    passing ``reasoning_effort`` to Haiku is rejected rather than quietly
    routed through litellm's legacy budget mapping, which also rewrites
    ``max_tokens`` (FRE-1430 F5).

    This answers **field membership only**. Two dialects carry rules that
    relate two fields to each other — ``openai_gpt5`` accepts temperature and
    top_p only at effort ``none``, and ``anthropic_budget`` takes temperature
    XOR top_p. Both are enforced on the declarative side by
    :meth:`ModelConfig._llm_deployments_declare_valid_modes`, and a combination
    assembled at a call site is refused by the provider itself, which is the
    outcome ``drop_params: off`` exists to preserve.

    Args:
        dialect: The resolved dialect of the model being dispatched to.
        param: The call-site keyword name (e.g. ``"temperature"``).

    Returns:
        ``True`` when the dialect accepts that parameter.
    """
    accepted = DIALECT_FIELDS[dialect]
    if param == "reasoning_effort":
        return "reasoning_effort" in accepted or "effort" in accepted
    return param in accepted


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


def required_kind_for_role(role: str) -> ModelKind:
    """Return the deployment kind a role may legally bind to (ADR-0121 §6, AC-2).

    Single source of the role→kind rule shared by config-load binding validation
    (:meth:`ModelConfig._bindings_are_valid_and_kind_compatible`) and the runtime
    selection guardrail (``config.model_loader.is_selectable_binding``). A role
    absent from :data:`ROLE_KIND_REQUIREMENTS` requires ``LLM`` — the fail-closed
    default, so a role added later cannot silently accept an arbitrary kind.

    Args:
        role: The role name.

    Returns:
        The required :class:`ModelKind` for the role.
    """
    return ROLE_KIND_REQUIREMENTS.get(role, ModelKind.LLM)


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
    dialect: Dialect | None = Field(
        None,
        description=(
            "Default wire vocabulary for this provider's kind=llm models "
            "(ADR-0145 D3a). None where every model on the provider must "
            "override — 'anthropic' has two chat models on two different "
            "dialects (FRE-1430 F6) and declares neither as default. "
            "Meaningless for a provider that only serves embedding/reranker "
            "deployments."
        ),
    )


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
        defaults_by_primary: ADR-0121 Addendum A (FRE-964) — a per-primary default
            map, currently meaningful only on the ``sub_agent`` binding: primary
            deployment key -> the sub deployment key it pairs with, one
            deliberate line per primary-eligible deployment (no derivation).
            Substrate only as of step 1 (FRE-965) — nothing resolves through it
            yet; the flat ``deployment`` above stays the operative default until
            step 3's resolver cutover. ``None`` when unset (every binding other
            than ``sub_agent`` today).
    """

    model_config = ConfigDict(frozen=True)

    deployment: str = Field(..., description="Deployment catalog key this role binds to")
    open: bool = Field(False, description="User-selectable? Pinned by default (fail closed)")
    max_tokens: int | None = Field(None, ge=1, description="Per-use output cap override")
    temperature: float | None = Field(None, ge=0.0, le=2.0, description="Per-use temperature")
    disable_thinking: bool | None = Field(None, description="Per-use thinking disable")
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = Field(
        None, description="Per-use reasoning-effort hint"
    )
    default_timeout: int | None = Field(None, ge=1, description="Per-use timeout override")
    defaults_by_primary: dict[str, str] | None = Field(
        None,
        description=(
            "ADR-0121 Addendum A: per-primary default sub map (primary deployment "
            "key -> paired sub deployment key). Substrate only until step 3's "
            "resolver reads it; the flat `deployment` field above is what actually "
            "resolves until then."
        ),
    )


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
        provider: Cloud provider name. "anthropic", "openai", etc. None means a
            local model — both dispatch through LiteLLMClient (ADR-0141 D1).
        max_tokens: Maximum output tokens for this model. Primarily useful for
            cloud models where output length is billed per token. None means
            provider default for cloud; omit-means-unbounded for local
            placement (ADR-0141 D5).
        endpoint: Optional base URL override for this model. If None, uses
            settings.slm_base_url. Not used for cloud models (they use provider SDK).
        context_length: Maximum context length for this model.
        quantization: Quantization level (e.g., "8bit", "4bit", "5bit"). None for
            cloud models where quantization is managed by the provider.
        max_concurrency: Maximum concurrent requests for this model.
        default_timeout: Default timeout in seconds for requests to this model.
        dialect: Override of the provider's default wire vocabulary (ADR-0145
            D3a). Required when the provider declares none, or when this model
            disagrees with its provider's default (the Anthropic case).
        modes: Named sampler/thinking presets, written in this model's dialect
            vocabulary (ADR-0145 D3a). The only declarative home for samplers
            and thinking — there is no top-level fallback. Required, with at
            least one entry, for every ``kind: llm`` deployment; validated
            against the resolved dialect's accepted fields
            (:data:`DIALECT_FIELDS`) at catalog load.
        default_mode: Which entry in ``modes`` a caller gets when it does not
            name one. Must be a key of ``modes``.
        supports_function_calling: Whether model/backend supports OpenAI-style function calling.
            If False, tools are not passed to the model. Defaults to True.
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
    dialect: Dialect | None = Field(
        None,
        description=(
            "Override of the provider's default dialect (ADR-0145 D3a). "
            "Required when the provider declares none for this kind=llm model."
        ),
    )
    modes: dict[str, ModeSpec] = Field(
        default_factory=dict,
        description=(
            "Named sampler/thinking presets, written in this model's dialect "
            "vocabulary (ADR-0145 D3a). Required, non-empty, for every "
            "kind=llm deployment — enforced at catalog load "
            "(ModelConfig._llm_deployments_declare_valid_modes), not here, so "
            "a standalone ModelDefinition built for an unrelated test stays "
            "cheap to construct."
        ),
    )
    default_mode: str | None = Field(
        None,
        description="Key into `modes` a caller gets when it names none. Must be a member of modes.",
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
    input_cost_per_token_eur: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "EUR cost per input token (FRE-974, ADR-0120 T0) — for vendors that bill "
            "in EUR (OVH AI Endpoints). Deliberately separate from input_cost_per_token "
            "(USD): reusing that field for a EUR rate would silently corrupt every "
            "USD-denominated consumer (register_model_pricing() -> litellm.model_cost, "
            "which assumes USD). Converted to USD at the call site via "
            "settings.eur_usd_rate; never registered into litellm's pricing registry, "
            "since embedding calls never go through litellm.completion_cost()."
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

    @model_validator(mode="after")
    def _modes_and_default_mode_consistent(self) -> "ModelDefinition":
        """A declared default_mode must name a declared mode (ADR-0145 D3a AC-3).

        Deliberately does not require kind=llm to declare modes at all — that
        stronger rule needs the provider (for dialect resolution) and lives on
        :class:`ModelConfig` instead, so a standalone ``ModelDefinition()``
        built for an unrelated test does not need a modes: block it never uses.
        """
        if self.default_mode is not None and self.default_mode not in self.modes:
            raise ValueError(
                f"default_mode {self.default_mode!r} is not a key of modes "
                f"{sorted(self.modes)} — a caller asking for the default would "
                "get nothing to resolve."
            )
        if self.modes and self.default_mode is None:
            raise ValueError(
                f"modes {sorted(self.modes)} declared with no default_mode — "
                "a caller asking for the default cannot be resolved arbitrarily."
            )
        return self

    def resolve_mode(self, name: str | None = None) -> ModeSpec:
        """Return the named mode's spec, or the default mode's when name is None.

        Args:
            name: A key of :attr:`modes`, or ``None`` for :attr:`default_mode`.

        Returns:
            The resolved :class:`ModeSpec`.

        Raises:
            ValueError: ``name`` (or the model's ``default_mode``) is not a
                declared mode. Unreachable for a deployment loaded through
                :class:`ModelConfig`, which requires every kind=llm entry to
                declare modes and a matching default_mode.
        """
        key = name if name is not None else self.default_mode
        if key is None or key not in self.modes:
            raise ValueError(
                f"{key!r} is not a declared mode on {self.id!r}; known modes: {sorted(self.modes)}"
            )
        return self.modes[key]

    def resolve_dialect(self, provider_def: "ProviderDefinition | None") -> Dialect | None:
        """Return this model's wire vocabulary: its own override, else its provider's.

        The single source of the resolution rule, shared by catalog load
        (:meth:`ModelConfig._llm_deployments_declare_valid_modes`) and the
        dispatch path (``LiteLLMClient``), so the vocabulary the loader
        validates against and the one the client builds from cannot drift.

        Args:
            provider_def: The model's provider entry, or ``None`` when the
                provider is not declared in the catalog.

        Returns:
            The resolved :class:`Dialect`, or ``None`` when neither the model
            nor its provider declares one. ``None`` is unreachable for a
            ``kind: llm`` deployment loaded through :class:`ModelConfig`, which
            refuses to load one; it is reachable for a definition constructed
            outside the catalog.
        """
        if self.dialect is not None:
            return self.dialect
        return provider_def.dialect if provider_def is not None else None

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
        """Every deployment must reference a known provider (ADR-0121 §8 replacement check).

        FRE-916 phase 2 tightened this from "no DANGLING provider reference" to
        "provider REQUIRED". Phase 1 introduced the layers additively and allowed
        a providerless deployment as a not-yet-migrated legacy entry; every entry
        now declares a provider, so the laxity has outlived its purpose and is a
        live fail-open. A deployment with no provider has no declared placement,
        and :meth:`placement_of` would default it to ``LOCAL`` — routing a
        would-be cloud model to the local client and silently skipping the
        paid-attachment cost gate (caught in code review). Requiring the provider
        makes that state unrepresentable rather than merely improbable.
        """
        if not self.providers:
            return self
        for key, definition in self.models.items():
            if definition.provider is None:
                raise ValueError(
                    f"deployment {key!r} declares no provider; every deployment must "
                    f"reference one of {sorted(self.providers)} (ADR-0121 — placement is a "
                    "provider fact, and a providerless deployment fails open to LOCAL)"
                )
            if definition.provider not in self.providers:
                raise ValueError(
                    f"deployment {key!r} references unknown provider "
                    f"{definition.provider!r}; known providers: {sorted(self.providers)}"
                )
        return self

    @model_validator(mode="after")
    def _llm_deployments_declare_valid_modes(self) -> "ModelConfig":
        """Every kind=llm deployment declares modes, in its dialect's vocabulary (ADR-0145 D3a).

        Skipped for a providerless/partial config (mirrors
        :meth:`_deployments_reference_known_providers`'s own guard) so a
        standalone ``ModelConfig`` built for an unrelated test does not need a
        dialect it never exercises. For a config that DOES declare providers,
        every kind=llm entry must resolve a dialect (its own override, or its
        provider's default) and declare at least one mode, each validated
        against that dialect's accepted fields and value domains.
        """
        if not self.providers:
            return self
        for key, definition in self.models.items():
            if definition.kind is not ModelKind.LLM:
                continue
            if not definition.modes or definition.default_mode is None:
                raise ValueError(
                    f"deployment {key!r} is kind=llm and must declare modes: with "
                    "at least one entry and a default_mode naming it (ADR-0145 D3a)"
                )
            provider_def = self.providers.get(definition.provider or "")
            dialect = definition.resolve_dialect(provider_def)
            if dialect is None:
                raise ValueError(
                    f"deployment {key!r} has no dialect — declare one on the model "
                    f"or on provider {definition.provider!r} (ADR-0145 D3a)"
                )
            accepted = DIALECT_FIELDS[dialect]
            domains = DIALECT_VALUE_DOMAINS.get(dialect, {})
            for mode_name, spec in definition.modes.items():
                where = f"deployment {key!r} mode {mode_name!r} (dialect {dialect.value!r})"
                set_fields = spec.model_dump(exclude_none=True)
                for field_name, value in set_fields.items():
                    if field_name not in accepted:
                        raise ValueError(
                            f"{where} sets {field_name!r}, which {dialect.value!r} does not "
                            f"accept; accepted fields: {sorted(accepted)} (ADR-0145 D3a, "
                            "FRE-1430 F1)"
                        )
                    domain = domains.get(field_name)
                    if domain is not None and value not in domain:
                        raise ValueError(
                            f"{where} sets {field_name}={value!r}, outside "
                            f"{dialect.value!r}'s domain {sorted(domain)} (ADR-0145 D3a, "
                            "FRE-1430 F3)"
                        )
                if (
                    dialect is Dialect.OPENAI_GPT5
                    and spec.reasoning_effort not in (None, "none")
                    and (spec.temperature is not None or spec.top_p is not None)
                ):
                    raise ValueError(
                        f"{where} sets temperature/top_p alongside "
                        f"reasoning_effort={spec.reasoning_effort!r} — openai_gpt5 accepts "
                        "temperature and top_p only at effort 'none' (FRE-1430 F1, F5)"
                    )
                if (
                    dialect is Dialect.ANTHROPIC_BUDGET
                    and spec.temperature is not None
                    and spec.top_p is not None
                ):
                    raise ValueError(
                        f"{where} sets both temperature and top_p — anthropic_budget accepts "
                        'only one at a time (FRE-1430 F6: "temperature and top_p cannot both '
                        'be specified for this model")'
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
            required = required_kind_for_role(role)
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
            :attr:`Placement.LOCAL` only for a deployment (or provider) absent
            from this config — a defensive fallback for a partially-constructed
            or test config, NOT a behavioural rule. It is unreachable for any
            catalog that loaded: :meth:`_deployments_reference_known_providers`
            fails config load on a providerless or dangling deployment. (It is
            *not* "pre-ADR-0121 parity": an unresolved ``provider_type`` used to
            default to cloud dispatch, the opposite direction — which is exactly
            why the providerless state had to become unrepresentable.)
        """
        definition = self.models.get(deployment_key)
        if definition is None or definition.provider is None:
            return Placement.LOCAL
        provider = self.providers.get(definition.provider)
        return Placement.LOCAL if provider is None else provider.placement
