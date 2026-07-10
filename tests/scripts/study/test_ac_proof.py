"""Tests for the AC-1 / mechanism-AC-2 Cypher aggregation report (FRE-839).

Unit-level: mocked Neo4j driver. The AC-1 tests are a direct regression
test for the codex-caught reasoning error — degree >= 2 does NOT imply
provenance-distinctness (the categorizer can propose 1-3 categories per
concept in a single call), so the two conditions must be computed
independently and combined with a conjunction, never one inferred from the
other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from scripts.study.ac_proof import compute_ac1_report, compute_mechanism_ac2_spot_check


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict[str, Any]]:
        for record in self._records:
            yield record

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None


class _ScriptedSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters or {}))
        return _FakeResult(self._records)

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.fake_session = _ScriptedSession(records)

    def session(self) -> _ScriptedSession:
        return self.fake_session


# ---------------------------------------------------------------------------
# compute_ac1_report — the conjunctive bar (codex-caught reasoning error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degree_from_single_episode_is_excluded_from_the_bar() -> None:
    """A concept with MEMBER_OF degree 2 whose assertions ALL trace back to
    ONE episode (one categorizer call proposing 2 categories at once) must
    be excluded from the ≥60% bar — this is exactly the scenario the first
    draft's flawed "degree implies provenance-distinct" reasoning missed.
    """
    driver = _FakeDriver(
        [
            {"concept_id": "c1", "degree": 2, "backing_episode_count": 1},
        ]
    )

    report = await compute_ac1_report(driver)

    assert report["eligible_set_size"] == 1
    assert report["pct_meeting_bar"] == 0.0


@pytest.mark.asyncio
async def test_degree_from_two_distinct_episodes_is_included_in_the_bar() -> None:
    driver = _FakeDriver(
        [
            {"concept_id": "c1", "degree": 2, "backing_episode_count": 2},
        ]
    )

    report = await compute_ac1_report(driver)

    assert report["pct_meeting_bar"] == 1.0


@pytest.mark.asyncio
async def test_mixed_population_computes_correct_percentage_and_median() -> None:
    driver = _FakeDriver(
        [
            {"concept_id": "c1", "degree": 2, "backing_episode_count": 2},  # meets bar
            {"concept_id": "c2", "degree": 3, "backing_episode_count": 3},  # meets bar
            {
                "concept_id": "c3",
                "degree": 2,
                "backing_episode_count": 1,
            },  # single-episode, excluded
            {"concept_id": "c4", "degree": 1, "backing_episode_count": 1},  # degree too low
            {"concept_id": "c5", "degree": 4, "backing_episode_count": 4},  # meets bar
        ]
    )

    report = await compute_ac1_report(driver)

    assert report["eligible_set_size"] == 5
    assert report["pct_meeting_bar"] == pytest.approx(3 / 5)
    assert report["median_degree"] == 2


@pytest.mark.asyncio
async def test_empty_eligible_set_reports_a_clean_null_not_a_crash() -> None:
    driver = _FakeDriver([])

    report = await compute_ac1_report(driver)

    assert report["eligible_set_size"] == 0
    assert report["pct_meeting_bar"] == 0.0
    assert report["median_degree"] == 0


# ---------------------------------------------------------------------------
# compute_mechanism_ac2_spot_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_spot_check_reports_true_when_surfaces_share_a_hub() -> None:
    driver = _FakeDriver([{"same_hub": True}])

    result = await compute_mechanism_ac2_spot_check(
        driver, pairs=[("Arterial calcification", "Arterial Calcification")]
    )

    assert result == [
        {"pair": ("Arterial calcification", "Arterial Calcification"), "same_hub": True}
    ]


@pytest.mark.asyncio
async def test_ac2_spot_check_reports_false_when_no_result_found() -> None:
    driver = _FakeDriver([])

    result = await compute_mechanism_ac2_spot_check(driver, pairs=[("A", "B")])

    assert result == [{"pair": ("A", "B"), "same_hub": False}]
