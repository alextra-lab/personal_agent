"""FRE-1433 — the in-flight local-call registry (ADR-0144 D4), unit level.

The dispatch-level proof (the registry as ``_respond_local`` drives it) lives in
``test_local_via_litellm.py::TestInflightRegistry``. These tests pin the registry's own contract:
a unique key per attempt, a dispatch stamp that gives a chunkless call an age, and a last-byte
stamp that only moves forward.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from personal_agent.llm_client.inflight import (
    InflightCallRegistry,
    get_inflight_registry,
    reset_inflight_registry,
)

from personal_agent.llm_client import inflight


@pytest.fixture
def clock() -> Iterator[list[float]]:
    """A one-element list that stands in for ``time.monotonic``; tests assign ``clock[0]``."""
    now = [100.0]
    with patch.object(inflight.time, "monotonic", lambda: now[0]):
        yield now


class TestRegistry:
    def test_a_registered_call_with_no_chunk_has_an_age_from_dispatch(
        self, clock: list[float]
    ) -> None:
        registry = InflightCallRegistry()
        key = registry.register("span-a", 1)

        clock[0] = 107.5
        call = registry.get(key)

        assert call is not None
        assert call.age_s() == pytest.approx(7.5)
        assert call.silence_s() == pytest.approx(7.5)

    def test_touch_moves_the_last_byte_stamp_and_leaves_the_dispatch_stamp(
        self, clock: list[float]
    ) -> None:
        registry = InflightCallRegistry()
        key = registry.register("span-a", 1)

        clock[0] = 110.0
        registry.touch(key)
        clock[0] = 113.0
        call = registry.get(key)

        assert call is not None
        assert call.dispatched_monotonic == 100.0
        assert call.last_byte_monotonic == 110.0
        assert call.age_s() == pytest.approx(13.0)
        assert call.silence_s() == pytest.approx(3.0)

    def test_two_calls_with_the_same_attempt_are_distinct(self) -> None:
        registry = InflightCallRegistry()

        first = registry.register("span-a", 1)
        second = registry.register("span-b", 1)

        assert first != second
        assert {c.span_id for c in registry.snapshot()} == {"span-a", "span-b"}
        assert len(registry.snapshot()) == 2

    def test_two_attempts_of_one_call_are_distinct(self) -> None:
        registry = InflightCallRegistry()

        registry.register("span-a", 1)
        registry.register("span-a", 2)

        assert sorted(c.attempt for c in registry.snapshot()) == [1, 2]

    def test_touching_one_call_leaves_its_sibling_alone(self, clock: list[float]) -> None:
        registry = InflightCallRegistry()
        first = registry.register("span-a", 1)
        second = registry.register("span-b", 1)

        clock[0] = 120.0
        registry.touch(first)

        sibling = registry.get(second)
        assert sibling is not None
        assert sibling.last_byte_monotonic == 100.0

    def test_remove_is_idempotent_and_touch_after_remove_registers_nothing(self) -> None:
        registry = InflightCallRegistry()
        key = registry.register("span-a", 1)

        registry.remove(key)
        registry.remove(key)
        registry.touch(key)

        assert registry.get(key) is None
        assert registry.snapshot() == ()

    def test_a_snapshot_is_a_copy(self) -> None:
        registry = InflightCallRegistry()
        before = registry.snapshot()

        registry.register("span-a", 1)

        assert before == ()
        assert len(registry.snapshot()) == 1


class TestProcessSingleton:
    def test_the_accessor_returns_one_registry_until_reset(self) -> None:
        reset_inflight_registry()
        first = get_inflight_registry()

        assert get_inflight_registry() is first

        reset_inflight_registry()
        assert get_inflight_registry() is not first
