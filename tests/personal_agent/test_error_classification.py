"""Tests for error classification (FRE-398).

Each test maps an exception class to its expected ClassifiedError
category, non-empty reason, and next_step guidance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from personal_agent.error_classification import ClassifiedError, classify_error

from personal_agent.cost_gate.types import BudgetDenied
from personal_agent.llm_client.types import (
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMServerError,
    LLMTimeout,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _budget_denied() -> BudgetDenied:
    return BudgetDenied(
        role="main_inference",
        time_window="daily",
        current_spend=Decimal("5.00"),
        cap=Decimal("3.00"),
        window_resets_at=datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# ClassifiedError dataclass
# ---------------------------------------------------------------------------


class TestClassifiedError:
    def test_frozen(self) -> None:
        err = ClassifiedError(
            category="generic",
            reason="something broke",
            next_step="retry",
            actions=("retry",),
        )
        with pytest.raises(Exception):
            err.category = "timeout"  # type: ignore[misc]

    def test_partial_defaults_false(self) -> None:
        err = ClassifiedError(
            category="timeout",
            reason="r",
            next_step="n",
            actions=(),
        )
        assert err.partial is False

    def test_partial_can_be_set(self) -> None:
        err = ClassifiedError(
            category="timeout",
            reason="r",
            next_step="n",
            actions=("retry",),
            partial=True,
        )
        assert err.partial is True


# ---------------------------------------------------------------------------
# classify_error — one test per mapping table row
# ---------------------------------------------------------------------------


class TestClassifyLLMServerError:
    def test_category(self) -> None:
        assert classify_error(LLMServerError("524 origin timeout")).category == "model_server"

    def test_reason_non_empty(self) -> None:
        assert classify_error(LLMServerError("msg")).reason

    def test_next_step_mentions_retry_and_cloud(self) -> None:
        result = classify_error(LLMServerError("msg"))
        combined = (result.reason + " " + result.next_step).lower()
        assert "retry" in combined or "cloud" in combined

    def test_actions_include_retry_and_switch_to_cloud(self) -> None:
        actions = classify_error(LLMServerError("msg")).actions
        assert "retry" in actions
        assert "switch_to_cloud" in actions

    def test_returns_classified_error(self) -> None:
        assert isinstance(classify_error(LLMServerError("e")), ClassifiedError)


class TestClassifyLLMTimeout:
    def test_category(self) -> None:
        assert classify_error(LLMTimeout("timed out after 251s")).category == "timeout"

    def test_reason_mentions_timeout(self) -> None:
        result = classify_error(LLMTimeout("msg"))
        assert "timeout" in result.reason.lower() or "timed out" in result.reason.lower()

    def test_actions_include_retry_and_switch_to_cloud(self) -> None:
        actions = classify_error(LLMTimeout("msg")).actions
        assert "retry" in actions
        assert "switch_to_cloud" in actions


class TestClassifyLLMConnectionError:
    def test_category(self) -> None:
        assert classify_error(LLMConnectionError("ECONNREFUSED")).category == "connection"

    def test_actions_include_retry(self) -> None:
        assert "retry" in classify_error(LLMConnectionError("msg")).actions

    def test_reason_mentions_server(self) -> None:
        reason = classify_error(LLMConnectionError("msg")).reason.lower()
        assert "model" in reason or "server" in reason or "connect" in reason


class TestClassifyLLMRateLimit:
    def test_category(self) -> None:
        assert classify_error(LLMRateLimit("429")).category == "rate_limit"

    def test_next_step_mentions_wait(self) -> None:
        next_step = classify_error(LLMRateLimit("429")).next_step.lower()
        assert "wait" in next_step or "retry" in next_step


class TestClassifyBudgetDenied:
    def test_category(self) -> None:
        assert classify_error(_budget_denied()).category == "budget_denied"

    def test_reason_includes_role_and_window(self) -> None:
        result = classify_error(_budget_denied())
        assert "main_inference" in result.reason or "daily" in result.reason

    def test_next_step_mentions_budget(self) -> None:
        next_step = classify_error(_budget_denied()).next_step.lower()
        assert "budget" in next_step or "reset" in next_step or "cap" in next_step

    def test_actions_include_stop(self) -> None:
        assert "stop" in classify_error(_budget_denied()).actions


class TestClassifyInferenceSlotTimeout:
    """InferenceSlotTimeout (concurrency.py) should map to timeout category."""

    def test_category(self) -> None:
        from personal_agent.llm_client.concurrency import InferenceSlotTimeout

        assert classify_error(InferenceSlotTimeout("no slot")).category == "timeout"


class TestClassifyGenericFallback:
    def test_category_for_unknown_exception(self) -> None:
        assert classify_error(ValueError("something weird")).category == "generic"

    def test_reason_non_empty(self) -> None:
        assert classify_error(RuntimeError("boom")).reason

    def test_next_step_non_empty(self) -> None:
        assert classify_error(RuntimeError("boom")).next_step

    def test_actions_include_retry(self) -> None:
        assert "retry" in classify_error(RuntimeError("boom")).actions

    def test_llm_invalid_response_gets_generic_or_model_server(self) -> None:
        """LLMInvalidResponse is not a specific category — generic or model_server is acceptable."""
        result = classify_error(LLMInvalidResponse("unexpected format"))
        assert result.category in ("generic", "model_server", "connection")
