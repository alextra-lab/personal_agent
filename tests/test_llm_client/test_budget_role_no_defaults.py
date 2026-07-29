"""FRE-989 finding three / AC-2: one budget role, set explicitly, at every door.

Three entry points each carried their *own* default, and they disagreed:

- ``get_llm_client_for_key``   defaulted to ``skill_routing``  ($0.10 daily cap)
- ``LiteLLMClient.__init__``   defaulted to ``main_inference`` ($10.00 daily cap)
- ``budget_role_for``          fell back to ``main_inference``

So a call that omitted the budget role landed in a different bucket depending on
which door it came through — either the user-facing budget or a near-zero one,
arbitrarily. Three defaults are reconciled to **zero**: every door now requires
the lane to be named. That is stronger than picking one winner, because an
omission becomes a ``TypeError`` at import/call time rather than a plausible
wrong answer at billing time.
"""

from __future__ import annotations

import inspect

import pytest

from personal_agent.cost_gate import budget_role_for
from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.litellm_client import LiteLLMClient


def test_litellm_client_requires_budget_role() -> None:
    """Door 3 — direct construction — has no default to fall into."""
    param = inspect.signature(LiteLLMClient.__init__).parameters["budget_role"]
    assert param.default is inspect.Parameter.empty, (
        "LiteLLMClient must not default budget_role: a direct construction that "
        "forgets it would silently bill main_inference (FRE-989 finding three)."
    )


def test_get_llm_client_for_key_requires_budget_role() -> None:
    """Door 2 — trusted-config key — has no default to fall into."""
    param = inspect.signature(get_llm_client_for_key).parameters["budget_role"]
    assert param.default is inspect.Parameter.empty, (
        "get_llm_client_for_key must not default budget_role to skill_routing: "
        "that lane carries a $0.10 daily cap (FRE-989 finding three)."
    )


def test_omitting_budget_role_is_a_type_error() -> None:
    """The omission fails loudly rather than resolving to an arbitrary lane."""
    with pytest.raises(TypeError):
        LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")  # type: ignore[call-arg]


def test_budget_role_for_is_the_only_resolver_on_the_role_name_door() -> None:
    """Door 1 — role name — resolves through one total function, no fallback."""
    assert budget_role_for("artifact_builder") == "artifact_builder"
    assert budget_role_for("skill_routing") == "skill_routing"
