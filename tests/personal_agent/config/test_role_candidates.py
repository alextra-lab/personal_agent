"""``role_candidates`` — the picker's candidate-set logic (ADR-0121 §3/§6, AC-5, FRE-918).

AC-5 requires the candidate list to equal exactly {catalog deployments where
kind matches the role} minus deployments whose provider is unavailable —
asserted in both directions, so these tests use full set-equality rather than
single-membership checks.

FRE-1415 layers a second dimension on top: for LOCAL deployments, ``model.id``
(not the catalog key) must also be confirmed served. ``_FLASH``/``_FLASH_INSTRUCT``
below mirror the real catalog's ``qwen3.8-flash-next``/``-instruct`` pair — two
keys sharing one served id, differing only in ``disable_thinking`` — so AC-2
is exercised inside the same fixture every other test here already uses.
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
_FLASH = "qwen3.8-flash-next"  # slm_local, llm — shares an id with _FLASH_INSTRUCT
_FLASH_INSTRUCT = "qwen3.8-flash-next-instruct"  # slm_local, llm — same id as _FLASH
_CLAUDE_SONNET = "claude_sonnet"  # anthropic, llm
_CLAUDE_HAIKU = "claude_haiku"  # anthropic, llm
_GPT_MINI = "gpt-5.4-mini"  # openai, llm
_EMBEDDING = "embedding"  # ovh, kind=embedding
_RERANKER = "reranker"  # voyage, kind=reranker

_QWEN_THINKING_ID = "unsloth/qwen3.6-35-A3B"
_QWEN_INSTRUCT_ID = "unsloth/qwen3.6-35-A3B-subagent"
_FLASH_ID = "unsloth/qwen3.8-flash-next"  # served by both _FLASH and _FLASH_INSTRUCT

#: Every local id the fixture declares — the served-ids map to hand
#: ``role_candidates`` when a test wants "the local host genuinely serves
#: everything", isolating the provider-availability dimension from the
#: served-id dimension.
_ALL_LOCAL_SERVED = {"slm_local": frozenset({_QWEN_THINKING_ID, _QWEN_INSTRUCT_ID, _FLASH_ID})}
_ALL_PROVIDERS_UP = {
    "slm_local": True,
    "anthropic": True,
    "openai": True,
    "ovh": True,
    "voyage": True,
}
_ALL_LLM_KEYS = {
    _QWEN_THINKING,
    _QWEN_INSTRUCT,
    _FLASH,
    _FLASH_INSTRUCT,
    _CLAUDE_SONNET,
    _CLAUDE_HAIKU,
    _GPT_MINI,
}


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
                id=_QWEN_THINKING_ID,
                provider="slm_local",
                context_length=131072,
                max_concurrency=1,
                default_timeout=600,
            ),
            _QWEN_INSTRUCT: ModelDefinition(
                id=_QWEN_INSTRUCT_ID,
                provider="slm_local",
                context_length=65536,
                max_concurrency=3,
                default_timeout=90,
            ),
            _FLASH: ModelDefinition(
                id=_FLASH_ID,
                provider="slm_local",
                context_length=131072,
                max_concurrency=3,
                default_timeout=600,
            ),
            _FLASH_INSTRUCT: ModelDefinition(
                id=_FLASH_ID,
                provider="slm_local",
                context_length=131072,
                max_concurrency=3,
                default_timeout=90,
                disable_thinking=True,
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
    availability = {**_ALL_PROVIDERS_UP, "slm_local": False}
    candidates = set(
        role_candidates("primary", cfg, availability, served_model_ids=_ALL_LOCAL_SERVED)
    )
    assert candidates == {_CLAUDE_SONNET, _CLAUDE_HAIKU, _GPT_MINI}
    # Both directions: no dead key leaked, no live key dropped, no wrong-kind key ever appears.
    assert _QWEN_THINKING not in candidates
    assert _QWEN_INSTRUCT not in candidates
    assert _FLASH not in candidates
    assert _FLASH_INSTRUCT not in candidates
    assert _EMBEDDING not in candidates
    assert _RERANKER not in candidates


def test_ac5_all_providers_up_returns_every_llm_deployment():
    """With every provider available and every local id served, all llm deployments are candidates."""
    cfg = _config()
    assert (
        set(role_candidates("primary", cfg, _ALL_PROVIDERS_UP, served_model_ids=_ALL_LOCAL_SERVED))
        == _ALL_LLM_KEYS
    )


def test_ac5_all_providers_down_returns_empty():
    """With every provider unavailable, the candidate list is empty, not an error."""
    cfg = _config()
    availability = dict.fromkeys(cfg.providers, False)
    assert role_candidates("primary", cfg, availability, served_model_ids=_ALL_LOCAL_SERVED) == []


def test_open_role_artifact_builder_gets_the_same_llm_candidate_set():
    """artifact_builder is open too — its candidate set is the same llm-kind filter."""
    cfg = _config()
    assert (
        set(
            role_candidates(
                "artifact_builder", cfg, _ALL_PROVIDERS_UP, served_model_ids=_ALL_LOCAL_SERVED
            )
        )
        == _ALL_LLM_KEYS
    )


@pytest.mark.parametrize("role", ["sub_agent", "embedding", "reranker", "vision", "unknown_role"])
def test_pinned_or_unbound_role_never_gets_candidates(role):
    """§6: kind-compatible ∩ open — a pinned (or unbound) role always returns []."""
    cfg = _config()
    assert role_candidates(role, cfg, _ALL_PROVIDERS_UP, served_model_ids=_ALL_LOCAL_SERVED) == []


def test_missing_provider_in_availability_map_treated_as_unavailable():
    """A provider absent from the availability map (e.g. a check that failed to run) fails closed."""
    cfg = _config()
    assert role_candidates("primary", cfg, {}, served_model_ids=_ALL_LOCAL_SERVED) == []


# ── FRE-1415: served-id gating for LOCAL deployments ──────────────────────────


def test_ac1_unserved_local_id_excluded_even_when_provider_up():
    """AC-1 — a local model whose id is not in the served set is excluded, provider notwithstanding."""
    cfg = _config()
    # Single-model host reality (2026-09-03): only the flash-next id is served.
    served = {"slm_local": frozenset({_FLASH_ID})}
    candidates = set(role_candidates("primary", cfg, _ALL_PROVIDERS_UP, served_model_ids=served))
    assert _QWEN_THINKING not in candidates
    assert _QWEN_INSTRUCT not in candidates
    assert candidates == {_FLASH, _FLASH_INSTRUCT, _CLAUDE_SONNET, _CLAUDE_HAIKU, _GPT_MINI}


def test_ac2_two_keys_sharing_one_served_id_are_both_available():
    """AC-2 — matching is on id, not catalog key: both flash-next variants stay available together."""
    cfg = _config()
    served = {"slm_local": frozenset({_FLASH_ID})}
    candidates = set(role_candidates("primary", cfg, _ALL_PROVIDERS_UP, served_model_ids=served))
    assert _FLASH in candidates
    assert _FLASH_INSTRUCT in candidates


def test_ac3_cloud_candidates_unaffected_by_served_model_ids():
    """AC-3 — an empty local served-set never removes a cloud candidate."""
    cfg = _config()
    candidates = set(
        role_candidates(
            "primary", cfg, _ALL_PROVIDERS_UP, served_model_ids={"slm_local": frozenset()}
        )
    )
    assert candidates == {_CLAUDE_SONNET, _CLAUDE_HAIKU, _GPT_MINI}


def test_ac4_and_ac5_local_provider_absent_from_served_map_fails_every_local_deployment_closed():
    """AC-4/AC-5 — a local provider missing from served_model_ids (probe never ran, or failed).

    Excludes every one of its deployments, never falling through to available.
    """
    cfg = _config()
    candidates = set(role_candidates("primary", cfg, _ALL_PROVIDERS_UP, served_model_ids={}))
    assert _QWEN_THINKING not in candidates
    assert _QWEN_INSTRUCT not in candidates
    assert _FLASH not in candidates
    assert _FLASH_INSTRUCT not in candidates
    assert candidates == {_CLAUDE_SONNET, _CLAUDE_HAIKU, _GPT_MINI}
