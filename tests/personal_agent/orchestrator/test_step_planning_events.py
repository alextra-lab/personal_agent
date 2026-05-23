"""FRE-376 Phase 3 / ADR-0074: orchestrator emits STEP_PLANNING_*, not MODEL_CALL_*.

Before Phase 3 the orchestrator emitted ``MODEL_CALL_STARTED`` at the LLM-call
boundary with a thinner ``(trace_id, span_id, model_role, channel)`` payload,
colliding with the canonical ``model_call_started`` event that
``LocalLLMClient`` / ``LiteLLMClient`` emit with the full
``(model, role, endpoint, session_id, parent_span_id, ...)`` shape. Two emits
under one event name = ambiguous Kibana queries.

Phase 3 renames the orchestrator emit to ``step_planning_started`` (and adds
a matching ``step_planning_completed`` on every exit path — success, error,
tool-execution branch). ``MODEL_CALL_STARTED`` is now exclusively client-side.

Static AST check — runs in ``make test``, no orchestrator wiring required.
"""

# ruff: noqa: D103

from __future__ import annotations

import ast
from pathlib import Path

EXECUTOR = Path("src/personal_agent/orchestrator/executor.py")


def _log_call_event_names(path: Path) -> list[str]:
    """Yield the first-positional-arg name for every ``log.info(EVENT, ...)`` call."""
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"info", "debug", "warning", "error", "exception"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "log"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Name):
                names.append(first.id)
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value)
    return names


def test_executor_does_not_emit_model_call_started() -> None:
    """ADR-0074 §I2: MODEL_CALL_STARTED is exclusively a client-side event."""
    names = _log_call_event_names(EXECUTOR)
    assert "MODEL_CALL_STARTED" not in names, (
        "Orchestrator must not emit MODEL_CALL_STARTED — that event is reserved "
        "for LocalLLMClient/LiteLLMClient. Use STEP_PLANNING_STARTED instead."
    )


def test_executor_emits_step_planning_started() -> None:
    names = _log_call_event_names(EXECUTOR)
    assert "STEP_PLANNING_STARTED" in names, (
        "Orchestrator should emit STEP_PLANNING_STARTED at the LLM-call boundary."
    )


def test_executor_emits_step_planning_completed() -> None:
    names = _log_call_event_names(EXECUTOR)
    assert "STEP_PLANNING_COMPLETED" in names, (
        "Orchestrator should emit STEP_PLANNING_COMPLETED on step exit "
        "(success, tool-execution branch, and error path)."
    )


def test_step_planning_completed_appears_at_least_twice() -> None:
    """Happy + error paths both emit completion — at minimum 2 occurrences."""
    names = _log_call_event_names(EXECUTOR)
    occurrences = sum(1 for n in names if n == "STEP_PLANNING_COMPLETED")
    assert occurrences >= 2, (
        f"Expected ≥2 STEP_PLANNING_COMPLETED emits (success + error paths), found {occurrences}."
    )
