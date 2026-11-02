"""FRE-1728 AC-4: the hard spend stop for the MemAudit pilot.

Spend has two sources. The eval gateway books its own calls in ``api_costs``, on eval Postgres.
MemAudit's simulator and judges call OpenAI directly, so the driver reports their tokens to the bridge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import asyncpg  # type: ignore[import-untyped]

#: The ticket's hard stop.
PILOT_CAP_USD = 45.0

#: MemAudit's own model is gpt-5.4-mini (its ``llm_client.GPT_MODEL``). Prices from litellm's cost map.
MEMAUDIT_INPUT_USD_PER_TOKEN = 7.5e-07
MEMAUDIT_OUTPUT_USD_PER_TOKEN = 4.5e-06

_EVAL_POSTGRES_HOST = "localhost"
_EVAL_POSTGRES_PORT = 5434


def assert_eval_postgres_dsn(dsn: str) -> None:
    """Refuse every Postgres but the host port of ``postgres-eval``.

    Args:
        dsn: The connection string.

    Raises:
        ValueError: If the host or port is not eval Postgres.
    """
    parts = urlsplit(dsn)
    if parts.hostname != _EVAL_POSTGRES_HOST or parts.port != _EVAL_POSTGRES_PORT:
        raise ValueError(f"refused: {parts.hostname}:{parts.port} is not eval Postgres")


def postgres_spend_reader(dsn: str, since: datetime) -> Callable[[], Awaitable[float]]:
    """Build a reader of the gateway's spend since the run started.

    Args:
        dsn: Connection string for eval Postgres.
        since: The run's start.

    Returns:
        A coroutine function that returns the spend in USD.
    """
    assert_eval_postgres_dsn(dsn)

    async def read() -> float:
        connection = await asyncpg.connect(dsn)
        try:
            value = await connection.fetchval(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM api_costs WHERE timestamp >= $1", since
            )
        finally:
            await connection.close()
        return float(value)

    return read


def postgres_budget_denied_reader(dsn: str, since: datetime) -> Callable[[], Awaitable[int]]:
    """Build a reader of extraction attempts that the eval budget gate denied.

    A denied extraction leaves a fact out of the graph, and that looks like a write failure.

    Args:
        dsn: Connection string for eval Postgres.
        since: The run's start.

    Returns:
        A coroutine function that returns the count of denied attempts.
    """
    assert_eval_postgres_dsn(dsn)

    async def read() -> int:
        connection = await asyncpg.connect(dsn)
        try:
            value = await connection.fetchval(
                "SELECT count(*) FROM consolidation_attempts "
                "WHERE outcome = 'budget_denied' AND started_at >= $1",
                since,
            )
        finally:
            await connection.close()
        return int(value)

    return read


@dataclass
class SpendGuard:
    """Adds up the pilot's spend.

    Attributes:
        read_gateway_spend: Reads the eval gateway's booked spend.
        cap_usd: The stop.
        memaudit_usd: Spend that the driver reported for MemAudit's own calls.
    """

    read_gateway_spend: Callable[[], Awaitable[float]]
    cap_usd: float = PILOT_CAP_USD
    memaudit_usd: float = 0.0

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Add one MemAudit call.

        Args:
            prompt_tokens: Input tokens.
            completion_tokens: Output tokens.
        """
        self.memaudit_usd += (
            prompt_tokens * MEMAUDIT_INPUT_USD_PER_TOKEN
            + completion_tokens * MEMAUDIT_OUTPUT_USD_PER_TOKEN
        )

    async def spent(self) -> float:
        """Return the total spend in USD.

        Returns:
            Gateway spend plus MemAudit spend.
        """
        return await self.read_gateway_spend() + self.memaudit_usd
