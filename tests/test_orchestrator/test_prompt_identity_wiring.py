"""FRE-1008: proves executor.py wires the actual request bytes into PromptIdentity.

The unit tests in tests/personal_agent/llm_client/test_prompt_identity.py prove the
derive_orchestrator_prompt_identity helper itself computes correctly. They do not
prove step_llm_call passes it the right values — that is what these tests drive,
following the mock-llm_client.respond pattern from test_skill_index_split.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_executor_tool_registry() -> object:
    """See test_skill_index_split.py — same leak, same reset."""
    import personal_agent.orchestrator.executor as _ex

    _ex._tool_registry = None
    yield
    _ex._tool_registry = None


def _make_ctx(*, user_message: str, memory_context: list[dict[str, object]] | None) -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    return ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message=user_message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": user_message}],
        memory_context=memory_context,
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
    user_message: str,
    memory_section_text: str = "",
) -> object:
    """Run step_llm_call once; return the PromptIdentity passed to llm_client.respond."""
    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
    monkeypatch.setattr(settings, "skill_routing_mode", "keyword")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")
    monkeypatch.setattr(settings, "skill_nudge_enabled", False)

    memory_context = [{"type": "entity", "name": "placeholder"}] if memory_section_text else None
    ctx = _make_ctx(user_message=user_message, memory_context=memory_context)
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client()
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch(
            "personal_agent.orchestrator.skills.get_all_skills",
            return_value={},
        ),
        patch(
            "personal_agent.llm_client.factory.get_llm_client",
            return_value=mock_llm,
        ),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
        patch(
            "personal_agent.orchestrator.executor._render_memory_section_with_ids",
            return_value=(memory_section_text, ("placeholder",) if memory_section_text else ()),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    call_kwargs = mock_llm.respond.call_args.kwargs
    return call_kwargs.get("prompt_identity")


class TestPromptIdentityWiring:
    """FRE-1008: dynamic_hash must move when the actual request content moves."""

    @pytest.mark.asyncio
    async def test_dynamic_hash_differs_when_user_query_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity_a = await _drive(monkeypatch, user_message="what is the weather in Paris?")
        identity_b = await _drive(monkeypatch, user_message="summarize the last chapter")

        assert identity_a is not None and identity_b is not None
        assert identity_a.static_prefix_hash == identity_b.static_prefix_hash
        assert identity_a.dynamic_hash != identity_b.dynamic_hash

    @pytest.mark.asyncio
    async def test_dynamic_hash_differs_when_memory_context_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity_a = await _drive(
            monkeypatch, user_message="hello", memory_section_text="## Memory\nrecall set ONE"
        )
        identity_b = await _drive(
            monkeypatch, user_message="hello", memory_section_text="## Memory\nrecall set TWO"
        )

        assert identity_a is not None and identity_b is not None
        assert identity_a.static_prefix_hash == identity_b.static_prefix_hash
        assert identity_a.dynamic_hash != identity_b.dynamic_hash

    @pytest.mark.asyncio
    async def test_regression_old_behavior_would_collapse_both_hashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documents the FRE-1008 defect this file guards against: before this fix,
        static_prefix_hash always equaled dynamic_hash for orchestrator.primary
        because full_prompt was system_prompt (identical to static_prefix) — the
        volatile tail was never hashed at all.
        """
        identity = await _drive(monkeypatch, user_message="hello")
        assert identity is not None
        assert identity.static_prefix_hash != identity.dynamic_hash
