"""Tests for brainstem expansion budget signal."""

from __future__ import annotations

from personal_agent.brainstem.expansion import (
    ContractionState,
    compute_expansion_budget,
    detect_contraction,
)


class TestComputeExpansionBudget:
    def test_calm_system_returns_max(self) -> None:
        metrics = {
            "perf_system_cpu_load": 20.0,
            "perf_system_mem_used": 40.0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget == 3

    def test_high_cpu_reduces_budget(self) -> None:
        metrics = {
            "perf_system_cpu_load": 85.0,
            "perf_system_mem_used": 40.0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget < 3

    def test_high_memory_reduces_budget(self) -> None:
        metrics = {
            "perf_system_cpu_load": 20.0,
            "perf_system_mem_used": 88.0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget < 3

    def test_active_inference_reduces_budget(self) -> None:
        metrics = {
            "perf_system_cpu_load": 20.0,
            "perf_system_mem_used": 40.0,
            "active_inference_count": 2,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget <= 1

    def test_active_inference_defaults_to_zero(self) -> None:
        """active_inference_count is optional — absent means 0, not a warning."""
        metrics = {
            "perf_system_cpu_load": 20.0,
            "perf_system_mem_used": 40.0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget == 3

    def test_extreme_pressure_returns_zero(self) -> None:
        metrics = {
            "perf_system_cpu_load": 95.0,
            "perf_system_mem_used": 95.0,
            "active_inference_count": 3,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget == 0

    def test_missing_metrics_returns_max_budget(self) -> None:
        """When metrics are unavailable, assume idle and permit full expansion."""
        budget = compute_expansion_budget({}, max_budget=3)
        assert budget == 3

    def test_budget_never_negative(self) -> None:
        metrics = {
            "perf_system_cpu_load": 100.0,
            "perf_system_mem_used": 100.0,
            "active_inference_count": 10,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget >= 0


class TestDetectContraction:
    def test_idle_with_no_sub_agents(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=0,
            idle_seconds=60.0,
        )
        assert state == ContractionState.READY

    def test_busy_with_sub_agents(self) -> None:
        state = detect_contraction(
            active_sub_agents=2,
            pending_requests=0,
            idle_seconds=0.0,
        )
        assert state == ContractionState.EXPANDING

    def test_pending_requests_blocks_contraction(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=1,
            idle_seconds=30.0,
        )
        assert state == ContractionState.BUSY

    def test_not_idle_enough(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=0,
            idle_seconds=5.0,
            idle_threshold=30.0,
        )
        assert state == ContractionState.COOLING
