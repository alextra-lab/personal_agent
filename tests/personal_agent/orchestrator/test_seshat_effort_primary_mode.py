"""FRE-1622 (ADR-0157 D5): the answering model runs the level's mode, else ``default`` (AC-4).

Both client paths: the session's primary through ``get_llm_client``, and the vision escalation
through ``get_llm_client_for_key``. The fifteen pickable deployments (nine, plus the three that FRE-1625 added, plus the one that
FRE-1631 added, plus the two that FRE-1680 added) are every ``kind: llm`` entry of the shipped catalog (``is_selectable_binding`` for the open ``primary`` role).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config.model_loader import load_model_config
from personal_agent.config.seshat_effort import (
    EFFORT_LEVELS,
    effort_spec,
    primary_mode_for,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.llm_client.factory import get_llm_client, get_llm_client_for_key
from personal_agent.llm_client.models import ModelKind
from tests.personal_agent.orchestrator.test_content_widening import (
    _make_minimal_ctx_with_block_content,
    _make_minimal_response,
    _restore_executor_tool_globals,  # noqa: F401 - autouse fixture
)

_CONFIG = load_model_config()
_PICKABLE = sorted(k for k, m in _CONFIG.models.items() if m.kind is ModelKind.LLM)
_FAST = {
    "qwen3.6-35b-thinking",
    "qwen3.8-flash-next",
    "qwen3.8-27b-mtplx",
    "qwen3.8-flash-next-mtplx",
    "gemma-4-26b-a4b",
    "qwen3.8-27b-ovh",
    "claude_sonnet",
    # FRE-1625: the three entries it added declare a fast mode (ADR-0157 D5).
    "gpt-6-luna",
    "gpt-6.1-sol",
    "claude_haiku_5_5",
    # FRE-1631: Sonnet 5.5 declares fast and deep (picker-only, bound to no role).
    "claude_sonnet_5_5",
    # FRE-1680: the two Scaleway entries declare a fast mode (ADR-0157 D5), no deep mode.
    "glm-5.3-scaleway",
    "deepseek-v4.1-flash-scaleway",
}
# FRE-1628: the first entry to declare a deep mode (ADR-0157 D5, effort high).
_DEEP = {"claude_haiku_5_5", "claude_sonnet_5_5"}
_DECLARED = {"fast": _FAST, "deep": _DEEP}


@pytest.fixture
def level(request: pytest.FixtureRequest) -> Iterator[str]:
    token = set_current_effort(effort_spec(request.param))
    try:
        yield request.param
    finally:
        reset_current_effort(token)


def _declared_mode(level: str, key: str) -> str | None:
    return level if key in _DECLARED.get(level, set()) else None


def _expected(level: str, key: str) -> str:
    return _declared_mode(level, key) or "default"


def test_there_are_fifteen_pickable_deployments() -> None:
    assert len(_PICKABLE) == 15
    assert _FAST <= set(_PICKABLE)


def test_only_the_listed_deployments_declare_a_deep_mode() -> None:
    assert {k for k in _PICKABLE if "deep" in _CONFIG.models[k].modes} == _DEEP


@pytest.mark.parametrize("key", _PICKABLE)
@pytest.mark.parametrize("level", list(EFFORT_LEVELS), indirect=True)
def test_the_ordinary_primary_path_requests_the_level_mode(level: str, key: str) -> None:
    client = get_llm_client(role_name="primary", selection_key=key, mode=primary_mode_for(key))
    assert client.model_key == key
    assert client.model_def.default_mode == _expected(level, key)


@pytest.mark.parametrize("key", _PICKABLE)
@pytest.mark.parametrize("level", list(EFFORT_LEVELS), indirect=True)
def test_the_vision_escalation_path_requests_the_level_mode(level: str, key: str) -> None:
    client = get_llm_client_for_key(key, budget_role="vision", mode=primary_mode_for(key))
    assert client.model_def.default_mode == _expected(level, key)


@pytest.mark.parametrize("key", _PICKABLE)
@pytest.mark.parametrize("level", list(EFFORT_LEVELS), indirect=True)
def test_an_undeclared_level_mode_requests_nothing(level: str, key: str) -> None:
    """The client keeps its own resolution, so a binding's mode is not overridden."""
    assert primary_mode_for(key) == _declared_mode(level, key)


@pytest.mark.parametrize("key", _PICKABLE)
def test_outside_a_turn_no_mode_is_requested(key: str) -> None:
    assert primary_mode_for(key) is None
    assert get_llm_client_for_key(key, budget_role="vision").model_def.default_mode == "default"


class TestStepLlmCallPassesTheMode:
    """The executor asks each branch for the mode of the deployment that answers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["fast"], indirect=True)
    async def test_the_ordinary_branch(self, level: str) -> None:
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.orchestrator.executor import step_llm_call
        from personal_agent.telemetry.trace import TraceContext

        ctx = _make_minimal_ctx_with_block_content()
        client = MagicMock(spec=LiteLLMClient)
        client.respond = AsyncMock(return_value=_make_minimal_response())
        client.model_def = _CONFIG.models["qwen3.8-flash-next"].model_copy(
            update={"default_mode": "fast"}
        )
        spy = MagicMock(return_value=client)
        session = MagicMock()
        session.add_message = AsyncMock()
        session.get_messages = AsyncMock(return_value=[])
        with (
            patch("personal_agent.orchestrator.skills.assemble_skill_catalog", return_value=""),
            patch("personal_agent.llm_client.factory.get_llm_client", spy),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]

        primary_key = _CONFIG.roles["primary"].deployment
        assert spy.call_args.kwargs["mode"] == _expected("fast", primary_key)
        assert ctx.primary_mode == "fast"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["fast"], indirect=True)
    async def test_the_vision_escalation_branch(self, level: str) -> None:
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.orchestrator.executor import step_llm_call
        from personal_agent.orchestrator.types import AttachmentRef
        from personal_agent.telemetry.trace import TraceContext

        ctx = _make_minimal_ctx_with_block_content()
        ctx.attachments = (  # type: ignore[attr-defined]
            AttachmentRef(
                artifact_id="abc-123",
                content_type="image/png",
                title="photo.png",
                r2_key="upload/user/GLOBAL/abc.png",
            ),
        )
        client = MagicMock(spec=LiteLLMClient)
        client.respond = AsyncMock(return_value=_make_minimal_response())
        client.model_def = _CONFIG.models["claude_sonnet"].model_copy(
            update={"default_mode": "fast"}
        )
        spy = MagicMock(return_value=client)
        session = MagicMock()
        session.add_message = AsyncMock()
        session.get_messages = AsyncMock(return_value=[])
        with (
            patch("personal_agent.orchestrator.skills.assemble_skill_catalog", return_value=""),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key", spy),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]

        escalated_key = spy.call_args.args[0]
        assert spy.call_args.kwargs["mode"] == _expected("fast", escalated_key)
        assert ctx.primary_mode == "fast"  # type: ignore[attr-defined]
