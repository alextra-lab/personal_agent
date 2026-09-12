"""ADR-0150 D1 — which dialects accept a constrained (`response_format`) landing call.

Mirrors ``test_synthesis_retains_tools.py``'s table-declaration pattern, with
the opposite unresolved-dialect default: an unknown dialect here returns
``False`` (the landing reports as text) rather than ``True``, because sending
a schema-shaped request to an unknown dialect risks a hard provider rejection,
while the text fallback only costs a cache miss.
"""

from __future__ import annotations

import structlog.testing

from personal_agent.llm_client.models import (
    LANDING_ACCEPTS_JSON_SCHEMA,
    Dialect,
    landing_accepts_json_schema,
)


class TestLandingAcceptsJsonSchemaTable:
    def test_every_dialect_declares_a_value(self) -> None:
        """A dialect missing from the table would silently take the None default."""
        assert set(LANDING_ACCEPTS_JSON_SCHEMA) == set(Dialect)

    def test_measured_and_provisional_dialects(self) -> None:
        assert landing_accepts_json_schema(Dialect.LLAMACPP_QWEN) is True
        assert landing_accepts_json_schema(Dialect.OPENAI_GPT5) is True
        assert landing_accepts_json_schema(Dialect.OVH_QWEN) is True

    def test_unprobed_anthropic_dialects_declare_false(self) -> None:
        assert landing_accepts_json_schema(Dialect.ANTHROPIC_ADAPTIVE) is False
        assert landing_accepts_json_schema(Dialect.ANTHROPIC_BUDGET) is False

    def test_unresolved_dialect_declares_false_and_warns(self) -> None:
        with structlog.testing.capture_logs() as logs:
            assert landing_accepts_json_schema(None) is False

        events = [entry["event"] for entry in logs]
        assert "landing_dialect_unresolved" in events
        assert logs[0]["log_level"] == "warning"

    def test_a_declared_dialect_does_not_warn(self) -> None:
        with structlog.testing.capture_logs() as logs:
            landing_accepts_json_schema(Dialect.LLAMACPP_QWEN)

        assert [entry["event"] for entry in logs] == []
