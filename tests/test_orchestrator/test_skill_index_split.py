"""Tests for the ADR-0081 D4 skill-index split (FRE-431).

Verifies that the skill block is partitioned at its volatility seam:
  * STABLE  — compact index + <skill_index_directive> → cached prefix
  * VOLATILE — selected bodies + <skill_usage_directives> → volatile tail

The cache-relevant invariant is that the static prefix is byte-identical
regardless of how many skill bodies are selected, so its hash is stable across
turns. These tests drive ``step_llm_call`` and inspect the assembled
``system_prompt`` plus the ``PromptIdentity`` passed to the LLM client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_INDEX_MARKER = "## INDEX_SENTINEL"
_BODY_MARKER = "## BODY_SENTINEL"
_INDEX_DIRECTIVE = "<skill_index_directive>"


@pytest.fixture(autouse=True)
def _reset_executor_tool_registry() -> object:
    """Reset the executor's module-level ``_tool_registry`` cache around each test.

    These tests patch ``executor.get_default_registry`` to a MagicMock, and
    ``step_llm_call`` caches its return into the module global ``_tool_registry``
    (``executor.py`` ~line 2157). ``patch()`` restores the *function*, not the
    assigned global, so without this reset the MagicMock leaks into later tests
    and silently empties their tool set. Reset before and after for isolation.
    """
    import personal_agent.orchestrator.executor as _ex

    _ex._tool_registry = None
    yield
    _ex._tool_registry = None


def _make_minimal_ctx() -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    return ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "hello"}],
    )


def _make_mock_llm_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.respond = AsyncMock(
        return_value={
            "content": "I understand.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    mock_client.model_configs = {}
    return mock_client


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    routing_mode: str,
    index_text: str,
    body_text: str,
) -> tuple[str, object]:
    """Run step_llm_call once; return (system_prompt, prompt_identity).

    Pinned to the D1/D4 head layout (cache_frozen_layout_enabled=False): these
    tests verify that the skill index and bodies land in the correct positions
    within system_prompt. Under the frozen layout (default True since FRE-440)
    volatile bodies ride the user turn, which is separately verified by
    test_frozen_layout.py. Pin flag-off here so the D4 system_prompt ordering
    invariants remain testable.
    """
    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "cache_frozen_layout_enabled", False)
    monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
    monkeypatch.setattr(settings, "skill_routing_mode", routing_mode)
    monkeypatch.setattr(settings, "skill_routing_model_key", "")
    monkeypatch.setattr(settings, "skill_nudge_enabled", True)

    ctx = _make_minimal_ctx()
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client()
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index",
            return_value=index_text,
        ),
        patch(
            "personal_agent.orchestrator.skills.get_skill_block",
            return_value=body_text,
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value=_INDEX_DIRECTIVE,
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch(
            "personal_agent.llm_client.factory.get_llm_client",
            return_value=mock_llm,
        ),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    call_kwargs = mock_llm.respond.call_args.kwargs
    system_prompt = call_kwargs.get("system_prompt", "") or ""
    prompt_identity = call_kwargs.get("prompt_identity")
    return system_prompt, prompt_identity


class TestSkillIndexSplit:
    """ADR-0081 D4: index → cached prefix, bodies → volatile tail."""

    @pytest.mark.asyncio
    async def test_index_precedes_bodies_in_hybrid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stable index must appear before volatile bodies in the assembled prompt."""
        system_prompt, _ = await _drive(
            monkeypatch,
            routing_mode="hybrid",
            index_text=_INDEX_MARKER,
            body_text=_BODY_MARKER,
        )
        assert _INDEX_MARKER in system_prompt
        assert _BODY_MARKER in system_prompt
        assert system_prompt.index(_INDEX_MARKER) < system_prompt.index(_BODY_MARKER), (
            "Stable skill index (cached) must precede volatile skill bodies (tail)."
        )

    @pytest.mark.asyncio
    async def test_index_directive_rides_cached_side(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """<skill_index_directive> (stable) sits with the index, before the bodies."""
        system_prompt, _ = await _drive(
            monkeypatch,
            routing_mode="hybrid",
            index_text=_INDEX_MARKER,
            body_text=_BODY_MARKER,
        )
        assert system_prompt.index(_INDEX_DIRECTIVE) < system_prompt.index(_BODY_MARKER)

    @pytest.mark.asyncio
    async def test_static_prefix_hash_identical_across_body_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache gate: 0-body and N-body turns produce the SAME static prefix hash.

        This is the separator/empty-fragment symmetry caution — the tail's
        presence must never alter the stable side's bytes.
        """
        _, id_no_bodies = await _drive(
            monkeypatch,
            routing_mode="hybrid",
            index_text=_INDEX_MARKER,
            body_text="",
        )
        _, id_with_bodies = await _drive(
            monkeypatch,
            routing_mode="hybrid",
            index_text=_INDEX_MARKER,
            body_text=_BODY_MARKER,
        )
        assert id_no_bodies is not None and id_with_bodies is not None
        assert id_no_bodies.static_prefix_hash == id_with_bodies.static_prefix_hash, (
            "Static prefix hash must be identical whether 0 or N bodies are selected."
        )

    @pytest.mark.asyncio
    async def test_hybrid_stamps_skill_index_component(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hybrid mode with an index → skill_index component stamped, plus skill_bodies."""
        _, identity = await _drive(
            monkeypatch,
            routing_mode="hybrid",
            index_text=_INDEX_MARKER,
            body_text=_BODY_MARKER,
        )
        assert identity is not None
        assert "skill_index" in identity.component_ids
        assert "skill_bodies" in identity.component_ids

    @pytest.mark.asyncio
    async def test_keyword_mode_does_not_stamp_skill_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keyword mode emits no index → skill_index must NOT be stamped (caution #2)."""
        _, identity = await _drive(
            monkeypatch,
            routing_mode="keyword",
            index_text="",  # keyword mode never calls assemble_skill_index anyway
            body_text=_BODY_MARKER,
        )
        assert identity is not None
        assert "skill_index" not in identity.component_ids, (
            "_skill_index_present must reflect actual index presence, not bodies."
        )
        assert "skill_bodies" in identity.component_ids
