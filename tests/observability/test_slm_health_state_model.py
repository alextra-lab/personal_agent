"""The SLM-health state model — FRE-1435 / ADR-0144 D4, rules 1 to 4.

Each test class maps to one acceptance criterion on FRE-1435. The tests drive the real cache, the
real reader (``is_provider_available``) and, where a probe is involved, the real
``probe_slm_health`` against an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from personal_agent.llm_client.inflight import InflightCall
from personal_agent.llm_client.models import ProviderDefinition
from personal_agent.llm_client.provider_health import is_provider_available
from personal_agent.observability.slm_health.cache import (
    clear_cache,
    get_cached_snapshot,
    inflight_verdict,
    set_cached_snapshot,
)
from personal_agent.observability.slm_health.scheduler_runner import (
    run_scheduled_slm_health_probe,
    slm_health_probe_interval_s,
)
from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

_TTL_S = 45.0
_NORMAL_INTERVAL_S = 300.0
_DOWN_INTERVAL_S = 60.0
_LOCAL = ProviderDefinition(placement="local", max_concurrency=3)
_PROBE_HTTP = "personal_agent.observability.slm_health.probe.create_guarded_http_client"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def _snap(
    status: Literal["up", "degraded", "down"],
    *,
    writer: Literal["probe", "call"] = "probe",
    observed: float | None = None,
    trace_id: str = "t",
) -> SlmHealthSnapshot:
    return SlmHealthSnapshot(
        status=status,
        reachable=status != "down",
        probed_at=datetime.now(timezone.utc),
        trace_id=trace_id,
        writer=writer,
        observed_monotonic=time.monotonic() if observed is None else observed,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(resolved_slm_health_url="https://slm.example.com/health")


def _client_factory(handler):
    """Return a ``create_guarded_http_client`` stand-in bound to a mock transport."""

    def _factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _factory


class TestAc1ProgressOutranksSilence:
    """AC-1: a stalled call beside a streaming one is not ``down``."""

    def test_one_silent_one_streaming_is_not_down(self) -> None:
        now = 10_000.0
        stalled = InflightCall(
            "span-a", 1, dispatched_monotonic=now - 300, last_byte_monotonic=now - 200
        )
        streaming = InflightCall(
            "span-b", 1, dispatched_monotonic=now - 300, last_byte_monotonic=now - 1
        )

        assert inflight_verdict([stalled, streaming], silence_threshold_s=90.0, now=now) == "up"

    def test_all_silent_is_down(self) -> None:
        """Seeded negative: the rule can return ``down``."""
        now = 10_000.0
        calls = [
            InflightCall(
                "span-a", 1, dispatched_monotonic=now - 300, last_byte_monotonic=now - 200
            ),
            InflightCall("span-b", 1, dispatched_monotonic=now - 300, last_byte_monotonic=now - 95),
        ]

        assert inflight_verdict(calls, silence_threshold_s=90.0, now=now) == "down"

    def test_chunkless_call_is_silent_from_dispatch(self) -> None:
        now = 10_000.0
        chunkless = InflightCall(
            "span-a", 1, dispatched_monotonic=now - 120, last_byte_monotonic=now - 120
        )

        assert inflight_verdict([chunkless], silence_threshold_s=90.0, now=now) == "down"

    def test_no_calls_is_no_verdict(self) -> None:
        assert inflight_verdict([], silence_threshold_s=90.0) is None


class TestAc2StaleWriteCannotOverwrite:
    """AC-2 (ADR-0144 AC-5(h)): a probe that began before a call failed cannot overwrite it."""

    @pytest.mark.asyncio
    async def test_slow_probe_write_does_not_replace_call_down(self, monkeypatch) -> None:
        """The slow probe generates, so its evidence alone would clear the ``down``.

        Only the observation-time ordering can reject it: the probe's stamp is its start,
        which is older than the call's failure.
        """
        in_flight = asyncio.Event()
        release = asyncio.Event()
        _recovered_generating_origin(monkeypatch, in_flight=in_flight, release=release)
        probe_task = asyncio.create_task(run_scheduled_slm_health_probe(es_client=None))
        await in_flight.wait()

        call_down = _snap("down", writer="call", trace_id="failing-call")
        assert set_cached_snapshot(call_down) is True
        release.set()
        probe_snapshot = await probe_task

        assert probe_snapshot is not None and probe_snapshot.generation_ok is True
        assert probe_snapshot.observed_monotonic < call_down.observed_monotonic
        stored = get_cached_snapshot(ttl=_TTL_S)
        assert stored is call_down and stored.trace_id == "failing-call"

    @pytest.mark.asyncio
    async def test_probe_write_lands_without_a_newer_call(self, monkeypatch) -> None:
        """Seeded negative: the same probe writes ``up`` when nothing newer exists."""

        async def origin(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        monkeypatch.setattr(_PROBE_HTTP, _client_factory(origin))
        older_up = _snap("up", writer="call", observed=time.monotonic() - 5)
        set_cached_snapshot(older_up)

        probe_snapshot = await run_scheduled_slm_health_probe(es_client=None)

        assert get_cached_snapshot(ttl=_TTL_S) is probe_snapshot

    def test_equal_or_older_observation_is_rejected(self) -> None:
        newer = _snap("down", observed=1_000.0)
        set_cached_snapshot(newer)

        assert set_cached_snapshot(_snap("up", observed=999.0)) is False
        assert set_cached_snapshot(_snap("up", observed=1_000.0)) is False
        assert get_cached_snapshot(ttl=_TTL_S) is newer


class TestAc3DownSurvivesTtl:
    """AC-3: a ``down`` survives the TTL with no traffic."""

    @pytest.mark.asyncio
    async def test_down_past_ttl_is_still_unavailable(self) -> None:
        set_cached_snapshot(_snap("down", writer="call", observed=time.monotonic() - (_TTL_S + 1)))

        assert await is_provider_available(_LOCAL, _settings()) is False  # type: ignore[arg-type]

    def test_down_past_ttl_is_still_read(self) -> None:
        down = _snap("down", observed=time.monotonic() - 10 * _TTL_S)
        set_cached_snapshot(down)

        assert get_cached_snapshot(ttl=_TTL_S) is down


class TestAc4PositiveEvidenceClearsDown:
    """AC-4: a successful call or a successful probe clears a standing ``down``.

    These drive the state model with the verdicts each writer publishes. The real call-side
    writer and the two-mode probe are FRE-1436, whose own criteria cover them end to end.
    """

    @pytest.mark.asyncio
    async def test_successful_call_clears_down(self) -> None:
        """(a) A call's ``up`` replaces a standing ``down``."""
        set_cached_snapshot(_snap("down", writer="call", observed=time.monotonic() - 1))
        call_up = _snap("up", writer="call")

        assert set_cached_snapshot(call_up) is True
        assert get_cached_snapshot(ttl=_TTL_S) is call_up
        assert await is_provider_available(_LOCAL, _settings()) is True  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_generating_probe_alone_clears_call_down(self, monkeypatch) -> None:
        """(b) With no real traffic, a probe that generates flips a call's ``down`` to ``up``."""
        _recovered_generating_origin(monkeypatch)
        set_cached_snapshot(_snap("down", writer="call", observed=time.monotonic() - 1))

        assert (
            slm_health_probe_interval_s(normal_s=_NORMAL_INTERVAL_S, down_s=_DOWN_INTERVAL_S)
            == _DOWN_INTERVAL_S
        )
        assert await is_provider_available(_LOCAL, _settings()) is False  # type: ignore[arg-type]

        probe_snapshot = await run_scheduled_slm_health_probe(es_client=None)

        assert probe_snapshot is not None and probe_snapshot.generation_ok is True
        assert get_cached_snapshot(ttl=_TTL_S) is probe_snapshot
        assert await is_provider_available(_LOCAL, _settings()) is True  # type: ignore[arg-type]
        assert (
            slm_health_probe_interval_s(normal_s=_NORMAL_INTERVAL_S, down_s=_DOWN_INTERVAL_S)
            == _NORMAL_INTERVAL_S
        )

    @pytest.mark.asyncio
    async def test_liveness_only_probe_does_not_clear_call_down(self, monkeypatch) -> None:
        """Seeded negative: a ``/health`` 200 is not evidence that a wedged slot recovered."""

        async def health_only(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        monkeypatch.setattr(_PROBE_HTTP, _client_factory(health_only))
        call_down = _snap("down", writer="call", observed=time.monotonic() - 1)
        set_cached_snapshot(call_down)

        probe_snapshot = await run_scheduled_slm_health_probe(es_client=None)

        assert probe_snapshot is not None and probe_snapshot.status == "up"
        assert probe_snapshot.generation_ok is None
        assert get_cached_snapshot(ttl=_TTL_S) is call_down
        assert await is_provider_available(_LOCAL, _settings()) is False  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_liveness_probe_clears_its_own_down(self, monkeypatch) -> None:
        """A probe-written ``down`` rested on liveness, so a liveness ``up`` clears it."""

        async def health_only(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        monkeypatch.setattr(_PROBE_HTTP, _client_factory(health_only))
        set_cached_snapshot(_snap("down", writer="probe", observed=time.monotonic() - 1))

        probe_snapshot = await run_scheduled_slm_health_probe(es_client=None)

        assert get_cached_snapshot(ttl=_TTL_S) is probe_snapshot
        assert await is_provider_available(_LOCAL, _settings()) is True  # type: ignore[arg-type]

    def test_failed_generation_does_not_clear_down(self) -> None:
        """A reachable origin whose generation check failed is not positive evidence."""
        down = _snap("down", writer="probe", observed=time.monotonic() - 1)
        set_cached_snapshot(down)
        failed = SlmHealthSnapshot(
            status="degraded",
            reachable=True,
            generation_ok=False,
            probed_at=datetime.now(timezone.utc),
            trace_id="probe",
        )

        assert set_cached_snapshot(failed) is False
        assert get_cached_snapshot(ttl=_TTL_S) is down

    @pytest.mark.asyncio
    async def test_probe_down_does_not_erase_call_provenance(self, monkeypatch) -> None:
        """A ``/health`` blip after a call failed must not let a later liveness ``up`` clear it."""
        call_down = _snap("down", writer="call", observed=time.monotonic() - 2)
        set_cached_snapshot(call_down)

        assert set_cached_snapshot(_snap("down", writer="probe", observed=time.monotonic() - 1)) is False
        assert set_cached_snapshot(_snap("up", writer="probe")) is False
        assert get_cached_snapshot(ttl=_TTL_S) is call_down

    def test_down_interval_never_exceeds_normal(self) -> None:
        set_cached_snapshot(_snap("down"))

        assert slm_health_probe_interval_s(normal_s=30.0, down_s=60.0) == 30.0


def _recovered_generating_origin(
    monkeypatch,
    *,
    in_flight: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    """Point the probe at an origin that answers ``/health``, ``/models`` and a completion.

    With *in_flight* and *release*, the ``/health`` answer waits: *in_flight* is set when the
    request arrives, and the answer goes out after *release* is set.
    """
    from personal_agent.config import settings

    async def origin(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health") and release is not None:
            if in_flight is not None:
                in_flight.set()
            await release.wait()
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m-primary"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
        return httpx.Response(200, json={})

    factory = _client_factory(origin)
    monkeypatch.setattr(_PROBE_HTTP, factory)
    monkeypatch.setattr("personal_agent.llm_client.provider_health.create_guarded_http_client", factory)
    monkeypatch.setattr(settings, "slm_health_generation_check_enabled", True)
    monkeypatch.setattr(
        "personal_agent.observability.slm_health.probe._resolve_primary_local_deployment",
        lambda: (("slm_local", "m-primary"), None),
    )
    monkeypatch.setattr(
        "personal_agent.observability.slm_health.probe._provider_active_count", lambda _key: 0
    )


class TestRule4SchedulerGate:
    """Rule 4 through the real lifecycle-loop gate: a ``down`` probes at the shorter interval."""

    async def _one_tick(self, monkeypatch, *, last_run_age_s: float) -> AsyncMock:
        from personal_agent.brainstem.scheduler import BrainstemScheduler

        sched = BrainstemScheduler()
        for flag in (
            "joinability_probe_enabled",
            "cache_erosion_probe_enabled",
            "delivery_ratio_probe_enabled",
            "quality_monitor_enabled",
        ):
            setattr(sched, flag, False)
        sched.slm_health_probe_enabled = True
        sched.slm_health_probe_interval_seconds = _NORMAL_INTERVAL_S
        sched.slm_health_probe_down_interval_seconds = _DOWN_INTERVAL_S
        sched._backfill_es_logger = None
        now = datetime.now(timezone.utc)
        # Skip every other conditional job this tick (FRE-1611 gate test's pinning).
        sched._last_disk_check = now
        sched._last_embedding_backfill_run = now
        sched._last_domain_guard_warm_run = now
        sched._last_freshness_review_week = now.isocalendar()[:2]
        sched._last_kg_stats_projection_date = now.date()
        sched._linear_client = None
        sched._last_slm_health_probe_run = now - timedelta(seconds=last_run_age_s)
        sched.running = True

        async def stop_after_first_sleep(_: float) -> None:
            sched.running = False

        tick = AsyncMock(return_value=None)
        monkeypatch.setattr("personal_agent.brainstem.scheduler.asyncio.sleep", stop_after_first_sleep)
        monkeypatch.setattr(
            "personal_agent.observability.slm_health.scheduler_runner.run_scheduled_slm_health_probe",
            tick,
        )
        with patch("personal_agent.brainstem.scheduler.settings") as mock_settings:
            mock_settings.data_lifecycle_enabled = False
            mock_settings.insights_enabled = False
            mock_settings.freshness_enabled = False
            mock_settings.sysgraph_maintenance_enabled = False
            mock_settings.feedback_polling_enabled = False
            await sched._lifecycle_loop()
        return tick

    @pytest.mark.asyncio
    async def test_down_probes_after_the_short_interval(self, monkeypatch) -> None:
        set_cached_snapshot(_snap("down", writer="call"))

        tick = await self._one_tick(monkeypatch, last_run_age_s=_DOWN_INTERVAL_S + 1)

        tick.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_up_waits_for_the_normal_interval(self, monkeypatch) -> None:
        """Seeded negative: the same elapsed time does not probe when the verdict is ``up``."""
        set_cached_snapshot(_snap("up"))

        tick = await self._one_tick(monkeypatch, last_run_age_s=_DOWN_INTERVAL_S + 1)

        tick.assert_not_awaited()


class TestAc5ReaderFiveStates:
    """AC-5: the reader answers all five states correctly."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "age_s", "expected"),
        [
            ("down", 1.0, False),  # an uncleared down
            ("down", _TTL_S + 10, False),  # a down older than the TTL, nothing since
            ("up", 1.0, True),  # a fresh up
            ("up", _TTL_S + 10, True),  # an up past the TTL
            (None, None, True),  # no verdict at all: the cold start
        ],
        ids=["uncleared-down", "down-past-ttl", "fresh-up", "up-past-ttl", "absent"],
    )
    async def test_reader(self, status, age_s, expected) -> None:
        if status is not None:
            set_cached_snapshot(_snap(status, observed=time.monotonic() - age_s))

        assert await is_provider_available(_LOCAL, _settings()) is expected  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fresh_degraded_is_available(self) -> None:
        set_cached_snapshot(_snap("degraded"))

        assert await is_provider_available(_LOCAL, _settings()) is True  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_local_read_makes_no_request(self, monkeypatch) -> None:
        """ADR-0144 AC-8: an availability read costs no request to the origin."""

        def _refuse(**kwargs: object) -> httpx.AsyncClient:
            raise AssertionError("is_provider_available must not open an HTTP client")

        monkeypatch.setattr(_PROBE_HTTP, _refuse)
        monkeypatch.setattr(
            "personal_agent.llm_client.provider_health.create_guarded_http_client", _refuse
        )

        assert await is_provider_available(_LOCAL, _settings()) is True  # type: ignore[arg-type]

    def test_expired_up_reads_as_no_snapshot(self) -> None:
        set_cached_snapshot(_snap("up", observed=time.monotonic() - (_TTL_S + 1)))

        assert get_cached_snapshot(ttl=_TTL_S) is None
