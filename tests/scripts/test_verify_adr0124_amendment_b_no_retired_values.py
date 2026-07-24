"""Amendment B verification seam (ADR-0124, FRE-956).

Covers the pure JSON scanner (`scan_digests`) and the query-and-parse path
(`run_scan`, exercised against a fake driver so no live Neo4j is required) plus
the pass/fail policy and CLI argument validation.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import orjson
import pytest
from scripts.verify_adr0124_amendment_b_no_retired_values import (
    RetiredValueScan,
    build_arg_parser,
    scan_digests,
    verdict,
)
from scripts.verify_adr0124_amendment_b_no_retired_values import run_scan as _run_scan

_DEPLOY_TS = datetime(2026, 7, 24, 0, 0, 0, tzinfo=timezone.utc)


def _digest(**slots: list[dict[str, Any]]) -> str:
    base: dict[str, Any] = {"established": [], "decisions": [], "unresolved": [], "corrections": []}
    base.update(slots)
    return orjson.dumps(base).decode()


# --------------------------------------------------------------------------
# scan_digests — pure function, no DB
# --------------------------------------------------------------------------


def test_empty_population_scans_clean() -> None:
    scan = scan_digests([])
    assert scan.population == 0
    assert scan.tool_evidence_count == 0
    assert scan.status_contradiction_count == 0
    assert scan.clean


def test_clean_population_scans_clean() -> None:
    digests = [
        _digest(established=[{"text": "x", "basis": "user_statement"}]),
        _digest(
            corrections=[
                {
                    "text": "y",
                    "basis": "assistant_reasoning",
                    "tier": "self_correction",
                    "span": "y",
                    "locator": {"capture_id": "c1", "field": "assistant_text"},
                    "evidence_span": "y",
                    "evidence_locator": {"capture_id": "c1", "field": "assistant_text"},
                }
            ]
        ),
    ]
    scan = scan_digests(digests)
    assert scan.population == 2
    assert scan.clean


def test_retired_tool_evidence_basis_is_counted() -> None:
    digests = [_digest(established=[{"text": "x", "basis": "tool_evidence"}])]
    scan = scan_digests(digests)
    assert scan.tool_evidence_count == 1
    assert scan.status_contradiction_count == 0
    assert not scan.clean


def test_retired_tool_evidence_basis_in_a_correction_is_also_counted() -> None:
    digests = [
        _digest(
            corrections=[
                {
                    "text": "y",
                    "basis": "tool_evidence",
                    "tier": "self_correction",
                    "span": "y",
                    "locator": {"capture_id": "c1", "field": "assistant_text"},
                    "evidence_span": "y",
                    "evidence_locator": {"capture_id": "c1", "field": "assistant_text"},
                }
            ]
        )
    ]
    scan = scan_digests(digests)
    assert scan.tool_evidence_count == 1


def test_retired_status_contradiction_tier_is_counted() -> None:
    digests = [
        _digest(
            corrections=[
                {
                    "text": "y",
                    "basis": "assistant_reasoning",
                    "tier": "status_contradiction",
                    "span": "y",
                    "locator": {"capture_id": "c1", "field": "assistant_text"},
                    "evidence_span": "y",
                    "evidence_locator": {"capture_id": "c1", "field": "assistant_text"},
                }
            ]
        )
    ]
    scan = scan_digests(digests)
    assert scan.status_contradiction_count == 1
    assert not scan.clean


# --------------------------------------------------------------------------
# verdict — pass/fail policy
# --------------------------------------------------------------------------


def test_verdict_fails_on_empty_population_by_default() -> None:
    reason = verdict(
        RetiredValueScan(population=0, tool_evidence_count=0, status_contradiction_count=0)
    )
    assert reason is not None
    assert "empty" in reason.lower()


def test_verdict_passes_on_empty_population_when_allowed() -> None:
    reason = verdict(
        RetiredValueScan(population=0, tool_evidence_count=0, status_contradiction_count=0),
        allow_empty=True,
    )
    assert reason is None


def test_verdict_passes_on_clean_nonempty_population() -> None:
    reason = verdict(
        RetiredValueScan(population=5, tool_evidence_count=0, status_contradiction_count=0)
    )
    assert reason is None


def test_verdict_fails_when_a_retired_value_survives() -> None:
    reason = verdict(
        RetiredValueScan(population=5, tool_evidence_count=1, status_contradiction_count=0)
    )
    assert reason is not None


# --------------------------------------------------------------------------
# run_scan — the query-and-parse path, against a fake driver (no live Neo4j)
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> "_FakeResult":
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration as e:
            raise StopAsyncIteration from e


class _FakeSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **params: Any) -> _FakeResult:
        self.queries.append((query, params))
        return _FakeResult(self._records)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.last_session: _FakeSession | None = None

    def session(self) -> _FakeSession:
        self.last_session = _FakeSession(self._records)
        return self.last_session


@pytest.mark.asyncio
async def test_run_scan_returns_clean_for_an_empty_result_set() -> None:
    driver = _FakeDriver([])
    scan = await _run_scan(driver, _DEPLOY_TS)
    assert scan.population == 0
    assert scan.clean


@pytest.mark.asyncio
async def test_run_scan_parses_a_non_empty_clean_population() -> None:
    driver = _FakeDriver(
        [
            {"digest": _digest(established=[{"text": "x", "basis": "user_statement"}])},
            {"digest": _digest(decisions=[{"text": "y", "basis": "mixed"}])},
        ]
    )
    scan = await _run_scan(driver, _DEPLOY_TS)
    assert scan.population == 2
    assert scan.clean


@pytest.mark.asyncio
async def test_run_scan_surfaces_a_retired_value() -> None:
    driver = _FakeDriver(
        [{"digest": _digest(established=[{"text": "x", "basis": "tool_evidence"}])}]
    )
    scan = await _run_scan(driver, _DEPLOY_TS)
    assert scan.population == 1
    assert scan.tool_evidence_count == 1
    assert not scan.clean


@pytest.mark.asyncio
async def test_run_scan_passes_the_deploy_timestamp_to_the_query() -> None:
    driver = _FakeDriver([])
    await _run_scan(driver, _DEPLOY_TS)
    assert driver.last_session is not None
    _, params = driver.last_session.queries[0]
    assert params["deploy_ts"] == _DEPLOY_TS.isoformat()


# --------------------------------------------------------------------------
# CLI argument validation
# --------------------------------------------------------------------------


def test_missing_deploy_timestamp_fails_argparse() -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_deploy_timestamp_is_parsed_as_iso8601() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--deploy-timestamp", "2026-07-24T00:00:00+00:00"])
    assert args.deploy_timestamp == _DEPLOY_TS


def test_allow_empty_defaults_to_false() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--deploy-timestamp", "2026-07-24T00:00:00+00:00"])
    assert args.allow_empty is False
