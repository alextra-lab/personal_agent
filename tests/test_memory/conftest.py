"""Conftest for memory tests: Neo4j test isolation.

Ensures test-created data is cleaned up after the test session to prevent
contamination of the production graph.
"""

import re

import pytest

from personal_agent.config.settings import get_settings

# Patterns that identify test-generated entity names (uuid hex suffixes, test prefixes)
_TEST_ENTITY_PATTERNS = [
    r"^TestLang_[0-9a-f]{8}$",
    r"^RecencyLang_[0-9a-f]{8}$",
    r"^test_[0-9a-f]{6}_\w+$",
    r"^RareLanguage$",
]
_TEST_ENTITY_RE = re.compile("|".join(_TEST_ENTITY_PATTERNS))


@pytest.fixture(autouse=True, scope="session")
def _cleanup_test_entities_after_session():
    """Clean up test-generated entities from Neo4j after the test session.

    Uses the synchronous Neo4j driver to avoid event-loop scope conflicts
    with pytest-asyncio's function-scoped event loop.
    """
    yield

    try:
        from neo4j import GraphDatabase
    except ImportError:
        return

    settings = get_settings()
    driver = None
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()

        with driver.session() as session:
            # Remove nodes explicitly marked as test data (top-level properties)
            session.run("MATCH (n {test: true}) DETACH DELETE n")
            session.run("MATCH (n {test_e2e: true}) DETACH DELETE n")
            session.run("MATCH (n {test_scoring: true}) DETACH DELETE n")

            # Remove Turn nodes with test markers in serialized properties JSON
            session.run(
                "MATCH (t:Turn) WHERE t.properties CONTAINS '\"test\":true' "
                "OR t.properties CONTAINS '\"test\": true' "
                "OR t.properties CONTAINS '\"test_scoring\":true' "
                "OR t.properties CONTAINS '\"test_scoring\": true' "
                "DETACH DELETE t"
            )

            # Remove entities matching test name patterns
            result = session.run("MATCH (e:Entity) RETURN e.name AS name")
            records = list(result)
            test_names = [
                record["name"] for record in records
                if record["name"] and _TEST_ENTITY_RE.match(record["name"])
            ]
            if test_names:
                session.run(
                    "MATCH (e:Entity) WHERE e.name IN $names DETACH DELETE e",
                    names=test_names,
                )

            # Remove orphaned entities with no remaining DISCUSSES edges
            session.run(
                "MATCH (e:Entity) WHERE NOT ()-[:DISCUSSES]->(e) DETACH DELETE e"
            )
    except Exception:
        pass  # Best-effort cleanup; don't fail the test session
    finally:
        if driver:
            driver.close()
