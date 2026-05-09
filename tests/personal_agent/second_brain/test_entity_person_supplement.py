"""Unit tests for heuristic Person entity supplementation (no LLM).

Also includes a regression test for the extraction-prompt rule clarification
introduced in FRE-213 / ADR-0052: the extraction prompt must explicitly permit
extracting the human operator (the person who speaks through the User slot) when
named, while still forbidding protocol role labels ("User", "Assistant", etc.).
"""

from __future__ import annotations

from personal_agent.second_brain.entity_extraction import (
    _EXTRACTION_PROMPT_TEMPLATE,
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


# ── FRE-213 regression: extraction prompt clarification (ADR-0052 §4) ─────────


def test_extraction_prompt_does_not_forbid_operator_by_name() -> None:
    """Regression: the amended rule #1 must NOT ban extracting the operator when named.

    The original rule 'NEVER extract "User" or "Assistant"' is correct but was
    potentially read as banning the harness-user Alex too. The amended rule explicitly
    clarifies: protocol labels are forbidden; the named human operator is permitted.
    """
    # The prompt must mention that self-reference ("my name is Alex") can be extracted
    assert "does NOT preclude" in _EXTRACTION_PROMPT_TEMPLATE or (
        "my name is" in _EXTRACTION_PROMPT_TEMPLATE.lower()
        or "operator" in _EXTRACTION_PROMPT_TEMPLATE.lower()
    ), (
        "Extraction prompt rule #1 must explicitly clarify that named operator "
        "self-references are extractable (FRE-213 / ADR-0052 §4)"
    )


def test_extraction_prompt_still_forbids_protocol_role_labels() -> None:
    """The protocol role labels 'User' / 'Assistant' must still be forbidden."""
    prompt_lower = _EXTRACTION_PROMPT_TEMPLATE.lower()
    assert "never extract" in prompt_lower
    # The forbidden labels appear (case-insensitive) in the prompt
    assert "user" in prompt_lower
    assert "assistant" in prompt_lower
