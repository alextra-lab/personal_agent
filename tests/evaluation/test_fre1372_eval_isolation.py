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

from typing import Any
from unittest.mock import AsyncMock

import pytest
from scripts.eval import eval_isolation
from scripts.eval.eval_isolation import ArmTurnResult, IsolatedArmRunner
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


@pytest.mark.asyncio
async def test_run_turn_wipes_before_posting_on_the_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
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
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[_FakeResponse("s1", "trace-s1")])
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    await runner.run_turn(http, es, "hello", arm="treatment")

    called_url = http.post.call_args.args[0]
    assert called_url == f"{EVAL_ARMS['treatment']}/chat"


@pytest.mark.asyncio
async def test_run_turn_refuses_an_arm_outside_eval_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    _patch_settle_always_true(monkeypatch, [])
    http = AsyncMock()
    es = AsyncMock()

    runner = IsolatedArmRunner(driver=driver)
    with pytest.raises(KeyError):
        await runner.run_turn(http, es, "hello", arm="production")
