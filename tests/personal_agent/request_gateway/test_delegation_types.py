"""Tests for Stage B delegation types."""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationOutcome,
    DelegationPackage,
)


class TestDelegationPackage:
    def test_construction(self) -> None:
        pkg = DelegationPackage(
            task_id="del-001",
            target_agent="claude-code",
            task_description="Add GET /sessions/{id}/export endpoint",
            context=DelegationContext(
                service_path="src/personal_agent/service/",
                relevant_files=["app.py", "models.py"],
                conventions=["Google-style docstrings", "structlog"],
            ),
            memory_excerpt=[
                {"type": "entity", "name": "FastAPI", "relevance": 0.9},
            ],
            acceptance_criteria=[
                "Tests pass: uv run pytest tests/service/",
                "Type check: uv run mypy src/",
            ],
            known_pitfalls=["Include DB schema — last delegation failed without it"],
            estimated_complexity="MODERATE",
            created_at=datetime.now(tz=timezone.utc),
        )
        assert pkg.target_agent == "claude-code"
        assert len(pkg.acceptance_criteria) == 2
        assert len(pkg.known_pitfalls) == 1

    def test_frozen(self) -> None:
        pkg = DelegationPackage(
            task_id="d1", target_agent="codex",
            task_description="test",
            context=DelegationContext(service_path="src/"),
            created_at=datetime.now(tz=timezone.utc),
        )
        try:
            pkg.task_id = "changed"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestDelegationOutcome:
    def test_success_outcome(self) -> None:
        outcome = DelegationOutcome(
            task_id="del-001",
            success=True,
            rounds_needed=1,
            what_worked="Included DB schema and test patterns",
            what_was_missing="",
            artifacts_produced=["src/service/export.py", "tests/test_export.py"],
            duration_minutes=12.0,
            user_satisfaction=4,
        )
        assert outcome.success is True
        assert outcome.user_satisfaction == 4

    def test_failure_outcome(self) -> None:
        outcome = DelegationOutcome(
            task_id="del-002",
            success=False,
            rounds_needed=3,
            what_worked="Basic endpoint created",
            what_was_missing="Neo4j query context, entity model schema",
            artifacts_produced=[],
            duration_minutes=45.0,
            user_satisfaction=2,
        )
        assert outcome.success is False
        assert outcome.rounds_needed == 3


class TestDelegationContext:
    def test_minimal_context(self) -> None:
        ctx = DelegationContext(service_path="src/")
        assert ctx.relevant_files is None
        assert ctx.conventions is None

    def test_full_context(self) -> None:
        ctx = DelegationContext(
            service_path="src/personal_agent/service/",
            relevant_files=["app.py"],
            conventions=["type hints", "structlog"],
            db_schema="session table: id, started_at, ...",
            test_patterns="mirror src/ structure in tests/",
        )
        assert ctx.db_schema is not None
