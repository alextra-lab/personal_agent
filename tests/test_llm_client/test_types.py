"""Tests for LLM client types."""

import pytest

from personal_agent.config.config_guard import load_matrix, repo_root
from personal_agent.llm_client.types import (
    LLMClientError,
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMServerError,
    LLMTimeout,
    ModelRole,
    ToolCall,
)

#: Roles that are real, currently-live background call-site roles (FRE-1037) but are
#: not declared in config/model_roles.yaml's `bindings:` block — skill_routing resolves
#: via a dedicated AppConfig field, study via scripts/study/categorizer.py's own
#: convention. Kept as a named, documented exception rather than silently widening the
#: "derived from config" claim to cover them (owner-confirmed FRE-1037 scoping).
_MATRIX_INDEPENDENT_ROLES = frozenset({"skill_routing", "study"})


class TestModelRole:
    """Test ModelRole enum."""

    def test_model_role_values(self) -> None:
        """Test that ModelRole has exactly two values: PRIMARY and SUB_AGENT."""
        assert ModelRole.PRIMARY == "primary"
        assert ModelRole.SUB_AGENT == "sub_agent"

    def test_model_role_matches_bindings_matrix(self) -> None:
        """Every config/model_roles.yaml `bindings:` role must be a ModelRole member.

        This is the "cannot drift apart again" guard FRE-1037 requires: a future
        matrix role added without a corresponding ModelRole member fails CI loudly,
        rather than silently forcing that role's call sites back to role=primary.
        """
        matrix = load_matrix(repo_root())
        bindings = matrix.get("bindings", {})
        assert bindings, "config/model_roles.yaml bindings: block must not be empty"

        role_values = {role.value for role in ModelRole}
        assert set(bindings.keys()) <= role_values

    def test_model_role_matrix_independent_roles_documented(self) -> None:
        """skill_routing/study are intentionally not in the matrix — assert they still exist."""
        role_values = {role.value for role in ModelRole}
        assert _MATRIX_INDEPENDENT_ROLES <= role_values

    def test_model_role_fifteen_members(self) -> None:
        """ModelRole has fifteen members: fourteen from FRE-1037, plus FRE-1281's."""
        assert len(list(ModelRole)) == 15
        assert ModelRole.SPAN_EXTRACTION == "span_extraction"
        assert ModelRole.COMPRESSOR == "compressor"
        assert ModelRole.ARTIFACT_BUILDER == "artifact_builder"
        assert ModelRole.ENTITY_EXTRACTION == "entity_extraction"
        assert ModelRole.CAPTAINS_LOG == "captains_log"
        assert ModelRole.SESSION_SUMMARY == "session_summary"
        assert ModelRole.INSIGHTS == "insights"
        assert ModelRole.EMBEDDING == "embedding"
        assert ModelRole.RERANKER == "reranker"
        assert ModelRole.RERANKER_FALLBACK == "reranker_fallback"
        assert ModelRole.VISION == "vision"
        assert ModelRole.SKILL_ROUTING == "skill_routing"
        assert ModelRole.STUDY == "study"

    def test_model_role_required_returns_matching_role(self) -> None:
        """ModelRole.required() resolves a valid string, case-insensitively."""
        assert ModelRole.required("captains_log") is ModelRole.CAPTAINS_LOG
        assert ModelRole.required("PRIMARY") is ModelRole.PRIMARY

    def test_model_role_required_raises_on_unassigned_role(self) -> None:
        """ModelRole.required() raises rather than silently defaulting (FRE-1037 step 3)."""
        with pytest.raises(ValueError, match="not a valid ModelRole"):
            ModelRole.required("entity_extraction_role")  # a resolved model key, not a role name

        with pytest.raises(ValueError, match="not a valid ModelRole"):
            ModelRole.required("")

    def test_model_role_string_representation(self) -> None:
        """Test that ModelRole values are strings."""
        assert isinstance(ModelRole.PRIMARY.value, str)
        assert ModelRole.PRIMARY.value == "primary"
        assert isinstance(ModelRole.SUB_AGENT.value, str)
        assert ModelRole.SUB_AGENT.value == "sub_agent"


class TestToolCall:
    """Test ToolCall TypedDict."""

    def test_tool_call_structure(self) -> None:
        """Test that ToolCall has required fields."""
        tool_call: ToolCall = {
            "id": "call_123",
            "name": "read_file",
            "arguments": '{"path": "/tmp/test.txt"}',
        }
        assert tool_call["id"] == "call_123"
        assert tool_call["name"] == "read_file"
        assert tool_call["arguments"] == '{"path": "/tmp/test.txt"}'


class TestErrorHierarchy:
    """Test LLM client error hierarchy."""

    def test_llm_client_error_is_base(self) -> None:
        """Test that LLMClientError is the base exception."""
        assert issubclass(LLMTimeout, LLMClientError)
        assert issubclass(LLMConnectionError, LLMClientError)
        assert issubclass(LLMRateLimit, LLMClientError)
        assert issubclass(LLMServerError, LLMClientError)
        assert issubclass(LLMInvalidResponse, LLMClientError)

    def test_error_messages(self) -> None:
        """Test that errors can be created with messages."""
        timeout = LLMTimeout("Request timed out")
        assert str(timeout) == "Request timed out"

        conn_error = LLMConnectionError("Connection failed")
        assert str(conn_error) == "Connection failed"

        rate_limit = LLMRateLimit("Rate limit exceeded")
        assert str(rate_limit) == "Rate limit exceeded"

        server_error = LLMServerError("Server error 500")
        assert str(server_error) == "Server error 500"

        invalid_response = LLMInvalidResponse("Invalid JSON")
        assert str(invalid_response) == "Invalid JSON"
