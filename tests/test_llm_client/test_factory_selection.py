"""Selection-path dispatch + guardrail (ADR-0121 §4/§6, FRE-917 — AC-4).

Exercises get_llm_client's real selection resolution against the live catalog
(no mocking of the resolution seam) — the direct proof that:
  * an open role honours a valid, kind-compatible selection (primary → cloud);
  * a non-catalog or wrong-kind selection for an open role falls back to the
    role default (AC-4c), never an empty/arbitrary model;
  * a pinned writer role IGNORES a selection by ANY route, including an explicit
    selection_key — the "by any route" half of AC-4 and the closed second door.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_agent.config import load_model_config
from personal_agent.config.profile import _current_profile
from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client.client import LocalLLMClient
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient


@pytest.fixture(autouse=True)
def _reset_context() -> Iterator[None]:
    """No active profile or selection leaks between tests in this module."""
    p_token = _current_profile.set(None)
    s_token = _current_selection.set({})
    try:
        yield
    finally:
        _current_profile.reset(p_token)
        _current_selection.reset(s_token)


class TestPrimarySelectionDispatch:
    """primary is open: a valid selection is honoured; a bad one falls back."""

    def test_valid_cloud_selection_honoured(self) -> None:
        """selection_key=claude_sonnet (open + kind=llm) → LiteLLMClient(claude_sonnet)."""
        client = get_llm_client("primary", selection_key="claude_sonnet")

        assert isinstance(client, LiteLLMClient)
        assert client.budget_role == "main_inference"
        assert client.model_id == load_model_config().models["claude_sonnet"].id

    def test_selection_via_contextvar_honoured(self) -> None:
        """The per-turn selection context is consulted when no explicit key is passed."""
        set_current_selection({"primary": "claude_sonnet"})

        client = get_llm_client("primary")

        assert isinstance(client, LiteLLMClient)
        assert client.model_id == load_model_config().models["claude_sonnet"].id

    def test_noncatalog_selection_falls_back_to_default(self) -> None:
        """AC-4c — a non-catalog key for primary → the local default, not empty/arbitrary."""
        client = get_llm_client("primary", selection_key="no_such_model_xyz")

        # primary's default (qwen3.6-35b-thinking) is local.
        assert isinstance(client, LocalLLMClient)

    def test_wrong_kind_selection_falls_back_to_default(self) -> None:
        """A wrong-kind catalog key (embedding) for primary → the local default."""
        client = get_llm_client("primary", selection_key="embedding")

        assert isinstance(client, LocalLLMClient)


class TestPinnedRoleGuardrail:
    """AC-4 'by any route': a pinned role never honours a selection."""

    def test_pinned_role_ignores_explicit_selection_key(self) -> None:
        """entity_extraction (pinned) resolves to its default even with a selection_key."""
        client = get_llm_client("entity_extraction", selection_key="claude_sonnet")

        assert isinstance(client, LiteLLMClient)
        assert client.budget_role == "entity_extraction"
        # Its configured default (gpt-5.4-mini), NOT the injected claude_sonnet.
        assert client.model_id == load_model_config().models["gpt-5.4-mini"].id

    def test_pinned_role_ignores_selection_context(self) -> None:
        """A selection context entry for a pinned role is structurally ignored."""
        set_current_selection({"entity_extraction": "claude_sonnet"})

        client = get_llm_client("entity_extraction")

        assert isinstance(client, LiteLLMClient)
        assert client.model_id == load_model_config().models["gpt-5.4-mini"].id
