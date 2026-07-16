"""Unit test for budget_role_for's artifact_builder lane (ADR-0118 T1, FRE-879).

Pure function test, no DB — the integration counterpart (AC-2b, that a real
reservation lands on this lane and leaves main_inference untouched) lives in
tests/personal_agent/llm_client/test_litellm_gate_wiring.py.
"""

from __future__ import annotations

from personal_agent.cost_gate import budget_role_for


def test_artifact_builder_has_own_lane() -> None:
    assert budget_role_for("artifact_builder") == "artifact_builder"
    assert budget_role_for("artifact_builder") != "main_inference"
