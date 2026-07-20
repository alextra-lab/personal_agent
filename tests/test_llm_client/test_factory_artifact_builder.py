"""AC-8 regression guard (ADR-0119, superseded by ADR-0121; FRE-879, FRE-920).

A local-profile artifact build must stay on the local model, never silently cross to
cloud Haiku — that was AC-8's original shape, when Path/ExecutionProfile decided
artifact_builder's model. ADR-0121 T5 (FRE-920) removed Path: artifact_builder is
now a single Layer-3 binding (``config/model_roles.yaml``) with no profile
involvement at all — pinned to ``claude_haiku`` (owner-decided 2026-07-20, preserving
what the (removed) cloud profile provided). The regression this guards against is
unchanged in spirit: artifact_builder must never silently resolve to the wrong model.
"""

from __future__ import annotations

from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient


class TestArtifactBuilderResolution:
    """artifact_builder resolves to its pinned binding, with no profile involved."""

    def test_resolves_to_claude_haiku_cloud_client(self) -> None:
        """artifact_builder dispatches to LiteLLMClient(Haiku) — the binding default."""
        client = get_llm_client(role_name="artifact_builder")

        assert isinstance(client, LiteLLMClient)
        assert client.budget_role == "artifact_builder"

        from personal_agent.config import load_model_config

        expected_model_id = load_model_config().models["claude_haiku"].id
        assert client.model_id == expected_model_id
