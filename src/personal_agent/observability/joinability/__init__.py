"""Joinability probe — Phase 5 of ADR-0074 (FRE-376).

Cross-substrate identity walker: pick one random session, walk Postgres ↔
Elasticsearch ↔ Neo4j ↔ Redis, assert every identity tuple matches, report
orphans. Acts as the runtime counterpart to the AST lint shipped in Phase 3.
"""

from personal_agent.observability.joinability.result import (
    Orphan,
    ResultDoc,
    SubstrateCheck,
    aggregate_outcome,
)

__all__ = [
    "Orphan",
    "ResultDoc",
    "SubstrateCheck",
    "aggregate_outcome",
]
