"""Tests for SubAgentSpec and SubAgentResult types."""

import pytest

from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


# ---------------------------------------------------------------------------
# SubAgentSpec
# ---------------------------------------------------------------------------


class TestSubAgentSpec:
    def test_minimal_construction(self) -> None:
        """Only required fields — all defaults applied."""
        spec = SubAgentSpec(task="Summarise the document.", context_slice=[])
        assert spec.task == "Summarise the document."
        assert spec.context_slice == []
        assert spec.output_format == "text"
        assert spec.max_tokens == 4096
        assert spec.timeout_seconds == 120.0
        assert spec.tools == []
        assert spec.background == ""
        assert spec.model_role == ModelRole.STANDARD

    def test_full_construction(self) -> None:
        """All fields set explicitly."""
        ctx = [{"role": "user", "content": "Analyse this"}]
        spec = SubAgentSpec(
            task="Deep analysis",
            context_slice=ctx,
            output_format="json",
            max_tokens=2048,
            timeout_seconds=60.0,
            tools=["search", "read_file"],
            background="Parent task: architecture review.",
            model_role=ModelRole.REASONING,
        )
        assert spec.output_format == "json"
        assert spec.max_tokens == 2048
        assert spec.timeout_seconds == 60.0
        assert spec.tools == ["search", "read_file"]
        assert spec.background == "Parent task: architecture review."
        assert spec.model_role == ModelRole.REASONING

    def test_immutability(self) -> None:
        """SubAgentSpec is frozen — mutation raises AttributeError."""
        spec = SubAgentSpec(task="test", context_slice=[])
        with pytest.raises(AttributeError):
            spec.task = "modified"  # type: ignore[misc]

    def test_context_slice_can_hold_structured_data(self) -> None:
        """context_slice accepts arbitrary dicts (messages, docs, tool results)."""
        ctx = [
            {"role": "user", "content": "Question"},
            {"type": "doc", "title": "Manual", "text": "..."},
            {"type": "tool_result", "tool": "search", "output": ["item1"]},
        ]
        spec = SubAgentSpec(task="process", context_slice=ctx)
        assert len(spec.context_slice) == 3

    def test_default_tools_is_empty_list(self) -> None:
        """Default tools list is empty, not None."""
        spec = SubAgentSpec(task="task", context_slice=[])
        assert spec.tools == []
        assert isinstance(spec.tools, list)


# ---------------------------------------------------------------------------
# SubAgentResult
# ---------------------------------------------------------------------------


class TestSubAgentResult:
    def _make_result(self, **overrides: object) -> SubAgentResult:
        defaults: dict[str, object] = {
            "task_id": "agent-001",
            "spec_task": "Summarise section 3",
            "summary": "Section 3 covers authentication flows.",
            "full_output": "Section 3 covers authentication flows in detail...",
            "tools_used": [],
            "token_count": 120,
            "duration_ms": 450.0,
            "success": True,
        }
        defaults.update(overrides)
        return SubAgentResult(**defaults)  # type: ignore[arg-type]

    def test_successful_result_construction(self) -> None:
        result = self._make_result()
        assert result.task_id == "agent-001"
        assert result.success is True
        assert result.error is None

    def test_failed_result_with_error_message(self) -> None:
        result = self._make_result(success=False, error="Timeout after 120s")
        assert result.success is False
        assert result.error == "Timeout after 120s"

    def test_error_defaults_to_none_on_success(self) -> None:
        result = self._make_result(success=True)
        assert result.error is None

    def test_immutability(self) -> None:
        """SubAgentResult is frozen — mutation raises AttributeError."""
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]

    def test_summary_vs_full_output_split(self) -> None:
        """summary and full_output are separate fields."""
        result = self._make_result(
            summary="Short summary for synthesis.",
            full_output="Very long detailed output... " * 100,
        )
        assert result.summary != result.full_output
        assert len(result.full_output) > len(result.summary)

    def test_tools_used_list(self) -> None:
        result = self._make_result(tools_used=["search", "read_file"])
        assert result.tools_used == ["search", "read_file"]

    def test_timing_and_token_fields(self) -> None:
        result = self._make_result(token_count=512, duration_ms=1234.5)
        assert result.token_count == 512
        assert result.duration_ms == 1234.5
