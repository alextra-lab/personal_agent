"""Metrics collection for the Graphiti experiment."""

from __future__ import annotations

import statistics
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class TimingResult:
    """Latency measurements for a set of operations."""

    label: str
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.median(self.latencies_ms)

    @property
    def p95(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_vals = sorted(self.latencies_ms)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    @property
    def mean(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.mean(self.latencies_ms)

    def to_dict(self) -> dict[str, float]:
        return {
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "mean_ms": round(self.mean, 2),
            "count": len(self.latencies_ms),
        }


@asynccontextmanager
async def timed(timing: TimingResult) -> AsyncIterator[None]:
    """Async context manager that records elapsed time in milliseconds."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        timing.latencies_ms.append(elapsed_ms)


@dataclass
class DedupMetrics:
    """Entity deduplication accuracy metrics."""

    raw_mentions: int = 0
    unique_entities_created: int = 0
    expected_canonical: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def dedup_ratio(self) -> float:
        if self.raw_mentions == 0:
            return 0.0
        return self.unique_entities_created / self.raw_mentions

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_mentions": self.raw_mentions,
            "unique_entities_created": self.unique_entities_created,
            "expected_canonical": self.expected_canonical,
            "dedup_ratio": round(self.dedup_ratio, 3),
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
        }


@dataclass
class RetrievalMetrics:
    """Precision and recall for retrieval scenarios."""

    queries: list[dict[str, Any]] = field(default_factory=list)

    def add_query(
        self,
        query_text: str,
        expected_ids: set[str],
        returned_ids: set[str],
        latency_ms: float,
    ) -> None:
        true_positives = len(expected_ids & returned_ids)
        precision = true_positives / len(returned_ids) if returned_ids else 0.0
        recall = true_positives / len(expected_ids) if expected_ids else 0.0

        self.queries.append({
            "query": query_text,
            "precision": precision,
            "recall": recall,
            "latency_ms": latency_ms,
            "expected_count": len(expected_ids),
            "returned_count": len(returned_ids),
        })

    @property
    def avg_precision(self) -> float:
        if not self.queries:
            return 0.0
        return statistics.mean(q["precision"] for q in self.queries)

    @property
    def avg_recall(self) -> float:
        if not self.queries:
            return 0.0
        return statistics.mean(q["recall"] for q in self.queries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_precision": round(self.avg_precision, 3),
            "avg_recall": round(self.avg_recall, 3),
            "query_count": len(self.queries),
            "queries": self.queries,
        }


@dataclass
class CostTracker:
    """Track LLM token usage and estimated cost."""

    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    embedding_cost_per_mtok: float = 0.02

    @property
    def estimated_cost_usd(self) -> float:
        input_cost = (self.input_tokens / 1_000_000) * self.input_cost_per_mtok
        output_cost = (self.output_tokens / 1_000_000) * self.output_cost_per_mtok
        embed_cost = (self.embedding_tokens / 1_000_000) * self.embedding_cost_per_mtok
        return round(input_cost + output_cost + embed_cost, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embedding_tokens": self.embedding_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass
class ScenarioResult:
    """Aggregated results for a single scenario."""

    scenario_name: str
    seshat: dict[str, Any] = field(default_factory=dict)
    graphiti: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_name,
            "seshat": self.seshat,
            "graphiti": self.graphiti,
            "notes": self.notes,
        }
