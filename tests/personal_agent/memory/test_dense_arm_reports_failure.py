"""A dead embedder stops reading as an empty recall (FRE-1476, ADR-0148 D1).

The dense arm caught its own failures and returned ``[]``, the same value it returns when
it honestly finds nothing, so ``MultiPathRecallResult.arms_failed`` never saw them. The
turn could then claim absence on a path that had not run.

The zero-vector case is the one that matters in production: ``generate_embedding`` catches
every provider exception and returns a zero vector rather than raising, so an arm that only
stopped swallowing exceptions would still report a clean empty result on a dead embedder.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.exceptions import RecallArmFailedError
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.service import MemoryService

_DIMENSIONS = 8


def _service() -> MemoryService:
    service = MemoryService()  # fre-375-allow: arms and embedding mocked; no substrate touched
    service.connected = True
    service.driver = object()
    return service


def _arms(monkeypatch) -> None:
    """Dense plus lexical, reranker off — the arm set the ADR's AC-2 names."""
    s = get_settings()
    monkeypatch.setattr(s, "multiquery_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "structural_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)


class TestZeroEmbeddingIsReported:
    """The ordinary provider failure, which arrives as a zero vector and not a raise."""

    @pytest.mark.asyncio
    async def test_zero_vector_puts_dense_in_arms_failed(self, monkeypatch) -> None:
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "dense" in result.arms_failed
        # The surviving arm still contributes: this is the partial-failure case, and the
        # items must not vanish just because one arm reported.
        assert [it.item_id for it in result.items] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_real_vector_leaves_arms_failed_empty(self, monkeypatch) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.1] * _DIMENSIONS),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])
        service._dense_vector_search_ranked = AsyncMock(return_value=[RankedResult("e2", 1)])
        service.driver = _FakeDriver()

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert result.arms_failed == []
        assert {it.item_id for it in result.items} == {"e1", "e2"}


class TestRaisingEmbedderIsReported:
    """The other shape: the call itself raises."""

    @pytest.mark.asyncio
    async def test_a_raising_embedder_puts_dense_in_arms_failed(self, monkeypatch) -> None:
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(side_effect=RuntimeError("embedder unreachable")),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "dense" in result.arms_failed


class TestPublicArmStaysFailOpen:
    """The documented contract for every caller outside the core is unchanged."""

    @pytest.mark.asyncio
    async def test_zero_vector_returns_empty_rather_than_raising(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )

        assert await _service().dense_recall_arm("sailing") == []

    @pytest.mark.asyncio
    async def test_a_raising_embedder_returns_empty_rather_than_raising(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(side_effect=RuntimeError("embedder unreachable")),
        )

        assert await _service().dense_recall_arm("sailing") == []

    @pytest.mark.asyncio
    async def test_the_strict_arm_raises_the_cause_the_public_one_hides(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )

        with pytest.raises(RecallArmFailedError):
            await _service()._dense_recall_arm_strict("sailing")

    @pytest.mark.asyncio
    async def test_an_empty_query_is_an_outcome_and_never_a_failure(self) -> None:
        """Blank text short-circuits before any embedding, so it raises nothing."""
        assert await _service()._dense_recall_arm_strict("   ") == []


class _FakeDriver:
    """Minimal async-context driver for the one path that opens a session."""

    def session(self) -> "_FakeDriver":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False
