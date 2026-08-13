"""Unit tests for the SLM-health scheduler runner (FRE-399 / ADR-0083)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_snapshot(status: str = "up") -> "SlmHealthSnapshot":
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    return SlmHealthSnapshot(
        status=status,  # type: ignore[arg-type]
        reachable=status != "down",
        probed_at=datetime.now(timezone.utc),
        trace_id="scheduler-runner-test",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    from personal_agent.observability.slm_health.cache import clear_cache

    clear_cache()
    yield
    clear_cache()


class TestRunScheduledSlmHealthProbe:
    """run_scheduled_slm_health_probe drives probe → cache → ES write."""

    @pytest.mark.asyncio
    async def test_writes_to_es_when_client_provided(self) -> None:
        snap = _make_snapshot("up")
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.probe_slm_health",
                new=AsyncMock(return_value=snap),
            ),
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.slm_health.scheduler_runner import (
                run_scheduled_slm_health_probe,
            )

            result = await run_scheduled_slm_health_probe(es_client=es)

        assert result is snap
        mock_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_cache(self) -> None:
        from personal_agent.observability.slm_health.cache import get_cached_snapshot

        snap = _make_snapshot("degraded")
        with (
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.probe_slm_health",
                new=AsyncMock(return_value=snap),
            ),
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.write_result",
                new=AsyncMock(),
            ),
        ):
            from personal_agent.observability.slm_health.scheduler_runner import (
                run_scheduled_slm_health_probe,
            )

            await run_scheduled_slm_health_probe(es_client=None)

        cached = get_cached_snapshot(ttl=60.0)
        assert cached is snap

    @pytest.mark.asyncio
    async def test_no_es_write_when_client_is_none(self) -> None:
        snap = _make_snapshot()
        with (
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.probe_slm_health",
                new=AsyncMock(return_value=snap),
            ),
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.slm_health.scheduler_runner import (
                run_scheduled_slm_health_probe,
            )

            await run_scheduled_slm_health_probe(es_client=None)

        mock_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_es_write_error(self) -> None:
        """An ES write failure must not propagate — scheduler must not crash."""
        snap = _make_snapshot()
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.probe_slm_health",
                new=AsyncMock(return_value=snap),
            ),
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.write_result",
                new=AsyncMock(side_effect=RuntimeError("ES down")),
            ),
        ):
            from personal_agent.observability.slm_health.scheduler_runner import (
                run_scheduled_slm_health_probe,
            )

            # Must not raise
            result = await run_scheduled_slm_health_probe(es_client=es)

        assert result is snap

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        """Master switch off → returns None without calling probe."""
        cfg = MagicMock()
        cfg.slm_health_probe_enabled = False

        with patch(
            "personal_agent.observability.slm_health.scheduler_runner.get_settings",
            return_value=cfg,
        ):
            from personal_agent.observability.slm_health.scheduler_runner import (
                run_scheduled_slm_health_probe,
            )

            result = await run_scheduled_slm_health_probe(es_client=None)

        assert result is None


class TestRunScheduledSlmHealthProbeRootSpan:
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

        from personal_agent.observability.slm_health.scheduler_runner import (
            run_scheduled_slm_health_probe,
        )
        from tests._helpers.log_capture import capture_log_records

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        snap = _make_snapshot("up")
        with (
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.probe_slm_health",
                new=AsyncMock(return_value=snap),
            ),
            patch(
                "personal_agent.observability.slm_health.scheduler_runner.write_result",
                new=AsyncMock(),
            ),
            capture_log_records() as records,
        ):
            await run_scheduled_slm_health_probe(es_client=None, tracer=tracer)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent is None
        assert span.attributes is not None
        assert span.attributes["personal_agent.kind"] == "system:slm_health_probe"

        expected_trace_id = format(span.context.trace_id, "032x")
        expected_span_id = format(span.context.span_id, "016x")
        assert records, "expected at least one log record during the probe"
        for record in records:
            assert record.get("trace_id") == expected_trace_id
            assert record.get("span_id") == expected_span_id
            assert record.get("kind") == "system:slm_health_probe"
