"""FRE-974 — config guard: OVH embedding / Voyage reranker deployments must carry pricing.

Regression protection against silent drift back to unpriced (which would
silently disable cost recording for both vendors — memory/embeddings.py and
memory/reranker.py skip cost recording when their pricing lookup returns
``None``, per design).
"""

from __future__ import annotations

from personal_agent.config.model_loader import load_model_config, resolve_role_definition


def test_embedding_role_carries_eur_pricing() -> None:
    model_def = resolve_role_definition("embedding", config=load_model_config())
    assert model_def is not None
    assert model_def.input_cost_per_token_eur is not None
    assert model_def.input_cost_per_token_eur > 0


def test_reranker_role_carries_usd_pricing() -> None:
    model_def = resolve_role_definition("reranker", config=load_model_config())
    assert model_def is not None
    assert model_def.input_cost_per_token is not None
    assert model_def.input_cost_per_token > 0
