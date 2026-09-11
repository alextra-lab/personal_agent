"""ADR-0149 D6 — the forced-synthesis call keeps its tools and pins ``tool_choice="none"``.

Dropping the ``tools`` array from a call re-renders the whole prompt prefix, so the
cached prefix is lost. Measured three times against live backends: by the adr seat
and again by master on the local llama.cpp backend (4 of 4,191 prefilled with tools
retained, 3,915 of 3,915 with them dropped), and by FRE-1482 against the two cloud
dialects (OpenAI 5,504 of 5,863 cached retained vs 0 dropped; OVH 0.85 s retained vs
5.90 s dropped, that gateway declaring no cache metrics).

Which form a provider gets is **declared**, not discovered at runtime — the same
representation ADR-0145 D3 uses for every other provider quirk.
"""

from __future__ import annotations

import structlog.testing

from personal_agent.llm_client.models import (
    SYNTHESIS_RETAINS_TOOLS,
    Dialect,
    ModelDefinition,
    ModelKind,
    ModeSpec,
    Placement,
    synthesis_retains_tools,
)
from personal_agent.llm_client.types import ModelRole


class TestSynthesisRetainsToolsTable:
    """The capability is a per-dialect declaration beside the other dialect tables."""

    def test_every_dialect_declares_a_value(self) -> None:
        """A dialect missing from the table would silently take the None default."""
        assert set(SYNTHESIS_RETAINS_TOOLS) == set(Dialect)

    def test_measured_dialects_retain_tools(self) -> None:
        """Every dialect measured so far declares True (ADR-0149 D6, FRE-1482 probe)."""
        assert synthesis_retains_tools(Dialect.LLAMACPP_QWEN) is True
        assert synthesis_retains_tools(Dialect.ANTHROPIC_ADAPTIVE) is True
        assert synthesis_retains_tools(Dialect.ANTHROPIC_BUDGET) is True
        assert synthesis_retains_tools(Dialect.OVH_QWEN) is True
        assert synthesis_retains_tools(Dialect.OPENAI_GPT5) is True

    def test_unresolved_dialect_retains_tools_and_warns(self) -> None:
        """``None`` takes the cache-preserving form and says so at WARNING.

        The safe direction: a client built outside the catalog keeps its cache
        rather than paying a re-prefill nobody measured.
        """
        with structlog.testing.capture_logs() as logs:
            assert synthesis_retains_tools(None) is True

        events = [entry["event"] for entry in logs]
        assert "synthesis_dialect_unresolved" in events
        assert logs[0]["log_level"] == "warning"

    def test_a_declared_dialect_does_not_warn(self) -> None:
        """Seeded negative for the warning above — a declared value is silent."""
        with structlog.testing.capture_logs() as logs:
            synthesis_retains_tools(Dialect.LLAMACPP_QWEN)

        assert [entry["event"] for entry in logs] == []


def _definition(dialect: Dialect | None) -> ModelDefinition:
    return ModelDefinition(
        id="probe-model",
        provider="no_such_provider",
        kind=ModelKind.LLM,
        dialect=dialect,
        max_tokens=1024,
        context_length=8192,
        max_concurrency=1,
        default_timeout=90.0,
        modes={"default": ModeSpec()},
        default_mode="default",
    )


def _client(dialect: Dialect | None) -> object:
    from personal_agent.llm_client.litellm_client import LiteLLMClient

    definition = _definition(dialect)
    return LiteLLMClient(
        model_id=definition.id,
        provider=definition.provider or "",
        max_tokens=1024,
        budget_role="captains_log",
        placement=Placement.CLOUD,
        model_def=definition,
        model_key="probe",
    )


class TestDialectForRole:
    """The seam the sub-agent reads before it builds its forced-synthesis call."""

    def test_reports_the_models_own_dialect_override(self) -> None:
        client = _client(Dialect.OVH_QWEN)

        assert client.dialect_for_role(ModelRole.SUB_AGENT) is Dialect.OVH_QWEN

    def test_an_undeclared_deployment_reports_none(self) -> None:
        """No model override and no catalog provider entry — nothing declares one."""
        client = _client(None)

        assert client.dialect_for_role(ModelRole.SUB_AGENT) is None

    def test_the_pair_resolves_to_the_cache_preserving_form(self) -> None:
        """The two halves compose: an undeclared deployment still keeps its tools."""
        client = _client(None)

        assert synthesis_retains_tools(client.dialect_for_role(ModelRole.SUB_AGENT)) is True
