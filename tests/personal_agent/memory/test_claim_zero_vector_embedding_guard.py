"""FRE-768: Claim-substrate twin of FRE-659.

Zero-vector Claim embeddings must never be persisted; missing/zeroed Claim embeddings
are repaired by the same idempotent, outage-safe backfill extended to cover ``Claim``
nodes.

When the embedder is unreachable ``generate_embedding`` degrades to a zero vector.
``assert_claim`` must NOT persist it (a zero vector has no meaningful cosine and
silently corrupts vector recall / facet-aware dedup matching), and
``backfill_missing_embeddings`` must re-embed such Claims from their content once the
embedder returns — writing only non-zero results, under a guard that never clobbers a
fresher concurrent embedding.

Also pins a known, documented-not-fixed limitation (codex plan review, FRE-768): during
an outage the *new* Claim's own embedding is a zero vector too, so ``matching_candidates``
never matches an existing current Claim by cosine — the new Claim is always treated as
unrelated-and-new rather than a possible supersession. A follow-up ticket covers
outage-mode re-adjudication; this test just proves the behavior exists so it is not
silently relied upon.

Uses a mocked Neo4j driver (same pattern as
``test_zero_vector_embedding_guard.py``) — runs in ``make test``.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from personal_agent.memory.models import Claim
from personal_agent.memory.service import MemoryService
from personal_agent.memory.supersession import ClaimRecord, matching_candidates

_DIM = 8
_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_service_with_mock() -> tuple[MemoryService, list[tuple[str, dict[str, Any]]]]:
    """Build a MemoryService with a mock driver that captures every session.run call.

    The mock resolves the ``assert_claim`` user-lookup + write to a synthetic
    "no candidates, no supersession" record, the FRE-768 Claim backfill read
    (``ORDER BY cl.claim_id`` candidate query) to ``_claim_candidate_rows``, and every
    ``count(*)``-returning write to ``_write_filled``; all settable per-test.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._claim_candidate_rows = []  # type: ignore[attr-defined]
    service._write_filled = 0  # type: ignore[attr-defined]

    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured.append((cypher, dict(kwargs)))
        result = AsyncMock()
        result.data = AsyncMock(return_value=[])
        result.single = AsyncMock(return_value=None)

        if "RETURN cl.claim_id AS claim_id, cl.content AS content" in cypher:
            result.data = AsyncMock(return_value=list(service._claim_candidate_rows))  # type: ignore[attr-defined]
        elif "MATCH (o:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)" in cypher:
            result.data = AsyncMock(return_value=[])
        elif "CREATE (o)-[:HAS_FACT]->(cl:Claim {" in cypher:
            result.single = AsyncMock(
                return_value={"claim_id": kwargs.get("claim_id"), "invalidated": 0}
            )
        elif "UNWIND $updates AS u" in cypher and "cl:Claim" in cypher:
            result.single = AsyncMock(return_value={"filled": service._write_filled})  # type: ignore[attr-defined]

        return result

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.run = capture_run

    mock_driver = AsyncMock()
    mock_driver.session = lambda: mock_session
    service.driver = mock_driver
    return service, captured


def _claim(content: str = "The lease ends in March.") -> Claim:
    return Claim(
        content=content, confidence=0.8, observed_at=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )


# --- Write-path guard (AC-1) -------------------------------------------------


@pytest.mark.asyncio
async def test_assert_claim_skips_zero_vector_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    service, captured = _make_service_with_mock()
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embedding",
        AsyncMock(return_value=[0.0] * _DIM),
    )

    claim_id = await service.assert_claim(_claim(), user_id=_USER_ID)
    assert claim_id != ""

    write_cypher, write_kwargs = next(
        (c, k) for c, k in captured if "CREATE (o)-[:HAS_FACT]->(cl:Claim {" in c
    )
    assert "cl.embedding = $embedding" not in write_cypher
    assert "embedding" not in write_kwargs


@pytest.mark.asyncio
async def test_assert_claim_persists_nonzero_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    service, captured = _make_service_with_mock()
    vec = [0.1] * _DIM
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embedding",
        AsyncMock(return_value=vec),
    )

    await service.assert_claim(_claim(), user_id=_USER_ID)

    write_cypher, write_kwargs = next(
        (c, k) for c, k in captured if "CREATE (o)-[:HAS_FACT]->(cl:Claim {" in c
    )
    assert "cl.embedding = $embedding" in write_cypher
    assert write_kwargs["embedding"] == vec


# --- Backfill (AC-2) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_populates_missing_claim_embedding_when_embedder_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._claim_candidate_rows = [{"claim_id": "c1", "content": "The lease ends in March."}]  # type: ignore[attr-defined]
    service._write_filled = 1  # type: ignore[attr-defined]
    vec = [0.2] * _DIM
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[vec]),
    )

    filled = await service.backfill_missing_embeddings()
    assert filled == 1

    writes = [c for c in captured if "UNWIND $updates AS u" in c[0] and "cl:Claim" in c[0]]
    assert writes, "expected a guarded Claim UNWIND write"
    _cypher, kwargs = writes[-1]
    assert kwargs["updates"] == [{"claim_id": "c1", "embedding": vec}]


@pytest.mark.asyncio
async def test_backfill_skips_claim_when_embedder_still_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._claim_candidate_rows = [{"claim_id": "c1", "content": "The lease ends in March."}]  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[[0.0] * _DIM]),
    )

    assert await service.backfill_missing_embeddings() == 0
    # Idempotent / outage-safe: a second run is still a no-op.
    assert await service.backfill_missing_embeddings() == 0
    assert not [c for c in captured if "UNWIND $updates AS u" in c[0] and "cl:Claim" in c[0]]


@pytest.mark.asyncio
async def test_backfill_claim_write_is_guarded_against_concurrent_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._claim_candidate_rows = [{"claim_id": "c1", "content": "The lease ends in March."}]  # type: ignore[attr-defined]
    service._write_filled = 1  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[[0.3] * _DIM]),
    )

    await service.backfill_missing_embeddings()

    writes = [c for c in captured if "UNWIND $updates AS u" in c[0] and "cl:Claim" in c[0]]
    assert writes
    cypher = writes[-1][0]
    assert "cl.embedding IS NULL OR none(x IN cl.embedding WHERE x <> 0.0)" in cypher


# --- Documented limitation (codex plan-review finding) -----------------------


def test_zero_vector_new_claim_never_matches_an_identical_current_claim() -> None:
    """A zero-vector new-claim embedding matches no current Claim, even an identical one.

    Cosine against an all-zero query is 0.0 for every candidate, below every
    supersession threshold.

    This pins the known outage-mode limitation documented in the FRE-768 plan: two
    Claims about the same fact-slot asserted during the same embedder outage can both
    end up "current" — the write-path guard and backfill added by this ticket repair
    embeddings, but do not re-run adjudication. Follow-up ticket: outage-mode
    re-adjudication.
    """
    current = ClaimRecord(
        claim_id="existing",
        content="The lease ends in March.",
        confidence=0.8,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        embedding=[0.5] * _DIM,
        facet="",
    )

    zero_vector_new_embedding = [0.0] * _DIM
    matches = matching_candidates("", zero_vector_new_embedding, [current])

    assert matches == []
