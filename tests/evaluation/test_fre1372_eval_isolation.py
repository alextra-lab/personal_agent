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
from scripts.eval.fre1372_isolation_probe import _extraction_failure


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


_FAKE_BATCH_TRACE_ID = "batch-trace-1"


def _patch_settle_always_true(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, bool, str]]
) -> None:
    """Record `(event_type, require_nonzero, field)` per call; never actually polls ES.

    Also patches `fetch_latest_event` to hand back a fake batch `trace_id` for
    `entity_extraction_completed` — `run_turn()` now chains a `consolidation_completed`
    wait off that value (FRE-1372), so a test exercising the full settle chain needs
    both faked together.
    """

    async def _fake_settle(
        client: Any,
        trace_id: str,
        event_type: str,
        *,
        timeout_s: float,
        require_nonzero: bool = True,
        field: str = "trace_id",
    ) -> bool:
        calls.append((event_type, require_nonzero, field))
        return True

    async def _fake_fetch_latest_event(
        client: Any, trace_id: str, event_type: str, *, field: str = "trace_id"
    ) -> dict[str, Any]:
        return {"trace_id": _FAKE_BATCH_TRACE_ID}

    monkeypatch.setattr(
        eval_isolation, "wait_for_event_settle", AsyncMock(side_effect=_fake_settle)
    )
    monkeypatch.setattr(
        eval_isolation, "fetch_latest_event", AsyncMock(side_effect=_fake_fetch_latest_event)
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
async def test_run_turn_wipes_per_session_not_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-1517 A1: a continuing turn neither wipes nor reseeds, and a new session still does.

    A wipe per turn would erase the memory a long session's own earlier turns wrote, so a
    back-reference past the history slice would test nothing.
    """
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    gateway_idle = _patch_gateway_idle_always_zero(monkeypatch)
    local_wait = AsyncMock(return_value=10.0)
    monkeypatch.setattr(eval_isolation, "wait_for_local_model_calls", local_wait)
    reseed = AsyncMock()
    http = AsyncMock()
    http.post = AsyncMock(
        side_effect=[
            _FakeResponse("s1", "trace-1"),
            _FakeResponse("s1", "trace-2"),
            _FakeResponse("s2", "trace-3"),
        ]
    )
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver, reseed=reseed)
    first = await runner.run_turn(http, es, "hello", arm="control", model="deployment-a")
    await runner.run_turn(http, es, "and then?", arm="control", session_id=first.session_id)
    assert driver.fake_session.queries == [WIPE_CYPHER]
    assert reseed.await_count == 1

    await runner.run_turn(http, es, "a new session", arm="control")
    assert driver.fake_session.queries == [WIPE_CYPHER, WIPE_CYPHER]
    assert reseed.await_count == 2
    # A new session waits for full gateway quiet; a continuing turn waits only for local calls.
    assert gateway_idle.await_count == 2
    assert local_wait.await_count == 1

    first_params, second_params, third_params = (
        call.kwargs["params"] for call in http.post.call_args_list
    )
    assert "session_id" not in first_params
    assert first_params["model"] == "deployment-a"
    assert second_params["session_id"] == "s1"
    assert "model" not in second_params
    assert "session_id" not in third_params


@pytest.mark.asyncio
async def test_run_turn_without_settle_returns_none_and_waits_for_no_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-1517: a caller that settles the session itself gets no extraction wait per turn."""
    calls: list[tuple[str, bool, str]] = []
    _patch_settle_always_true(monkeypatch, calls)
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])

    runner = IsolatedArmRunner(driver=_FakeDriver())
    result = await runner.run_turn(http, AsyncMock(), "hello", arm="control", wait_settle=False)

    assert result.extraction_settled is None
    assert [event_type for event_type, _, _ in calls] == ["model_call_completed"]


@pytest.mark.asyncio
async def test_local_model_calls_in_flight_pairs_started_with_ended_by_span() -> None:
    """A call ends with completed or error on its span; only slm_local calls are asked for."""
    hits = [
        {"_source": {"event_type": "model_call_started", "span_id": "a"}},
        {"_source": {"event_type": "model_call_completed", "span_id": "a"}},
        {"_source": {"event_type": "model_call_started", "span_id": "b"}},
        {"_source": {"event_type": "model_call_started", "span_id": "c"}},
        {"_source": {"event_type": "model_call_error", "span_id": "c"}},
    ]
    es = AsyncMock()
    es.post = AsyncMock(return_value=_FakeESHitsResponse(hits))

    assert await eval_isolation.local_model_calls_in_flight(es) == ["b"]
    filters = es.post.call_args.kwargs["json"]["query"]["bool"]["filter"]
    assert {"term": {"provider": "slm_local"}} in filters


@pytest.mark.asyncio
async def test_local_model_calls_in_flight_refuses_a_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filled page would silently under-report what is in flight."""
    monkeypatch.setattr(eval_isolation, "_IN_FLIGHT_PAGE", 2)
    hits = [{"_source": {"event_type": "model_call_started", "span_id": s}} for s in "ab"]
    es = AsyncMock()
    es.post = AsyncMock(return_value=_FakeESHitsResponse(hits))

    with pytest.raises(RuntimeError, match="filled its page"):
        await eval_isolation.local_model_calls_in_flight(es)


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
    settle_calls: list[tuple[str, bool, str]] = []
    _patch_settle_always_true(monkeypatch, settle_calls)
    _patch_gateway_idle_always_zero(monkeypatch)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="control")

    assert settle_calls == [
        ("model_call_completed", True, "trace_id"),
        ("entity_extraction_completed", True, "capture_trace_id"),
        ("consolidation_completed", True, "trace_id"),
    ]


# ---------------------------------------------------------------------------
# FRE-1372 (reopen) — the consolidation-batch write must also settle, not just
# entity_extraction_completed (the LLM call returning, not the graph write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_settled_false_when_consolidation_never_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction settling is not enough: a measured run showed entity_extraction_completed
    landing ~57s before the resulting graph write (consolidation_completed) — if that
    write never settles, the turn is not yet safe to wipe past.
    """
    driver = _FakeDriver()
    _patch_gateway_idle_always_zero(monkeypatch)

    async def _fake_settle(
        client: Any,
        trace_id: str,
        event_type: str,
        *,
        timeout_s: float,
        require_nonzero: bool = True,
        field: str = "trace_id",
    ) -> bool:
        return event_type != "consolidation_completed"

    async def _fake_fetch_latest_event(
        client: Any, trace_id: str, event_type: str, *, field: str = "trace_id"
    ) -> dict[str, Any]:
        return {"trace_id": "batch-trace-1"}

    monkeypatch.setattr(
        eval_isolation, "wait_for_event_settle", AsyncMock(side_effect=_fake_settle)
    )
    monkeypatch.setattr(
        eval_isolation, "fetch_latest_event", AsyncMock(side_effect=_fake_fetch_latest_event)
    )
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert result.extraction_settled is False


@pytest.mark.asyncio
async def test_run_turn_never_waits_on_consolidation_when_extraction_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No entity_extraction_completed landed at all (e.g. a bare greeting) -> there is
    no batch to chase; `fetch_latest_event`/the consolidation wait must never fire.
    """
    driver = _FakeDriver()
    _patch_gateway_idle_always_zero(monkeypatch)

    async def _fake_settle(
        client: Any,
        trace_id: str,
        event_type: str,
        *,
        timeout_s: float,
        require_nonzero: bool = True,
        field: str = "trace_id",
    ) -> bool:
        return event_type != "entity_extraction_completed"

    fetch_mock = AsyncMock()
    monkeypatch.setattr(
        eval_isolation, "wait_for_event_settle", AsyncMock(side_effect=_fake_settle)
    )
    monkeypatch.setattr(eval_isolation, "fetch_latest_event", fetch_mock)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert result.extraction_settled is False
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_turn_settled_false_when_no_batch_event_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_latest_event` returning None (ES read raced its own indexing) must not
    crash `run_turn` — it must fall back to "not settled", never guess a batch id.
    """
    driver = _FakeDriver()
    _patch_gateway_idle_always_zero(monkeypatch)
    _patch_settle_always_true(monkeypatch, [])

    async def _fake_fetch_latest_event_none(
        client: Any, trace_id: str, event_type: str, *, field: str = "trace_id"
    ) -> None:
        return None

    monkeypatch.setattr(
        eval_isolation, "fetch_latest_event", AsyncMock(side_effect=_fake_fetch_latest_event_none)
    )
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert result.extraction_settled is False


@pytest.mark.asyncio
async def test_run_turn_settled_true_when_both_extraction_and_consolidation_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full chain settling — extraction, then its batch's own consolidation
    write — is what makes a turn genuinely safe to wipe past.
    """
    driver = _FakeDriver()
    _patch_gateway_idle_always_zero(monkeypatch)
    _patch_settle_always_true(monkeypatch, [])
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    result = await runner.run_turn(http, es, "hello", arm="control")

    assert result.extraction_settled is True


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


# ---------------------------------------------------------------------------
# FRE-1372 (reopen) — the probe fails loudly when extraction never settled
# ---------------------------------------------------------------------------


def _result(*, session_id: str, extraction_settled: bool) -> ArmTurnResult:
    return ArmTurnResult(
        session_id=session_id,
        trace_id=f"trace-{session_id}",
        arm="control",
        extraction_settled=extraction_settled,
    )


class TestExtractionFailure:
    """`_extraction_failure` catches the vacuous-pass shape the 2026-09-07 bounce named:
    an empty-graph run reporting AC-1 held because it had nothing to leak.
    """

    def test_none_when_both_turns_settled(self) -> None:
        first = _result(session_id="s1", extraction_settled=True)
        second = _result(session_id="s2", extraction_settled=True)

        assert _extraction_failure(first=first, second=second) is None

    def test_names_first_turn_when_only_it_is_unsettled(self) -> None:
        first = _result(session_id="s1", extraction_settled=False)
        second = _result(session_id="s2", extraction_settled=True)

        message = _extraction_failure(first=first, second=second)

        assert message is not None
        assert "first turn" in message
        assert "s1" in message

    def test_names_second_turn_when_only_it_is_unsettled(self) -> None:
        first = _result(session_id="s1", extraction_settled=True)
        second = _result(session_id="s2", extraction_settled=False)

        message = _extraction_failure(first=first, second=second)

        assert message is not None
        assert "second turn" in message
        assert "s2" in message

    def test_names_first_turn_when_both_are_unsettled(self) -> None:
        """Both turns share `arm="control"` in the real probe — this proves the message
        distinguishes them by call order, not by the (identical) `.arm` field.
        """
        first = _result(session_id="s1", extraction_settled=False)
        second = _result(session_id="s2", extraction_settled=False)

        message = _extraction_failure(first=first, second=second)

        assert message is not None
        assert "first turn" in message
        assert "s1" in message


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
