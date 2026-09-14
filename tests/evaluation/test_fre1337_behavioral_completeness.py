"""AC-4 tightening: a behavioral report with a missing field must fail loudly.

Also covers `wait_for_event_settle` — the fix for the codex/code-review finding that
wiping `neo4j-eval` immediately after a turn (no wait) races entity extraction's async
lag and can reproduce FRE-1338's contamination instead of preventing it. These two are
the pieces of `behavioral.py` that are pure-enough logic to unit test without live
ES/gateway — the rest is exercised live per the harness README, matching the
`fre481`/`fre1286` precedent of an untested-by-design ES-reading driver.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock

import pytest
from scripts.eval.fre1337_intent_probe import behavioral
from scripts.eval.fre1337_intent_probe.behavioral import (
    BehavioralReport,
    assert_behavioral_signals_complete,
    wait_for_event_settle,
)


def _complete_report(**overrides: object) -> BehavioralReport:
    base = dict(
        fixture_label="gpsr_research",
        session_id="s1",
        trace_id="t1",
        tool_call_count=3,
        web_search_count=2,
        web_search_result_counts=[10, 8],
        fetch_url_count=1,
        input_token_growth=4200,
        wall_time_s=12.5,
        tool_budget_exhausted=False,
        extraction_settled=True,
    )
    base.update(overrides)
    return BehavioralReport(**base)  # type: ignore[arg-type]


def test_complete_report_passes() -> None:
    assert_behavioral_signals_complete(_complete_report())  # must not raise


@pytest.mark.parametrize(
    "field",
    [
        "tool_call_count",
        "web_search_count",
        "fetch_url_count",
        "input_token_growth",
        "wall_time_s",
        "tool_budget_exhausted",
    ],
)
def test_missing_field_raises(field: str) -> None:
    report = _complete_report(**{field: None})
    with pytest.raises(ValueError, match=field):
        assert_behavioral_signals_complete(report)


def test_genuine_zero_counts_are_not_treated_as_missing() -> None:
    """A fixture that fires no tool calls at all is a real (valid) zero, not a failure."""
    report = _complete_report(
        tool_call_count=0, web_search_count=0, fetch_url_count=0, input_token_growth=0
    )
    assert_behavioral_signals_complete(report)  # must not raise


def _patch_polling(
    monkeypatch: pytest.MonkeyPatch, counts: list[int], *, poll_interval_s: float = 0.01
) -> AsyncMock:
    """Feed `_count_event` a repeating sequence of counts, one per poll, with a short
    real poll interval so a timeout test actually reaches its deadline rather than
    spinning through the whole sequence near-instantly.
    """
    mock_count = AsyncMock(side_effect=itertools.cycle(counts))
    monkeypatch.setattr(behavioral, "_count_event", mock_count)
    monkeypatch.setattr(behavioral, "_POLL_INTERVAL_S", poll_interval_s)
    return mock_count


@pytest.mark.asyncio
async def test_settle_returns_true_once_count_stabilizes_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_polling(monkeypatch, [1, 2, 2, 999])  # settles at 2; a later 999 must never be read
    result = await wait_for_event_settle(
        AsyncMock(), "trace-1", "entity_extraction_completed", timeout_s=5.0
    )
    assert result is True


@pytest.mark.asyncio
async def test_settle_times_out_false_when_require_nonzero_and_always_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_polling(monkeypatch, [0])
    result = await wait_for_event_settle(
        AsyncMock(), "trace-1", "entity_extraction_completed", timeout_s=0.05
    )
    assert result is False


@pytest.mark.asyncio
async def test_settle_accepts_stable_zero_when_require_nonzero_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some turns (e.g. a bare greeting) never extract an entity — that must not hang."""
    _patch_polling(monkeypatch, [0, 0])
    result = await wait_for_event_settle(
        AsyncMock(),
        "trace-1",
        "entity_extraction_completed",
        timeout_s=5.0,
        require_nonzero=False,
    )
    assert result is True


@pytest.mark.asyncio
async def test_settle_polls_the_correct_trace_and_event(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_count = _patch_polling(monkeypatch, [3, 3])
    await wait_for_event_settle(AsyncMock(), "trace-xyz", "model_call_completed", timeout_s=5.0)
    for call in mock_count.call_args_list:
        args = call.args
        assert args[1] == "trace-xyz"
        assert args[2] == "model_call_completed"


@pytest.mark.asyncio
async def test_settle_defaults_to_matching_on_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `field` given -> `_count_event` matches on `trace_id`, the pre-existing
    default correct for `model_call_completed`/`tool_call_completed`.
    """
    mock_count = _patch_polling(monkeypatch, [3, 3])
    await wait_for_event_settle(AsyncMock(), "trace-xyz", "model_call_completed", timeout_s=5.0)
    for call in mock_count.call_args_list:
        assert call.kwargs["field"] == "trace_id"


@pytest.mark.asyncio
async def test_settle_field_override_is_passed_through_to_count_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-1372: `entity_extraction_completed`'s own `trace_id` is a batch sweep id
    when consolidation batches several pending captures — only `capture_trace_id`
    still equals the original request's trace_id. A caller passing `field=` must
    reach `_count_event`, or the wait polls a field that can never match.
    """
    mock_count = _patch_polling(monkeypatch, [1, 1])
    await wait_for_event_settle(
        AsyncMock(),
        "trace-xyz",
        "entity_extraction_completed",
        timeout_s=5.0,
        field="capture_trace_id",
    )
    for call in mock_count.call_args_list:
        assert call.kwargs["field"] == "capture_trace_id"


@pytest.mark.asyncio
async def test_count_event_query_uses_the_given_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_count_event`'s ES query term key is the `field` argument, not hardcoded."""
    captured_body: dict[str, object] = {}

    async def _fake_es_search(client: object, body: dict[str, object]) -> dict[str, object]:
        captured_body.update(body)
        return {"hits": {"total": {"value": 0}}}

    monkeypatch.setattr(behavioral, "_es_search", _fake_es_search)

    await behavioral._count_event(
        AsyncMock(), "cap-123", "entity_extraction_completed", field="capture_trace_id"
    )

    must_clauses = captured_body["query"]["bool"]["must"]  # type: ignore[index]
    assert {"term": {"capture_trace_id": "cap-123"}} in must_clauses
