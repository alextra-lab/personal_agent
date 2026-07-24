"""Config-owned model pricing registration (ADR-0101 §8b / FRE-691).

Model pricing is authored in ``config/models.{cloud,}.yaml`` (the source of truth,
per ADR-0099) as ``input_cost_per_token`` / ``output_cost_per_token`` on each
``ModelDefinition``. At startup :func:`register_model_pricing` pushes those rates
into litellm's global ``model_cost`` registry so both the pre-call estimator
(``cost_estimator``) and the post-call reconciliation (``litellm.completion_cost``)
compute a **deterministic, non-zero** cost owned by our config — not by whatever
litellm happens to ship for a given model id (which drifts across upgrades and
would silently meter cloud vision as zero; see FRE-734 and AC-11).

``litellm.register_model`` folds a prefixed key (``anthropic/claude-sonnet-4-6``)
onto the bare id it already knows, so callers must look cost up via
``litellm.completion_cost`` / ``cost_per_token`` (which resolve prefixed→bare),
never by asserting a specific registry key.
"""

from __future__ import annotations

import structlog

from personal_agent.llm_client.models import ModelConfig, ModelKind

log = structlog.get_logger(__name__)


def register_model_pricing(config: ModelConfig) -> int:
    """Register every priced LLM chat model into ``litellm.model_cost``.

    Idempotent: re-registering the same config overwrites with identical values.
    A definition with no ``input_cost_per_token``, or a non-LLM ``kind``
    (``embedding``/``reranker``), is skipped: ``mode: "chat"`` below is a
    litellm chat-completion registration, and `litellm.completion_cost()` is
    never called for embedding/reranker calls (FRE-974 — they compute cost
    directly from vendor-response token usage, not through litellm). Reusing
    ``input_cost_per_token`` for the reranker's real USD price was almost a
    silent registry pollution bug — reranker's ``kind`` filters it back out
    here rather than needing a separate never-registered field the way
    embedding's EUR price does (``input_cost_per_token_eur``, never read by
    this function at all).

    Args:
        config: Loaded model configuration whose definitions may carry pricing.

    Returns:
        The number of model definitions registered.
    """
    import litellm  # noqa: PLC0415 — keep litellm import off module load

    entries: dict[str, dict[str, object]] = {}
    for definition in config.models.values():
        if definition.input_cost_per_token is None or definition.kind is not ModelKind.LLM:
            continue
        # litellm dispatch key mirrors LiteLLMClient._litellm_model (provider/id).
        provider = definition.provider or "openai"
        litellm_model = f"{provider}/{definition.id}"
        entries[litellm_model] = {
            "input_cost_per_token": definition.input_cost_per_token,
            "output_cost_per_token": definition.output_cost_per_token or 0.0,
            "litellm_provider": provider,
            "mode": "chat",
        }

    if entries:
        litellm.register_model(entries)

    log.info("model_pricing_registered", count=len(entries), models=sorted(entries))
    return len(entries)
