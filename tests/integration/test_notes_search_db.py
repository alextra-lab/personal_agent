"""DB-backed integration test for notes_search_executor (FRE-384 regression guard).

Exercises the real asyncpg driver to catch parameter-binding errors that fully-mocked
unit tests cannot detect. Specifically guards against the `AmbiguousParameterError` on
the bare `:tag_filter IS NULL` predicate that was the root cause of FRE-384.

Requires the isolated test substrate:

    make test-infra-up   # Postgres on :5433 (conftest.py redirects DATABASE_URL)

Invoked by:

    PERSONAL_AGENT_INTEGRATION=1 pytest -m integration -k notes_search -v
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal
from personal_agent.tools.notes_tools import _pgvector_literal, notes_search_executor

pytestmark = pytest.mark.integration

# Deterministic 1024-dim embedding so tests don't need the embedding server.
_TEST_EMBEDDING: list[float] = [0.01] * 1024
_TEST_TAG = "fre-384-regression"


def _postgres_available() -> bool:
    """Return True when the test Postgres substrate (port 5433) is reachable.

    Uses a raw TCP connect rather than a SQLAlchemy connection so it is safe to
    call from any test regardless of which asyncio event loop is active.
    """
    import socket

    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


def _ctx(user_id: UUID) -> Any:
    """Minimal orchestrator context stub for notes_search_executor."""
    return SimpleNamespace(user_id=user_id, trace_id="fre-384-test")


class TestNotesSearchDB:
    """Real-DB round-trips for notes_search_executor (asyncpg parameter binding)."""

    @pytest.mark.asyncio
    async def test_search_without_tags_returns_results(self) -> None:
        """notes_search_executor(tags=None) must not raise AmbiguousParameterError.

        This is the exact call path that failed before the FRE-384 fix: the bare
        `:tag_filter IS NULL` predicate was unresolvable by asyncpg's prepared-statement
        protocol, so *every* search call errored regardless of whether tags were passed.
        """
        if not _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        user_id = uuid4()
        artifact_id = uuid4()
        r2_key = f"notes/test/{artifact_id}/note.md"

        async with AsyncSessionLocal() as db:
            # Seed user (required by artifacts FK).
            await db.execute(
                text(
                    "INSERT INTO users (user_id, email) VALUES (:uid, :email)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"uid": user_id, "email": f"test-fre384-{user_id}@test.invalid"},
            )
            # Seed one note row with a deterministic embedding.
            await db.execute(
                text(
                    "INSERT INTO artifacts"
                    " (id, user_id, type, slug, title, content_type, size_bytes,"
                    "  r2_key, tags, embedding, created_by)"
                    " VALUES (:id, :uid, 'note', :slug, :title, :ct, :sz,"
                    "         :rk, CAST(:tags AS text[]), CAST(:emb AS vector), 'agent')"
                ),
                {
                    "id": artifact_id,
                    "uid": user_id,
                    "slug": "fre-384-test-note",
                    "title": "FRE-384 regression test note",
                    "ct": "text/markdown; charset=utf-8",
                    "sz": 42,
                    "rk": r2_key,
                    "tags": [_TEST_TAG],
                    "emb": _pgvector_literal(_TEST_EMBEDDING),
                },
            )
            await db.commit()

        try:
            result = await notes_search_executor(
                query="regression test",
                k=5,
                tags=None,  # ← the previously broken path
                ctx=_ctx(user_id),
            )
            assert result["result_count"] >= 1
            slugs = [r["slug"] for r in result["results"]]
            assert "fre-384-test-note" in slugs
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("DELETE FROM artifacts WHERE id = :id"),
                    {"id": artifact_id},
                )
                await db.execute(
                    text("DELETE FROM users WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                await db.commit()

    @pytest.mark.asyncio
    async def test_search_with_matching_tag_filters_correctly(self) -> None:
        """notes_search_executor(tags=[...]) returns only notes with that tag.

        Also validates that the CAST(:tag_filter AS text[]) path works for non-None
        values — ensuring the tag-filter branch is exercised end-to-end.
        """
        if not _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        user_id = uuid4()
        id_tagged = uuid4()
        id_untagged = uuid4()
        r2_base = f"notes/test/{user_id}"

        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "INSERT INTO users (user_id, email) VALUES (:uid, :email)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"uid": user_id, "email": f"test-fre384b-{user_id}@test.invalid"},
            )
            for row_id, tags, slug in [
                (id_tagged, [_TEST_TAG], "tagged-note"),
                (id_untagged, [], "untagged-note"),
            ]:
                await db.execute(
                    text(
                        "INSERT INTO artifacts"
                        " (id, user_id, type, slug, title, content_type, size_bytes,"
                        "  r2_key, tags, embedding, created_by)"
                        " VALUES (:id, :uid, 'note', :slug, :title, :ct, :sz,"
                        "         :rk, CAST(:tags AS text[]), CAST(:emb AS vector), 'agent')"
                    ),
                    {
                        "id": row_id,
                        "uid": user_id,
                        "slug": slug,
                        "title": slug,
                        "ct": "text/markdown; charset=utf-8",
                        "sz": 42,
                        "rk": f"{r2_base}/{row_id}/note.md",
                        "tags": tags,
                        "emb": _pgvector_literal(_TEST_EMBEDDING),
                    },
                )
            await db.commit()

        try:
            result = await notes_search_executor(
                query="test",
                k=10,
                tags=[_TEST_TAG],
                ctx=_ctx(user_id),
            )
            slugs = [r["slug"] for r in result["results"]]
            assert "tagged-note" in slugs, "tagged note must appear in filtered results"
            assert "untagged-note" not in slugs, "untagged note must be excluded by tag filter"
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("DELETE FROM artifacts WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                await db.execute(
                    text("DELETE FROM users WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                await db.commit()
