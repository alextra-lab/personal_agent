"""Neo4j graph state checker for evaluation assertions.

Queries Neo4j directly to verify entity existence, promotion state,
and other graph conditions. Includes retry logic to handle async
consolidation delays.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from tests.evaluation.harness.models import AssertionResult, Neo4jAssertion

log = structlog.get_logger(__name__)

DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "neo4j_dev_password"
DEFAULT_RETRY_DELAY_S = 3.0
DEFAULT_MAX_RETRIES = 4


class Neo4jChecker:
    """Checks Neo4j graph assertions against live graph state.

    Args:
        neo4j_uri: Neo4j bolt URI.
        neo4j_user: Neo4j username.
        neo4j_password: Neo4j password.
        retry_delay_s: Seconds between retries.
        max_retries: Maximum retry attempts for queries returning 0 rows.
    """

    def __init__(  # noqa: D107
        self,
        neo4j_uri: str = DEFAULT_NEO4J_URI,
        neo4j_user: str = DEFAULT_NEO4J_USER,
        neo4j_password: str = DEFAULT_NEO4J_PASSWORD,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._neo4j_uri = neo4j_uri
        self._neo4j_user = neo4j_user
        self._neo4j_password = neo4j_password
        self._retry_delay_s = retry_delay_s
        self._max_retries = max_retries
        self._driver: Any | None = None

    async def connect(self) -> bool:
        """Connect to Neo4j.

        Returns:
            True if connected successfully.
        """
        try:
            from neo4j import AsyncGraphDatabase
        except ModuleNotFoundError:
            log.error("neo4j_checker_dependency_missing")
            return False

        try:
            self._driver = AsyncGraphDatabase.driver(
                self._neo4j_uri,
                auth=(self._neo4j_user, self._neo4j_password),
            )
            await self._driver.verify_connectivity()
            log.info("neo4j_checker_connected", uri=self._neo4j_uri)
            return True
        except Exception as e:
            log.error("neo4j_checker_connection_failed", error=str(e))
            self._driver = None
            return False

    async def disconnect(self) -> None:
        """Close Neo4j connection."""
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def check_assertion(self, assertion: Neo4jAssertion) -> AssertionResult:
        """Check a single Neo4j assertion with retry logic.

        Retries when the query returns fewer rows than expected, since
        entity extraction and promotion happen asynchronously.

        Args:
            assertion: The Neo4j assertion to check.

        Returns:
            AssertionResult with pass/fail and row count as actual_value.
        """
        if self._driver is None:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=f"Neo4j not connected — cannot check: {assertion.description}",
            )

        params = dict(assertion.query_params)
        row_count = 0

        for attempt in range(self._max_retries):
            try:
                async with self._driver.session() as session:
                    result = await session.run(assertion.cypher_query, **params)
                    rows = await result.values()
                    row_count = len(rows)

                if row_count >= assertion.min_result_count:
                    log.debug(
                        "neo4j_assertion_passed",
                        description=assertion.description,
                        row_count=row_count,
                        attempt=attempt + 1,
                    )
                    return AssertionResult(
                        assertion=assertion,
                        passed=True,
                        actual_value=row_count,
                        message=(
                            f"Neo4j: {assertion.description} — "
                            f"{row_count} rows (need >= {assertion.min_result_count})"
                        ),
                    )

                if attempt < self._max_retries - 1:
                    log.debug(
                        "neo4j_assertion_retrying",
                        description=assertion.description,
                        row_count=row_count,
                        attempt=attempt + 1,
                        retry_in_s=self._retry_delay_s,
                    )
                    await asyncio.sleep(self._retry_delay_s)

            except Exception as e:  # noqa: BLE001 — neo4j is optional; can't import specific exceptions
                log.warning(
                    "neo4j_assertion_error",
                    description=assertion.description,
                    error=str(e),
                    attempt=attempt + 1,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay_s)
                else:
                    return AssertionResult(
                        assertion=assertion,
                        passed=False,
                        actual_value=None,
                        message=f"Neo4j error: {assertion.description} — {e}",
                    )

        return AssertionResult(
            assertion=assertion,
            passed=False,
            actual_value=row_count,
            message=(
                f"Neo4j: {assertion.description} — "
                f"{row_count} rows (need >= {assertion.min_result_count}) "
                f"after {self._max_retries} attempts"
            ),
        )

    async def check_assertions(
        self,
        assertions: tuple[Neo4jAssertion, ...],
    ) -> list[AssertionResult]:
        """Check multiple Neo4j assertions sequentially.

        Args:
            assertions: Tuple of Neo4j assertions to check.

        Returns:
            List of AssertionResult for each assertion.
        """
        results: list[AssertionResult] = []
        for assertion in assertions:
            result = await self.check_assertion(assertion)
            results.append(result)
        return results
