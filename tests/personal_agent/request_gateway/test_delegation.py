"""Tests for Stage A + Stage B delegation instruction composition."""

from personal_agent.request_gateway.delegation import (
    compose_delegation_instructions,
    compose_delegation_package,
    record_delegation_outcome,
)
from personal_agent.request_gateway.delegation_types import DelegationOutcome


class TestComposeDelegationInstructions:
    """Tests for compose_delegation_instructions()."""

    def test_basic_delegation_package(self) -> None:
        """Basic package includes task description, conventions, and criteria."""
        result = compose_delegation_instructions(
            task_description="Add a GET /sessions/{id}/export endpoint",
            context_notes=["FastAPI service on port 9000"],
            conventions=["Google-style docstrings", "structlog with trace_id"],
            acceptance_criteria=["Tests pass", "Type check passes"],
        )
        assert "Add a GET /sessions/{id}/export endpoint" in result
        assert "Google-style docstrings" in result
        assert "Tests pass" in result

    def test_includes_known_pitfalls(self) -> None:
        """Known pitfalls section rendered when provided."""
        result = compose_delegation_instructions(
            task_description="Add endpoint",
            known_pitfalls=["Include DB schema — last delegation failed without it"],
        )
        assert "Known pitfall" in result.lower() or "pitfall" in result.lower()

    def test_markdown_format(self) -> None:
        """Output starts with a markdown heading."""
        result = compose_delegation_instructions(
            task_description="Test task",
        )
        assert result.startswith("# ")  # Markdown heading

    def test_context_notes_rendered(self) -> None:
        """Context notes appear as bullet list."""
        result = compose_delegation_instructions(
            task_description="Task",
            context_notes=["Python 3.12", "Uses asyncio"],
        )
        assert "- Python 3.12" in result
        assert "- Uses asyncio" in result

    def test_acceptance_criteria_as_checklist(self) -> None:
        """Acceptance criteria rendered as markdown checklist items."""
        result = compose_delegation_instructions(
            task_description="Task",
            acceptance_criteria=["Unit tests pass", "Mypy clean"],
        )
        assert "- [ ] Unit tests pass" in result
        assert "- [ ] Mypy clean" in result

    def test_minimal_package(self) -> None:
        """Minimal package with only task description still valid."""
        result = compose_delegation_instructions(
            task_description="Simple task",
        )
        assert "Simple task" in result
        assert result.startswith("# ")


class TestComposeDelegationPackage:
    def test_creates_package_with_id(self) -> None:
        pkg = compose_delegation_package(
            task_description="Add export endpoint",
            trace_id="test-trace-1",
            target_agent="claude-code",
        )
        assert pkg.task_id.startswith("del-")
        assert pkg.target_agent == "claude-code"

    def test_includes_all_fields(self) -> None:
        pkg = compose_delegation_package(
            task_description="test",
            trace_id="test-trace-2",
            relevant_files=["app.py"],
            conventions=["type hints"],
            acceptance_criteria=["tests pass"],
            known_pitfalls=["include schema"],
        )
        assert pkg.context.relevant_files == ["app.py"]
        assert len(pkg.acceptance_criteria) == 1


class TestRecordDelegationOutcome:
    def test_logs_outcome(self) -> None:
        import structlog.testing

        outcome = DelegationOutcome(
            task_id="del-test",
            success=True,
            rounds_needed=1,
            what_worked="Good context",
            what_was_missing="",
        )
        with structlog.testing.capture_logs() as cap_logs:
            record_delegation_outcome(outcome, trace_id="test-trace-3")
        events = [e for e in cap_logs if "delegation_outcome" in e.get("event", "")]
        assert len(events) == 1
        assert events[0]["trace_id"] == "test-trace-3"
