"""FRE-1549: the primary prompt carries the sequential-thinking discipline.

The skill in ``docs/skills/sequential-thinking.md`` reached the model only on a keyword
match, so the rule moved into the stable part of the primary system prompt. Covers the
ticket's three criteria that a unit test can decide:

- AC-1: the rendered primary system prompt carries the rule, including "never invent".
- AC-2: the planner system prompt is byte-identical to main's (ADR-0154 D7).
- AC-6: no tool and no governance entry is introduced.

AC-3 to AC-5 need live turns and are verified by master after deploy.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt
from personal_agent.orchestrator.prompts import REASONING_DISCIPLINE_PROMPT
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.skills import get_all_skills
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext

# Computed on unchanged origin/main (e0bb6e7b) before this ticket's first edit, with the
# round budgets stubbed (see ``_pin_round_budgets``) so an AGENT_SUB_AGENT_* override
# cannot move them. A legitimate planner edit moves them too: that is the point
# (ADR-0154 D7 — a planner prompt change needs a new probe run, then a new pin).
#
# Re-pinned for FRE-1564 (2026-10-10): the `researcher` and `general` descriptions changed and
# their tool lists grew. Before: with tools af31d592…, no tools b2da2a94…. The ADR-0154 D7
# probe on qwen3.8-flash-next is run for this change and posted on FRE-1564.
_MAIN_PLANNER_SHA256_WITH_TOOLS = "38fdec72f9b11945b720122ce302a27c4df7076b92eec7ded0f51eddc72a4719"
_MAIN_PLANNER_SHA256_NO_TOOLS = "253510e7b0916df330966a3103bc44cce816e8b347ff85f2f88b075ac7dc7e1a"
_PINNED_ROUNDS = {"quick": 3, "standard": 10, "thorough": 20}

# No skill keyword: nothing here can route the sequential-thinking skill.
_PLAIN_MESSAGE = "What do you know about Python?"
_OTHER_MESSAGE = "Which port does the gateway listen on?"


def _mock_client() -> AsyncMock:
    mock = AsyncMock()
    mock.model_configs = {}
    mock.respond.return_value = {
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
        "raw": {},
        "cost_usd": 0.0,
    }
    return mock


@pytest.fixture(autouse=True)
def _hybrid_skill_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the default routing mode so no test reaches a real skill-routing client."""
    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached tool registry after each test."""
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    yield
    _ex._tool_registry = saved_registry


async def _dispatched_call_kwargs(
    *,
    message: str,
    with_tools: bool,
    memory_context: list[dict] | None = None,
    turn_started_at: datetime | None = None,
) -> dict:
    """Drive the real pipeline and return the kwargs sent to the first LLM call."""
    import personal_agent.orchestrator.executor as _ex

    _ex._tool_registry = None
    mock_client = _mock_client()
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message=message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        memory_context=memory_context or [],
    )
    if turn_started_at is not None:
        ctx.turn_started_at = turn_started_at
    tool_defs = (
        [{"name": "web_search", "description": "d", "parameters": {"type": "object"}}]
        if with_tools
        else []
    )
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=tool_defs)),
        ),
    ):
        await execute_task_safe(ctx, session_manager)
    return mock_client.respond.call_args_list[0].kwargs


class TestPrimaryPromptCarriesTheRule:
    """AC-1: the rule is in the rendered primary system prompt, with no keyword."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_tools", [False, True])
    async def test_rule_is_in_the_system_prompt(self, with_tools: bool) -> None:
        kwargs = await _dispatched_call_kwargs(message=_PLAIN_MESSAGE, with_tools=with_tools)
        system_prompt = kwargs.get("system_prompt") or ""
        assert REASONING_DISCIPLINE_PROMPT in system_prompt

    @pytest.mark.asyncio
    async def test_rule_forbids_an_invented_false_start(self) -> None:
        kwargs = await _dispatched_call_kwargs(message=_PLAIN_MESSAGE, with_tools=False)
        system_prompt = kwargs.get("system_prompt") or ""
        assert "false start" in system_prompt
        assert "Never invent" in system_prompt

    def test_rule_keeps_short_turns_plain(self) -> None:
        """AC-4/AC-5 guard at the text level: the rule says no scaffold on a short turn."""
        assert "no scaffold" in REASONING_DISCIPLINE_PROMPT
        assert "stays invisible" in REASONING_DISCIPLINE_PROMPT

    @pytest.mark.asyncio
    async def test_rule_is_inside_the_cached_static_prefix(self) -> None:
        """The static-prefix hash sent with the call covers the whole system prompt, so
        the rule is in the cached prefix, and the volatile datetime block is not.
        """
        kwargs = await _dispatched_call_kwargs(message=_PLAIN_MESSAGE, with_tools=False)
        system_prompt = kwargs["system_prompt"]
        static_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
        assert kwargs["prompt_identity"].static_prefix_hash == static_hash
        assert REASONING_DISCIPLINE_PROMPT in system_prompt
        assert "## Current Date & Time" not in system_prompt

    @pytest.mark.asyncio
    async def test_system_prompt_does_not_vary_with_the_turn(self) -> None:
        """A different message, memory and clock leave the system prompt byte-identical,
        and the rule never rides the user turn.
        """
        memory = [{"type": "entity", "name": "Python", "entity_type": "Technology"}]
        first = await _dispatched_call_kwargs(
            message=_PLAIN_MESSAGE,
            with_tools=False,
            turn_started_at=datetime(2026, 10, 5, 8, 0, 0, tzinfo=UTC),
        )
        second = await _dispatched_call_kwargs(
            message=_OTHER_MESSAGE,
            with_tools=False,
            memory_context=memory,
            turn_started_at=datetime(2026, 10, 5, 9, 30, 0, tzinfo=UTC),
        )
        assert first["system_prompt"] == second["system_prompt"]
        for kwargs in (first, second):
            user_turns = [m["content"] for m in kwargs["messages"] if m.get("role") == "user"]
            assert all(REASONING_DISCIPLINE_PROMPT not in turn for turn in user_turns)


@pytest.fixture
def _pin_round_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix the per-level round budgets the planner prompt renders from settings."""
    import personal_agent.orchestrator.expansion_controller as ec

    monkeypatch.setattr(
        ec,
        "get_settings",
        lambda: SimpleNamespace(sub_agent_rounds_for=lambda level: _PINNED_ROUNDS[level]),
    )


@pytest.mark.usefixtures("_pin_round_budgets")
class TestPlannerPromptUnchanged:
    """AC-2: the planner system prompt is byte-identical to main's."""

    def test_planner_prompt_hash_equals_main_with_tools(self) -> None:
        prompt = _build_planner_system_prompt(["web_search", "run_python"])
        assert hashlib.sha256(prompt.encode()).hexdigest() == _MAIN_PLANNER_SHA256_WITH_TOOLS

    def test_planner_prompt_hash_equals_main_without_tools(self) -> None:
        prompt = _build_planner_system_prompt([])
        assert hashlib.sha256(prompt.encode()).hexdigest() == _MAIN_PLANNER_SHA256_NO_TOOLS

    def test_planner_prompt_does_not_carry_the_rule(self) -> None:
        assert REASONING_DISCIPLINE_PROMPT not in _build_planner_system_prompt(["web_search"])


class TestNoToolIntroduced:
    """AC-6: the rule adds no tool and no governance entry (ADR-0028:83)."""

    def test_skill_still_declares_no_tools(self) -> None:
        assert get_all_skills()["sequential-thinking"].tools == ()

    def test_governance_has_no_sequential_thinking_entry(self) -> None:
        governance_path = (
            Path(__file__).resolve().parents[3] / "config" / "governance" / "tools.yaml"
        )
        tools = yaml.safe_load(governance_path.read_text(encoding="utf-8"))["tools"]
        assert "sequential-thinking" not in tools
        assert "sequential_thinking" not in tools
