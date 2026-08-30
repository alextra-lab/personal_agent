"""AC-4 tightening: a behavioral report with a missing field must fail loudly.

This is the one piece of `behavioral.py` that's pure logic (no live ES/gateway needed) —
the rest is exercised live per the harness README, matching the `fre481`/`fre1286`
precedent of an untested-by-design ES-reading driver.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1337_intent_probe.behavioral import (
    BehavioralReport,
    assert_behavioral_signals_complete,
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
