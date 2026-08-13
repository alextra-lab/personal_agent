"""Unit tests for the delivery-ratio scheduler runner (FRE-1051 / FRE-1189)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_report(status: str = "pass") -> "DeliveryReport":
    from personal_agent.observability.delivery_ratio.probe import DeliveryReport

    yesterday = date.today() - timedelta(days=1)
    return DeliveryReport(since=yesterday, until=yesterday, families=[])


class TestRunScheduledDeliveryRatioProbe:
    """run_scheduled_delivery_ratio_probe drives connect → collect → sink."""

    @pytest.mark.asyncio
    async def test_writes_to_es_when_client_and_pg_available(self) -> None:
        report = _make_report()
        es = AsyncMock()
        fake_conn = AsyncMock()

        with (
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._open_pg_conn",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._close_pg_conn",
                new=AsyncMock(),
            ) as mock_close,
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.collect_report",
                new=AsyncMock(return_value=report),
            ) as mock_collect,
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            result = await run_scheduled_delivery_ratio_probe(es_client=es)

        assert result is not None
        mock_write.assert_awaited_once()
        mock_close.assert_awaited_once_with(fake_conn)
        # since/until must both be yesterday, derived from one captured run_at.
        call_kwargs = mock_collect.call_args.kwargs
        assert call_kwargs["since"] == call_kwargs["until"]
        assert call_kwargs["since"] == date.today() - timedelta(days=1)

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        cfg = MagicMock()
        cfg.delivery_ratio_probe_enabled = False

        with patch(
            "personal_agent.observability.delivery_ratio.scheduler_runner.get_settings",
            return_value=cfg,
        ):
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            result = await run_scheduled_delivery_ratio_probe(es_client=None)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_and_skips_pg_open_when_es_client_is_none(self) -> None:
        with patch(
            "personal_agent.observability.delivery_ratio.scheduler_runner._open_pg_conn",
            new=AsyncMock(),
        ) as mock_open:
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            result = await run_scheduled_delivery_ratio_probe(es_client=None)

        assert result is None
        mock_open.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_none_when_pg_open_fails(self) -> None:
        """A Postgres-open failure (e.g. transient outage) must not write to ES and must
        return None — the scheduler must retry next tick, not wait a full interval.
        """
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._open_pg_conn",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            result = await run_scheduled_delivery_ratio_probe(es_client=es)

        assert result is None
        mock_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_es_write_error(self) -> None:
        report = _make_report()
        es = AsyncMock()
        fake_conn = AsyncMock()

        with (
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._open_pg_conn",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._close_pg_conn",
                new=AsyncMock(),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.collect_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.write_result",
                new=AsyncMock(side_effect=RuntimeError("ES down")),
            ),
        ):
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            result = await run_scheduled_delivery_ratio_probe(es_client=es)

        assert result is not None

    @pytest.mark.asyncio
    async def test_pg_conn_closed_even_when_write_result_raises(self) -> None:
        """Verifies the finally: _close_pg_conn(...) runs regardless of write outcome."""
        report = _make_report()
        es = AsyncMock()
        fake_conn = AsyncMock()

        with (
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._open_pg_conn",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner._close_pg_conn",
                new=AsyncMock(),
            ) as mock_close,
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.collect_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.delivery_ratio.scheduler_runner.write_result",
                new=AsyncMock(side_effect=RuntimeError("ES down")),
            ),
        ):
            from personal_agent.observability.delivery_ratio.scheduler_runner import (
                run_scheduled_delivery_ratio_probe,
            )

            await run_scheduled_delivery_ratio_probe(es_client=es)

        mock_close.assert_awaited_once_with(fake_conn)


class TestClosePgConn:
    """_close_pg_conn swallows a close failure rather than propagating it."""

    @pytest.mark.asyncio
    async def test_close_failure_is_caught_and_logged(self) -> None:
        from personal_agent.observability.delivery_ratio.scheduler_runner import (
            _close_pg_conn,
        )

        fake_conn = AsyncMock()
        fake_conn.close = AsyncMock(side_effect=RuntimeError("close failed"))

        # Must not raise.
        await _close_pg_conn(fake_conn)

        fake_conn.close.assert_awaited_once()


class TestOpenPgConn:
    """_open_pg_conn wraps asyncpg.connect, returning None on failure (AC-5: settings-driven DSN)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_connect_raises(self) -> None:
        with patch(
            "personal_agent.observability.delivery_ratio.scheduler_runner._normalize_asyncpg_dsn",
            return_value="postgresql://fake",
        ):
            fake_asyncpg = MagicMock()
            fake_asyncpg.connect = AsyncMock(side_effect=RuntimeError("pg down"))
            with patch.dict("sys.modules", {"asyncpg": fake_asyncpg}):
                from personal_agent.observability.delivery_ratio.scheduler_runner import (
                    _open_pg_conn,
                )

                result = await _open_pg_conn()

        assert result is None


class TestRunScheduledDeliveryRatioProbeRootSpan:
    """AC-1/AC-2 (ADR-0129 D3, FRE-1069): the probe opens exactly one root span, and
    every log record it emits carries that span's identity and ``kind``.

    Folded in as a sibling to the ticket's three named observability probes — see
    the ticket's PR/handoff notes.
    """

    @pytest.mark.asyncio
    async def test_opens_exactly_one_root_span_with_full_log_coverage(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from personal_agent.observability.delivery_ratio.scheduler_runner import (
            run_scheduled_delivery_ratio_probe,
        )
        from tests._helpers.log_capture import capture_log_records

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with capture_log_records() as records:
            # es_client=None: cheapest path — logs the no-client warning and returns,
            # without needing Postgres/collect_report/write_result mocked.
            result = await run_scheduled_delivery_ratio_probe(es_client=None, tracer=tracer)

        assert result is None

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent is None
        assert span.attributes is not None
        assert span.attributes["personal_agent.kind"] == "system:delivery_ratio_probe"

        expected_trace_id = format(span.context.trace_id, "032x")
        expected_span_id = format(span.context.span_id, "016x")
        assert records, "expected at least one log record during the probe"
        for record in records:
            assert record.get("trace_id") == expected_trace_id
            assert record.get("span_id") == expected_span_id
            assert record.get("kind") == "system:delivery_ratio_probe"
