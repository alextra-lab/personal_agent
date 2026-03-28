"""Unit tests for Neo4j assertion types and checker."""

from __future__ import annotations

from tests.evaluation.harness.models import (
    neo4j_cypher,
    neo4j_entity,
    neo4j_promoted,
)


class TestNeo4jAssertionBuilders:
    """Test compact builder helpers for Neo4j assertions."""

    def test_neo4j_entity_creates_entity_exists_query(self) -> None:
        """neo4j_entity produces correct assertion for entity existence check."""
        a = neo4j_entity("Project Atlas")
        assert a.kind == "neo4j"
        assert "Project Atlas" in a.cypher_query
        assert a.min_result_count == 1
        assert a.description == "Entity 'Project Atlas' exists in Neo4j"

    def test_neo4j_promoted_creates_semantic_check(self) -> None:
        """neo4j_promoted produces assertion targeting semantic memory_type."""
        a = neo4j_promoted("Project Atlas")
        assert a.kind == "neo4j"
        assert "memory_type" in a.cypher_query
        assert "semantic" in a.cypher_query
        assert a.min_result_count == 1

    def test_neo4j_cypher_passes_through(self) -> None:
        """neo4j_cypher passes query and metadata through unchanged."""
        query = "MATCH (e:Entity) RETURN count(e) AS cnt"
        a = neo4j_cypher("at least one entity", query, min_result_count=1)
        assert a.cypher_query == query
        assert a.min_result_count == 1
        assert a.description == "at least one entity"
