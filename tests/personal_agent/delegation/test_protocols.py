"""Tests for delegation module protocol re-exports and DelegationExecutorProtocol.

Verifies that:
- DelegationPackage and DelegationOutcome are importable from delegation.protocols
- The existing frozen dataclasses behave correctly (immutability, field defaults)
- DelegationExecutorProtocol is a runtime-checkable structural type
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from personal_agent.delegation.protocols import (
    DelegationContext,
    DelegationExecutorProtocol,
    DelegationOutcome,
    DelegationPackage,
)


class TestDelegationPackage:
    """DelegationPackage creation and immutability."""

    def _make_package(self, **overrides: object) -> DelegationPackage:
        defaults: dict = {
            "task_id": "del-abc123",
            "target_agent": "claude-code",
            "task_description": "Refactor the memory module",
            "context": DelegationContext(service_path="src/personal_agent/memory/"),
            "created_at": datetime(2026, 4, 14, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return DelegationPackage(**defaults)

    def test_creation_with_defaults(self) -> None:
        pkg = self._make_package()
        assert pkg.task_id == "del-abc123"
        assert pkg.target_agent == "claude-code"
        assert pkg.estimated_complexity == "MODERATE"
        assert pkg.memory_excerpt == []
        assert pkg.acceptance_criteria == []
        assert pkg.known_pitfalls == []

    def test_frozen(self) -> None:
        pkg = self._make_package()
        with pytest.raises((AttributeError, TypeError)):
            pkg.task_id = "mutated"  # type: ignore[misc]

    def test_custom_complexity(self) -> None:
        pkg = self._make_package(estimated_complexity="COMPLEX")
        assert pkg.estimated_complexity == "COMPLEX"

    def test_with_context_fields(self) -> None:
        ctx = DelegationContext(
            service_path="src/",
            relevant_files=["memory.py"],
            conventions=["No print()"],
        )
        pkg = self._make_package(context=ctx)
        assert pkg.context.relevant_files == ["memory.py"]


class TestDelegationOutcome:
    """DelegationOutcome creation and immutability."""

    def _make_outcome(self, **overrides: object) -> DelegationOutcome:
        defaults: dict = {
            "task_id": "del-abc123",
            "success": True,
            "rounds_needed": 1,
            "what_worked": "Task completed cleanly",
            "what_was_missing": "",
        }
        defaults.update(overrides)
        return DelegationOutcome(**defaults)

    def test_creation_success(self) -> None:
        outcome = self._make_outcome()
        assert outcome.success is True
        assert outcome.task_id == "del-abc123"
        assert outcome.duration_minutes == 0.0
        assert outcome.user_satisfaction is None

    def test_creation_failure(self) -> None:
        outcome = self._make_outcome(
            success=False,
            rounds_needed=0,
            what_worked="",
            what_was_missing="Claude Code CLI not found",
        )
        assert outcome.success is False
        assert "Claude Code" in outcome.what_was_missing

    def test_frozen(self) -> None:
        outcome = self._make_outcome()
        with pytest.raises((AttributeError, TypeError)):
            outcome.success = False  # type: ignore[misc]

    def test_with_artifacts(self) -> None:
        outcome = self._make_outcome(artifacts_produced=["src/memory.py", "tests/test_memory.py"])
        assert len(outcome.artifacts_produced) == 2


class TestDelegationExecutorProtocol:
    """DelegationExecutorProtocol structural conformance."""

    def test_protocol_has_delegate_and_available(self) -> None:
        """Protocol must define delegate and available as members."""
        assert hasattr(DelegationExecutorProtocol, "delegate")
        assert hasattr(DelegationExecutorProtocol, "available")
