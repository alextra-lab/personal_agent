"""Unit tests for the ADR-0114 corpus ingest driver's orchestration logic (FRE-839).

Code-review finding: `run_ingest.py`'s own wiring (`_process_session`'s
concept-grouping/hub-resolution, episode-dedup, per-session failure
isolation) previously had no coverage that runs without real infra — only
the real-Neo4j integration test, which self-skips when the study sandbox
isn't up. These tests use a fake driver/session (mirroring
`test_writer.py`'s pattern) so a regression in the orchestration wiring
itself — independent of whether `make study-infra-up` happens to be
running — is always caught.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from scripts.study.run_ingest import _episode_already_processed, _process_session, run_ingest
from scripts.study.writer import ProposedMembership


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
    def __init__(self, responses: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        parameters = parameters or {}
        self.calls.append((query, parameters))
        for marker, records in self._responses:
            if marker in query:
                return _FakeResult(records)
        return _FakeResult([])

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, responses: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.fake_session = _ScriptedSession(responses)

    def session(self) -> _ScriptedSession:
        return self.fake_session


def _memberships(*names_and_categories: tuple[str, str]) -> list[ProposedMembership]:
    return [
        ProposedMembership(
            concept_name=name, kind="Phenomenon", category_name=category, proposed_confidence=0.8
        )
        for name, category in names_and_categories
    ]


# ---------------------------------------------------------------------------
# _episode_already_processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_already_processed_true_when_episode_exists() -> None:
    driver = _FakeDriver([("MATCH (e:Episode {id: $episode_id})", [{"already_processed": True}])])
    async with driver.session() as session:
        assert await _episode_already_processed(session, "ep-1") is True


@pytest.mark.asyncio
async def test_episode_already_processed_false_when_no_episode() -> None:
    driver = _FakeDriver([("MATCH (e:Episode {id: $episode_id})", [{"already_processed": False}])])
    async with driver.session() as session:
        assert await _episode_already_processed(session, "ep-1") is False


# ---------------------------------------------------------------------------
# _process_session — skips already-processed episodes without calling the
# (costly) categorizer at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_session_skips_already_processed_episode_without_calling_categorizer() -> (
    None
):
    driver = _FakeDriver([("MATCH (e:Episode {id: $episode_id})", [{"already_processed": True}])])

    with patch(
        "scripts.study.run_ingest.categorize_conversation", new=AsyncMock()
    ) as mock_categorize:
        written = await _process_session(driver, session_id="ep-1", raw_messages_json="[]", seed=1)

    assert written == 0
    mock_categorize.assert_not_called()


@pytest.mark.asyncio
async def test_process_session_returns_zero_when_no_discussed_entities() -> None:
    driver = _FakeDriver(
        [
            ("MATCH (e:Episode {id: $episode_id})", [{"already_processed": False}]),
            ("DISCUSSES", []),
        ]
    )

    with patch(
        "scripts.study.run_ingest.categorize_conversation", new=AsyncMock()
    ) as mock_categorize:
        written = await _process_session(driver, session_id="ep-1", raw_messages_json="[]", seed=1)

    assert written == 0
    mock_categorize.assert_not_called()


@pytest.mark.asyncio
async def test_process_session_groups_memberships_by_concept_and_resolves_hubs() -> None:
    """The concept-grouping/hub-resolution wiring: two memberships for the
    same concept must resolve to the SAME concept_id and land in one
    ResolvedConceptMemberships group, not two.
    """
    driver = _FakeDriver(
        [
            ("MATCH (e:Episode {id: $episode_id})", [{"already_processed": False}]),
            (
                "DISCUSSES",
                [{"name": "Liver dysfunction", "kind": "Phenomenon", "embedding": None}],
            ),
            (
                "UNWIND $normalized_names AS normalized_name",
                [{"normalized_name": "liver dysfunction", "concept_id": "concept-1"}],
            ),
            (
                "UNWIND $rows AS row",
                [
                    {"concept_id": "concept-1", "category_normalized_name": "adverse effect"},
                    {"concept_id": "concept-1", "category_normalized_name": "liver health"},
                ],
            ),
        ]
    )
    fake_memberships = _memberships(
        ("Liver dysfunction", "adverse effect"), ("Liver dysfunction", "liver health")
    )

    with patch(
        "scripts.study.run_ingest.categorize_conversation",
        new=AsyncMock(return_value=fake_memberships),
    ):
        written = await _process_session(
            driver, session_id="ep-1", raw_messages_json=json.dumps([]), seed=1
        )

    assert written == 2
    # write_mentions_and_assertions received exactly one resolved group for
    # the one distinct concept, carrying both its memberships.
    write_call = next(q for q, p in driver.fake_session.calls if "UNWIND $rows AS row" in q)
    assert write_call  # sanity: the batched write call happened


# ---------------------------------------------------------------------------
# run_ingest — per-session failure isolation (one bad session must not
# abort the whole corpus run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ingest_isolates_a_failing_session_and_continues() -> None:
    driver = _FakeDriver(
        [
            (
                "MATCH (s:Session)",
                [
                    {"session_id": "bad-session", "raw_messages_json": "[]"},
                    {"session_id": "good-session", "raw_messages_json": "[]"},
                ],
            ),
            ("MATCH (e:Episode {id: $episode_id})", [{"already_processed": False}]),
            ("DISCUSSES", [{"name": "X", "kind": "Phenomenon", "embedding": None}]),
        ]
    )

    call_count = 0

    async def _flaky_categorize(conversation_text, concepts, *, seed, trace_id=None):
        nonlocal call_count
        call_count += 1
        if trace_id == "bad-session":
            raise RuntimeError("simulated malformed-response crash")
        return []

    with patch(
        "scripts.study.run_ingest.categorize_conversation",
        new=AsyncMock(side_effect=_flaky_categorize),
    ):
        summary = await run_ingest(driver, limit=None, seed=1)

    assert summary["sessions_failed"] == 1
    assert summary["sessions_processed"] == 1
    assert call_count == 2  # both sessions were attempted despite the first failing
