"""Comprehensive model benchmarking framework.

This module provides a structured framework for evaluating model performance across
multiple dimensions: reasoning accuracy, coding quality, system analysis, latency,
resource usage, and quality metrics.

Usage:
    python tests/evaluation/model_benchmarks.py --model reasoning --suite all
    python tests/evaluation/model_benchmarks.py --model all --suite math --runs 10
"""

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from statistics import mean, median

from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext


class BenchmarkSuite(str, Enum):
    """Available benchmark suites."""

    MATH = "math"  # Math reasoning (MATH-500 style)
    CODING = "coding"  # Coding tasks (LiveCodeBench style)
    SYSTEM_ANALYSIS = "system_analysis"  # System health reasoning
    SIMPLE_QA = "simple_qa"  # Simple question answering
    ALL = "all"  # Run all suites


@dataclass
class BenchmarkTask:
    """A single benchmark task."""

    id: str
    suite: BenchmarkSuite
    category: str
    prompt: str
    expected_output: str | None = None
    expected_pattern: str | None = None  # Regex pattern to match
    min_tokens: int = 0
    max_tokens: int = 1000
    tools: list[dict] | None = None
    difficulty: int = 1  # 1-10 scale


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    task_id: str
    model_role: ModelRole
    model_id: str
    response: str
    success: bool
    score: float  # 0.0-1.0
    latency_ms: float
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    tokens_per_sec: float
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class BenchmarkReport:
    """Aggregate report for a benchmark run."""

    model_role: ModelRole
    model_id: str
    suite: BenchmarkSuite
    total_tasks: int
    successful_tasks: int
    failed_tasks: int
    success_rate: float
    avg_score: float
    avg_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    avg_tokens_per_sec: float
    total_tokens: int
    total_cost_estimate: float
    results: list[BenchmarkResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BenchmarkDataLoader:
    """Load benchmark tasks from JSON files."""

    def __init__(self, data_dir: Path = Path("tests/evaluation/benchmark_data")):
        """Initialize the data loader with a directory for benchmark data."""
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_suite(self, suite: BenchmarkSuite) -> list[BenchmarkTask]:
        """Load tasks for a specific suite."""
        if suite == BenchmarkSuite.ALL:
            tasks = []
            for s in [BenchmarkSuite.MATH, BenchmarkSuite.CODING, BenchmarkSuite.SYSTEM_ANALYSIS]:
                tasks.extend(self.load_suite(s))
            return tasks

        suite_file = self.data_dir / f"{suite.value}_tasks.json"
        if not suite_file.exists():
            print(f"⚠️  Benchmark suite file not found: {suite_file}")
            print("   Creating placeholder file with sample tasks...")
            self._create_placeholder_suite(suite, suite_file)

        with open(suite_file) as f:
            data = json.load(f)

        tasks = []
        for task_data in data.get("tasks", []):
            tasks.append(
                BenchmarkTask(
                    id=task_data["id"],
                    suite=suite,
                    category=task_data.get("category", "general"),
                    prompt=task_data["prompt"],
                    expected_output=task_data.get("expected_output"),
                    expected_pattern=task_data.get("expected_pattern"),
                    min_tokens=task_data.get("min_tokens", 0),
                    max_tokens=task_data.get("max_tokens", 1000),
                    tools=task_data.get("tools"),
                    difficulty=task_data.get("difficulty", 1),
                )
            )

        return tasks

    def _create_placeholder_suite(self, suite: BenchmarkSuite, file_path: Path) -> None:
        """Create placeholder benchmark suite."""
        if suite == BenchmarkSuite.MATH:
            data = {
                "suite": "math",
                "description": "Mathematical reasoning tasks (MATH-500 style)",
                "tasks": [
                    {
                        "id": "math_001",
                        "category": "arithmetic",
                        "prompt": "What is 234 + 567?",
                        "expected_output": "801",
                        "difficulty": 1,
                    },
                    {
                        "id": "math_002",
                        "category": "algebra",
                        "prompt": "Solve for x: 2x + 5 = 13",
                        "expected_output": "4",
                        "difficulty": 2,
                    },
                    {
                        "id": "math_003",
                        "category": "word_problem",
                        "prompt": "If a train travels 60 miles in 1.5 hours, what is its average speed in miles per hour?",
                        "expected_output": "40",
                        "difficulty": 3,
                    },
                ],
            }
        elif suite == BenchmarkSuite.CODING:
            data = {
                "suite": "coding",
                "description": "Coding tasks (LiveCodeBench style)",
                "tasks": [
                    {
                        "id": "code_001",
                        "category": "basic_function",
                        "prompt": "Write a Python function that returns the sum of two numbers.",
                        "expected_pattern": r"def.*\(.*,.*\):.*return.*\+",
                        "min_tokens": 20,
                        "difficulty": 1,
                    },
                    {
                        "id": "code_002",
                        "category": "list_manipulation",
                        "prompt": "Write a Python function that takes a list and returns only the even numbers.",
                        "expected_pattern": r"def.*\(.*\):.*\[.*for.*in.*if.*%.*2.*==.*0.*\]",
                        "min_tokens": 30,
                        "difficulty": 2,
                    },
                    {
                        "id": "code_003",
                        "category": "algorithm",
                        "prompt": "Write a Python function that implements binary search on a sorted list.",
                        "expected_pattern": r"def.*binary.*search.*\(.*\):.*while.*<.*:.*mid",
                        "min_tokens": 50,
                        "difficulty": 4,
                    },
                ],
            }
        elif suite == BenchmarkSuite.SYSTEM_ANALYSIS:
            data = {
                "suite": "system_analysis",
                "description": "System health analysis and reasoning",
                "tasks": [
                    {
                        "id": "sys_001",
                        "category": "interpretation",
                        "prompt": "Given CPU usage at 85%, memory at 70%, and disk at 60%, is the system healthy?",
                        "expected_pattern": r"(alert|warning|concern|high).*CPU",
                        "difficulty": 2,
                    },
                    {
                        "id": "sys_002",
                        "category": "trend_analysis",
                        "prompt": "If memory usage increased from 45GB to 60GB over the last week, what does this trend indicate?",
                        "expected_pattern": r"increas(ing|ed).*trend.*(monitor|concern|investigate)",
                        "difficulty": 3,
                    },
                ],
            }
        else:
            data = {
                "suite": suite.value,
                "description": f"{suite.value} benchmark tasks",
                "tasks": [],
            }

        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)


class BenchmarkEvaluator:
    """Evaluate benchmark results."""

    def __init__(self):
        """Initialize the result evaluator."""
        pass

    def evaluate_result(
        self, task: BenchmarkTask, response: str, latency_ms: float, usage: dict
    ) -> BenchmarkResult:
        """Evaluate a single benchmark result."""
        import re

        # Determine success and score
        success = False
        score = 0.0

        if task.expected_output:
            # Exact match (case-insensitive, stripped)
            if task.expected_output.strip().lower() in response.strip().lower():
                success = True
                score = 1.0
            else:
                # Partial credit based on string similarity
                score = self._similarity_score(task.expected_output, response)
                success = score > 0.7

        elif task.expected_pattern:
            # Regex pattern match
            if re.search(task.expected_pattern, response, re.IGNORECASE | re.DOTALL):
                success = True
                score = 1.0
            else:
                score = 0.0

        else:
            # No expected output - just check if response is reasonable
            # Estimate tokens: ~4 characters per token (rough approximation)
            estimated_tokens = len(response.strip()) / 4
            if estimated_tokens > task.min_tokens:
                success = True
                score = 1.0
            else:
                success = False
                score = 0.0

        # Extract token usage
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        reasoning_tokens = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
        tokens_per_sec = (output_tokens / latency_ms * 1000) if latency_ms > 0 else 0

        return BenchmarkResult(
            task_id=task.id,
            model_role=ModelRole.REASONING,  # Will be set by caller
            model_id="",  # Will be set by caller
            response=response,
            success=success,
            score=score,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            tokens_per_sec=tokens_per_sec,
        )

    def _similarity_score(self, expected: str, actual: str) -> float:
        """Calculate similarity score between expected and actual output."""
        # Simple word overlap score
        expected_words = set(expected.lower().split())
        actual_words = set(actual.lower().split())

        if not expected_words:
            return 0.0

        overlap = len(expected_words & actual_words)
        return overlap / len(expected_words)


class ModelBenchmark:
    """Main benchmark orchestrator."""

    def __init__(self):
        """Initialize the benchmark runner with LLM client, data loader, and evaluator."""
        self.client = LocalLLMClient()
        self.data_loader = BenchmarkDataLoader()
        self.evaluator = BenchmarkEvaluator()
        self.results_dir = Path("telemetry/evaluation/benchmarks")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    async def benchmark_model(
        self, role: ModelRole, suite: BenchmarkSuite, num_runs: int = 1
    ) -> BenchmarkReport:
        """Run benchmark suite against a model."""
        # Load tasks
        tasks = self.data_loader.load_suite(suite)

        if not tasks:
            print(f"⚠️  No tasks found for suite {suite.value}")
            return None

        print(f"\n{'=' * 80}")
        print(f"🔄 Benchmarking {role.value} model on {suite.value} suite")
        print(f"{'=' * 80}")
        print(f"   Tasks: {len(tasks)}")
        print(f"   Runs per task: {num_runs}")
        print(f"   Total evaluations: {len(tasks) * num_runs}")
        print()

        results: list[BenchmarkResult] = []
        from personal_agent.config import load_model_config

        config = load_model_config()
        model_def = config.models.get(role.value)
        model_id = model_def.id if model_def else "unknown"

        for i, task in enumerate(tasks, 1):
            print(f"   [{i}/{len(tasks)}] {task.id} ({task.category})... ", end="", flush=True)

            task_results = []
            for _run in range(num_runs):
                trace_ctx = TraceContext.new_trace()
                start_time = time.time()

                try:
                    response = await self.client.respond(
                        role=role,
                        messages=[{"role": "user", "content": task.prompt}],
                        tools=task.tools,
                        max_tokens=task.max_tokens,
                        trace_ctx=trace_ctx,
                    )

                    elapsed_ms = (time.time() - start_time) * 1000
                    content = response.get("content", "")
                    usage = response.get("usage", {})

                    result = self.evaluator.evaluate_result(task, content, elapsed_ms, usage)
                    result.model_role = role
                    result.model_id = model_id
                    task_results.append(result)

                except Exception as e:
                    elapsed_ms = (time.time() - start_time) * 1000
                    result = BenchmarkResult(
                        task_id=task.id,
                        model_role=role,
                        model_id=model_id,
                        response="",
                        success=False,
                        score=0.0,
                        latency_ms=elapsed_ms,
                        input_tokens=0,
                        output_tokens=0,
                        reasoning_tokens=0,
                        total_tokens=0,
                        tokens_per_sec=0.0,
                        error=str(e),
                    )
                    task_results.append(result)

            # Average across runs if multiple
            if num_runs > 1:
                avg_result = self._average_results(task_results)
                results.append(avg_result)
                print(
                    f"✓ ({avg_result.score:.2f} avg score, {avg_result.latency_ms:.0f}ms avg latency)"
                )
            else:
                result = task_results[0]
                results.append(result)
                status = "✓" if result.success else "✗"
                print(f"{status} ({result.score:.2f} score, {result.latency_ms:.0f}ms)")

        # Generate report
        report = self._generate_report(role, model_id, suite, results)
        self._save_report(report)
        self._print_report(report)

        return report

    def _average_results(self, results: list[BenchmarkResult]) -> BenchmarkResult:
        """Average multiple results for the same task."""
        if not results:
            raise ValueError("Cannot average empty results list")

        avg_result = results[0]
        avg_result.score = mean([r.score for r in results])
        avg_result.success = avg_result.score > 0.7
        avg_result.latency_ms = mean([r.latency_ms for r in results])
        avg_result.input_tokens = int(mean([r.input_tokens for r in results]))
        avg_result.output_tokens = int(mean([r.output_tokens for r in results]))
        avg_result.reasoning_tokens = int(mean([r.reasoning_tokens for r in results]))
        avg_result.total_tokens = int(mean([r.total_tokens for r in results]))
        avg_result.tokens_per_sec = mean([r.tokens_per_sec for r in results])

        return avg_result

    def _generate_report(
        self, role: ModelRole, model_id: str, suite: BenchmarkSuite, results: list[BenchmarkResult]
    ) -> BenchmarkReport:
        """Generate aggregate report."""
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        latencies = [r.latency_ms for r in results]
        latencies_sorted = sorted(latencies)

        p95_index = int(len(latencies_sorted) * 0.95)
        p99_index = int(len(latencies_sorted) * 0.99)

        return BenchmarkReport(
            model_role=role,
            model_id=model_id,
            suite=suite,
            total_tasks=len(results),
            successful_tasks=len(successful),
            failed_tasks=len(failed),
            success_rate=len(successful) / len(results) if results else 0.0,
            avg_score=mean([r.score for r in results]) if results else 0.0,
            avg_latency_ms=mean(latencies) if latencies else 0.0,
            median_latency_ms=median(latencies) if latencies else 0.0,
            p95_latency_ms=latencies_sorted[p95_index] if latencies_sorted else 0.0,
            p99_latency_ms=latencies_sorted[p99_index] if latencies_sorted else 0.0,
            avg_tokens_per_sec=mean([r.tokens_per_sec for r in results]) if results else 0.0,
            total_tokens=sum([r.total_tokens for r in results]),
            total_cost_estimate=sum([r.total_tokens for r in results]) * 0.0001,  # Placeholder
            results=results,
        )

    def _save_report(self, report: BenchmarkReport) -> None:
        """Save report to JSON file."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{report.model_role.value}_{report.suite.value}.json"
        filepath = self.results_dir / filename

        with open(filepath, "w") as f:
            json.dump(asdict(report), f, indent=2)

        print(f"\n📊 Report saved: {filepath}")

    def _print_report(self, report: BenchmarkReport) -> None:
        """Print formatted report."""
        print(f"\n{'=' * 80}")
        print(f"BENCHMARK REPORT - {report.model_role.value} on {report.suite.value}")
        print(f"{'=' * 80}")
        print(f"Model: {report.model_id}")
        print(f"Timestamp: {report.timestamp}")
        print()
        print(f"Tasks:          {report.total_tasks}")
        print(
            f"Success Rate:   {report.success_rate * 100:.1f}% ({report.successful_tasks}/{report.total_tasks})"
        )
        print(f"Avg Score:      {report.avg_score:.3f}")
        print()
        print("Latency (ms):")
        print(f"  Average:      {report.avg_latency_ms:.0f}ms")
        print(f"  Median:       {report.median_latency_ms:.0f}ms")
        print(f"  P95:          {report.p95_latency_ms:.0f}ms")
        print(f"  P99:          {report.p99_latency_ms:.0f}ms")
        print()
        print("Token Generation:")
        print(f"  Total Tokens:     {report.total_tokens}")
        print(f"  Avg Tok/s:        {report.avg_tokens_per_sec:.1f}")
        print()
        print(f"Cost Estimate:      ${report.total_cost_estimate:.4f}")
        print(f"{'=' * 80}")


async def main():
    """Run benchmarks from command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Run model benchmarks")
    parser.add_argument(
        "--model",
        type=str,
        choices=["router", "reasoning", "coding", "all"],
        default="reasoning",
        help="Model role to benchmark",
    )
    parser.add_argument(
        "--suite",
        type=str,
        choices=["math", "coding", "system_analysis", "simple_qa", "all"],
        default="math",
        help="Benchmark suite to run",
    )
    parser.add_argument("--runs", type=int, default=1, help="Number of runs per task")

    args = parser.parse_args()

    benchmark = ModelBenchmark()

    if args.model == "all":
        roles = [ModelRole.ROUTER, ModelRole.REASONING, ModelRole.CODING]
    else:
        roles = [ModelRole(args.model)]

    suite = BenchmarkSuite(args.suite)

    for role in roles:
        await benchmark.benchmark_model(role, suite, num_runs=args.runs)


if __name__ == "__main__":
    asyncio.run(main())
