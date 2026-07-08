"""Tests for the per-session Captain's Log reflection cadence gate (FRE-710).

Captain's Log reflection previously fired on every completed turn (~0.7 reflections/turn live,
flooding the promotion funnel). ``ReflectionCadenceGate`` approximates a coarser "once per session"
cadence via a per-``session_id`` minimum-interval debounce, with an always-reflect bypass for turns
that hit the iteration limit (so proposals that mattered under the old per-turn cadence still surface).

No durable session-end signal exists anywhere in the codebase (confirmed by research), so this is a
process-local, in-memory approximation — mirroring ``SessionManager``'s own in-memory-only model.
"""

from __future__ import annotations

import inspect

from personal_agent.captains_log.reflection_cadence import (
    _EVICTION_MULTIPLIER,
    ReflectionCadenceGate,
    get_reflection_cadence_gate,
    reset_reflection_cadence_gate,
)


class TestReflectionCadenceGate:
    """Unit tests for the ReflectionCadenceGate debounce + bypass logic."""

    def test_first_turn_for_a_fresh_session_reflects(self) -> None:
        """The first turn seen for a session always reflects."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        assert gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0) is True

    def test_second_turn_within_the_interval_does_not_reflect(self) -> None:
        """A second turn inside the debounce window does not reflect."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        assert gate.should_reflect("session-a", hit_iteration_limit=False, now=1500.0) is False

    def test_second_turn_at_the_interval_boundary_reflects(self) -> None:
        """A turn exactly at the interval boundary reflects (>=, not >)."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        assert (
            gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0 + 1800.0) is True
        )

    def test_second_turn_after_the_interval_reflects(self) -> None:
        """A turn well past the interval reflects."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        assert gate.should_reflect("session-a", hit_iteration_limit=False, now=5000.0) is True

    def test_hit_iteration_limit_always_reflects_regardless_of_interval(self) -> None:
        """A notable (iteration-limit) turn bypasses the debounce window."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        # Well within the debounce window, but a notable (iteration-limit) turn bypasses it.
        assert gate.should_reflect("session-a", hit_iteration_limit=True, now=1001.0) is True

    def test_different_sessions_are_independent(self) -> None:
        """One session's debounce must not starve another session's first turn."""
        gate = ReflectionCadenceGate(min_interval_seconds=1800.0)
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        assert gate.should_reflect("session-b", hit_iteration_limit=False, now=1001.0) is True

    def test_stale_entry_is_pruned_on_a_later_call_for_another_session(self) -> None:
        """An entry older than min_interval_seconds * _EVICTION_MULTIPLIER is pruned."""
        gate = ReflectionCadenceGate(min_interval_seconds=100.0)
        gate.should_reflect("session-old", hit_iteration_limit=False, now=0.0)
        eviction_after = 100.0 * _EVICTION_MULTIPLIER
        # Well past eviction for session-old; the call for session-new prunes it opportunistically.
        gate.should_reflect("session-new", hit_iteration_limit=False, now=eviction_after + 1.0)
        assert "session-old" not in gate._last_reflected_at

    def test_should_reflect_is_not_a_coroutine_function(self) -> None:
        """Pins the synchronous-by-design invariant the asyncio-safety argument depends on.

        No `await` anywhere in this method is what makes the check-then-set against the shared
        dict safe under asyncio's single-threaded cooperative scheduling. A future edit must not
        make this async without re-establishing that safety some other way.
        """
        assert inspect.iscoroutinefunction(ReflectionCadenceGate.should_reflect) is False


class TestReflectionCadenceGateSingleton:
    """Unit tests for the process-global singleton accessors."""

    def teardown_method(self) -> None:
        """Reset the process-global gate between tests."""
        reset_reflection_cadence_gate()

    def test_get_returns_the_same_instance_across_calls(self) -> None:
        """The lazy singleton returns the same instance on repeated calls."""
        first = get_reflection_cadence_gate()
        second = get_reflection_cadence_gate()
        assert first is second

    def test_reset_returns_a_fresh_instance_with_clean_state(self) -> None:
        """Resetting the singleton yields a new instance with an empty map."""
        gate = get_reflection_cadence_gate()
        gate.should_reflect("session-a", hit_iteration_limit=False, now=1000.0)
        reset_reflection_cadence_gate()
        fresh = get_reflection_cadence_gate()
        assert fresh is not gate
        assert fresh._last_reflected_at == {}
