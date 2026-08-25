"""Grounding contract prompt changes (ADR-0138 D1/D2/D6, FRE-1283).

Covers this ticket's five acceptance criteria:

- AC-1: the "Do NOT say you have no memory." prohibition is gone from the assembled prompt.
- AC-2: the adjacent FRE-1150 wording it used to trail survives.
- AC-3: the recency enumeration no longer gates the web_search tool rule.
- AC-4: a citation-emission instruction is present, describes the FRE-1280 marker format,
  and a completion using that exact format round-trips through the real parser.
- AC-5 (no regression) is covered by the full suite, not this file.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.prompts import _TOOL_RULES, GROUNDING_CONTRACT_PROMPT
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext

_PROHIBITION = "Do NOT say you have no memory."
_MEMORY_MARKER = "## Your Memory Graph"
_MEMORY_CONTEXT = [
    {
        "type": "entity",
        "name": "Python",
        "entity_type": "Technology",
        "description": "A programming language the user works with daily",
        "mentions": 5,
    }
]


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
def _restore_executor_tool_globals() -> object:
    """Mirrors test_deployment_context_prompt.py's fixture: the executor lazily caches
    the tool registry on a module global, which would otherwise leak a mocked empty
    registry from one test into the next.
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    yield
    _ex._tool_registry = saved_registry


async def _dispatched_call_kwargs(*, with_tools: bool) -> dict:
    """Drive the real pipeline and return the kwargs actually sent to the LLM client.

    The frozen layout (ADR-0081 D2) inlines memory_section into the volatile *last user
    message*, not system_prompt — so a check for entity-section wording must look at
    ``messages``, while a check for an unconditional STATIC block like the grounding
    contract belongs in ``system_prompt``.
    """
    import personal_agent.orchestrator.executor as _ex

    _ex._tool_registry = None
    mock_client = _mock_client()
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="What do you know about Python?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        memory_context=_MEMORY_CONTEXT,
    )

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


async def _assembled_system_prompt(*, with_tools: bool) -> str:
    kwargs = await _dispatched_call_kwargs(with_tools=with_tools)
    return kwargs.get("system_prompt", "") or ""


async def _last_user_message(*, with_tools: bool) -> str:
    """The volatile last-user-message content, where memory_section actually rides."""
    kwargs = await _dispatched_call_kwargs(with_tools=with_tools)
    messages = kwargs.get("messages") or []
    return next(m["content"] for m in reversed(messages) if m.get("role") == "user")


class TestProhibitionDeletedFRE1150Survives:
    """AC-1 and AC-2, exercised through the real assembled prompt."""

    @pytest.mark.asyncio
    async def test_prohibition_absent_from_assembled_prompt(self) -> None:
        last_user_message = await _last_user_message(with_tools=False)
        assert _MEMORY_MARKER in last_user_message, "fixture assumption: memory did render"
        assert _PROHIBITION not in last_user_message

    @pytest.mark.asyncio
    async def test_fre_1150_wording_survives(self) -> None:
        last_user_message = await _last_user_message(with_tools=False)
        assert "not who you are speaking with" in last_user_message


class TestRecencyNoLongerGatesSearch:
    """AC-3: the recency enumeration is gone; the replacement is assertion-driven."""

    def test_recency_enumeration_removed(self) -> None:
        assert "current events, recent news, CVEs, product versions" not in _TOOL_RULES

    def test_web_search_rule_no_longer_recency_gated(self) -> None:
        assert "not gated on whether the topic" in _TOOL_RULES
        assert "web_search" in _TOOL_RULES

    def test_forecast_category_hints_survive(self) -> None:
        """Regression guard: the category-selection guidance must not be dropped."""
        assert "categories='it'" in _TOOL_RULES
        assert "'weather' for forecasts" in _TOOL_RULES


class TestCitationEmissionInstruction:
    """AC-4: the instruction is present, unconditional, and describes the real format."""

    @pytest.mark.asyncio
    async def test_grounding_contract_present_without_tools(self) -> None:
        system_prompt = await _assembled_system_prompt(with_tools=False)
        assert "Any claim you make about the world needs a source" in system_prompt

    @pytest.mark.asyncio
    async def test_grounding_contract_present_with_tools(self) -> None:
        """The citation obligation is not gated behind tool availability."""
        system_prompt = await _assembled_system_prompt(with_tools=True)
        assert "Any claim you make about the world needs a source" in system_prompt

    def test_admissible_source_set_matches_d2(self) -> None:
        for source in ("memory graph", "tool and web results", "documentation", "user's own words"):
            assert source in GROUNDING_CONTRACT_PROMPT

    def test_no_source_replacement_behaviour_documented(self) -> None:
        """D6's replacement: stating the absence of a source is correct, not forbidden."""
        assert "I don't have a source for that" in GROUNDING_CONTRACT_PROMPT
        assert _PROHIBITION not in GROUNDING_CONTRACT_PROMPT

    def test_instructed_marker_format_round_trips_through_the_real_parser(self) -> None:
        """The instruction must describe a format citations.py actually accepts.

        Builds a completion the way the instruction tells the model to build one —
        `<claim> [S{n}@{digest}]` — using a real registry-minted identifier, and
        confirms it parses with no format violations and resolves cleanly.
        """
        registry = SourceRegistry(turn_id="trace-fre1283-0001")
        registration = registry.register_tool_result(
            tool_name="web_search",
            arguments={"query": "tinned tuna France"},
            content="Ortiz packs bonito del norte in olive oil.",
        )
        source = registration.source
        assert source is not None

        completion = f"Ortiz packs bonito del norte in olive oil [{source.identifier}]."
        parse = parse_citations(completion)

        assert parse.violations == ()
        assert [span.identifier for span in parse.spans] == [source.identifier]

    def test_worked_example_in_the_instruction_segments_as_two_atomic_claims(self) -> None:
        """The worked example must not teach a bad segmentation.

        D1's own canonical example is ``Ortiz [S1] is better than Nardin [S2]`` — two
        distinct sourced claims in one comparison, each binding its own adjacent marker.
        The instruction must use exactly this shape, not attach a marker to a bare noun
        phrase (e.g. "Paris [S1]...") that is not itself an atomic claim.
        """
        example = "Ortiz [S1@a3f91c2b7d4e6f80] is better than Nardin [S2@c4d8e1f2a9b07653]"
        assert example in GROUNDING_CONTRACT_PROMPT

        parse = parse_citations(example)
        assert parse.violations == ()
        assert [(span.text, span.identifier) for span in parse.spans] == [
            ("Ortiz", "S1@a3f91c2b7d4e6f80"),
            ("is better than Nardin", "S2@c4d8e1f2a9b07653"),
        ]

    def test_exemptions_do_not_overclaim_beyond_d1(self) -> None:
        """D1 exempts code offered to run — not string literals/comments/docstrings
        presented as the answer — and only narrowly-evaluative judgement, not
        judgement in general. The instruction must not state the exemption more
        broadly than that (a broader exemption would license exactly the confabulation
        D1 exists to close).
        """
        assert "does not apply to code," not in GROUNDING_CONTRACT_PROMPT
        assert "code you're offering the user to run" in GROUNDING_CONTRACT_PROMPT
        assert "your own judgement" not in GROUNDING_CONTRACT_PROMPT
        assert "adds no new factual claim of its own" in GROUNDING_CONTRACT_PROMPT

    @pytest.mark.asyncio
    async def test_grounding_contract_appears_exactly_once(self) -> None:
        """Regression guard for the splice restructuring: no duplicate insertion."""
        system_prompt = await _assembled_system_prompt(with_tools=True)
        assert system_prompt.count("## Grounding") == 1

    @pytest.mark.asyncio
    async def test_grounding_contract_present_in_the_static_prefix_alongside_tool_rules(
        self,
    ) -> None:
        """Both STATIC blocks land in the same assembled prompt when tools are offered —
        the splice must not have silently dropped one or the other.
        """
        system_prompt = await _assembled_system_prompt(with_tools=True)
        assert "## Grounding" in system_prompt
        assert "You are a tool-using assistant" in system_prompt
