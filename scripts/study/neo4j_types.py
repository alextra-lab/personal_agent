"""Shared narrow Neo4j driver Protocols for the study sandbox scripts (FRE-839).

Factored out of ``schema.py``/``writer.py``/``categorizer.py``/``run_ingest.py``
so each doesn't redefine the same three Protocols — the subset of the async
neo4j driver API these scripts use, kept narrow so unit tests can fake the
driver instead of needing real infra (mirrors ``export_snapshot.py``'s
original per-file Protocols, FRE-838).
"""

from __future__ import annotations

from typing import Any, Protocol


class Neo4jResult(Protocol):
    """The subset of the neo4j async result API this script uses."""

    def __aiter__(self) -> Any: ...
    async def single(self) -> Any: ...


class Neo4jSession(Protocol):
    """The subset of the neo4j async session API this script uses."""

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> Neo4jResult: ...
    async def __aenter__(self) -> "Neo4jSession": ...
    async def __aexit__(self, *exc_info: object) -> None: ...


class Neo4jDriver(Protocol):
    """The subset of the neo4j async driver API this script uses."""

    def session(self) -> Neo4jSession: ...
