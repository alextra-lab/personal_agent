"""Prod-scale end-to-end verification for the frozen-snapshot export (FRE-838 fix-forward).

Master's first live corpus-load attempt (2026-07-10) OOM-killed the study
Neo4j while writing the real prod corpus (10,290 nodes / 34,301
relationships) — the original build session's isolation suite only ever
exercised an empty graph, so the write-volume/memory failure mode went
uncaught. This test closes that exact gap: it populates a synthetic corpus
at the same order of magnitude, then runs the REAL `run_export(execute=True)`
path against the REAL (now resource-bumped) study substrate — not a mock.

Requires infra already up:
    make test-infra-up     # synthetic "prod" source — Neo4j :7688, Postgres :5433
    make study-infra-up    # real study target — Neo4j :7691 (bumped resources)

Skips gracefully if either is unreachable. Cleans up everything it wrote
(source and target) on completion, so the study substrate is left empty for
master's real run against actual prod data afterward.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from scripts.study.config import StudySettings
from scripts.study.export_snapshot import count_nodes_and_relationships, run_export

from personal_agent.config import get_settings

pytestmark = pytest.mark.integration

# Same order of magnitude as the real prod corpus master reported
# (10,290 nodes / 34,301 relationships) — not an exact replica, but close
# enough to reproduce the write-volume/memory failure mode.
NODE_COUNTS: dict[str, int] = {
    "Entity": 4000,
    "Turn": 3000,
    "Session": 500,
    "Person": 10,
    "Agent": 5,
    "Claim": 800,
    "Location": 50,
    "EntityDescriptionVersion": 1925,
}  # sum = 10,290

REL_SPECS: list[tuple[str, str, int, int]] = [
    # (rel_type, start_label, end_label, count) — endpoints picked by index
    # modulo the relevant label's population, not semantically meaningful,
    # just structurally valid edges at realistic volume.
    ("DISCUSSES", "Turn", "Entity", 15000),
    ("DISCUSSES", "Session", "Entity", 6000),
    ("PARTICIPATED_IN", "Person", "Turn", 3000),
    ("CONTAINS", "Session", "Turn", 3000),
    ("NEXT", "Turn", "Turn", 2500),
    ("HAD_DESCRIPTION", "Entity", "EntityDescriptionVersion", 1925),
    ("HAS_STANCE", "Person", "Entity", 2000),
    ("HAS_FACT", "Person", "Claim", 800),
    ("OPERATED_BY", "Agent", "Person", 5),
    ("CURRENTLY_AT", "Person", "Location", 40),
    ("VISITED", "Person", "Location", 31),
]  # sum = 34,301

EMBEDDING_DIM = 1024  # matches the real embedder dimension (OVH-managed, 8B @ 1024)
SESSIONS_WITH_TRACES = 300  # subset of Session nodes get a matching Postgres row
BATCH_SIZE = 500


def _entity_properties(i: int, sk: str) -> dict[str, Any]:
    return {
        "sk": sk,
        "name": f"Synthetic entity {i}",
        "entity_type": "Phenomenon",
        "description": f"Synthetic scale-test entity #{i}" * 3,
        "embedding": [((i + j) % 997) / 997.0 for j in range(EMBEDDING_DIM)],
        "mention_count": i % 50,
    }


def _turn_properties(i: int, sk: str, session_id: str | None) -> dict[str, Any]:
    return {
        "sk": sk,
        "turn_id": sk,
        "session_id": session_id,
        "sequence_number": i,
        "summary": f"Synthetic turn summary #{i}",
        "user_message": f"Synthetic user message #{i} " * 10,
        "assistant_response": f"Synthetic assistant response #{i} " * 10,
    }


def _session_properties(i: int, sk: str, session_id: str) -> dict[str, Any]:
    return {"sk": sk, "session_id": session_id, "mode": "NORMAL"}


def _generic_properties(label: str, i: int, sk: str) -> dict[str, Any]:
    return {"sk": sk, "name": f"Synthetic {label} #{i}"}


async def _neo4j_available(uri: str, user: str, password: str) -> bool:
    try:
        driver = AsyncGraphDatabase.driver(  # fre-375-allow: test-infra/study substrate probe
            uri, auth=(user, password)
        )
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return True
    except Exception:  # noqa: BLE001 — availability probe, any failure means skip
        return False


async def _postgres_available(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn)
        await conn.close()
        return True
    except Exception:  # noqa: BLE001 — availability probe, any failure means skip
        return False


@pytest_asyncio.fixture
async def prod_scale_source_and_target() -> AsyncIterator[dict[str, Any]]:
    """Populate a prod-scale synthetic corpus in the test stack, yield
    session UUIDs for assertions, and clean up source + study target after.
    """
    app_settings = get_settings()
    study_settings = StudySettings()

    if not await _neo4j_available(
        app_settings.neo4j_uri, app_settings.neo4j_user, app_settings.neo4j_password
    ):
        pytest.skip("Synthetic source Neo4j not available (make test-infra-up)")
    pg_dsn = app_settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    if not await _postgres_available(pg_dsn):
        pytest.skip("Synthetic source Postgres not available (make test-infra-up)")
    if not await _neo4j_available(
        study_settings.neo4j_uri, study_settings.neo4j_user, study_settings.neo4j_password
    ):
        pytest.skip("Study substrate Neo4j not available (make study-infra-up)")

    # Wipe any leftovers from a prior interrupted run on BOTH sides before
    # populating (self-review follow-up, FRE-838): the study substrate is
    # the one master's real export will run against next, and leftover
    # data there — from a prior crashed run of this same test, or a prior
    # manual export attempt — would make count_nodes_and_relationships()'s
    # unscoped `MATCH (n)` assertions spuriously fail even when
    # export_snapshot.py itself worked correctly.
    study_driver = AsyncGraphDatabase.driver(  # fre-375-allow: study substrate, pre-test wipe
        study_settings.neo4j_uri, auth=(study_settings.neo4j_user, study_settings.neo4j_password)
    )
    async with study_driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    await study_driver.close()

    driver = (
        AsyncGraphDatabase.driver(  # fre-375-allow: synthetic-source population, test-infra only
            app_settings.neo4j_uri, auth=(app_settings.neo4j_user, app_settings.neo4j_password)
        )
    )
    session_uuids = [str(uuid.uuid4()) for _ in range(NODE_COUNTS["Session"])]

    async with driver.session() as session:
        # Wipe any leftovers from a prior interrupted run.
        await session.run("MATCH (n) WHERE n.sk IS NOT NULL DETACH DELETE n")

        # Nodes, batched per label.
        for label, count in NODE_COUNTS.items():
            for start in range(0, count, BATCH_SIZE):
                end = min(start + BATCH_SIZE, count)
                rows = []
                for i in range(start, end):
                    sk = f"{label}-{i}"
                    if label == "Entity":
                        rows.append(_entity_properties(i, sk))
                    elif label == "Turn":
                        session_id = session_uuids[i % len(session_uuids)]
                        rows.append(_turn_properties(i, sk, session_id))
                    elif label == "Session":
                        rows.append(_session_properties(i, sk, session_uuids[i]))
                    else:
                        rows.append(_generic_properties(label, i, sk))
                await session.run(
                    f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", {"rows": rows}
                )

        # Index `sk` per label before relationship creation — an unindexed
        # property MATCH is a full-graph scan per lookup (this is exactly
        # the production bug this fix-forward closes in export_snapshot.py
        # itself; the synthetic-data generator here needs the same fix for
        # its own writes to complete in reasonable time at this volume).
        for label in NODE_COUNTS:
            await session.run(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.sk)")
        await session.run("CALL db.awaitIndexes(60)")  # block until indexes are online

        # Relationships, batched per (rel_type, start_label, end_label).
        for rel_type, start_label, end_label, count in REL_SPECS:
            start_pop = NODE_COUNTS[start_label]
            end_pop = NODE_COUNTS[end_label]
            for batch_start in range(0, count, BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, count)
                rows = [
                    {
                        "a_sk": f"{start_label}-{i % start_pop}",
                        "b_sk": f"{end_label}-{(i * 7 + 3) % end_pop}",
                    }
                    for i in range(batch_start, batch_end)
                ]
                await session.run(
                    "UNWIND $rows AS row "
                    f"MATCH (a:{start_label} {{sk: row.a_sk}}), (b:{end_label} {{sk: row.b_sk}}) "
                    f"CREATE (a)-[r:{rel_type}]->(b)",
                    {"rows": rows},
                )
    await driver.close()

    pg_conn = await asyncpg.connect(pg_dsn)
    try:
        user_id = await pg_conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING user_id",
            f"scale-test-{uuid.uuid4()}@example.invalid",
        )
        for session_id in session_uuids[:SESSIONS_WITH_TRACES]:
            await pg_conn.execute(
                "INSERT INTO sessions (session_id, user_id, messages) VALUES ($1, $2, $3)",
                uuid.UUID(session_id),
                user_id,
                json.dumps([{"role": "user", "content": "synthetic scale-test message"}]),
            )
    finally:
        await pg_conn.close()

    try:
        yield {"session_uuids": session_uuids}
    finally:
        # Each step is independently isolated (self-review follow-up,
        # FRE-838): the original single `finally` block ran these three
        # cleanup steps sequentially with no per-step isolation, so a
        # failure in an earlier step (e.g. the source-Neo4j wipe, after a
        # prod-scale run stressed the same infra) would skip the study-
        # substrate wipe entirely — leaving stale synthetic data for
        # master's next real export run. The study wipe runs FIRST since
        # it matters most.
        async def _wipe_study() -> None:
            study_driver = AsyncGraphDatabase.driver(  # fre-375-allow: study substrate teardown
                study_settings.neo4j_uri,
                auth=(study_settings.neo4j_user, study_settings.neo4j_password),
            )
            try:
                async with study_driver.session() as session:
                    await session.run("MATCH (n) DETACH DELETE n")
            finally:
                await study_driver.close()

        async def _wipe_source_neo4j() -> None:
            cleanup_driver = AsyncGraphDatabase.driver(  # fre-375-allow: test-infra teardown only
                app_settings.neo4j_uri, auth=(app_settings.neo4j_user, app_settings.neo4j_password)
            )
            try:
                async with cleanup_driver.session() as session:
                    await session.run("MATCH (n) WHERE n.sk IS NOT NULL DETACH DELETE n")
            finally:
                await cleanup_driver.close()

        async def _wipe_source_postgres() -> None:
            pg_conn = await asyncpg.connect(pg_dsn)
            try:
                await pg_conn.execute(
                    "DELETE FROM sessions WHERE session_id = ANY($1::uuid[])",
                    [uuid.UUID(s) for s in session_uuids],
                )
                await pg_conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            finally:
                await pg_conn.close()

        errors: list[Exception] = []
        for step in (_wipe_study, _wipe_source_neo4j, _wipe_source_postgres):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 — must not skip the remaining cleanup steps
                errors.append(exc)
        if errors:
            raise ExceptionGroup("scale-test cleanup step(s) failed", errors)


class _Args:
    def __init__(self, execute: bool, snapshot_dir: str) -> None:
        self.execute = execute
        self.snapshot_dir = snapshot_dir


@pytest.mark.asyncio
async def test_execute_mode_completes_at_prod_scale_without_oom(
    prod_scale_source_and_target: dict[str, Any], tmp_path: Any
) -> None:
    """The actual regression this fix-forward closes: a real --execute run
    against a prod-scale corpus must complete, not OOM-kill the study Neo4j
    (master's finding, 2026-07-10, at the pre-fix 1.5g mem_limit / 1g heap).
    """
    manifest = await run_export(_Args(execute=True, snapshot_dir=str(tmp_path)))

    assert manifest is not None, "run_export returned None — refused or crashed"
    assert manifest["prod_node_total"] == sum(NODE_COUNTS.values())
    assert manifest["prod_relationship_total"] == sum(spec[3] for spec in REL_SPECS)
    assert manifest["prod_session_count"] >= SESSIONS_WITH_TRACES

    study_settings = StudySettings()
    study_driver = (
        AsyncGraphDatabase.driver(  # fre-375-allow: study substrate, real assertion target
            study_settings.neo4j_uri,
            auth=(study_settings.neo4j_user, study_settings.neo4j_password),
        )
    )
    try:
        node_count, rel_count = await count_nodes_and_relationships(study_driver)
    finally:
        await study_driver.close()

    assert node_count == sum(NODE_COUNTS.values())
    assert rel_count == sum(spec[3] for spec in REL_SPECS)

    manifest_path = tmp_path / "snapshot_manifest.json"
    assert manifest_path.exists()
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk["content_hash"] == manifest["content_hash"]
