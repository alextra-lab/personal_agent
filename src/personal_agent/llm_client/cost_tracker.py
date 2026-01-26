"""Cost tracking service for API calls."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


class CostTrackerService:
    """Service for persisting API cost tracking to PostgreSQL."""

    def __init__(self) -> None:
        """Initialize cost tracker service."""
        self.pool: asyncpg.Pool | None = None
        self.db_url = settings.database_url

    async def connect(self) -> None:
        """Connect to PostgreSQL database."""
        try:
            self.pool = await asyncpg.create_pool(
                self.db_url,
                min_size=1,
                max_size=5,
                command_timeout=10,
            )
            log.info("cost_tracker_connected", database="postgresql")
        except Exception as e:
            log.error("cost_tracker_connection_failed", error=str(e), exc_info=True)
            self.pool = None

    async def disconnect(self) -> None:
        """Disconnect from database."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            log.info("cost_tracker_disconnected")

    async def record_api_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        trace_id: UUID | None = None,
        purpose: str | None = None,
    ) -> int | None:
        """Record an API call cost to the database.

        Args:
            provider: API provider (e.g., 'anthropic', 'openai')
            model: Model name (e.g., 'claude-sonnet-4.5')
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD
            trace_id: Optional trace ID for request tracking
            purpose: Optional purpose ('user_request', 'second_brain', etc.)

        Returns:
            ID of inserted record, or None if failed
        """
        if not self.pool:
            log.warning("cost_tracker_not_connected", provider=provider)
            return None

        try:
            async with self.pool.acquire() as conn:
                record_id = await conn.fetchval(
                    """
                    INSERT INTO api_costs (
                        timestamp, provider, model,
                        input_tokens, output_tokens, cost_usd,
                        trace_id, purpose
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    datetime.now(timezone.utc),
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    Decimal(str(cost_usd)),  # Convert to Decimal for precision
                    trace_id,
                    purpose,
                )

                log.debug(
                    "api_cost_recorded",
                    provider=provider,
                    model=model,
                    cost_usd=cost_usd,
                    record_id=record_id,
                )

                return record_id

        except Exception as e:
            log.error("cost_recording_failed", error=str(e), exc_info=True, provider=provider)
            return None

    async def get_total_cost(self, provider: str | None = None) -> float:
        """Get total API costs across all time.

        Args:
            provider: Optional provider filter

        Returns:
            Total cost in USD
        """
        if not self.pool:
            return 0.0

        try:
            async with self.pool.acquire() as conn:
                if provider:
                    result = await conn.fetchval(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_costs WHERE provider = $1",
                        provider,
                    )
                else:
                    result = await conn.fetchval("SELECT COALESCE(SUM(cost_usd), 0) FROM api_costs")

                return float(result) if result else 0.0

        except Exception as e:
            log.error("total_cost_fetch_failed", error=str(e), exc_info=True)
            return 0.0

    async def get_weekly_cost(self, provider: str | None = None, weeks: int = 1) -> float:
        """Get API costs for the last N weeks.

        Args:
            provider: Optional provider filter
            weeks: Number of weeks to look back (default: 1)

        Returns:
            Cost in USD for the specified period
        """
        if not self.pool:
            return 0.0

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7 * weeks)

            async with self.pool.acquire() as conn:
                if provider:
                    result = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(cost_usd), 0)
                        FROM api_costs
                        WHERE provider = $1 AND timestamp >= $2
                        """,
                        provider,
                        cutoff,
                    )
                else:
                    result = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(cost_usd), 0)
                        FROM api_costs
                        WHERE timestamp >= $1
                        """,
                        cutoff,
                    )

                return float(result) if result else 0.0

        except Exception as e:
            log.error("weekly_cost_fetch_failed", error=str(e), exc_info=True)
            return 0.0

    async def get_cost_summary(self, provider: str | None = None) -> dict[str, Any]:
        """Get comprehensive cost summary.

        Args:
            provider: Optional provider filter

        Returns:
            Dict with total, weekly, and monthly costs
        """
        total = await self.get_total_cost(provider)
        weekly = await self.get_weekly_cost(provider, weeks=1)
        monthly = await self.get_weekly_cost(provider, weeks=4)

        return {
            "total_cost_usd": total,
            "weekly_cost_usd": weekly,
            "monthly_cost_usd": monthly,
            "provider": provider if provider else "all",
        }

    async def get_cost_by_purpose(
        self, days: int = 7, provider: str | None = None
    ) -> dict[str, float]:
        """Get cost breakdown by purpose for the last N days.

        Args:
            days: Number of days to look back
            provider: Optional provider filter

        Returns:
            Dict mapping purpose to cost in USD
        """
        if not self.pool:
            return {}

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            async with self.pool.acquire() as conn:
                if provider:
                    rows = await conn.fetch(
                        """
                        SELECT purpose, SUM(cost_usd) as cost
                        FROM api_costs
                        WHERE provider = $1 AND timestamp >= $2
                        GROUP BY purpose
                        """,
                        provider,
                        cutoff,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT purpose, SUM(cost_usd) as cost
                        FROM api_costs
                        WHERE timestamp >= $1
                        GROUP BY purpose
                        """,
                        cutoff,
                    )

                return {row["purpose"] or "unknown": float(row["cost"]) for row in rows}

        except Exception as e:
            log.error("purpose_cost_fetch_failed", error=str(e), exc_info=True)
            return {}
