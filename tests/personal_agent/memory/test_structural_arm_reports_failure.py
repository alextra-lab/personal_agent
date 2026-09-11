"""A failed structural query stops reading as an empty recall (FRE-1481, ADR-0148 D1).

Split exactly like the dense arm (FRE-1476), with one extra wrinkle: the shared query
core also returns ``None`` for the legitimate "gated off" case, which must stay a
non-failure. Only the query exception itself raises past that point.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.exceptions import RecallArmFailedError
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.service import MemoryService


class _FakeDriver:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    def session(self) -> "_FakeDriver":
        return self

    async def __aenter__(self) -> "_FakeSession":
        return _FakeSession(self._exc)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, exc: Exception | None) -> None:
        self._exc = exc

    async def run(self, *args: object, **kwargs: object) -> object:
        if self._exc is not None:
            raise self._exc

        class _Result:
            async def data(self) -> list[dict[str, object]]:
                return []

        return _Result()


def _service(exc: Exception | None = None) -> MemoryService:
    service = MemoryService()  # fre-375-allow: driver faked; no substrate touched
    service.connected = True
    service.driver = _FakeDriver(exc)
    return service


def _arms(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "multiquery_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "lexical_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "structural_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)


class TestRaisingQueryIsReported:
    @pytest.mark.asyncio
    async def test_a_raising_query_puts_structural_in_arms_failed(self, monkeypatch) -> None:
        _arms(monkeypatch)
        service = _service(exc=RuntimeError("neo4j unreachable"))
        service._dense_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")

        assert "structural" in result.arms_failed
        assert [it.item_id for it in result.items] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_healthy_query_leaves_arms_failed_empty(self, monkeypatch) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        _arms(monkeypatch)
        service = _service(exc=None)
        service._dense_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("vision", path="broad", trace_id="t")

        assert result.arms_failed == []


class TestGatedOffIsNotAFailure:
    """The strict query helper's own ``None`` for "gated off" must never raise."""

    @pytest.mark.asyncio
    async def test_disabled_arm_returns_none_without_raising(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", False, raising=False)
        service = _service(exc=RuntimeError("would raise if the query ran"))

        result = await service._run_structural_arm_query_strict(
            entity_types=None,
            recency_days=None,
            anchor_names=None,
            entity_classes=None,
            limit=None,
            trace_id="t",
            session_id=None,
            user_id=None,
            authenticated=False,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_disconnected_returns_none_without_raising(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("would raise if the query ran"))
        service.connected = False

        result = await service._run_structural_arm_query_strict(
            entity_types=None,
            recency_days=None,
            anchor_names=None,
            entity_classes=None,
            limit=None,
            trace_id="t",
            session_id=None,
            user_id=None,
            authenticated=False,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ranked_strict_returns_empty_for_gated_off(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", False, raising=False)
        service = _service(exc=RuntimeError("would raise if the query ran"))

        assert await service._structural_recall_arm_ranked_strict() == []


class TestPublicArmStaysFailOpen:
    @pytest.mark.asyncio
    async def test_the_ranked_public_method_returns_empty_on_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("neo4j unreachable"))

        assert await service.structural_recall_arm_ranked() == []

    @pytest.mark.asyncio
    async def test_the_entitynode_public_method_returns_empty_on_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("neo4j unreachable"))

        assert await service.structural_recall_arm() == []

    @pytest.mark.asyncio
    async def test_the_strict_ranked_arm_raises_the_cause_the_public_one_hides(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("neo4j unreachable"))

        with pytest.raises(RecallArmFailedError):
            await service._structural_recall_arm_ranked_strict()
