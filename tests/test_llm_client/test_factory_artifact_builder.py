"""AC-8 regression guard (ADR-0119, superseded by ADR-0121; FRE-879, FRE-920).

A local-profile artifact build must stay on the local model, never silently cross to
a cloud model — that was AC-8's original shape, when Path/ExecutionProfile decided
artifact_builder's model. ADR-0121 T5 (FRE-920) removed Path: artifact_builder is
now a single Layer-3 binding (``config/model_roles.yaml``) with no profile
involvement at all — pinned to ``claude_sonnet`` (owner-directed 2026-07-20 at the
master gate on FRE-920's PR; neither the removed ``local`` nor ``cloud`` profile's
prior value carries forward as-is). The regression this guards against is
unchanged in spirit: artifact_builder must never silently resolve to the wrong model.
"""

from __future__ import annotations

from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient


class TestArtifactBuilderResolution:
    """artifact_builder resolves to its pinned binding, with no profile involved."""

    def test_resolves_to_claude_sonnet_cloud_client(self) -> None:
        """artifact_builder dispatches to LiteLLMClient(Sonnet) — the binding default."""
        client = get_llm_client(role_name="artifact_builder")

        assert isinstance(client, LiteLLMClient)
        assert client.budget_role == "artifact_builder"

        from personal_agent.config import load_model_config

        expected_model_id = load_model_config().models["claude_sonnet"].id
        assert client.model_id == expected_model_id
