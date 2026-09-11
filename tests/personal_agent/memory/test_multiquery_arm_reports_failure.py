"""A failed multi-query variant stops reading as an empty recall (FRE-1481, ADR-0148 D1).

Two independent failure points inside one arm: the paraphrase step, and the per-variant
embedding+search step. The granularity is ANY variant failing, not every variant failing
(codex plan-review corrected the first draft against ADR-0148 D2: a partially degraded
recall composes UNAVAILABLE, never NOTHING_RELEVANT, and this matches every other arm's
existing all-or-nothing shape). The zero-vector case matters most in production:
``generate_embedding`` never raises on a provider failure, it degrades to a zero vector.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.exceptions import RecallArmFailedError
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.service import MemoryService

_DIMENSIONS = 8


class _FakeDriver:
    def session(self) -> "_FakeDriver":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _service() -> MemoryService:
    service = MemoryService()  # fre-375-allow: embedding/search mocked; no substrate touched
    service.connected = True
    service.driver = _FakeDriver()
    return service


def _arms(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "multiquery_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "structural_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)
    monkeypatch.setattr(s, "multipath_paraphrase_count", 2, raising=False)


class TestParaphraseStepFailure:
    @pytest.mark.asyncio
    async def test_a_raising_paraphrase_call_puts_multi_query_in_arms_failed(
        self, monkeypatch
    ) -> None:
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(side_effect=RuntimeError("paraphraser unreachable")),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "multi_query" in result.arms_failed
        assert [it.item_id for it in result.items] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_healthy_paraphrase_call_leaves_arms_failed_empty(self, monkeypatch) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=["sailing boats"]),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.1] * _DIMENSIONS),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[])
        service._dense_vector_search_ranked = AsyncMock(return_value=[RankedResult("e2", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert result.arms_failed == []


class TestPerVariantZeroVectorFailure:
    @pytest.mark.asyncio
    async def test_a_zero_vector_variant_puts_multi_query_in_arms_failed(self, monkeypatch) -> None:
        """The ordinary failure shape: no exception, just a dead embedder."""
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "multi_query" in result.arms_failed
        assert [it.item_id for it in result.items] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_real_vector_leaves_arms_failed_empty(self, monkeypatch) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.1] * _DIMENSIONS),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[])
        service._dense_vector_search_ranked = AsyncMock(return_value=[RankedResult("e2", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert result.arms_failed == []

    @pytest.mark.asyncio
    async def test_one_bad_variant_among_others_still_fails_the_whole_arm(
        self, monkeypatch
    ) -> None:
        """AC-3's granularity: ANY variant failing raises, not only every variant.

        The first variant (the original query) gets a real vector; the paraphrase
        variant gets a zero vector. Per ADR-0148 D2, a partially degraded recall
        composes UNAVAILABLE -- the arm must not read as having completed just
        because one variant out of several succeeded.
        """
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=["sailing boats"]),
        )
        embeddings = AsyncMock(side_effect=[[0.1] * _DIMENSIONS, [0.0] * _DIMENSIONS])
        monkeypatch.setattr("personal_agent.memory.service.generate_embedding", embeddings)
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])
        service._dense_vector_search_ranked = AsyncMock(return_value=[RankedResult("e2", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "multi_query" in result.arms_failed
        # The whole arm's contribution is dropped -- e2, from the surviving variant,
        # must not leak through a partially failed arm.
        assert "e2" not in {it.item_id for it in result.items}


class TestSessionAcquisitionFailure:
    @pytest.mark.asyncio
    async def test_a_raising_session_puts_multi_query_in_arms_failed(self, monkeypatch) -> None:
        _arms(monkeypatch)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=[]),
        )
        service = _service()
        service._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        class _RaisingDriver:
            def session(self) -> "_RaisingDriver":
                return self

            async def __aenter__(self) -> object:
                raise RuntimeError("pool exhausted")

            async def __aexit__(self, *exc: object) -> bool:
                return False

        service.driver = _RaisingDriver()

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "multi_query" in result.arms_failed


class TestPublicArmStaysFailOpen:
    """The documented "never hard-fails recall" contract is unchanged."""

    @pytest.mark.asyncio
    async def test_a_raising_paraphrase_call_degrades_to_the_original_query(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "multipath_paraphrase_count", 2, raising=False)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(side_effect=RuntimeError("paraphraser unreachable")),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.1] * _DIMENSIONS),
        )
        service = _service()
        service._dense_vector_search_ranked = AsyncMock(return_value=[RankedResult("e2", 1)])

        result = await service.multi_query_recall_arm("sailing")

        assert [it.item_id for it in result] == ["e2"]

    @pytest.mark.asyncio
    async def test_a_zero_vector_variant_returns_empty_rather_than_raising(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "multipath_paraphrase_count", 1, raising=False)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )
        service = _service()

        assert await service.multi_query_recall_arm("sailing") == []

    @pytest.mark.asyncio
    async def test_the_strict_arm_raises_the_cause_the_public_one_hides(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "multipath_paraphrase_count", 1, raising=False)
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_query_paraphrases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "personal_agent.memory.service.generate_embedding",
            AsyncMock(return_value=[0.0] * _DIMENSIONS),
        )
        service = _service()

        with pytest.raises(RecallArmFailedError):
            await service._multi_query_recall_arm_strict("sailing")

    @pytest.mark.asyncio
    async def test_an_empty_query_is_an_outcome_and_never_a_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        service = _service()

        assert await service._multi_query_recall_arm_strict("   ") == []
