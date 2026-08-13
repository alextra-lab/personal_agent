"""Convenience wrapper for invoking the delivery-ratio probe from the scheduler.

Opens its own short-lived Postgres connection (the brainstem scheduler's ES client
is accepted as a parameter, mirroring the joinability probe's pattern), measures
yesterday's UTC window, writes the result doc to Elasticsearch, and releases the
connection. Any failure is logged and swallowed — the scheduler must not crash
because a probe tick failed. Does not touch :mod:`probe` or :mod:`collect`'s verdict
logic (FRE-1051) — execution and persistence only.

Uses a single ``asyncpg.connect()`` connection rather than a pool: ``collect_report``
issues one sequential Postgres count per run, so a pool's setup/teardown cost buys
no reuse or concurrency at a daily cadence (unlike joinability, which walks multiple
substrates and justifies its pool).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.observability.delivery_ratio.collect import collect_report
from personal_agent.observability.delivery_ratio.result import (
    DeliveryRatioResultDoc,
    from_report,
)
from personal_agent.observability.delivery_ratio.sink import write_result
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.spans import close_root_span, open_root_span
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
    from opentelemetry.trace import Tracer

log = get_logger(__name__)

_PG_CONNECT_TIMEOUT_SECONDS = 10.0


async def run_scheduled_delivery_ratio_probe(
    *, es_client: "AsyncElasticsearch | None", tracer: "Tracer | None" = None
) -> DeliveryRatioResultDoc | None:
    """Run one delivery-ratio probe tick from the brainstem scheduler.

    Opens a root span for the tick (ADR-0129 D3, FRE-1069, folded in alongside
    this ticket's three named sibling probes) — only when the probe actually
    runs; a disabled probe produces no span.

    Args:
        es_client: Already-open ``AsyncElasticsearch`` client owned by the
            scheduler's :class:`DataLifecycleManager`. The probe also needs
            Postgres, opened here as a short-lived connection since the
            scheduler does not otherwise own one.
        tracer: Tracer to open the root span with. Defaults to the
            process-wide tracer; tests inject their own tracer bound to an
            in-memory exporter.

    Returns:
        The :class:`DeliveryRatioResultDoc` for the run, or ``None`` if the
        probe is disabled, no ES client is available, or Postgres could not
        be reached. A ``None`` return means "did not run this tick" — the
        caller must not advance its last-run timestamp on ``None``, only on
        a produced doc.
    """
    settings = get_settings()
    if not getattr(settings, "delivery_ratio_probe_enabled", True):
        return None

    span, token, cv_tokens = open_root_span("delivery_ratio_probe", tracer=tracer)
    pg_conn = None
    try:
        ctx = SystemTraceContext.new("delivery_ratio_probe")
        if es_client is None:
            log.warning(
                "delivery_ratio_probe_skipped_no_es_client",
                trace_id=ctx.trace_id,
                component="delivery_ratio",
            )
            return None

        run_at = datetime.now(timezone.utc)
        yesterday = (run_at - timedelta(days=1)).date()

        pg_conn = await _open_pg_conn()
        if pg_conn is None:
            log.warning(
                "delivery_ratio_probe_pg_open_failed",
                trace_id=ctx.trace_id,
                component="delivery_ratio",
            )
            return None

        report = await collect_report(
            es_client,
            pg_conn,
            logs_prefix=settings.elasticsearch_index_prefix,
            since=yesterday,
            until=yesterday,
            min_ratio=settings.delivery_ratio_probe_min_ratio,
        )
        doc = from_report(report, run_at=run_at, trace_id=ctx.trace_id)

        log.info(
            "delivery_ratio_probe_completed",
            status=doc.status,
            trace_id=ctx.trace_id,
            component="delivery_ratio",
        )

        try:
            await write_result(es_client, doc, prefix=settings.delivery_ratio_probe_index_prefix)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "delivery_ratio_probe_es_write_failed",
                error=str(exc),
                trace_id=ctx.trace_id,
                component="delivery_ratio",
            )

        return doc
    finally:
        try:
            if pg_conn is not None:
                await _close_pg_conn(pg_conn)
        finally:
            close_root_span(span, token, cv_tokens)


async def _open_pg_conn() -> Any | None:
    """Open a short-lived asyncpg connection against ``settings.database_url``.

    Returns:
        An open connection, or ``None`` if asyncpg is unavailable or the
        connection failed (e.g. a transient outage).
    """
    settings = get_settings()
    try:
        import asyncpg  # type: ignore[import-untyped]

        dsn = _normalize_asyncpg_dsn(settings.database_url)
        conn: Any = await asyncpg.connect(dsn, timeout=_PG_CONNECT_TIMEOUT_SECONDS)
        return conn
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "delivery_ratio_probe_pg_connect_failed",
            error=str(exc),
            component="delivery_ratio",
        )
        return None


async def _close_pg_conn(conn: Any) -> None:
    """Close a Postgres connection, catching and logging a close failure.

    A bare ``await conn.close()`` in a ``finally`` block can raise and overturn
    an otherwise-successful return; this helper mirrors
    :func:`personal_agent.observability.joinability.scheduler_runner._close`'s
    per-resource try/except so that never happens.

    Args:
        conn: The connection to close.
    """
    try:
        await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "delivery_ratio_probe_pg_close_failed",
            error=str(exc),
            component="delivery_ratio",
        )
