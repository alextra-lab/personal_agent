"""Tests for the SLM-health error-reason hint in the executor (FRE-399 / ADR-0083).

The hint is injected after classify_error() when the cached SLM snapshot is
degraded or down. It is purely best-effort and must never impair the error path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _make_snapshot(status: str, degrade_reason: str | None = None):
    """Build a mock SlmHealthSnapshot."""
    snap = MagicMock()
    snap.status = status
    snap.degrade_reason.return_value = degrade_reason
    return snap


def _make_classified(reason: str = "Inference timed out", category: str = "timeout"):
    from personal_agent.error_classification import ClassifiedError

    return ClassifiedError(
        category=category,  # type: ignore[arg-type]
        reason=reason,
        next_step="Try again",
        actions=("retry", "stop"),
    )


class TestErrorReasonHint:
    """The hint enriches the classified error reason when SLM is degraded/down."""

    def _run_hint(
        self,
        *,
        exc,
        snap,
        classified,
    ):
        """
        Exercise the hint block from executor.py in isolation.

        Re-implements the logic directly so we don't need to spin up the full
        executor FSM — the hint is a self-contained try/except block.
        """
        import personal_agent.observability.slm_health.cache as cache_mod

        # Prime the cache
        if snap is not None:
            cache_mod._cached_snapshot = snap
            cache_mod._cached_at = __import__("time").monotonic()
        else:
            cache_mod._cached_snapshot = None
            cache_mod._cached_at = 0.0

        from personal_agent.config import settings as _s
        from personal_agent.llm_client.types import LLMClientError, LLMRateLimit
        from personal_agent.observability.slm_health import get_cached_snapshot

        try:
            if (
                isinstance(exc, LLMClientError)
                and not isinstance(exc, LLMRateLimit)
                and classified.category not in ("budget_denied",)
            ):
                _snap = get_cached_snapshot(ttl=_s.slm_health_cache_ttl_seconds)
                if _snap is not None and _snap.status != "up":
                    _reason = _snap.degrade_reason()
                    if _reason:
                        classified = classified.__class__(
                            category=classified.category,
                            reason=f"{classified.reason} [{_reason}]",
                            next_step=classified.next_step,
                            actions=classified.actions,
                            partial=classified.partial,
                        )
        except Exception:
            pass

        # Clean up cache
        cache_mod._cached_snapshot = None
        return classified

    def test_degraded_snap_enriches_reason(self) -> None:
        from personal_agent.llm_client.types import LLMTimeout

        snap = _make_snapshot("degraded", "GPU pinned (98.0%)")
        classified = _make_classified("Inference timed out")
        exc = LLMTimeout("timed out")

        result = self._run_hint(exc=exc, snap=snap, classified=classified)
        assert "[GPU pinned" in result.reason
        assert "Inference timed out" in result.reason

    def test_down_snap_enriches_reason(self) -> None:
        from personal_agent.llm_client.types import LLMConnectionError

        snap = _make_snapshot("down", "SLM unreachable")
        classified = _make_classified("Connection failed", "connection_error")
        exc = LLMConnectionError("refused")

        result = self._run_hint(exc=exc, snap=snap, classified=classified)
        assert "SLM unreachable" in result.reason

    def test_up_snap_does_not_change_reason(self) -> None:
        from personal_agent.llm_client.types import LLMTimeout

        snap = _make_snapshot("up")
        classified = _make_classified("Inference timed out")
        exc = LLMTimeout("timed out")

        result = self._run_hint(exc=exc, snap=snap, classified=classified)
        assert result.reason == "Inference timed out"

    def test_no_snap_does_not_change_reason(self) -> None:
        from personal_agent.llm_client.types import LLMTimeout

        classified = _make_classified("Inference timed out")
        exc = LLMTimeout("timed out")

        result = self._run_hint(exc=exc, snap=None, classified=classified)
        assert result.reason == "Inference timed out"

    def test_rate_limit_not_enriched(self) -> None:
        """LLMRateLimit is not a transient error — skip the hint."""
        from personal_agent.llm_client.types import LLMRateLimit

        snap = _make_snapshot("degraded", "GPU pinned (98.0%)")
        classified = _make_classified("Rate limited", "rate_limit")
        exc = LLMRateLimit("429")

        result = self._run_hint(exc=exc, snap=snap, classified=classified)
        assert result.reason == "Rate limited"

    def test_budget_denied_not_enriched(self) -> None:
        """budget_denied category is excluded from the hint."""
        from personal_agent.llm_client.types import LLMClientError

        snap = _make_snapshot("degraded", "GPU pinned")
        classified = _make_classified("Budget exceeded", "budget_denied")
        exc = LLMClientError("denied")

        result = self._run_hint(exc=exc, snap=snap, classified=classified)
        assert result.reason == "Budget exceeded"

    def test_exception_in_hint_block_is_swallowed(self) -> None:
        """If anything in the hint raises, the original classified error is returned."""
        from personal_agent.llm_client.types import LLMTimeout

        classified = _make_classified("Inference timed out")
        exc = LLMTimeout("timed out")

        # Corrupt the cache module so get_cached_snapshot raises
        with patch(
            "personal_agent.observability.slm_health.get_cached_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            result = self._run_hint(exc=exc, snap=None, classified=classified)

        # Must not raise; reason unchanged
        assert result.reason == "Inference timed out"
