"""``sub_agent.defaults_by_primary`` seed — ADR-0121 Addendum A step 1 (FRE-965).

Step 1 only adds the map's data structure and seeds it for the current
``kind: llm`` deployments; the enforcing guard (must-define-on-add, dangling
value) is step 2 (FRE-966) and the resolver that reads the map is step 3
(FRE-967). This module proves the substrate: the real
``config/model_roles.yaml`` carries a total, well-formed map through the real
loader, and the migration window holds — the flat ``deployment``/``open``
binding sub_agent resolves through today is untouched.
"""

from __future__ import annotations

from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client.models import ModelKind

_QWEN_FLASH = "qwen3.8-flash-next"  # FRE-1317: the bound primary since 2026-08-28
_QWEN_THINKING = "qwen3.6-35b-thinking"  # retained in the catalog so reverting stays one line
_QWEN_INSTRUCT = "qwen3.6-35b-instruct"
_CLAUDE_SONNET = "claude_sonnet"
_CLAUDE_HAIKU = "claude_haiku"
_GPT_MINI = "gpt-5.4-mini"
_QWEN_27B_OVH = "qwen3.6-27b-ovh"

_EXPECTED_DEFAULTS_BY_PRIMARY = {
    # One entry per primary-eligible deployment. This key appeared twice until
    # 2026-08-30 — FRE-1317's line then FRE-1319's — and Python silently kept the
    # second, so the first was dead and the dict asserted one fewer mapping than
    # it appeared to. Deduplicated; the surviving value is FRE-1319's, which is
    # what config/model_roles.yaml actually carries.
    _QWEN_FLASH: _GPT_MINI,  # FRE-1319: on a Flash-Next primary the local companion is unloaded, so the cloud model expresses no-thinking
    _QWEN_THINKING: _QWEN_INSTRUCT,  # ADR-0121 Addendum A example; durable form of FRE-963
    _QWEN_INSTRUCT: _QWEN_INSTRUCT,  # self-pair, no cheaper local companion
    _CLAUDE_SONNET: _CLAUDE_SONNET,  # ADR-0121 Addendum A example
    _CLAUDE_HAIKU: _CLAUDE_HAIKU,  # self-pair, already the cheap Anthropic tier
    _GPT_MINI: _GPT_MINI,  # self-pair, no cheaper GPT tier in the catalog
    _QWEN_27B_OVH: _QWEN_27B_OVH,  # self-pair, no cheaper OVH-hosted companion
}


class TestDefaultsByPrimarySeededThroughTheRealLoader:
    """Load the real catalog + role matrix together, exactly as production does."""

    def test_sub_agent_defaults_by_primary_matches_expected_map(self) -> None:
        config = load_model_config()
        binding = config.roles["sub_agent"]
        assert binding.defaults_by_primary == _EXPECTED_DEFAULTS_BY_PRIMARY

    def test_every_kind_llm_deployment_has_an_entry(self) -> None:
        """AC slice (a): every current kind:llm deployment carries an entry."""
        config = load_model_config()
        binding = config.roles["sub_agent"]
        assert binding.defaults_by_primary is not None

        llm_deployments = {
            key for key, model in config.models.items() if model.kind is ModelKind.LLM
        }
        assert llm_deployments == set(binding.defaults_by_primary)

    def test_every_value_names_an_existing_kind_llm_deployment(self) -> None:
        config = load_model_config()
        binding = config.roles["sub_agent"]
        assert binding.defaults_by_primary is not None

        for primary, sub in binding.defaults_by_primary.items():
            model = config.models.get(sub)
            assert model is not None, f"{primary!r} pairs to undefined deployment {sub!r}"
            assert model.kind is ModelKind.LLM, f"{primary!r} pairs to non-llm deployment {sub!r}"

    def test_qwen_thinking_pairs_to_qwen_instruct(self) -> None:
        """AC slice (c): seeded value matches the durable intent of the FRE-963 stopgap."""
        config = load_model_config()
        assert config.roles["sub_agent"].defaults_by_primary[_QWEN_THINKING] == _QWEN_INSTRUCT


class TestMigrationWindowLeavesFlatBindingOperative:
    """The flat binding must keep resolving exactly as today (no intermediate gap)."""

    def test_deployment_and_open_are_unchanged(self) -> None:
        """The flat binding still resolves; the value has moved twice, FRE-967 has not yet cut over.

        FRE-1319 moved it to gpt-5.4-mini on 2026-08-28 because the MBP held one
        model at Flash-Next's 87 GiB; the owner-directed 2026-08-30 revert to the
        qwen3.6-35b pair loads both deployments again, so it is back on the local
        instruct companion. What this test guards is unchanged and is not the
        value: `deployment` is still the field that actually resolves, and
        `defaults_by_primary` is still substrate-only until FRE-967 cuts the
        resolver over. A binding that silently stopped resolving through
        `deployment` during that window is the gap this catches.
        """
        config = load_model_config()
        binding = config.roles["sub_agent"]
        assert binding.deployment == _QWEN_INSTRUCT
        assert binding.open is True
