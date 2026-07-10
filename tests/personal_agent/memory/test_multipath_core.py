"""Unit tests for the shared multi-path recall core (FRE-724, ADR-0104).

Substrate-free: the arm methods and the reranker are mocked, so these run under
``make test``. They prove the assembled-core ACs at the mechanism altitude:

* AC-1 — ≥2 independent arms run and are fused by RRF rank (arms_executed emitted).
* AC-5 — the reranker reorders but never drops the fused set to empty.
* AC-6(a) — the fused set handed to the reranker never exceeds the input cap.
* Operating point — an all-arms-miss recall yields an empty fused set (the
  "no prior discussions" condition), never a per-arm hard gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.reranker import RerankResult
from personal_agent.memory.service import MemoryService


def _service() -> MemoryService:
    service = MemoryService()  # fre-375-allow: arms/rerank mocked; no substrate touched
    service.connected = True
    service.driver = object()  # truthy; never used when reranker is off
    return service


def _enable(monkeypatch, *, multiquery: bool, lexical: bool, reranker: bool = False) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "multiquery_arm_enabled", multiquery, raising=False)
    monkeypatch.setattr(s, "lexical_arm_enabled", lexical, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", reranker, raising=False)


class TestArmAssembly:
    """AC-1: ≥2 independent arms run and are RRF-fused by rank."""

    @pytest.mark.asyncio
    async def test_two_arms_run_and_fuse_by_rank(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(
            return_value=[RankedResult("e1", 1), RankedResult("e2", 2)]
        )
        service.lexical_recall_arm = AsyncMock(
            return_value=[RankedResult("e1", 1), RankedResult("t1", 3, kind="turn")]
        )
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert set(result.arms_executed) == {"multi_query", "lexical"}
        assert result.arms_failed == []
        # e1 surfaced by both arms → highest agreement → ranks first.
        assert result.items[0].item_id == "e1"
        assert result.items[0].arm_count == 2
        assert result.fused_set_size == 3

    @pytest.mark.asyncio
    async def test_executed_but_empty_arm_still_counted(self, monkeypatch) -> None:
        """An executed arm that returns nothing stays visible in telemetry (AC-1)."""
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        service.lexical_recall_arm = AsyncMock(return_value=[])
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert set(result.arms_executed) == {"multi_query", "lexical"}
        assert result.per_arm_counts["lexical"] == 0
        assert result.per_arm_counts["multi_query"] == 1

    @pytest.mark.asyncio
    async def test_dense_arm_used_when_multiquery_off(self, monkeypatch) -> None:
        """Multi-query off ⇒ the baseline dense arm supplies the dense signal."""
        _enable(monkeypatch, multiquery=False, lexical=True)
        service = _service()
        service.dense_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        service.lexical_recall_arm = AsyncMock(return_value=[RankedResult("t1", 1, kind="turn")])
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert "dense" in result.arms_executed
        assert "multi_query" not in result.arms_executed

    @pytest.mark.asyncio
    async def test_arm_exception_recorded_not_raised(self, monkeypatch) -> None:
        """A raising arm falls to arms_failed; recall still returns the other arm."""
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        service.lexical_recall_arm = AsyncMock(side_effect=RuntimeError("boom"))
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert "lexical" in result.arms_failed
        assert result.per_arm_counts["lexical"] == 0
        assert result.items[0].item_id == "e1"


class TestTailCaseMechanism:
    """AC-3 (mechanism): the extra arm recovers an item the dense arm misses.

    Substrate-free discriminator — the dense-family arm is forced to miss the
    out-of-vocabulary target while the lexical arm hits it. The full lived-tail
    proof on the real corpus/embedder is master-owned (FRE-489/670, deploy-gated).
    """

    @pytest.mark.asyncio
    async def test_lexical_recovers_dense_family_miss(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[])  # OOV for dense
        service.lexical_recall_arm = AsyncMock(return_value=[RankedResult("t-oov", 1, kind="turn")])
        result = await service._multipath_fused_recall("perception", path="broad", trace_id="t")
        assert any(i.item_id == "t-oov" for i in result.items)

    @pytest.mark.asyncio
    async def test_dense_family_alone_stays_missed(self, monkeypatch) -> None:
        """With the lexical arm off, the OOV target is not recovered (the off arm)."""
        _enable(monkeypatch, multiquery=True, lexical=False)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[])
        result = await service._multipath_fused_recall("perception", path="broad", trace_id="t")
        assert list(result.items) == []


class TestCapAndOperatingPoint:
    """AC-6(a) fused-set cap + the empty-after-all-arms operating point."""

    @pytest.mark.asyncio
    async def test_fused_set_capped_to_input_cap(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True)
        monkeypatch.setattr(get_settings(), "reranker_input_cap", 5, raising=False)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(
            return_value=[RankedResult(f"e{i}", i + 1) for i in range(10)]
        )
        service.lexical_recall_arm = AsyncMock(
            return_value=[RankedResult(f"x{i}", i + 1) for i in range(10)]
        )
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert result.fused_set_size == 5
        assert len(result.items) == 5

    @pytest.mark.asyncio
    async def test_all_arms_miss_yields_empty(self, monkeypatch) -> None:
        """Empty fused set = every arm missed (the 'no prior discussions' condition)."""
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[])
        service.lexical_recall_arm = AsyncMock(return_value=[])
        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert list(result.items) == []
        assert result.fused_set_size == 0


class TestLatencyTelemetry:
    """FRE-724 AC-6(c) — the multipath_recall event carries a numeric latency_ms.

    The end-to-end recall latency is the durable signal p50/p95 panels read and the
    standing auto-rollback guard watches, so the emit must always carry it as a float.
    """

    @pytest.mark.asyncio
    async def test_latency_ms_emitted_as_float(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        service.lexical_recall_arm = AsyncMock(return_value=[RankedResult("t1", 1, kind="turn")])
        with patch("personal_agent.memory.service.log") as mock_log:
            await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        events = [
            call
            for call in mock_log.info.call_args_list
            if call.args and call.args[0] == "multipath_recall"
        ]
        assert events, "multipath_recall event was not emitted"
        latency = events[0].kwargs["latency_ms"]
        assert isinstance(latency, float)
        assert latency >= 0.0


class TestRerankNeverGates:
    """AC-5: the reranker reorders the fused set; it never drops it to empty."""

    @pytest.mark.asyncio
    async def test_rerank_reorders_and_keeps_every_item(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True, reranker=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(
            return_value=[RankedResult("e1", 1), RankedResult("e2", 2), RankedResult("e3", 3)]
        )
        service.lexical_recall_arm = AsyncMock(return_value=[])
        service._resolve_item_texts = AsyncMock(return_value={"e1": "a", "e2": "b", "e3": "c"})
        # Reranker inverts the order with uniformly low scores — must not drop any.
        rerank_stub = AsyncMock(
            return_value=[
                RerankResult(index=2, score=0.01, document="c"),
                RerankResult(index=1, score=0.005, document="b"),
                RerankResult(index=0, score=0.001, document="a"),
            ]
        )
        with patch("personal_agent.memory.reranker.rerank", rerank_stub):
            result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert {i.item_id for i in result.items} == {"e1", "e2", "e3"}
        assert result.items[0].item_id == "e3"  # reranker promoted the last fused item

    @pytest.mark.asyncio
    async def test_rerank_failure_keeps_fused_order(self, monkeypatch) -> None:
        _enable(monkeypatch, multiquery=True, lexical=True, reranker=True)
        service = _service()
        service.multi_query_recall_arm = AsyncMock(
            return_value=[RankedResult("e1", 1), RankedResult("e2", 2)]
        )
        service.lexical_recall_arm = AsyncMock(return_value=[])
        service._resolve_item_texts = AsyncMock(return_value={"e1": "a", "e2": "b"})
        with patch(
            "personal_agent.memory.reranker.rerank",
            AsyncMock(side_effect=RuntimeError("reranker down")),
        ):
            result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")
        assert [i.item_id for i in result.items] == ["e1", "e2"]
