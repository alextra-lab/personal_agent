"""Unit tests for the cache-erosion scheduler runner (ADR-0078 / FRE-1189)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_report(any_eroded: bool = False) -> "ErosionReport":
    from personal_agent.observability.cache_erosion.monitor import ErosionReport

    return ErosionReport(
        computed_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc),
        results=[],
        any_eroded=any_eroded,
        threshold=0.9,
    )


class TestRunScheduledCacheErosionProbe:
    """run_scheduled_cache_erosion_probe drives compute → sink."""

    @pytest.mark.asyncio
    async def test_writes_to_es_when_client_provided(self) -> None:
        report = _make_report()
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=es)

        assert result is not None
        assert result.any_eroded is False
        assert result.run_at == report.computed_at
        mock_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        cfg = MagicMock()
        cfg.cache_erosion_probe_enabled = False

        with patch(
            "personal_agent.observability.cache_erosion.scheduler_runner.get_settings",
            return_value=cfg,
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=None)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_and_skips_compute_when_es_client_is_none(self) -> None:
        with patch(
            "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
            new=AsyncMock(),
        ) as mock_compute:
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=None)

        assert result is None
        mock_compute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_es_write_error(self) -> None:
        """An ES write failure must not propagate — scheduler must not crash — and the doc
        is still returned, since a write failure is not the same as "the probe did not run"
        (the scheduler's advance-timestamp check reads only the return value).
        """
        report = _make_report()
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.write_result",
                new=AsyncMock(side_effect=RuntimeError("ES down")),
            ),
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=es)

        assert result is not None


class TestRunScheduledCacheErosionProbeRootSpan:
    """AC-1/AC-2 (ADR-0129 D3, FRE-1069): the probe opens exactly one root span, and
    every log record it emits carries that span's identity and ``kind``.
    """

    @pytest.mark.asyncio
    async def test_opens_exactly_one_root_span_with_full_log_coverage(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from personal_agent.observability.cache_erosion.scheduler_runner import (
            run_scheduled_cache_erosion_probe,
        )
        from tests._helpers.log_capture import capture_log_records

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with capture_log_records() as records:
            # es_client=None: cheapest path — logs the no-client warning and returns,
            # without needing compute_erosion_report/write_result mocked.
            result = await run_scheduled_cache_erosion_probe(es_client=None, tracer=tracer)

        assert result is None

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent is None
        assert span.attributes is not None
        assert span.attributes["personal_agent.kind"] == "system:cache_erosion_probe"

        expected_trace_id = format(span.context.trace_id, "032x")
        expected_span_id = format(span.context.span_id, "016x")
        assert records, "expected at least one log record during the probe"
        for record in records:
            assert record.get("trace_id") == expected_trace_id
            assert record.get("span_id") == expected_span_id
            assert record.get("kind") == "system:cache_erosion_probe"
