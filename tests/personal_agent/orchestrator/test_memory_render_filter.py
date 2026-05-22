"""Tests for FRE-374: executor skips entity lines with empty/None descriptions."""

from __future__ import annotations


class TestMemoryRenderFilter:
    """Render-time empty-description filter (FRE-374 D1)."""

    def _render(self, entities: list[dict]) -> str:
        from personal_agent.orchestrator.executor import _render_memory_section
        return _render_memory_section(entities)

    def test_entity_with_description_is_included(self) -> None:
        result = self._render([
            {"type": "entity", "name": "Neo4j", "entity_type": "Technology",
             "description": "Graph database system.", "mentions": 100},
        ])
        assert "Neo4j" in result
        assert "Graph database system." in result

    def test_entity_with_none_description_is_skipped(self) -> None:
        result = self._render([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
        ])
        assert "Paris" not in result

    def test_entity_with_empty_string_description_is_skipped(self) -> None:
        result = self._render([
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "London" not in result

    def test_entity_with_whitespace_only_description_is_skipped(self) -> None:
        result = self._render([
            {"type": "entity", "name": "Venice", "entity_type": "Location",
             "description": "   ", "mentions": 21},
        ])
        assert "Venice" not in result

    def test_mixed_entities_only_described_appear(self) -> None:
        result = self._render([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
            {"type": "entity", "name": "Neo4j", "entity_type": "Technology",
             "description": "Graph database.", "mentions": 287},
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "Paris" not in result
        assert "London" not in result
        assert "Neo4j" in result

    def test_empty_entity_list_produces_empty_string(self) -> None:
        assert self._render([]) == ""

    def test_all_empty_descriptions_suppresses_entire_section(self) -> None:
        result = self._render([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "Your Memory Graph" not in result
        assert "Do NOT say you have no memory" not in result
