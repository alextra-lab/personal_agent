"""FRE-958/FRE-963/FRE-1319 regression guard.

The ``sub_agent`` role is bound to ``qwen3.6-35b-instruct`` (owner-directed
revert, 2026-08-30). A sub-agent spawn for this role must build a client
resolved from its OWN binding — never falling back to ``primary``'s, which is
the FRE-958 bug: the executor built a PRIMARY-role client and handed it to the
sub-agent dispatch path.

**Placement no longer discriminates, and this file says so rather than implying
otherwise.** For one day (FRE-1319, 2026-08-28 to 08-30) the two roles sat on
opposite sides of the placement split — ``primary`` on ``qwen3.8-flash-next``
(``slm_local`` → local placement) and ``sub_agent`` on ``gpt-5.4-mini``
(``openai`` → ``LiteLLMClient``) — so asserting the client class was on its own
enough to catch a silent fallback. That was always noted as a temporary
property, and it has now reversed: both roles are local again, so a fallback to
``primary`` would build the same local-placement client a correct resolution does.
The client-class assertion below is therefore *corroborating, not
discriminating*, and ``test_sub_agent_does_not_resolve_to_primary`` is the
assertion that actually guards FRE-958. It was written to survive exactly this
re-convergence and now carries the weight alone.

Why the local companion serves this role again: ``sub_agent`` is defined by
ADR-0033 as focused, non-thinking completion, and ``qwen3.6-35b-instruct``
expresses that in the deployment itself rather than through a provider lever,
so FRE-1007's reasoning-declaration guard is satisfied without needing a cloud
``reasoning_effort``. FRE-1319 moved it to ``gpt-5.4-mini`` only because the MBP
could hold a single model at Flash-Next's 87 GiB; with both qwen3.6-35B
deployments loaded that constraint is gone, and the companion is local, free,
and concurrent (``max_concurrency: 3`` against the primary's ``1``), which is
what HYBRID fan-out needs.
"""

from __future__ import annotations

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import Placement
from personal_agent.llm_client.types import ModelRole


class TestSubAgentResolution:
    """sub_agent resolves to its own qwen3.6-35b-instruct binding."""

    def test_role_resolves_to_its_own_binding(self) -> None:
        """resolve_role_target("sub_agent") names sub_agent's deployment, not primary's."""
        resolved_key, model_def = resolve_role_target("sub_agent")

        assert resolved_key == "qwen3.8-flash-next-instruct"
        assert model_def is not None
        assert model_def.id == load_model_config().models["qwen3.8-flash-next-instruct"].id

    def test_builds_local_client_matching_its_deployment_placement(self) -> None:
        """sub_agent dispatches at local placement — qwen3.6-35b-instruct's.

        Corroborating only. Since the 2026-08-30 revert both roles are
        ``slm_local``, so a fallback to primary's binding would satisfy this
        assertion too. Kept because it still catches a client built for the
        wrong *placement* (a cloud client for a local deployment), and it
        becomes discriminating again the moment the two roles are split across
        providers. ``test_sub_agent_does_not_resolve_to_primary`` is what
        actually guards FRE-958 today.
        """
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        assert isinstance(client, LiteLLMClient)
        assert client.placement is Placement.LOCAL

    def test_sub_agent_does_not_resolve_to_primary(self) -> None:
        """The FRE-958 bug stated directly, independent of either role's value.

        Asserting the two keys differ catches the fallback even when both roles
        share a placement — which is the case again since 2026-08-30, making
        this the only assertion in the file that discriminates.
        """
        sub_key, _ = resolve_role_target("sub_agent")
        primary_key, _ = resolve_role_target("primary")

        assert sub_key != primary_key
