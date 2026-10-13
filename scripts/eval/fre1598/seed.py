"""FRE-1598: write and read ``sessions.revealed_tools`` in postgres-eval (the control arm's seed).

The owner chose the control arm on 2026-10-13: the final build with no skill catalog, and the two
skill-provided tools visible from call 1. The gateway loads ``sessions.revealed_tools`` at the start
of each turn (``orchestrator/executor.py``, ``_load_revealed_tools``), so a row written before
``/chat`` makes the tools visible. No API can write the column (a PATCH refuses it, ADR-0156 A5.4),
so this module writes it directly, to postgres-eval only.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import quote

import asyncpg  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from asyncpg import Connection

EVAL_DB_HOST = "localhost"
EVAL_DB_PORT = 5434  # docker-compose.eval.yml, postgres-eval
EVAL_DB_NAME = "personal_agent"
EVAL_DB_USER = "agent"

ADD_SQL = (
    "UPDATE sessions SET revealed_tools = ARRAY("
    "SELECT DISTINCT n FROM unnest(revealed_tools || $2::TEXT[]) AS n ORDER BY n) "
    "WHERE session_id = $1::uuid"
)
READ_SQL = "SELECT revealed_tools FROM sessions WHERE session_id = $1::uuid"


def eval_dsn(password: str, port: int = EVAL_DB_PORT) -> str:
    """Return the postgres-eval DSN.

    Args:
        password: The eval Postgres password.
        port: The port; must be the eval port.

    Returns:
        The DSN.

    Raises:
        ValueError: The port is not postgres-eval's (5432 is production).
    """
    if port != EVAL_DB_PORT:
        raise ValueError(f"port {port} is not postgres-eval ({EVAL_DB_PORT})")
    return f"postgresql://{EVAL_DB_USER}:{quote(password, safe='')}@{EVAL_DB_HOST}:{port}/{EVAL_DB_NAME}"


class EvalRevealSeeder:
    """A ``RevealSeeder`` (``scripts/eval/fre1590/help.py``) on one postgres-eval connection."""

    def __init__(self, conn: Connection) -> None:
        """Wrap an open connection.

        Args:
            conn: A connection from :meth:`connect`.
        """
        self._conn = conn

    @classmethod
    async def connect(cls) -> EvalRevealSeeder:
        """Connect to postgres-eval with ``POSTGRES_PASSWORD``.

        Returns:
            The seeder.

        Raises:
            RuntimeError: ``POSTGRES_PASSWORD`` is not set.
        """
        password = os.environ.get("POSTGRES_PASSWORD")
        if not password:
            raise RuntimeError("POSTGRES_PASSWORD (the postgres-eval password) must be set")
        return cls(await asyncpg.connect(eval_dsn(password)))

    async def close(self) -> None:
        """Close the connection."""
        await self._conn.close()

    async def seed(self, session_id: str, names: Sequence[str]) -> list[str]:
        """Add ``names`` to one session's revealed tools, as the server's own add does.

        Args:
            session_id: The session, created a moment before by ``POST /sessions``.
            names: The tool names.

        Returns:
            The stored names, sorted (read back after the write).

        Raises:
            ValueError: The update did not change exactly one row.
        """
        status = await self._conn.execute(ADD_SQL, session_id, list(names))
        if status != "UPDATE 1":
            raise ValueError(f"seeding session {session_id} returned {status!r}, not 'UPDATE 1'")
        return await self.read(session_id)

    async def read(self, session_id: str) -> list[str]:
        """Read one session's revealed tools.

        Args:
            session_id: The session.

        Returns:
            The names, sorted.

        Raises:
            ValueError: No such session.
        """
        row = await self._conn.fetchrow(READ_SQL, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in postgres-eval")
        return sorted(row["revealed_tools"] or [])
