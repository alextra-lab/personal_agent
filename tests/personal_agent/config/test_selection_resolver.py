"""Guardrail resolver for session model selections (ADR-0121 §6 / FRE-917, AC-4).

The fail-closed resolver is the structural half of the writer guardrail: a
selection is honoured ONLY for an ``open`` role naming a valid, kind-compatible
catalog key; otherwise the role's configured binding default wins. Pinned roles
are never consulted, even when a selection row exists for them — the AC-4
discriminator (an implementation that reads selections for all roles and leans
only on "pinned roles have no candidates" passes a benign check and fails here).
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import (
    is_selectable_binding,
    resolve_selected_deployment,
)
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ProviderDefinition,
    RoleBinding,
)

# ── A minimal catalog covering the three model kinds + open/pinned roles ──────
_PRIMARY_DEFAULT = "qwen3.6-35b-thinking"
_OPEN_ALT = "claude_sonnet"  # a non-default, kind=llm, valid primary candidate
_EMBED_DEFAULT = "embedding"
_RERANK_DEFAULT = "reranker"


def _config() -> ModelConfig:
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(placement="local", max_concurrency=2),
            "anthropic": ProviderDefinition(placement="cloud", max_concurrency=50),
            "ovh": ProviderDefinition(placement="cloud", max_concurrency=50),
            "voyage": ProviderDefinition(placement="cloud", max_concurrency=50),
        },
        models={
            _PRIMARY_DEFAULT: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B",
                provider="slm_local",
                context_length=131072,
                max_concurrency=1,
                default_timeout=600,
            ),
            _OPEN_ALT: ModelDefinition(
                id="claude-sonnet-5",
                provider="anthropic",
                context_length=200000,
                max_concurrency=10,
                default_timeout=180,
            ),
            "gpt-5.4-mini": ModelDefinition(
                id="gpt-5.4-mini",
                provider="anthropic",
                context_length=128000,
                max_concurrency=10,
                default_timeout=60,
            ),
            _EMBED_DEFAULT: ModelDefinition(
                id="Qwen3-Embedding-8B",
                provider="ovh",
                kind="embedding",
                dimensions=1024,
                context_length=32768,
                max_concurrency=50,
                default_timeout=60,
            ),
            _RERANK_DEFAULT: ModelDefinition(
                id="rerank-2.5",
                provider="voyage",
                kind="reranker",
                context_length=32000,
                max_concurrency=5,
                default_timeout=30,
            ),
        },
        roles={
            "primary": RoleBinding(deployment=_PRIMARY_DEFAULT, open=True),
            "entity_extraction": RoleBinding(deployment="gpt-5.4-mini"),  # pinned
            "captains_log": RoleBinding(deployment=_OPEN_ALT),  # pinned
            "embedding": RoleBinding(deployment=_EMBED_DEFAULT),  # pinned, kind=embedding
            "reranker": RoleBinding(deployment=_RERANK_DEFAULT),  # pinned, kind=reranker
        },
    )


# ── AC-4a: a selection for a pinned role is never honoured (default wins) ──────
@pytest.mark.parametrize(
    ("role", "injected", "expected_default"),
    [
        ("entity_extraction", _OPEN_ALT, "gpt-5.4-mini"),
        ("captains_log", _OPEN_ALT, _OPEN_ALT),  # default is claude_sonnet; injection is a no-op
        ("embedding", _OPEN_ALT, _EMBED_DEFAULT),  # cross-kind injection ignored
        ("reranker", _PRIMARY_DEFAULT, _RERANK_DEFAULT),
        ("vision", _OPEN_ALT, "vision"),  # not even a bound role → default is the role name
    ],
)
def test_pinned_role_selection_ignored_resolves_to_default(role, injected, expected_default):
    """AC-4a — injecting a row for a pinned role leaves it on its configured default."""
    cfg = _config()
    assert resolve_selected_deployment(role, injected, cfg) == expected_default


# ── AC-4c: an open role with a bad/incompatible key falls back to its default ──
def test_open_role_noncatalog_key_falls_back_to_default():
    """AC-4c — a non-catalog key for an open role → the role default, never empty/arbitrary."""
    cfg = _config()
    assert resolve_selected_deployment("primary", "no_such_model_xyz", cfg) == _PRIMARY_DEFAULT


def test_open_role_wrong_kind_key_falls_back_to_default():
    """An open role handed a wrong-kind catalog key (embedding) falls back to default."""
    cfg = _config()
    assert resolve_selected_deployment("primary", _EMBED_DEFAULT, cfg) == _PRIMARY_DEFAULT


def test_open_role_valid_selection_is_honoured():
    """The one honour path: an open role + valid kind-compatible key → the selection."""
    cfg = _config()
    assert resolve_selected_deployment("primary", _OPEN_ALT, cfg) == _OPEN_ALT


def test_no_selection_resolves_to_default():
    """No selection (None) → the role's configured binding default."""
    cfg = _config()
    assert resolve_selected_deployment("primary", None, cfg) == _PRIMARY_DEFAULT


# ── The write-side predicate (AC-4b input): only open + valid + kind-compatible ─
@pytest.mark.parametrize(
    ("role", "key", "writable"),
    [
        ("primary", _OPEN_ALT, True),  # open + valid llm
        ("primary", _PRIMARY_DEFAULT, True),
        ("primary", "no_such_model_xyz", False),  # non-catalog
        ("primary", _EMBED_DEFAULT, False),  # wrong kind
        ("entity_extraction", "gpt-5.4-mini", False),  # pinned role
        ("captains_log", _OPEN_ALT, False),  # pinned role
        ("vision", _OPEN_ALT, False),  # unbound/pinned
    ],
)
def test_is_selectable_binding(role, key, writable):
    """AC-4b input — the write API rejects anything this predicate returns False for."""
    cfg = _config()
    assert is_selectable_binding(role, key, cfg) is writable
