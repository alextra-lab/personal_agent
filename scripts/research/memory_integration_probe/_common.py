"""Shared helpers for the memory-integration probe scripts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from neo4j import AsyncGraphDatabase, AsyncSession

from personal_agent.config import settings

OUTPUT_DIR = Path(__file__).parent / "output"


@asynccontextmanager
async def neo4j_session() -> AsyncIterator[AsyncSession]:
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        async with driver.session() as session:
            yield session
    finally:
        await driver.close()


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
