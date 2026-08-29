"""FRE-958/FRE-963/FRE-1319 regression guard.

The ``sub_agent`` role is bound to ``gpt-5.4-mini`` (FRE-1319, 2026-08-28). A
sub-agent spawn for this role must build a client resolved from its OWN binding
— never falling back to ``primary``'s, which is the FRE-958 bug: the executor
built a PRIMARY-role client and handed it to the sub-agent dispatch path.

**Placement now discriminates, and it did not before.** This file previously
carried an explicit caveat that client type alone could not prove FRE-958 fixed,
because ``sub_agent``'s ``qwen3.6-35b-instruct`` and ``primary``'s
``qwen3.6-35b-thinking`` were both local — a fallback to primary would have
produced the same ``LocalLLMClient`` a correct resolution does. Since FRE-1319
the two roles sit on opposite sides of the placement split: ``primary`` is
``qwen3.8-flash-next`` (``slm_local`` → ``LocalLLMClient``) and ``sub_agent`` is
``gpt-5.4-mini`` (``openai`` → ``LiteLLMClient``). A silent fallback to primary
now yields the wrong client class outright, so the placement assertion is a real
discriminator rather than a corroborating one. The key assertion below is kept
anyway: it survives any future re-convergence of the two placements.

Why ``gpt-5.4-mini`` serves this role (FRE-1319): ``sub_agent`` is defined by
ADR-0033 as focused, non-thinking completion, and the outgoing local companion
expressed that with ``disable_thinking`` — a local-dispatch field. On the cloud
path the equivalent lever is a verified ``reasoning_effort: none``, which this
deployment declares and which litellm confirms reaches the wire. The MBP holds
only one model at Flash-Next's 87 GiB, so the local companion is unloaded.
"""

from __future__ import annotations

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole


class TestSubAgentResolution:
    """sub_agent resolves to its own gpt-5.4-mini binding."""

    def test_role_resolves_to_its_own_binding(self) -> None:
        """resolve_role_target("sub_agent") names sub_agent's deployment, not primary's."""
        resolved_key, model_def = resolve_role_target("sub_agent")

        assert resolved_key == "gpt-5.4-mini"
        assert model_def is not None
        assert model_def.id == load_model_config().models["gpt-5.4-mini"].id

    def test_builds_cloud_client_matching_its_deployment_placement(self) -> None:
        """sub_agent dispatches to LiteLLMClient — gpt-5.4-mini's placement.

        Since FRE-1319 this is on its own sufficient to catch the FRE-958
        regression: ``primary`` is ``slm_local`` and would build a
        ``LocalLLMClient``, so a fallback to primary's binding fails this
        assertion rather than passing it by coincidence.
        """
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        assert isinstance(client, LiteLLMClient)

    def test_sub_agent_does_not_resolve_to_primary(self) -> None:
        """The FRE-958 bug stated directly, independent of either role's value.

        Asserting the two keys differ catches the fallback even if both roles are
        later rebound — including back onto the same placement, where the client
        type would stop discriminating again.
        """
        sub_key, _ = resolve_role_target("sub_agent")
        primary_key, _ = resolve_role_target("primary")

        assert sub_key != primary_key
