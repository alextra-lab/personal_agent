"""Cost tracking service for API calls."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from personal_agent.config.settings import get_settings
from personal_agent.exceptions import MissingIdentityError
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


async def _publish_model_call_completed(
    *,
    trace_id: UUID,
    session_id: UUID,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    model_role: str | None,
) -> None:
    """Publish a best-effort live cost event for the ADR-0088 projector (FRE-513).

    ``record_api_call`` is the hard-enforced identity boundary every model call passes
    through — including sub-agents — so emitting here gives the live meter a
    topology-independent cost cadence (D3) without per-loop accumulation. Live-only: the
    durable ``api_costs`` row is the source of truth and a bus failure never affects it.

    Args:
        trace_id: Trace UUID of the request that produced this cost.
        session_id: Session UUID the request ran in.
        cost_usd: Cost of this single model call in USD.
        input_tokens: Prompt tokens billed for this call.
        output_tokens: Completion tokens billed for this call.
        model_role: Purpose / model role attributed to the call, when known.
    """
    try:
        from personal_agent.events import get_event_bus
        from personal_agent.events.models import (
            STREAM_TURN_OBSERVED,
            ModelCallCompletedEvent,
        )
        from personal_agent.observability.topology import current_topology

        await get_event_bus().publish(
            STREAM_TURN_OBSERVED,
            ModelCallCompletedEvent(
                trace_id=str(trace_id),
                session_id=str(session_id),
                cost_usd=float(cost_usd),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_role=model_role,
                # ADR-0088 D7: stamp the active topology so a call made outside any
                # observe_topology surfaces as topology=None (an out-of-seam violation).
                topology=current_topology(),
            ),
            maxlen=settings.turn_observed_stream_maxlen,
        )
    except Exception:
        log.debug("model_call_completed_publish_failed", trace_id=str(trace_id))


class CostTrackerService:
    """Service for persisting API cost tracking to PostgreSQL."""

    def __init__(self) -> None:
        """Initialize cost tracker service."""
        self.pool: asyncpg.Pool | None = None
        self.db_url = _normalize_asyncpg_dsn(settings.database_url)

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
        trace_id: UUID,
        session_id: UUID,
        purpose: str | None = None,
        latency_ms: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
    ) -> int | None:
        """Record an API call cost to the database.

        Args:
            provider: API provider (e.g., ``"anthropic"``, ``"openai"``).
            model: Model name (e.g., ``"claude-sonnet-4.5"``).
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.
            cost_usd: Cost in USD.  For Anthropic, this reflects cache-tier
                pricing via ``litellm.completion_cost()`` — cache reads at
                ~0.1× and cache creation at ~1.25× the standard input rate.
            trace_id: Trace UUID of the request that produced this cost.
                Required by ADR-0074 (FRE-376) — the cost row must be
                attributable to the originating request.
            session_id: Session UUID the request ran in. Also required by
                ADR-0074 so cost rows roll up to a session.
            purpose: Optional purpose (``"user_request"``, ``"second_brain"``,
                ``"entity_extraction"`` …).
            latency_ms: Wall-clock time of the API round-trip in milliseconds.
            cache_read_input_tokens: Anthropic cache-read tokens (FRE-437).
                ``None`` for non-Anthropic providers or calls without caching.
            cache_creation_input_tokens: Anthropic cache-creation tokens
                (FRE-437). ``None`` for non-Anthropic providers.

        Returns:
            ID of inserted record, or ``None`` if the pool is unavailable or
            the underlying INSERT failed.

        Raises:
            MissingIdentityError: If ``trace_id`` or ``session_id`` is ``None``.
                ADR-0074 makes identity load-bearing; silently inserting NULL
                produced 4,077 unattributable rows in production before this
                contract was tightened.
        """
        if trace_id is None or session_id is None:
            raise MissingIdentityError(
                f"record_api_call requires trace_id and session_id "
                f"(got trace_id={trace_id!r}, session_id={session_id!r})"
            )

        if not self.pool:
            log.warning(
                "cost_tracker_not_connected",
                provider=provider,
                trace_id=str(trace_id),
            )
            return None

        try:
            async with self.pool.acquire() as conn:
                record_id = await conn.fetchval(
                    """
                    INSERT INTO api_costs (
                        timestamp, provider, model,
                        input_tokens, output_tokens, cost_usd,
                        cache_read_input_tokens, cache_creation_input_tokens,
                        trace_id, session_id, purpose, latency_ms
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING id
                    """,
                    datetime.now(timezone.utc),
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    Decimal(str(cost_usd)),  # Convert to Decimal for precision
                    cache_read_input_tokens,
                    cache_creation_input_tokens,
                    trace_id,
                    session_id,
                    purpose,
                    latency_ms,
                )

                log.debug(
                    "api_cost_recorded",
                    provider=provider,
                    model=model,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    record_id=record_id,
                    trace_id=str(trace_id),
                    session_id=str(session_id),
                    cache_read_input_tokens=cache_read_input_tokens,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                )

            # Connection released before the (best-effort, live-only) bus publish.
            await _publish_model_call_completed(
                trace_id=trace_id,
                session_id=session_id,
                cost_usd=cost_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_role=purpose,
            )
            return cast(int | None, record_id)

        except Exception as e:
            log.error(
                "cost_recording_failed",
                error=str(e),
                exc_info=True,
                provider=provider,
                trace_id=str(trace_id),
            )
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


def _normalize_asyncpg_dsn(database_url: str) -> str:
    """Normalize SQLAlchemy-style URLs to asyncpg-compatible DSNs.

    Args:
        database_url: Raw database URL from app settings.

    Returns:
        DSN accepted by asyncpg.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if database_url.startswith("postgres+asyncpg://"):
        return database_url.replace("postgres+asyncpg://", "postgres://", 1)
    return database_url
