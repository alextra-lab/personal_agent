"""Tests for Phase B.5 pre-execution skill guards in _dispatch_tool_call.

The guard intercepts known-bad tool arguments (e.g. ES index patterns like
``logs-*``) before the tool executes and returns a structured error with a
correction suggestion.  It is data-driven from ``docs/skills/*.md`` frontmatter
— no per-tool code changes needed when new bad patterns are discovered.

Test strategy: patch ``personal_agent.orchestrator.skills.find_skill_for_tool``
(the canonical import location) so the guard picks up the mock regardless of
where it is imported inside _dispatch_tool_call.
"""

from __future__ import annotations

import json
import unittest.mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.loop_gate import GateDecision, GateResult, ToolCallState, ToolLoopPolicy
from personal_agent.orchestrator.skills import SkillDoc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate_result() -> GateResult:
    return GateResult(
        decision=GateDecision.ALLOW,
        tool_name="bash",
        state_before=ToolCallState.IDLE,
        state_after=ToolCallState.IDLE,
        reason="test",
        consecutive_count=0,
        total_calls=1,
    )


def _make_skill_doc(
    name: str = "query-elasticsearch",
    tools: tuple[str, ...] = ("bash",),
    known_bad_patterns: tuple[dict[str, Any], ...] = (),
) -> SkillDoc:
    return SkillDoc(
        name=name,
        description="test",
        when_to_use="test",
        tools=tools,
        keywords=(),
        canonical_patterns=(),
        known_bad_patterns=known_bad_patterns,
        body="",
    )


def _bad_pattern(
    pattern: str,
    reason: str = "Bad index.",
    suggestion: str = "Use agent-logs-*.",
    tool: str | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "pattern": pattern,
        "reason": reason,
        "suggestion": suggestion,
    }
    if tool is not None or fields is not None:
        applies: dict[str, Any] = {}
        if tool is not None:
            applies["tool"] = tool
        if fields is not None:
            applies["fields"] = fields
        entry["applies_to"] = applies
    return entry


async def _call_dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    linked_skill: SkillDoc | None,
) -> dict[str, Any]:
    """Invoke _dispatch_tool_call with the given linked_skill injected via patch."""
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext
    from personal_agent.telemetry.trace import TraceContext

    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="test",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[],
    )
    trace_ctx = TraceContext.new_trace()
    gate_result = _make_gate_result()
    loop_policy = ToolLoopPolicy()

    mock_tool_layer = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.output = {"ok": True}
    mock_result.error = None
    mock_result.latency_ms = 10
    mock_tool_layer.execute_tool = AsyncMock(return_value=mock_result)

    # Patch the canonical source so any local import inside _dispatch_tool_call picks it up.
    # find_skills_for_tool returns a list; wrap the fixture skill in a list (or empty list).
    skills_list = [linked_skill] if linked_skill is not None else []
    with patch(
        "personal_agent.orchestrator.skills.find_skills_for_tool",
        return_value=skills_list,
    ):
        from personal_agent.orchestrator.executor import _dispatch_tool_call

        return await _dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name=tool_name,
            arguments=arguments,
            args_hash="abc",
            gate_result=gate_result,
            loop_policy=loop_policy,
            tool_layer=mock_tool_layer,
            ctx=ctx,
            trace_ctx=trace_ctx,
        )


# ---------------------------------------------------------------------------
# Guard fires on pattern match
# ---------------------------------------------------------------------------


class TestGuardBlocksOnMatch:
    """Guard returns structured error when a bad pattern is found in arguments."""

    @pytest.mark.asyncio
    async def test_bad_pattern_in_command_blocks_call(self) -> None:
        """A bash command containing 'logs-*' triggers the guard."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern(
                    pattern="logs-*",
                    reason="Generic 'logs-*' does not exist.",
                    suggestion="Use 'agent-logs-*'.",
                    tool="bash",
                    fields=["command"],
                ),
            ),
        )
        result = await _call_dispatch(
            tool_name="bash",
            arguments={"command": "curl http://elasticsearch:9200/logs-*/_search"},
            linked_skill=skill,
        )
        assert result["success"] is False
        assert result["tool_layer_error"] == "known_bad_pattern"
        content = json.loads(result["content"])
        assert "agent-logs-*" in content["hint"]

    @pytest.mark.asyncio
    async def test_error_message_contains_reason_and_suggestion(self) -> None:
        """The hint field concatenates reason + suggestion."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern(
                    "agent-events-*",
                    reason="Index does not exist.",
                    suggestion="Use agent-logs-*.",
                    tool="bash",
                    fields=["command"],
                ),
            ),
        )
        result = await _call_dispatch(
            "bash",
            {"command": "FROM agent-events-* | LIMIT 10"},
            linked_skill=skill,
        )
        content = json.loads(result["content"])
        assert "Index does not exist." in content["hint"]
        assert "agent-logs-*" in content["hint"]

    @pytest.mark.asyncio
    async def test_guard_emits_warning_telemetry(self) -> None:
        """tool_call_blocked_known_bad_pattern warning is emitted on block."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern("logs-*", tool="bash", fields=["command"]),
            ),
        )
        import personal_agent.orchestrator.executor as exec_mod

        with (
            patch(
                "personal_agent.orchestrator.skills.find_skills_for_tool",
                return_value=[skill],
            ),
            unittest.mock.patch.object(exec_mod, "log") as mock_log,
        ):
            from personal_agent.governance.models import Mode
            from personal_agent.orchestrator.channels import Channel
            from personal_agent.orchestrator.executor import _dispatch_tool_call
            from personal_agent.orchestrator.types import ExecutionContext
            from personal_agent.telemetry.trace import TraceContext

            ctx = ExecutionContext(
                session_id="s", trace_id="t", user_message="u",
                mode=Mode.NORMAL, channel=Channel.CHAT, messages=[],
            )
            await _dispatch_tool_call(
                tool_call_id="tc",
                tool_name="bash",
                arguments={"command": "curl /logs-*/_search"},
                args_hash="x",
                gate_result=_make_gate_result(),
                loop_policy=ToolLoopPolicy(),
                tool_layer=MagicMock(),
                ctx=ctx,
                trace_ctx=TraceContext.new_trace(),
            )

        warning_events = [
            call.args[0] if call.args else None
            for call in mock_log.warning.call_args_list
        ]
        assert "tool_call_blocked_known_bad_pattern" in warning_events


# ---------------------------------------------------------------------------
# Guard does NOT fire when pattern is absent
# ---------------------------------------------------------------------------


class TestGuardPassesOnNoMatch:
    """Guard does not block tool calls when no bad pattern matches."""

    @pytest.mark.asyncio
    async def test_correct_index_pattern_not_blocked(self) -> None:
        """A bash command not containing the bad pattern passes the guard.

        Note: agent-logs-* itself contains 'logs-*' as a substring, so we
        use agent-captains-captures-* which does not.
        """
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern("logs-*", tool="bash", fields=["command"]),
            ),
        )
        result = await _call_dispatch(
            "bash",
            {"command": "curl http://elasticsearch:9200/agent-captains-captures-*/_search"},
            linked_skill=skill,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_no_linked_skill_passes_through(self) -> None:
        """When find_skill_for_tool returns None, no guard fires."""
        result = await _call_dispatch(
            "bash",
            {"command": "echo hello"},
            linked_skill=None,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_empty_bad_patterns_passes_through(self) -> None:
        """A skill with no known_bad_patterns does not block any call."""
        skill = _make_skill_doc(tools=("bash",), known_bad_patterns=())
        result = await _call_dispatch(
            "bash",
            {"command": "curl /logs-*/_search"},
            linked_skill=skill,
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# applies_to scoping
# ---------------------------------------------------------------------------


class TestAppliesTo:
    """Guard respects applies_to.tool scoping."""

    @pytest.mark.asyncio
    async def test_applies_to_different_tool_does_not_fire(self) -> None:
        """Pattern scoped to run_python does not fire for bash."""
        skill = _make_skill_doc(
            tools=("bash", "run_python"),
            known_bad_patterns=(
                _bad_pattern("logs-*", tool="run_python", fields=["code"]),
            ),
        )
        result = await _call_dispatch(
            tool_name="bash",
            arguments={"command": "curl /logs-*/_search"},
            linked_skill=skill,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_applies_to_matching_tool_fires(self) -> None:
        """Pattern scoped to bash fires for bash."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern("logs-*", tool="bash", fields=["command"]),
            ),
        )
        result = await _call_dispatch(
            tool_name="bash",
            arguments={"command": "curl /logs-*/_search"},
            linked_skill=skill,
        )
        assert result["success"] is False
        assert result["tool_layer_error"] == "known_bad_pattern"

    @pytest.mark.asyncio
    async def test_no_applies_to_searches_all_string_fields(self) -> None:
        """When applies_to is omitted, all string arguments are searched."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(_bad_pattern("logs-*"),),  # no applies_to
        )
        result = await _call_dispatch(
            tool_name="bash",
            arguments={"command": "curl /logs-*/_search", "timeout_seconds": 30},
            linked_skill=skill,
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_specific_field_not_searched_when_not_in_applies_to(self) -> None:
        """If applies_to.fields = [command], other string args are not searched."""
        skill = _make_skill_doc(
            tools=("bash",),
            known_bad_patterns=(
                _bad_pattern("logs-*", tool="bash", fields=["command"]),
            ),
        )
        # Pattern is in 'other' (not a declared field) and 'command' is clean.
        # Note: agent-captains-captures-* does not contain 'logs-*' as a substring.
        result = await _call_dispatch(
            tool_name="bash",
            arguments={"command": "curl /agent-captains-captures-*/_search", "other": "FROM logs-*"},
            linked_skill=skill,
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Incident-class smoke test (real frontmatter)
# ---------------------------------------------------------------------------


class TestIncidentClassSmoke:
    """Verify the original incident (logs-* hallucination) is caught by the guard."""

    @pytest.mark.asyncio
    async def test_logs_star_in_bash_command_is_blocked(self) -> None:
        """The logs-* known_bad_pattern from query-elasticsearch.md blocks bash calls."""
        from personal_agent.orchestrator.skills import find_skills_for_tool

        skills = find_skills_for_tool("bash")
        es_skill = next(
            (s for s in skills if s.known_bad_patterns),
            None,
        )
        assert es_skill is not None, (
            "No skill with known_bad_patterns found for 'bash' — "
            "query-elasticsearch.md must list tools: [bash] and have known_bad_patterns"
        )

        # Pattern is '/logs-*' (with leading slash) — matches HTTP URL forms.
        # ES DSL form "FROM logs-* | LIMIT 10" lacks the leading slash so the
        # guard does not fire for it (adding bare 'logs-*' would false-positive
        # on the correct 'agent-logs-*' pattern via substring match).
        bad_commands = [
            "curl -s http://elasticsearch:9200/logs-*/_search",
            "curl -XGET 'http://elasticsearch:9200/logs-*/_count'",
        ]
        for cmd in bad_commands:
            # Use real find_skills_for_tool (no patch) so real frontmatter is exercised
            result = await _call_dispatch(
                tool_name="bash",
                arguments={"command": cmd},
                linked_skill=es_skill,
            )
            assert result["success"] is False, (
                f"Guard did not fire for command: {cmd!r}"
            )
            content = json.loads(result["content"])
            assert "agent-logs-*" in content["hint"], (
                f"Suggestion missing for command: {cmd!r}\nhint: {content['hint']}"
            )
