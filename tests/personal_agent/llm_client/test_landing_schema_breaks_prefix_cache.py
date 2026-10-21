"""FRE-1639 — which dialects lose the prompt cache when the landing carries ``response_format``.

OpenAI renders the schema into the prompt ahead of the history, so the landing's prefix
differs from its rounds' from the first token. A sampling-grammar provider (llama.cpp,
OVH vLLM) does not.
"""

from __future__ import annotations

from personal_agent.llm_client.models import (
    LANDING_SCHEMA_BREAKS_PREFIX_CACHE,
    Dialect,
    landing_schema_breaks_prefix_cache,
)


class TestLandingSchemaBreaksPrefixCacheTable:
    def test_every_dialect_declares_a_value(self) -> None:
        assert set(LANDING_SCHEMA_BREAKS_PREFIX_CACHE) == set(Dialect)

    def test_only_openai_breaks_the_prefix(self) -> None:
        assert landing_schema_breaks_prefix_cache(Dialect.OPENAI_GPT5) is True
        assert landing_schema_breaks_prefix_cache(Dialect.LLAMACPP_QWEN) is False
        assert landing_schema_breaks_prefix_cache(Dialect.OVH_QWEN) is False
        assert landing_schema_breaks_prefix_cache(Dialect.ANTHROPIC_ADAPTIVE) is False
        assert landing_schema_breaks_prefix_cache(Dialect.ANTHROPIC_BUDGET) is False

    def test_unresolved_dialect_declares_false(self) -> None:
        """An unresolved dialect already takes the prose landing, so it sends no schema."""
        assert landing_schema_breaks_prefix_cache(None) is False
