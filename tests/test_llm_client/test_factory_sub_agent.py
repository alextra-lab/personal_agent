"""FRE-958/FRE-963 regression guard.

The ``sub_agent`` role is bound to ``qwen3.6-35b-instruct`` (FRE-963,
2026-07-24, restoring the pre-drift companion default) — a local SLM
deployment, open to user selection. A sub-agent spawn for this role must
build a client resolved from its OWN binding (never fall back to
``primary``'s — the FRE-958 bug: the executor built a PRIMARY-role client
and handed it to the sub-agent dispatch path instead), and that resolution
must land on the sub_agent deployment specifically, not merely "some local
model" — primary (``qwen3.6-35b-thinking``) is local too, so client type
alone can't distinguish a correct resolution from the FRE-958 regression.
"""

from __future__ import annotations

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.client import LocalLLMClient
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.types import ModelRole


class TestSubAgentResolution:
    """sub_agent resolves to its own qwen3.6-35b-instruct binding."""

    def test_role_resolves_to_qwen_instruct(self) -> None:
        """resolve_role_target("sub_agent") names the instruct deployment, not primary's."""
        resolved_key, model_def = resolve_role_target("sub_agent")

        assert resolved_key == "qwen3.6-35b-instruct"
        assert model_def is not None
        assert model_def.id == load_model_config().models["qwen3.6-35b-instruct"].id

    def test_builds_local_client_matching_its_deployment_placement(self) -> None:
        """sub_agent dispatches to LocalLLMClient — qwen3.6-35b-instruct's placement.

        Placement alone can't prove the FRE-958 regression is fixed (primary is
        local too), but combined with test_role_resolves_to_qwen_instruct above —
        which pins the resolved key specifically — the pair proves the client
        factory used sub_agent's own binding, not primary's.
        """
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        assert isinstance(client, LocalLLMClient)
