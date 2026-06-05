"""Tests for Phase B skill routing: read_skill tool, skill index, dedup, hints.

Verification criteria from the plan:
- read_skill("query-elasticsearch") returns body
- read_skill("nonexistent") returns structured error
- read_skill called twice → second call returns dedup marker
- skill index injected in system prompt with model_decided routing
- keyword body suppressed for already-loaded skill in hybrid mode
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.skills import assemble_skill_index, get_all_skills, get_skill_block
from personal_agent.tools.read_skill import read_skill_executor

# ---------------------------------------------------------------------------
# read_skill tool executor
# ---------------------------------------------------------------------------


class TestReadSkillExecutor:
    """Unit tests for read_skill_executor (stateless body lookup)."""

    @pytest.mark.asyncio
    async def test_known_skill_returns_body(self) -> None:
        """read_skill('query-elasticsearch') returns the skill body."""
        from personal_agent.telemetry.trace import TraceContext

        result = await read_skill_executor(
            name="query-elasticsearch",
            ctx=TraceContext.new_trace(),
        )
        assert result["status"] == "ok"
        assert result["skill_name"] == "query-elasticsearch"
        assert "agent-logs-" in result["body"]

    @pytest.mark.asyncio
    async def test_unknown_skill_returns_error(self) -> None:
        """read_skill('nonexistent') returns structured error with available list."""
        from personal_agent.telemetry.trace import TraceContext

        result = await read_skill_executor(
            name="nonexistent-skill-xyz",
            ctx=TraceContext.new_trace(),
        )
        assert result["status"] == "error"
        assert "nonexistent-skill-xyz" in result["hint"]
        assert "bash" in result["hint"]  # known skills listed

    @pytest.mark.asyncio
    async def test_unknown_skill_emits_missing_skill_requested_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-328 Phase 1: unknown skill emits structured `missing_skill_requested` event.

        Captures the structlog warning call by monkeypatching ``log.warning`` on
        the read_skill module; verifies the event name and field shape.
        """
        from personal_agent.telemetry.trace import TraceContext
        from personal_agent.tools import read_skill as read_skill_mod

        captured: list[tuple[str, dict[str, Any]]] = []

        def fake_warning(event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        monkeypatch.setattr(read_skill_mod.log, "warning", fake_warning)

        trace_ctx = TraceContext.new_trace()
        result = await read_skill_executor(
            name="nonexistent-skill-xyz",
            ctx=trace_ctx,
            session_id="session-abc",
        )

        assert result["status"] == "error"
        missing_events = [(e, kw) for e, kw in captured if e == "missing_skill_requested"]
        assert len(missing_events) == 1, f"captured: {captured}"
        _, kw = missing_events[0]
        assert kw["requested_name"] == "nonexistent-skill-xyz"
        assert kw["session_id"] == "session-abc"
        assert kw["trace_id"] == trace_ctx.trace_id
        assert isinstance(kw["available_skills"], list)
        assert len(kw["available_skills"]) > 0
        assert kw["known_count"] == len(kw["available_skills"])

    @pytest.mark.asyncio
    async def test_bash_skill_returns_body(self) -> None:
        """read_skill('bash') returns the bash skill doc body."""
        from personal_agent.telemetry.trace import TraceContext

        result = await read_skill_executor(name="bash", ctx=TraceContext.new_trace())
        assert result["status"] == "ok"
        assert "bash — Shell Command Executor" in result["body"]


# ---------------------------------------------------------------------------
# assemble_skill_index
# ---------------------------------------------------------------------------


class TestAssembleSkillIndex:
    """Unit tests for the compact skill index assembler."""

    def test_index_contains_all_skill_names(self) -> None:
        """Every loaded skill appears in the index."""
        index = assemble_skill_index()
        skills = get_all_skills()
        for name in skills:
            assert name in index, f"Skill '{name}' missing from index"

    def test_index_starts_with_header(self) -> None:
        """Index starts with the Available Skills header."""
        index = assemble_skill_index()
        assert "Available Skills" in index

    def test_index_entries_have_description(self) -> None:
        """Each entry has '- name: description' format."""
        index = assemble_skill_index()
        skills = get_all_skills()
        for name, skill in skills.items():
            assert f"- {name}:" in index
            # description should be present on the same line
            line = next((l for l in index.splitlines() if l.startswith(f"- {name}:")), None)
            assert line is not None
            assert skill.description[:20] in line

    def test_token_cap_truncates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A very small cap_tokens causes truncation."""
        import personal_agent.orchestrator.skills as skills_mod

        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", tmp_path)
        monkeypatch.setattr(skills_mod, "_cache", None)

        # Write 5 skills
        for i in range(5):
            (tmp_path / f"skill-{i}.md").write_text(
                f"---\nname: skill-{i}\ndescription: {'x' * 50}\nwhen_to_use: always\n---\nbody",
                encoding="utf-8",
            )

        # Cap at 2 tokens (8 chars) — should truncate after header
        tiny_index = assemble_skill_index(cap_tokens=2)
        # With only 8 chars budget and the header alone being much longer, result is empty
        assert tiny_index == "" or len(tiny_index) <= 8 + len(skills_mod._SKILL_INDEX_HEADER)

    def test_index_header_invites_capability_gap_calls(self) -> None:
        """FRE-328 follow-up: the header tells the model to propose new skills via read_skill."""
        import personal_agent.orchestrator.skills as skills_mod

        header = skills_mod._SKILL_INDEX_HEADER
        # Positive phrasing: action-first, no negation.
        assert "To propose a new skill" in header
        assert 'read_skill(name="your-desired-name")' in header
        assert "capability-gap signal" in header


# ---------------------------------------------------------------------------
# Dedup in _dispatch_tool_call
# ---------------------------------------------------------------------------


async def _dispatch_read_skill(
    skill_name: str,
    loaded_skills: set[str],
) -> dict[str, Any]:
    """Helper: call the shared dispatch_tool_call for read_skill with given loaded_skills."""
    from personal_agent.orchestrator.loop_gate import (
        GateDecision,
        GateResult,
        ToolCallState,
        ToolLoopPolicy,
    )
    from personal_agent.telemetry.trace import TraceContext

    gate_result = GateResult(
        decision=GateDecision.ALLOW,
        tool_name="read_skill",
        state_before=ToolCallState.IDLE,
        state_after=ToolCallState.IDLE,
        reason="test",
        consecutive_count=0,
        total_calls=1,
    )

    mock_tool_layer = MagicMock()
    mock_tool_layer.registry.get_tool = MagicMock(return_value=None)
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.output = {
        "status": "ok",
        "skill_name": skill_name,
        "body": f"body of {skill_name}",
    }
    mock_result.error = None
    mock_result.latency_ms = 5
    mock_tool_layer.execute_tool = AsyncMock(return_value=mock_result)

    with patch("personal_agent.orchestrator.skills.find_skills_for_tool", return_value=[]):
        from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

        return await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="read_skill",
            arguments={"name": skill_name},
            args_hash="abc",
            gate_result=gate_result,
            loop_policy=ToolLoopPolicy(),
            tool_layer=mock_tool_layer,
            trace_ctx=TraceContext.new_trace(),
            trace_id="test-trace",
            session_id="test",
            loaded_skills=loaded_skills,
        )


class TestReadSkillDedup:
    """read_skill dedup: second call returns marker instead of body."""

    @pytest.mark.asyncio
    async def test_first_call_executes(self) -> None:
        """First read_skill call executes the tool."""
        result = await _dispatch_read_skill("query-elasticsearch", loaded_skills=set())
        assert result["success"] is True
        output = json.loads(result["content"])
        assert "body of query-elasticsearch" in output.get("body", "")

    @pytest.mark.asyncio
    async def test_second_call_returns_dedup_marker(self) -> None:
        """Second call for already-loaded skill returns dedup marker."""
        result = await _dispatch_read_skill(
            "query-elasticsearch",
            loaded_skills={"query-elasticsearch"},
        )
        assert result["success"] is True
        output = json.loads(result["content"])
        assert "already loaded" in output["body"]
        assert "query-elasticsearch" in output["body"]

    @pytest.mark.asyncio
    async def test_loaded_skills_updated_after_first_call(self) -> None:
        """After a successful read_skill, the loaded_skills set is updated in place."""
        from personal_agent.orchestrator.loop_gate import (
            GateDecision,
            GateResult,
            ToolCallState,
            ToolLoopPolicy,
        )
        from personal_agent.telemetry.trace import TraceContext

        loaded_skills: set[str] = set()
        assert "bash" not in loaded_skills

        mock_layer = MagicMock()
        mock_layer.registry.get_tool = MagicMock(return_value=None)
        mock_res = MagicMock()
        mock_res.success = True
        mock_res.output = {"status": "ok", "skill_name": "bash", "body": "bash body"}
        mock_res.error = None
        mock_res.latency_ms = 2
        mock_layer.execute_tool = AsyncMock(return_value=mock_res)

        gate = GateResult(
            decision=GateDecision.ALLOW,
            tool_name="read_skill",
            state_before=ToolCallState.IDLE,
            state_after=ToolCallState.IDLE,
            reason="ok",
            consecutive_count=0,
            total_calls=1,
        )
        with patch("personal_agent.orchestrator.skills.find_skills_for_tool", return_value=[]):
            from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

            await dispatch_tool_call(
                tool_call_id="tc",
                tool_name="read_skill",
                arguments={"name": "bash"},
                args_hash="x",
                gate_result=gate,
                loop_policy=ToolLoopPolicy(),
                tool_layer=mock_layer,
                trace_ctx=TraceContext.new_trace(),
                trace_id="t",
                session_id="test",
                loaded_skills=loaded_skills,
            )

        assert "bash" in loaded_skills


# ---------------------------------------------------------------------------
# Skill index injection — mode-aware
# ---------------------------------------------------------------------------


class TestSkillIndexInjection:
    """Skill index appears in system prompt for model_decided; keyword body for keyword."""

    def test_keyword_block_suppressed_for_loaded_skill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_skill_block suppresses body for skills in loaded_skills set."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        skills = get_all_skills()
        # Pick any skill that has keywords
        keyed = {n: s for n, s in skills.items() if s.keywords}
        if not keyed:
            pytest.skip("No skills with keywords found")

        name, skill = next(iter(keyed.items()))
        keyword = list(skill.keywords)[0]

        # With the skill loaded, its body should be suppressed
        result_with_loaded = get_skill_block(
            message=keyword,
            loaded_skills={name},
        )
        result_without_loaded = get_skill_block(message=keyword)

        # The unloaded version should include the body; the loaded version should not
        if result_without_loaded:
            assert skill.body[:50] not in result_with_loaded or result_with_loaded == ""
