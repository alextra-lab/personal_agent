"""Unit test for budget_role_for's artifact_builder lane (ADR-0118 T1, FRE-879).

Pure function test, no DB — the integration counterpart (cost-gate reservation
isolation from main_inference) lives in
tests/personal_agent/llm_client/test_litellm_gate_wiring.py.
"""

from __future__ import annotations

from personal_agent.cost_gate import budget_role_for


def test_artifact_builder_has_own_lane() -> None:
    """artifact_builder resolves to its own budget lane, not main_inference."""
    assert budget_role_for("artifact_builder") == "artifact_builder"
    assert budget_role_for("artifact_builder") != "main_inference"


def test_session_summary_shares_captains_log_lane() -> None:
    """FRE-1037: session_summary is explicit, not a main_inference default fall-through.

    ADR-0124 D2 defers a dedicated budget class for the summariser — it shares
    captains_log's lane deliberately.
    """
    assert budget_role_for("session_summary") == "captains_log"


def test_vision_lands_on_main_inference_explicitly() -> None:
    """FRE-1037: vision escalations bill to main_inference, declared not coincidental."""
    assert budget_role_for("vision") == "main_inference"


def test_skill_routing_has_own_lane() -> None:
    """FRE-1037: skill_routing is explicit, independent of the factory's own default."""
    assert budget_role_for("skill_routing") == "skill_routing"
