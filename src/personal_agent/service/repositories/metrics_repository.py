"""Metrics storage repository using Postgres."""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import MetricModel, MetricQuery, MetricStats, MetricWrite


class MetricsRepository:
    """Repository for metrics storage and querying.

    Usage:
        async with get_db_session() as db:
            repo = MetricsRepository(db)
            await repo.write(MetricWrite(metric_name="cpu_percent", metric_value=45.2))
    """

    def __init__(self, db: AsyncSession):  # noqa: D107
        """Initialize repository with database session."""
        self.db = db

    async def write(self, metric: MetricWrite) -> MetricModel:
        """Write a single metric.

        Args:
            metric: Metric to write

        Returns:
            Created metric model
        """
        model = MetricModel(
            timestamp=datetime.utcnow(),
            trace_id=metric.trace_id,
            metric_name=metric.metric_name,
            metric_value=metric.metric_value,
            unit=metric.unit,
            tags=metric.tags,
        )
        self.db.add(model)
        await self.db.commit()
        await self.db.refresh(model)
        return model

    async def write_batch(self, metrics: list[MetricWrite]) -> int:
        """Write multiple metrics efficiently.

        Args:
            metrics: List of metrics to write

        Returns:
            Number of metrics written
        """
        now = datetime.utcnow()
        models = [
            MetricModel(
                timestamp=now,
                trace_id=m.trace_id,
                metric_name=m.metric_name,
                metric_value=m.metric_value,
                unit=m.unit,
                tags=m.tags,
            )
            for m in metrics
        ]
        self.db.add_all(models)
        await self.db.commit()
        return len(models)

    async def query(self, params: MetricQuery) -> list[MetricModel]:
        """Query metrics with filters.

        Args:
            params: Query parameters

        Returns:
            List of matching metrics
        """
        stmt = select(MetricModel)

        if params.metric_name:
            stmt = stmt.where(MetricModel.metric_name == params.metric_name)
        if params.trace_id:
            stmt = stmt.where(MetricModel.trace_id == params.trace_id)
        if params.start_time:
            stmt = stmt.where(MetricModel.timestamp >= params.start_time)
        if params.end_time:
            stmt = stmt.where(MetricModel.timestamp <= params.end_time)

        stmt = stmt.order_by(MetricModel.timestamp.desc()).limit(params.limit)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_stats(self, metric_name: str, hours: int = 24) -> Optional[MetricStats]:
        """Get statistical summary for a metric.

        Args:
            metric_name: Name of metric
            hours: Time window in hours

        Returns:
            MetricStats or None if no data
        """
        start_time = datetime.utcnow() - timedelta(hours=hours)

        result = await self.db.execute(
            select(
                func.count(MetricModel.id).label("count"),
                func.min(MetricModel.metric_value).label("min_value"),
                func.max(MetricModel.metric_value).label("max_value"),
                func.avg(MetricModel.metric_value).label("avg_value"),
            )
            .where(MetricModel.metric_name == metric_name)
            .where(MetricModel.timestamp >= start_time)
        )
        row = result.one()

        if row.count == 0:
            return None

        return MetricStats(
            metric_name=metric_name,
            count=row.count,
            min_value=row.min_value,
            max_value=row.max_value,
            avg_value=float(row.avg_value),
        )

    async def get_recent_by_trace(self, trace_id: UUID) -> list[MetricModel]:
        """Get all metrics for a specific trace.

        Args:
            trace_id: Trace UUID

        Returns:
            All metrics for that trace
        """
        result = await self.db.execute(
            select(MetricModel)
            .where(MetricModel.trace_id == trace_id)
            .order_by(MetricModel.timestamp.asc())
        )
        return list(result.scalars().all())
