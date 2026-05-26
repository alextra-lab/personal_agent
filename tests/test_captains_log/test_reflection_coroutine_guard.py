"""Tests for the coroutine-guard in reflection_dspy._ensure_str (FRE-385).

Root cause: DSPy runs via asyncio.to_thread with a cloud async LM backend.
Intermittently, DSPy result fields are coroutine objects instead of strings.
The code called .strip() on these fields before guarding them with _ensure_str,
producing: AttributeError: 'coroutine' object has no attribute 'strip'

These tests ensure:
1. _ensure_str returns the default for coroutine values
2. _ensure_str CLOSES the coroutine to suppress RuntimeWarning
3. The log-statement pattern at the crash site (line 419) survives coroutine fields
4. The _parse_enum inputs are also guarded (latent bug at lines 430-431)
"""

from __future__ import annotations

from typing import Any

import pytest

from personal_agent.captains_log.reflection_dspy import _ensure_str

# ---------------------------------------------------------------------------
# _ensure_str unit tests
# ---------------------------------------------------------------------------


class TestEnsureStr:
    """Unit tests for the _ensure_str coroutine guard."""

    def test_string_value_returned_unchanged(self) -> None:
        """Plain string passes through without modification."""
        assert _ensure_str("hello") == "hello"

    def test_none_returns_default(self) -> None:
        """None returns the empty-string default."""
        assert _ensure_str(None) == ""

    def test_none_returns_custom_default(self) -> None:
        """None returns a caller-supplied default."""
        assert _ensure_str(None, "fallback") == "fallback"

    def test_non_string_coerced_to_str(self) -> None:
        """Non-string, non-None values are coerced with str()."""
        assert _ensure_str(42) == "42"
        assert _ensure_str(3.14) == "3.14"

    def test_coroutine_returns_default(self) -> None:
        """A coroutine returns the empty-string default."""

        async def _coro() -> None:
            pass

        coro = _coro()
        try:
            result = _ensure_str(coro)
            assert result == ""
        finally:
            if coro.cr_frame is not None:
                coro.close()  # safety: close if _ensure_str didn't

    def test_coroutine_is_closed_to_prevent_runtime_warning(self) -> None:
        """_ensure_str must call .close() on the coroutine to prevent RuntimeWarning.

        Without .close(), Python emits "RuntimeWarning: coroutine was never awaited"
        when the coroutine is garbage-collected.
        """

        async def _coro() -> None:
            pass

        coro = _coro()
        assert coro.cr_frame is not None, "pre-condition: coroutine starts open"

        _ensure_str(coro)

        # After _ensure_str, the coroutine must be closed
        assert coro.cr_frame is None, (
            "_ensure_str must call .close() on coroutine values to suppress "
            "RuntimeWarning: coroutine was never awaited"
        )

    def test_coroutine_returns_custom_default(self) -> None:
        """A coroutine with a custom default returns that default."""

        async def _coro() -> None:
            pass

        coro = _coro()
        result = _ensure_str(coro, "custom")
        assert result == "custom"
        assert coro.cr_frame is None, "coroutine must be closed"

    def test_empty_string_returned_unchanged(self) -> None:
        """Empty string is a valid string value, not treated as falsy."""
        assert _ensure_str("") == ""


# ---------------------------------------------------------------------------
# Regression: the crash site pattern (was line 419)
# ---------------------------------------------------------------------------


class TestLogStatementPattern:
    """Guards the pattern that crashed in production.

    Before fix: bool(result.proposed_change_what.strip()) crashed because
    result.proposed_change_what was a coroutine.

    After fix: bool(_ensure_str(getattr(result, "proposed_change_what", "")).strip())
    never crashes regardless of field type.
    """

    def _make_coroutine(self) -> Any:
        """Return a fresh coroutine object."""

        async def _coro() -> None:
            pass

        return _coro()

    def test_unfixed_pattern_crashes_on_coroutine(self) -> None:
        """Documents the pre-fix behaviour: direct .strip() on coroutine raises AttributeError."""
        coro = self._make_coroutine()
        try:
            with pytest.raises(AttributeError, match="'coroutine' object has no attribute 'strip'"):
                _ = coro.strip()
        finally:
            coro.close()

    def test_fixed_pattern_survives_coroutine_for_proposed_change(self) -> None:
        """The fixed pattern for has_proposed_change does not raise."""

        class FakeDspyResult:
            proposed_change_what = property(lambda self: self._make_coroutine())

            def _make_coroutine(self) -> Any:
                async def _coro() -> None:
                    pass

                return _coro()

        result = FakeDspyResult()
        coro = result._make_coroutine()
        try:
            # Fixed pattern: _ensure_str before .strip()
            has_proposed_change = bool(_ensure_str(coro).strip())
            assert has_proposed_change is False
        finally:
            if coro.cr_frame is not None:
                coro.close()

    def test_fixed_pattern_survives_coroutine_for_rationale(self) -> None:
        """The fixed pattern for has_rationale does not mislead when rationale is a coroutine."""
        coro = self._make_coroutine()
        try:
            # Fixed pattern
            has_rationale = bool(_ensure_str(coro))
            assert has_rationale is False
        finally:
            if coro.cr_frame is not None:
                coro.close()

    def test_fixed_pattern_returns_true_for_real_string(self) -> None:
        """The fixed pattern correctly reports True when the field IS a string."""
        has_proposed_change = bool(_ensure_str("Add retry logic").strip())
        assert has_proposed_change is True


# ---------------------------------------------------------------------------
# Regression: _parse_enum latent crash (was lines 430-431)
# ---------------------------------------------------------------------------


class TestParseEnumCoroutineGuard:
    """_parse_enum receives raw getattr() output which can be a coroutine.

    Before fix: _parse_enum called raw.strip().lower(), crashing when raw is a coroutine.
    After fix: callers wrap with _ensure_str() so _parse_enum always receives a string.
    """

    def test_parse_enum_receives_str_from_ensure_str(self) -> None:
        """_ensure_str wrapping ensures _parse_enum always gets a string.

        This mirrors the fixed call site for proposed_change_category and
        proposed_change_scope (reflection_dspy.py lines 430-431).
        """
        from personal_agent.captains_log.models import ChangeCategory
        from personal_agent.captains_log.reflection_dspy import _parse_enum

        async def _coro() -> None:
            pass

        coro = _coro()
        try:
            # Fixed: wrap with _ensure_str before _parse_enum
            safe = _ensure_str(coro, "")
            result = _parse_enum(ChangeCategory, safe)
            # Empty string → None (not a valid ChangeCategory)
            assert result is None
        finally:
            if coro.cr_frame is not None:
                coro.close()

    def test_parse_enum_crashes_on_raw_coroutine(self) -> None:
        """Documents the pre-fix latent crash: _parse_enum calls .strip() on its input."""
        from personal_agent.captains_log.models import ChangeCategory
        from personal_agent.captains_log.reflection_dspy import _parse_enum

        async def _coro() -> None:
            pass

        coro = _coro()
        try:
            with pytest.raises(AttributeError, match="'coroutine' object has no attribute 'strip'"):
                _parse_enum(ChangeCategory, coro)  # type: ignore[arg-type]
        finally:
            coro.close()
