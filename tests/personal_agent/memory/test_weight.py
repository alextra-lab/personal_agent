"""Tests for KnowledgeWeight model (ADR-0047 D5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.memory.weight import KnowledgeWeight


def test_default_weight() -> None:
    """KnowledgeWeight has correct defaults."""
    w = KnowledgeWeight()
    assert w.confidence == 0.5
    assert w.source_type == "inferred"
    assert w.corroboration_count == 0
    assert w.last_confirmed is None


def test_from_source_conversation() -> None:
    """from_source returns correct confidence for conversation source."""
    w = KnowledgeWeight.from_source("conversation")
    assert w.confidence == 0.8
    assert w.source_type == "conversation"


def test_from_source_tool_result() -> None:
    """from_source returns correct confidence for tool_result source."""
    w = KnowledgeWeight.from_source("tool_result")
    assert w.confidence == 0.7
    assert w.source_type == "tool_result"


def test_from_source_web_search() -> None:
    """from_source returns correct confidence for web_search source."""
    w = KnowledgeWeight.from_source("web_search")
    assert w.confidence == 0.6


def test_from_source_manual() -> None:
    """from_source returns 1.0 confidence for manual source."""
    w = KnowledgeWeight.from_source("manual")
    assert w.confidence == 1.0


def test_from_source_inferred() -> None:
    """from_source returns 0.4 confidence for inferred source."""
    w = KnowledgeWeight.from_source("inferred")
    assert w.confidence == 0.4


def test_from_source_base_confidence_override() -> None:
    """from_source respects an explicit base_confidence override."""
    w = KnowledgeWeight.from_source("conversation", base_confidence=0.3)
    assert w.confidence == 0.3


def test_confidence_upper_bound() -> None:
    """Confidence above 1.0 raises a ValidationError."""
    with pytest.raises((ValidationError, Exception)):
        KnowledgeWeight(confidence=1.5)


def test_confidence_lower_bound() -> None:
    """Confidence below 0.0 raises a ValidationError."""
    with pytest.raises((ValidationError, Exception)):
        KnowledgeWeight(confidence=-0.1)


def test_weight_is_frozen() -> None:
    """KnowledgeWeight is immutable (frozen Pydantic model)."""
    w = KnowledgeWeight()
    with pytest.raises(Exception):
        w.confidence = 0.9  # type: ignore[misc]


def test_entity_has_weight_field() -> None:
    """Entity model includes the weight field defaulting to KnowledgeWeight."""
    from personal_agent.memory.models import Entity

    e = Entity(name="Alice", entity_type="Person")
    assert hasattr(e, "weight")
    assert isinstance(e.weight, KnowledgeWeight)
    assert e.weight.confidence == 0.5  # default


def test_entity_weight_custom() -> None:
    """Entity can be constructed with a custom KnowledgeWeight."""
    from personal_agent.memory.models import Entity

    w = KnowledgeWeight.from_source("manual")
    e = Entity(name="Bob", entity_type="Person", weight=w)
    assert e.weight.confidence == 1.0
    assert e.weight.source_type == "manual"
