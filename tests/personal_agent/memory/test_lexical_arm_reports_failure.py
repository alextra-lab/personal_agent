"""A failed lexical query stops reading as an empty recall (FRE-1481, ADR-0148 D1).

The lexical arm caught its own query exception and returned ``[]``, the same value it
returns when it honestly finds nothing, so ``MultiPathRecallResult.arms_failed`` never
saw it. Split exactly like the dense arm (FRE-1476): the public method stays fail-open,
the strict sibling the core gathers raises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.exceptions import RecallArmFailedError
from personal_agent.memory.fusion import RankedResult
from personal_agent.memory.service import MemoryService


class _FakeDriver:
    """Minimal async-context driver whose session raises on ``run``."""

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
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "structural_arm_enabled", False, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)


class TestRaisingQueryIsReported:
    @pytest.mark.asyncio
    async def test_a_raising_query_puts_lexical_in_arms_failed(self, monkeypatch) -> None:
        _arms(monkeypatch)
        service = _service(exc=RuntimeError("fulltext index unreachable"))
        service._dense_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert "lexical" in result.arms_failed
        # The surviving arm still contributes: the partial-failure case.
        assert [it.item_id for it in result.items] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_healthy_query_leaves_arms_failed_empty(self, monkeypatch) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        _arms(monkeypatch)
        service = _service(exc=None)
        service._dense_recall_arm_strict = AsyncMock(return_value=[RankedResult("e1", 1)])

        result = await service._multipath_fused_recall("sailing", path="broad", trace_id="t")

        assert result.arms_failed == []


class TestPublicArmStaysFailOpen:
    """The documented contract for every caller outside the core is unchanged."""

    @pytest.mark.asyncio
    async def test_the_public_method_returns_empty_on_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("fulltext index unreachable"))

        assert await service.lexical_recall_arm("sailing") == []

    @pytest.mark.asyncio
    async def test_the_strict_arm_raises_the_cause_the_public_one_hides(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("fulltext index unreachable"))

        with pytest.raises(RecallArmFailedError):
            await service._lexical_recall_arm_strict("sailing")

    @pytest.mark.asyncio
    async def test_an_empty_query_is_an_outcome_and_never_a_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
        service = _service(exc=RuntimeError("would raise if reached"))

        assert await service._lexical_recall_arm_strict("   ") == []
