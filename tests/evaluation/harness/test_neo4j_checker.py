"""Unit tests for Neo4j assertion types and checker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.evaluation.harness.models import (
    Neo4jAssertion,
    neo4j_cypher,
    neo4j_entity,
    neo4j_promoted,
)
from tests.evaluation.harness.neo4j_checker import Neo4jChecker


class TestNeo4jAssertionBuilders:
    """Test compact builder helpers for Neo4j assertions."""

    def test_neo4j_entity_creates_entity_exists_query(self) -> None:
        """neo4j_entity produces correct parameterized assertion for entity existence check."""
        a = neo4j_entity("Project Atlas")
        assert a.kind == "neo4j"
        assert "$name" in a.cypher_query
        assert a.query_params == (("name", "Project Atlas"),)
        assert a.min_result_count == 1
        assert a.description == "Entity 'Project Atlas' exists in Neo4j"

    def test_neo4j_promoted_creates_semantic_check(self) -> None:
        """neo4j_promoted produces parameterized assertion targeting semantic memory_type."""
        a = neo4j_promoted("Project Atlas")
        assert a.kind == "neo4j"
        assert "$name" in a.cypher_query
        assert "memory_type" in a.cypher_query
        assert a.query_params == (("name", "Project Atlas"),)
        assert a.min_result_count == 1

    def test_neo4j_cypher_passes_through(self) -> None:
        """neo4j_cypher passes query and metadata through unchanged."""
        query = "MATCH (e:Entity) RETURN count(e) AS cnt"
        a = neo4j_cypher("at least one entity", query, min_result_count=1)
        assert a.cypher_query == query
        assert a.min_result_count == 1
        assert a.description == "at least one entity"


class TestNeo4jChecker:
    """Tests for Neo4jChecker with mocked Neo4j driver."""

    @pytest.fixture
    def checker(self) -> Neo4jChecker:
        """Return a Neo4jChecker with fast retry settings for testing."""
        return Neo4jChecker(
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="test",
            max_retries=2,
            retry_delay_s=0.01,
        )

    @pytest.mark.asyncio
    async def test_check_assertion_passes_when_rows_returned(self, checker: Neo4jChecker) -> None:
        """AssertionResult.passed is True when driver returns rows >= min_result_count."""
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: $name}) RETURN e",
            query_params=(("name", "Foo"),),
            min_result_count=1,
        )

        mock_record = MagicMock()
        mock_result = AsyncMock()
        mock_result.values.return_value = [[mock_record]]
        mock_session = AsyncMock()
        mock_session.run.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        checker._driver = mock_driver

        result = await checker.check_assertion(assertion)
        assert result.passed is True
        assert result.actual_value == 1
        # Verify params were passed to driver
        mock_session.run.assert_called_once_with(assertion.cypher_query, name="Foo")

    @pytest.mark.asyncio
    async def test_check_assertion_fails_when_no_rows(self, checker: Neo4jChecker) -> None:
        """AssertionResult.passed is False when driver returns 0 rows."""
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: $name}) RETURN e",
            query_params=(("name", "Missing"),),
            min_result_count=1,
        )

        mock_result = AsyncMock()
        mock_result.values.return_value = []
        mock_session = AsyncMock()
        mock_session.run.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        checker._driver = mock_driver

        result = await checker.check_assertion(assertion)
        assert result.passed is False
        assert result.actual_value == 0

    @pytest.mark.asyncio
    async def test_check_assertion_returns_failure_on_no_driver(
        self, checker: Neo4jChecker
    ) -> None:
        """AssertionResult.passed is False with 'not connected' message when driver is None."""
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: $name}) RETURN e",
            query_params=(("name", "Foo"),),
            min_result_count=1,
        )
        result = await checker.check_assertion(assertion)
        assert result.passed is False
        assert "not connected" in result.message.lower()
