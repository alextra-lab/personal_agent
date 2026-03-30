"""Unit tests for heuristic Person entity supplementation (no LLM)."""

from __future__ import annotations

from personal_agent.second_brain.entity_extraction import (
    _supplement_person_entities_from_user_message,
)


def test_supplement_project_lead_cp26() -> None:
    """CP-26: 'The project lead is Priya Sharma' adds Person when list empty."""
    msg = (
        "The project lead is Priya Sharma. We're targeting "
        "a throughput of 50,000 events per second on GCP."
    )
    out = _supplement_person_entities_from_user_message(msg, [])
    names = [e.get("name") for e in out]
    assert "Priya Sharma" in names
    assert any(e.get("type") == "Person" for e in out if e.get("name") == "Priya Sharma")


def test_supplement_dedupes_existing() -> None:
    """Does not duplicate if model already extracted the person."""
    existing = [{"name": "Priya Sharma", "type": "Person", "description": "x", "properties": {}}]
    msg = "The project lead is Priya Sharma. Extra text."
    out = _supplement_person_entities_from_user_message(msg, existing)
    assert len([e for e in out if e.get("name") == "Priya Sharma"]) == 1
