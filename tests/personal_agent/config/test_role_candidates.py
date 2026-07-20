"""``role_candidates`` — the picker's candidate-set logic (ADR-0121 §3/§6, AC-5, FRE-918).

AC-5 requires the candidate list to equal exactly {catalog deployments where
kind matches the role} minus deployments whose provider is unavailable —
asserted in both directions, so these tests use full set-equality rather than
single-membership checks.
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import role_candidates
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ProviderDefinition,
    RoleBinding,
)

_QWEN_THINKING = "qwen3.6-35b-thinking"  # slm_local, llm
_QWEN_INSTRUCT = "qwen3.6-35b-instruct"  # slm_local, llm
_CLAUDE_SONNET = "claude_sonnet"  # anthropic, llm
_CLAUDE_HAIKU = "claude_haiku"  # anthropic, llm
_GPT_MINI = "gpt-5.4-mini"  # openai, llm
_EMBEDDING = "embedding"  # ovh, kind=embedding
_RERANKER = "reranker"  # voyage, kind=reranker


def _config() -> ModelConfig:
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(placement="local", max_concurrency=2),
            "anthropic": ProviderDefinition(placement="cloud", max_concurrency=50),
            "openai": ProviderDefinition(placement="cloud", max_concurrency=50),
            "ovh": ProviderDefinition(placement="cloud", max_concurrency=50),
            "voyage": ProviderDefinition(placement="cloud", max_concurrency=50),
        },
        models={
            _QWEN_THINKING: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B",
                provider="slm_local",
                context_length=131072,
                max_concurrency=1,
                default_timeout=600,
            ),
            _QWEN_INSTRUCT: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B-subagent",
                provider="slm_local",
                context_length=65536,
                max_concurrency=3,
                default_timeout=90,
            ),
            _CLAUDE_SONNET: ModelDefinition(
                id="claude-sonnet-5",
                provider="anthropic",
                context_length=200000,
                max_concurrency=10,
                default_timeout=180,
            ),
            _CLAUDE_HAIKU: ModelDefinition(
                id="claude-haiku-4-5-20251001",
                provider="anthropic",
                context_length=200000,
                max_concurrency=20,
                default_timeout=30,
            ),
            _GPT_MINI: ModelDefinition(
                id="gpt-5.4-mini",
                provider="openai",
                context_length=128000,
                max_concurrency=10,
                default_timeout=60,
            ),
            _EMBEDDING: ModelDefinition(
                id="Qwen3-Embedding-8B",
                provider="ovh",
                kind="embedding",
                dimensions=1024,
                context_length=32768,
                max_concurrency=50,
                default_timeout=60,
            ),
            _RERANKER: ModelDefinition(
                id="rerank-2.5",
                provider="voyage",
                kind="reranker",
                context_length=32000,
                max_concurrency=5,
                default_timeout=30,
            ),
        },
        roles={
            "primary": RoleBinding(deployment=_QWEN_THINKING, open=True),
            "artifact_builder": RoleBinding(deployment=_QWEN_INSTRUCT, open=True),
            "sub_agent": RoleBinding(deployment=_QWEN_INSTRUCT),  # pinned
            "embedding": RoleBinding(deployment=_EMBEDDING),  # pinned, kind=embedding
            "reranker": RoleBinding(deployment=_RERANKER),  # pinned, kind=reranker
        },
    )


def test_ac5_local_provider_down_excludes_its_deployments_both_directions():
    """AC-5 — with slm_local down, qwen* are absent and available cloud llms are present."""
    cfg = _config()
    availability = {
        "slm_local": False,
        "anthropic": True,
        "openai": True,
        "ovh": True,
        "voyage": True,
    }
    candidates = set(role_candidates("primary", cfg, availability))
    assert candidates == {_CLAUDE_SONNET, _CLAUDE_HAIKU, _GPT_MINI}
    # Both directions: no dead key leaked, no live key dropped, no wrong-kind key ever appears.
    assert _QWEN_THINKING not in candidates
    assert _QWEN_INSTRUCT not in candidates
    assert _EMBEDDING not in candidates
    assert _RERANKER not in candidates


def test_ac5_all_providers_up_returns_every_llm_deployment():
    """With every provider available, all five llm-kind deployments are candidates."""
    cfg = _config()
    availability = dict.fromkeys(cfg.providers, True)
    assert set(role_candidates("primary", cfg, availability)) == {
        _QWEN_THINKING,
        _QWEN_INSTRUCT,
        _CLAUDE_SONNET,
        _CLAUDE_HAIKU,
        _GPT_MINI,
    }


def test_ac5_all_providers_down_returns_empty():
    """With every provider unavailable, the candidate list is empty, not an error."""
    cfg = _config()
    availability = dict.fromkeys(cfg.providers, False)
    assert role_candidates("primary", cfg, availability) == []


def test_open_role_artifact_builder_gets_the_same_llm_candidate_set():
    """artifact_builder is open too — its candidate set is the same llm-kind filter."""
    cfg = _config()
    availability = {
        "slm_local": True,
        "anthropic": True,
        "openai": True,
        "ovh": True,
        "voyage": True,
    }
    assert set(role_candidates("artifact_builder", cfg, availability)) == {
        _QWEN_THINKING,
        _QWEN_INSTRUCT,
        _CLAUDE_SONNET,
        _CLAUDE_HAIKU,
        _GPT_MINI,
    }


@pytest.mark.parametrize("role", ["sub_agent", "embedding", "reranker", "vision", "unknown_role"])
def test_pinned_or_unbound_role_never_gets_candidates(role):
    """§6: kind-compatible ∩ open — a pinned (or unbound) role always returns []."""
    cfg = _config()
    availability = dict.fromkeys(cfg.providers, True)
    assert role_candidates(role, cfg, availability) == []


def test_missing_provider_in_availability_map_treated_as_unavailable():
    """A provider absent from the availability map (e.g. a check that failed to run) fails closed."""
    cfg = _config()
    assert role_candidates("primary", cfg, {}) == []
