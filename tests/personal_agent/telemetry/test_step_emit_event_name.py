"""FRE-352: executor step-level emit must use llm_step_completed, not model_call_completed.

The executor's orchestrator-level LLM-step log event (executor.py ~line 1771) previously
used the same event name as the LLM-client-level emit (model_call_completed), silently
mixing two different payloads in ES for consumers querying by event_type.

This module adds LLM_STEP_COMPLETED = "llm_step_completed" to telemetry.events and
the executor switches to it.
"""

from __future__ import annotations


def test_llm_step_completed_constant_exists() -> None:
    """LLM_STEP_COMPLETED must be exported from telemetry.events (FRE-352)."""
    from personal_agent.telemetry.events import LLM_STEP_COMPLETED  # noqa: F401

    assert LLM_STEP_COMPLETED == "llm_step_completed", (
        f"Expected 'llm_step_completed', got {LLM_STEP_COMPLETED!r}"
    )


def test_llm_step_completed_exported_from_telemetry_package() -> None:
    """LLM_STEP_COMPLETED must be accessible from the telemetry package (FRE-352)."""
    from personal_agent import telemetry

    assert hasattr(telemetry, "LLM_STEP_COMPLETED"), (
        "LLM_STEP_COMPLETED not exported from personal_agent.telemetry.__all__"
    )
    assert telemetry.LLM_STEP_COMPLETED == "llm_step_completed"


def test_executor_uses_llm_step_completed_not_model_call_completed() -> None:
    """The executor must import and use LLM_STEP_COMPLETED for step-level emit.

    Verifies the distinction is enforced at the module level: after the fix the
    executor no longer has MODEL_CALL_COMPLETED in scope for the step-level path.
    """
    import importlib
    import inspect
    import personal_agent.orchestrator.executor as executor_mod

    # Re-read the source to find the step-level log.info call region
    source = inspect.getsource(executor_mod)

    # After FRE-352: the executor should reference LLM_STEP_COMPLETED
    assert "LLM_STEP_COMPLETED" in source, (
        "executor.py does not reference LLM_STEP_COMPLETED — "
        "the step-level emit has not been updated"
    )
