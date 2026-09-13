"""FRE-1372 — `IsolatedArmRunner`'s structural cross-arm isolation.

Wipes `neo4j-eval` before every turn (including the first — no run silently inherits
whatever a prior script left behind), replays an optional `reseed` coroutine after the
wipe (AC-2's fixture-preservation, via the caller's own production write path rather
than a selective restore — see `eval_isolation.py`'s module docstring for why a
selective restore was rejected), and waits out entity-extraction settle with
`require_nonzero=True` before returning (the restore-safety fix: an early "stable at
zero" exit would let the *next* call's wipe race a turn's extraction that simply
hadn't started yet, reproducing FRE-1338's leak).

Pure-logic coverage only, following `test_fre1337_substrate_guard.py` (fake Neo4j
driver/session capturing queries) and `test_fre1337_behavioral_completeness.py`
(mocking the settle wait rather than polling real ES) — `run_turn`'s live behavior
against a real gateway/graph is exercised manually via `fre1372_isolation_probe.py`,
matching this codebase's existing eval-harness precedent of not requiring live infra
for `make test`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from scripts.eval import eval_isolation
from scripts.eval.eval_isolation import ArmTurnResult, IsolatedArmRunner, wait_for_gateway_idle
from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS, WIPE_CYPHER


class _FakeSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def run(self, query: str, **params: Any) -> None:
        self.queries.append(query)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeDriver:
    def __init__(self) -> None:
        self.fake_session = _FakeSession()

    def session(self) -> _FakeSession:
        return self.fake_session


class _FakeResponse:
    def __init__(self, session_id: str, trace_id: str) -> None:
        self._data = {"session_id": session_id, "trace_id": trace_id}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return self._data


class _FakeESHitsResponse:
    """A `resp.raise_for_status()` / `resp.json()` pair shaped like an ES `_search` hit."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._body = {"hits": {"hits": hits}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


def _patch_settle_always_true(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, bool]]
) -> None:
    """Record `(event_type, require_nonzero)` per call; never actually polls ES."""

    async def _fake_settle(
        client: Any,
        trace_id: str,
        event_type: str,
        *,
        timeout_s: float,
        require_nonzero: bool = True,
    ) -> bool:
        calls.append((event_type, require_nonzero))
        return True

    monkeypatch.setattr(
        eval_isolation, "wait_for_event_settle", AsyncMock(side_effect=_fake_settle)
    )


def _patch_gateway_idle_always_zero(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Never actually polls ES; records that the wait was requested."""
    mock = AsyncMock(return_value=0.0)
    monkeypatch.setattr(eval_isolation, "wait_for_gateway_idle", mock)
    return mock


@pytest.mark.asyncio
async def test_run_turn_wipes_before_posting_on_the_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert driver.fake_session.queries == [WIPE_CYPHER]
    assert result == ArmTurnResult(
        session_id="s1", trace_id="trace-s1", arm="control", extraction_settled=True
    )


@pytest.mark.asyncio
async def test_run_turn_wipes_on_every_call_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run must not silently inherit whatever a prior script left in `neo4j-eval` —
    the first call wipes too, not only calls after it.
    """
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(
        side_effect=[_FakeResponse("s1", "trace-s1"), _FakeResponse("s2", "trace-s2")]
    )
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="control")
    await runner.run_turn(http, es, "hello again", arm="control")

    assert driver.fake_session.queries == [WIPE_CYPHER, WIPE_CYPHER]


@pytest.mark.asyncio
async def test_run_turn_invokes_reseed_after_wipe_before_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    order: list[str] = []

    async def _reseed() -> None:
        assert driver.fake_session.queries == [WIPE_CYPHER], "reseed must run after the wipe"
        order.append("reseed")

    async def _post(*args: Any, **kwargs: Any) -> _FakeResponse:
        order.append("post")
        return _FakeResponse("s1", "trace-s1")

    http = AsyncMock()
    http.post = AsyncMock(side_effect=_post)
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver, reseed=_reseed)
    await runner.run_turn(http, es, "hello", arm="control")

    assert order == ["reseed", "post"]


@pytest.mark.asyncio
async def test_run_turn_skips_reseed_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)  # reseed=None
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert result.session_id == "s1"


@pytest.mark.asyncio
async def test_run_turn_waits_extraction_settle_with_require_nonzero_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore-safety gate: unlike `behavioral.py`'s own reporting-only use of this
    settle wait (`require_nonzero=False`), an early "stable at zero" exit here would
    let the *next* call's wipe race a turn's extraction that hadn't started yet,
    reproducing FRE-1338's leak instead of preventing it.
    """
    driver = _FakeDriver()
    settle_calls: list[tuple[str, bool]] = []
    _patch_settle_always_true(monkeypatch, settle_calls)
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="control")

    assert settle_calls == [
        ("model_call_completed", True),
        ("entity_extraction_completed", True),
    ]


@pytest.mark.asyncio
async def test_run_turn_posts_to_the_named_arms_url(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="treatment")

    called_url = http.post.call_args.args[0]
    assert called_url == f"{EVAL_ARMS['treatment']}/chat"


@pytest.mark.asyncio
async def test_run_turn_refuses_an_arm_outside_eval_arms_before_wiping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm is validated before the wipe, not after — an invalid `arm` must never
    wipe `neo4j-eval` (or run `reseed`, or wait for the gateway to go idle) on its
    way to raising.
    """
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    idle_wait = _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    es = AsyncMock()
    reseed = AsyncMock()

    runner = IsolatedArmRunner(driver=driver, reseed=reseed)
    with pytest.raises(KeyError):
        await runner.run_turn(http, es, "hello", arm="production")

    assert driver.fake_session.queries == []
    reseed.assert_not_called()
    idle_wait.assert_not_called()


# ---------------------------------------------------------------------------
# FRE-1503 — client timeout and cross-fixture gateway idle wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_waits_for_gateway_idle_before_wiping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_turn` must wait for the gateway to go idle before it wipes/reseeds/posts —
    the FRE-1503 fix: a fixture starting on top of a still-running prior turn is
    exactly the overlap this closes.
    """
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    order: list[str] = []

    async def _fake_idle(es: Any, **kwargs: Any) -> float:
        order.append("idle_wait")
        return 0.0

    monkeypatch.setattr(eval_isolation, "wait_for_gateway_idle", AsyncMock(side_effect=_fake_idle))

    async def _post(*args: Any, **kwargs: Any) -> _FakeResponse:
        order.append("post")
        return _FakeResponse("s1", "trace-s1")

    http = AsyncMock()
    http.post = AsyncMock(side_effect=_post)
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="control")

    assert order == ["idle_wait", "post"]
    assert driver.fake_session.queries == [WIPE_CYPHER]


@pytest.mark.asyncio
async def test_run_turn_client_timeout_is_read_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old hardcoded `timeout=1200.0` is below the gateway's own absolute turn
    cap (`orchestrator_turn_lifetime_seconds`) — a fan-out turn that legitimately
    runs close to that cap gets a client `ReadTimeout` while the gateway keeps
    running (FRE-1503). The client timeout must be derived from that setting, with
    a buffer, so the client always outlives the gateway's own bound.
    """
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    _patch_gateway_idle_always_zero(monkeypatch)
    monkeypatch.setattr(
        eval_isolation.settings, "orchestrator_turn_lifetime_seconds", 3600, raising=False
    )
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="control")

    used_timeout = http.post.call_args.kwargs["timeout"]
    assert used_timeout > 3600
    assert used_timeout == 3600 + eval_isolation._CLIENT_TIMEOUT_BUFFER_S


@pytest.mark.asyncio
async def test_wait_for_gateway_idle_returns_immediately_with_no_activity() -> None:
    """No `model_call`/`tool_call` event at all means the gateway is trivially idle."""
    es = AsyncMock()
    es.post = AsyncMock(return_value=_FakeESHitsResponse([]))

    waited = await wait_for_gateway_idle(es, quiet_s=150.0, max_wait_s=5.0)

    assert waited < 1.0
    es.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_gateway_idle_queries_tool_call_started_too() -> None:
    """A still-running tool call (started but not yet completed) must count as activity —
    querying only `tool_call_completed` would read a long tool call as idle and let the
    next fixture's wipe/post start on top of it (FRE-1503's own overlap bug, reproduced
    for tool calls specifically).
    """
    es = AsyncMock()
    es.post = AsyncMock(return_value=_FakeESHitsResponse([]))

    await wait_for_gateway_idle(es, quiet_s=150.0, max_wait_s=5.0)

    queried_types = es.post.call_args.kwargs["json"]["query"]["terms"]["event_type"]
    assert "tool_call_started" in queried_types
    assert "tool_call_completed" in queried_types
    assert "model_call_started" in queried_types
    assert "model_call_completed" in queried_types


@pytest.mark.asyncio
async def test_wait_for_gateway_idle_returns_once_last_event_is_old_enough() -> None:
    """A last-activity timestamp older than `quiet_s` counts as idle right away."""
    es = AsyncMock()
    old_ts = (datetime.now(UTC) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    es.post = AsyncMock(return_value=_FakeESHitsResponse([{"_source": {"@timestamp": old_ts}}]))

    waited = await wait_for_gateway_idle(es, quiet_s=150.0, max_wait_s=5.0)

    assert waited < 1.0


@pytest.mark.asyncio
async def test_wait_for_gateway_idle_polls_until_recent_activity_goes_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent last-activity timestamp forces at least one more poll before idle."""
    es = AsyncMock()
    recent_ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    old_ts = (datetime.now(UTC) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    es.post = AsyncMock(
        side_effect=[
            _FakeESHitsResponse([{"_source": {"@timestamp": recent_ts}}]),
            _FakeESHitsResponse([{"_source": {"@timestamp": old_ts}}]),
        ]
    )

    sleeps: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(eval_isolation.asyncio, "sleep", _fake_sleep)

    await wait_for_gateway_idle(es, quiet_s=150.0, max_wait_s=60.0)

    assert es.post.await_count == 2
    assert sleeps == [eval_isolation._GATEWAY_IDLE_POLL_INTERVAL_S]
