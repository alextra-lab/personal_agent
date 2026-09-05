"""SubAgentCapture model + writer (FRE-505).

Per-sub-agent durable audit record: input-context breakdown + full output +
injected digest + truncation ratio, identity-threaded (trace_id/session_id/
task_id, ADR-0074), surfaced in the captures index family.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from personal_agent.captains_log import capture as capture_mod
from personal_agent.captains_log.capture import (
    SUBAGENT_CAPTURES_INDEX_PREFIX,
    SubAgentCapture,
    write_sub_agent_capture,
)


def _capture(**overrides: object) -> SubAgentCapture:
    base: dict[str, object] = {
        "trace_id": "trace-1",
        "session_id": "sess-1",
        "task_id": "sub-abc123",
        "timestamp": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "system_prompt_chars": 120,
        "skill_index_block_chars": 0,
        "spec_task": "find the config",
        "context_message_count": 2,
        "context_chars": 40,
        "context_messages": [{"role": "user", "chars": 40, "content_preview": "hi"}],
        "memory_in_context": False,
        "mode": "PARALLEL_INFERENCE",
        "model_role": "sub_agent",
        "max_tokens": 4096,
        "tools_granted": [],
        "tools_used": [],
        "full_output": "x" * 1000,
        "full_output_chars": 1000,
        "injected_digest": "x" * 500,
        "digest_chars": 500,
        "truncation_ratio": 0.5,
        "success": True,
        "error": None,
        "duration_ms": 12.0,
        "cost_usd": 0.0,
    }
    base.update(overrides)
    return SubAgentCapture(**base)  # type: ignore[arg-type]


def test_index_prefix_is_captures_family() -> None:
    """The sub-agent index sits inside the agent-captains-captures-* family."""
    assert SUBAGENT_CAPTURES_INDEX_PREFIX.endswith("-captures-subagents")
    assert "-captures" in SUBAGENT_CAPTURES_INDEX_PREFIX


def test_tools_denied_defaults_to_empty_list() -> None:
    """FRE-1388: a capture with no denial keeps today's shape (default empty)."""
    cap = _capture()
    assert cap.tools_denied == []


def test_tools_denied_records_a_refusal() -> None:
    """FRE-1388: a refused tool is visible on the audit record itself."""
    cap = _capture(tools_granted=["run_python"], tools_denied=["bash"])
    assert cap.tools_denied == ["bash"]
    assert cap.tools_granted == ["run_python"]


def test_tool_loop_fields_default_to_empty() -> None:
    """FRE-1389: today's no-tool-loop shape keeps its defaults."""
    cap = _capture()
    assert cap.tool_iterations == 0
    assert cap.tool_result_chars_absorbed == 0
    assert cap.refused_tool_attempts == []
    assert cap.stated_tool_gap is None


def test_tool_loop_fields_record_activity() -> None:
    """FRE-1389: a runtime refusal and a stated gap are visible on the record itself."""
    cap = _capture(
        tools_granted=["run_python"],
        tools_used=["run_python"],
        tool_iterations=2,
        tool_result_chars_absorbed=1234,
        refused_tool_attempts=["bash"],
        stated_tool_gap="web_search",
    )
    assert cap.tool_iterations == 2
    assert cap.tool_result_chars_absorbed == 1234
    assert cap.refused_tool_attempts == ["bash"]
    assert cap.stated_tool_gap == "web_search"


def test_frozen() -> None:
    """The record is immutable (ConfigDict frozen=True)."""
    cap = _capture()
    with pytest.raises(ValidationError):
        cap.success = False  # type: ignore[misc]


def test_write_schedules_es_with_composite_doc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_sub_agent_capture indexes to the dated sub-agents index with doc_id trace:task."""
    calls: list[tuple[str, dict, str | None]] = []

    def _fake_schedule(index_name, document, es_handler=None, doc_id=None):  # type: ignore[no-untyped-def]
        calls.append((index_name, document, doc_id))

    monkeypatch.setattr(capture_mod, "schedule_es_index", _fake_schedule)

    write_sub_agent_capture(_capture())

    assert len(calls) == 1
    index_name, document, doc_id = calls[0]
    assert index_name == f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-2026-06"
    assert doc_id == "trace-1:sub-abc123"
    assert document["injected_digest"] == "x" * 500
    assert document["trace_id"] == "trace-1"
    assert document["task_id"] == "sub-abc123"


def test_write_never_raises_on_indexer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing indexer is swallowed — capture must never break the sub-agent."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ES down")

    monkeypatch.setattr(capture_mod, "schedule_es_index", _boom)

    # Should not raise.
    write_sub_agent_capture(_capture())
