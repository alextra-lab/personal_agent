"""Tests for the full gateway pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import tiktoken

from personal_agent.config.selection import reset_current_selection, set_current_selection
from personal_agent.governance.models import Mode
from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    DecompositionStrategy,
    GatewayOutput,
    TaskType,
)

_ENCODING = tiktoken.get_encoding("cl100k_base")
_UNIT = "the quick brown fox jumps over the lazy dog. "
_UNIT_TOKENS = len(_ENCODING.encode(_UNIT))


def _sized_text(target_tokens: int) -> str:
    """Plain text whose cl100k_base token count is ~target_tokens."""
    reps = max(1, int(target_tokens / _UNIT_TOKENS))
    return _UNIT * reps


def _history_sized(total_tokens: int, turns: int = 6) -> list[dict[str, str]]:
    per_turn = total_tokens // turns
    history = []
    for i in range(turns):
        history.append({"role": "user", "content": f"turn {i} " + _sized_text(per_turn)})
        history.append({"role": "assistant", "content": f"reply {i}"})
    return history


class TestRunGatewayPipeline:
    """Tests for run_gateway_pipeline() — full gateway orchestration."""

    @pytest.mark.asyncio
    async def test_simple_conversational_request(self) -> None:
        """Conversational message produces valid GatewayOutput."""
        result = await run_gateway_pipeline(
            user_message="Hello, how are you?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert isinstance(result, GatewayOutput)
        assert result.intent.task_type == TaskType.CONVERSATIONAL
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.session_id == "test-session"
        assert result.trace_id == "test-trace"

    @pytest.mark.asyncio
    async def test_memory_recall_request(self) -> None:
        """Memory recall request enriches context via adapter."""
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall_broad = AsyncMock(
            return_value=MagicMock(
                entities_by_type={"Topic": [{"name": "Python"}]},
                recent_sessions=[],
                total_entity_count=1,
            )
        )
        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        assert result.intent.task_type == TaskType.MEMORY_RECALL
        assert result.context.memory_context is not None

    @pytest.mark.asyncio
    async def test_coding_maps_to_delegation(self) -> None:
        """Coding request maps to DELEGATION task type."""
        result = await run_gateway_pipeline(
            user_message="Write a function to sort a list",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.DELEGATION

    @pytest.mark.asyncio
    async def test_alert_mode_disables_expansion(self) -> None:
        """ALERT mode disables expansion in governance context."""
        result = await run_gateway_pipeline(
            user_message="Hello",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.ALERT,
            memory_adapter=None,
        )
        assert result.governance.expansion_permitted is False

    @pytest.mark.asyncio
    async def test_pipeline_emits_telemetry_events(self) -> None:
        """Pipeline emits intent_classified and gateway_output structlog events."""
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Hello",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )

        # intent_classified event (emitted after Stage 4)
        intent_events = [e for e in cap_logs if e.get("event") == "intent_classified"]
        assert len(intent_events) == 1
        ie = intent_events[0]
        assert "task_type" in ie
        assert "complexity" in ie
        assert "confidence" in ie
        assert "signals" in ie
        assert ie["trace_id"] == "t"

        # gateway_output summary event (emitted at end of pipeline)
        output_events = [e for e in cap_logs if e.get("event") == "gateway_output"]
        assert len(output_events) == 1
        evt = output_events[0]
        assert "task_type" in evt
        assert "complexity" in evt
        assert "trace_id" in evt
        assert "strategy" in evt
        assert "has_memory" in evt
        assert "degraded_stages" in evt

    @pytest.mark.asyncio
    async def test_pipeline_logs_intent_classification_to_es(self) -> None:
        """Analysis intent emits telemetry with correct task_type value.

        Verifies the structured log event contains the specific intent
        classification result (not just field presence). ES indexing is
        handled by the existing structlog → ElasticsearchHandler.
        """
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Analyze the trade-offs",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        # Check intent_classified event for analysis classification
        intent_events = [e for e in cap_logs if e.get("event") == "intent_classified"]
        assert len(intent_events) == 1
        ie = intent_events[0]
        assert ie["task_type"] == "analysis"
        assert "confidence" in ie
        assert ie["trace_id"] == "t"

        # Check gateway_output summary event also present
        output_events = [e for e in cap_logs if e.get("event") == "gateway_output"]
        assert len(output_events) == 1

    @pytest.mark.asyncio
    async def test_degraded_stages_tracked(self) -> None:
        """Disconnected memory adapter produces degraded_stages entry."""
        # Memory adapter that fails
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=False)

        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        # Context assembly should report degraded memory
        assert result.context.memory_context is None
        assert "context_assembly:memory_unavailable" in result.degraded_stages

    # --- Slice 2 integration tests ---

    @pytest.mark.asyncio
    async def test_complex_analysis_produces_decompose_strategy(self) -> None:
        """Complex multi-part analysis request routes to DECOMPOSE strategy.

        Uses 3+ question marks to trigger COMPLEX complexity (question_count >= 3).
        """
        message = (
            "Analyze the trade-offs between microservices and monolithic architecture. "
            "What are the scalability implications? What are the hidden maintenance costs? "
            "What are the team-size thresholds where each approach breaks down?"
        )
        result = await run_gateway_pipeline(
            user_message=message,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.ANALYSIS
        assert result.decomposition.strategy == DecompositionStrategy.DECOMPOSE

    @pytest.mark.asyncio
    async def test_delegation_produces_delegate_strategy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coding/delegation request produces DELEGATE strategy when an adapter is wired.

        Uses 'write a function' keyword (word-boundary match in _CODING_KEYWORD_PATTERN).
        FRE-1376: DELEGATE is no longer the unconditional default — explicitly enable
        delegation to exercise the AC-3 positive path.
        """
        from personal_agent.config import get_settings

        monkeypatch.setattr(get_settings(), "delegation_enabled", True)
        result = await run_gateway_pipeline(
            user_message="write a function to parse and validate JSON schemas",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.DELEGATION
        assert result.decomposition.strategy == DecompositionStrategy.DELEGATE

    @pytest.mark.asyncio
    async def test_delegation_without_target_configured_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1376 AC-1 + AC-2: without a wired adapter, DELEGATION never reaches DELEGATE.

        AC-1: the real research-query fixture (session 5014ca54) classifies as ANALYSIS,
        not DELEGATION, once the word-boundary fix lands.
        AC-2: forcing a genuine coding message to classify as DELEGATION still does not
        reach DELEGATE when no adapter is configured — it falls back by complexity, and
        the fallback is recorded in the decomposition reason.
        """
        from personal_agent.config import get_settings

        monkeypatch.setattr(get_settings(), "delegation_enabled", False)

        research_query = (
            "Research how skills, memory, and subagents are actually used in "
            "state-of-the-art AI agent harnesses today, and determine what distinct "
            "role each plays. Compare how leading agent systems and harnesses "
            "implement these three capabilities in practice."
        )
        result = await run_gateway_pipeline(
            user_message=research_query,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.ANALYSIS
        assert result.decomposition.strategy != DecompositionStrategy.DELEGATE

        coding_result = await run_gateway_pipeline(
            user_message="Refactor the routing module",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert coding_result.intent.task_type == TaskType.DELEGATION
        assert coding_result.decomposition.strategy != DecompositionStrategy.DELEGATE
        assert coding_result.decomposition.reason.startswith("delegation_no_target_fallback")

    @pytest.mark.asyncio
    async def test_decomposition_assessed_reason_telemetry_reflects_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1376 AC-2: the gate's fallback is recorded in telemetry, not just returned."""
        import structlog.testing

        from personal_agent.config import get_settings

        monkeypatch.setattr(get_settings(), "delegation_enabled", False)
        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Refactor the routing module",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        events = [e for e in cap_logs if e.get("event") == "decomposition_assessed"]
        assert len(events) >= 1
        assert all(e["reason"].startswith("delegation_no_target_fallback") for e in events)
        assert all(e["strategy"] != "delegate" for e in events)

        monkeypatch.setattr(get_settings(), "delegation_enabled", True)
        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Refactor the routing module",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        events = [e for e in cap_logs if e.get("event") == "decomposition_assessed"]
        assert len(events) >= 1
        assert all(e["reason"] == "delegation_route_external" for e in events)
        assert all(e["strategy"] == "delegate" for e in events)

    @pytest.mark.asyncio
    async def test_budget_trim_when_context_exceeds_limit(self) -> None:
        """apply_budget() trims context when messages exceed max_tokens."""
        long_content = " ".join(["word"] * 200)
        large_history = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
        ]
        result = await run_gateway_pipeline(
            user_message="current question",
            session_id="s",
            session_messages=large_history,
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=3,
            max_context_tokens=50,  # tiny budget to force trimming
        )
        # Context should have been trimmed due to large history
        assert result.context.trimmed is True
        assert result.context.overflow_action is not None
        # Last user message must always be preserved
        last_user = next(
            (m for m in reversed(result.context.messages) if m["role"] == "user"),
            None,
        )
        assert last_user is not None
        assert last_user["content"] == "current question"

    @pytest.mark.asyncio
    async def test_zero_expansion_budget_forces_single(self) -> None:
        """Pipeline with expansion_budget=0 forces SINGLE regardless of intent."""
        message = (
            "Analyze the trade-offs between microservices and monolithic architecture "
            "in detail. Consider scalability, team size, deployment complexity, "
            "observability, and data consistency. What are the hidden costs?"
        )
        result = await run_gateway_pipeline(
            user_message=message,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=0,
        )
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.decomposition.reason == "zero_budget"

    @pytest.mark.asyncio
    async def test_telemetry_includes_budget_fields(self) -> None:
        """gateway_output event includes budget_trimmed and overflow_action."""
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Hello",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        events = [e for e in cap_logs if e.get("event") == "gateway_output"]
        assert len(events) == 1
        evt = events[0]
        assert "budget_trimmed" in evt
        assert "overflow_action" in evt
        assert "expansion_budget" in evt


class TestStage7BudgetResolvesActiveSelection:
    """FRE-978 — Stage 7's budget trim must measure the session's real window.

    Before the fix, an unset ``max_context_tokens`` always fell back to the
    static ``settings.context_budget_max_tokens`` (120K, Qwen-calibrated)
    regardless of the session's selected primary model — the sibling bug to
    FRE-972 (the in-turn compaction/consent gate), in Stage 7 of the pre-LLM
    gateway instead of the executor's state machine.

    ``qwen3.6-35b-instruct`` (context_length 65536) is smaller than the
    static fallback (120000); ``claude_sonnet`` (200000) is larger. A history
    sized between the two proves the trim now tracks the *selected* model,
    not a constant every session sizes identically against.
    """

    _SMALL_KEY = "qwen3.6-35b-instruct"  # context_length 65536 (config/models.yaml)
    _LARGE_KEY = "claude_sonnet"  # context_length 200000 (config/models.yaml)
    _HISTORY_TOKENS = 90000  # > 65536, < 120000 (static fallback), < 200000

    @pytest.mark.asyncio
    async def test_trims_for_a_selection_smaller_than_the_static_fallback(self) -> None:
        """A selection with a smaller window than the static fallback still trims."""
        history = _history_sized(self._HISTORY_TOKENS)
        token = set_current_selection({"primary": self._SMALL_KEY})
        try:
            result = await run_gateway_pipeline(
                user_message="current question",
                session_id="s",
                session_messages=history,
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        finally:
            reset_current_selection(token)

        assert result.context.trimmed is True

    @pytest.mark.asyncio
    async def test_does_not_trim_for_a_selection_larger_than_the_history(self) -> None:
        """The identical history is untouched under a larger-window selection.

        Proves the trim is selection-aware, not just more aggressive across
        the board: the static fallback (120000) would NOT have trimmed this
        history either, but the old code ignored the selection entirely, so
        this alone doesn't prove the fix — paired with the smaller-selection
        test above (which the static fallback would also NOT have trimmed,
        since 90000 < 120000) it shows the ceiling actually moves with the
        selection in both directions.
        """
        history = _history_sized(self._HISTORY_TOKENS)
        token = set_current_selection({"primary": self._LARGE_KEY})
        try:
            result = await run_gateway_pipeline(
                user_message="current question",
                session_id="s",
                session_messages=history,
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        finally:
            reset_current_selection(token)

        assert result.context.trimmed is False

    @pytest.mark.asyncio
    async def test_falls_back_to_static_budget_when_no_selection_is_set(self) -> None:
        """No active selection -> resolves via settings.context_budget_max_tokens (unchanged)."""
        history = _history_sized(self._HISTORY_TOKENS)
        result = await run_gateway_pipeline(
            user_message="current question",
            session_id="s",
            session_messages=history,
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        # 90000 tokens of history stays under the 120000 static fallback.
        assert result.context.trimmed is False


class TestPivot1Regression:
    """ADR-0063 §D1 / FRE-260 — TaskType→tool-filter wire severed.

    The deprecated GovernanceContext.allowed_tool_categories field was removed
    in FRE-265 (PIVOT-6); the regression guard is now structural (no such
    attribute) and is covered in test_governance.py.
    """

    def test_tool_registry_returns_tools_in_normal_mode(self) -> None:
        """Mode-only gate: NORMAL mode always yields a non-empty tool list.

        This is the direct regression guard for the FRE-254 failure class —
        a conversational intent previously produced tool_count=0 and forced
        the model to emit <tool_code> pseudo-code.
        """
        from personal_agent.tools import get_default_registry

        registry = get_default_registry()
        tools = registry.get_tool_definitions_for_llm(mode=Mode.NORMAL)
        assert len(tools) > 0, "NORMAL mode must expose at least one tool (FRE-260 regression)"
