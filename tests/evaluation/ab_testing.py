r"""A/B testing framework for comparing models.

This module provides infrastructure for running controlled experiments comparing
two models (or configurations) across the same test suite.

Usage:
    python tests/evaluation/ab_testing.py \
        --model-a reasoning --model-b reasoning_baseline \
        --suite math --queries 20
"""

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from personal_agent.config import load_model_config
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext


@dataclass
class ABTestResult:
    """Result of comparing two models on a single query."""

    query: str
    model_a_role: ModelRole
    model_a_id: str
    model_a_response: str
    model_a_latency_ms: float
    model_a_tokens: int
    model_b_role: ModelRole
    model_b_id: str
    model_b_response: str
    model_b_latency_ms: float
    model_b_tokens: int
    judgment: str  # "a_wins", "b_wins", "tie", "both_fail"
    confidence: float  # 0.0-1.0
    reasoning: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ABTestReport:
    """Aggregate report for an A/B test."""

    model_a_role: ModelRole
    model_a_id: str
    model_b_role: ModelRole
    model_b_id: str
    total_queries: int
    a_wins: int
    b_wins: int
    ties: int
    both_fail: int
    a_win_rate: float
    b_win_rate: float
    tie_rate: float
    a_avg_latency_ms: float
    b_avg_latency_ms: float
    a_avg_tokens: float
    b_avg_tokens: float
    latency_speedup: float  # b_latency / a_latency
    results: list[ABTestResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ABTester:
    """A/B testing orchestrator."""

    def __init__(self):
        """Initialize the A/B tester with LLM client and result storage."""
        self.client = LocalLLMClient()
        self.results_dir = Path("telemetry/evaluation/ab_tests")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    async def run_ab_test(
        self,
        model_a_role: ModelRole,
        model_b_role: ModelRole,
        queries: list[str],
        use_judge: bool = True,
    ) -> ABTestReport:
        """Run A/B test comparing two models."""
        config = load_model_config()
        models = config.models

        model_a_def = models.get(model_a_role.value)
        model_a_id = model_a_def.id if model_a_def else "unknown"
        model_b_def = models.get(model_b_role.value)
        model_b_id = model_b_def.id if model_b_def else "unknown"

        print(f"\n{'=' * 80}")
        print(f"🔬 A/B Test: {model_a_role.value} vs {model_b_role.value}")
        print(f"{'=' * 80}")
        print(f"Model A: {model_a_id}")
        print(f"Model B: {model_b_id}")
        print(f"Queries: {len(queries)}")
        print(f"Judge:   {'LLM' if use_judge else 'Automated'}")
        print()

        results: list[ABTestResult] = []

        for i, query in enumerate(queries, 1):
            print(f"   [{i}/{len(queries)}] Testing query... ", end="", flush=True)

            # Run both models
            result_a = await self._run_model(model_a_role, model_a_id, query)
            result_b = await self._run_model(model_b_role, model_b_id, query)

            # Judge results
            if use_judge:
                judgment, confidence, reasoning = await self._llm_judge(
                    query, result_a, result_b, model_a_id, model_b_id
                )
            else:
                judgment, confidence, reasoning = self._automated_judge(result_a, result_b)

            test_result = ABTestResult(
                query=query,
                model_a_role=model_a_role,
                model_a_id=model_a_id,
                model_a_response=result_a["response"],
                model_a_latency_ms=result_a["latency_ms"],
                model_a_tokens=result_a["tokens"],
                model_b_role=model_b_role,
                model_b_id=model_b_id,
                model_b_response=result_b["response"],
                model_b_latency_ms=result_b["latency_ms"],
                model_b_tokens=result_b["tokens"],
                judgment=judgment,
                confidence=confidence,
                reasoning=reasoning,
            )

            results.append(test_result)

            winner_symbol = {"a_wins": "A", "b_wins": "B", "tie": "=", "both_fail": "X"}[judgment]
            print(f"{winner_symbol} (conf: {confidence:.2f})")

        # Generate report
        report = self._generate_report(model_a_role, model_a_id, model_b_role, model_b_id, results)
        self._save_report(report)
        self._print_report(report)

        return report

    async def _run_model(self, role: ModelRole, model_id: str, query: str) -> dict:
        """Run a single model on a query."""
        trace_ctx = TraceContext.new_trace()
        start_time = time.time()

        try:
            response = await self.client.respond(
                role=role, messages=[{"role": "user", "content": query}], trace_ctx=trace_ctx
            )

            elapsed_ms = (time.time() - start_time) * 1000
            content = response.get("content", "")
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
            total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

            return {
                "response": content,
                "latency_ms": elapsed_ms,
                "tokens": total_tokens,
                "error": None,
            }

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            return {"response": "", "latency_ms": elapsed_ms, "tokens": 0, "error": str(e)}

    async def _llm_judge(
        self,
        query: str,
        result_a: dict,
        result_b: dict,
        model_a_id: str,
        model_b_id: str,
    ) -> tuple[str, float, str]:
        """Use LLM to judge which response is better."""
        # Use router model as judge (fast, cheap)
        judge_prompt = f"""You are an impartial judge comparing two AI model responses.

Query: {query}

Response A (from {model_a_id}):
{result_a["response"]}

Response B (from {model_b_id}):
{result_b["response"]}

Compare these responses based on:
1. Correctness - Is the answer accurate?
2. Completeness - Does it fully address the query?
3. Clarity - Is it well-explained and easy to understand?
4. Conciseness - Is it appropriately concise without being verbose?

Output your judgment in JSON format:
{{
  "winner": "a" | "b" | "tie" | "both_fail",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of your judgment"
}}

Output ONLY valid JSON, nothing else."""

        try:
            trace_ctx = TraceContext.new_trace()
            response = await self.client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": judge_prompt}],
                trace_ctx=trace_ctx,
            )

            content = response.get("content", "{}")

            # Extract JSON from response
            import re

            json_match = re.search(r"\{[^}]+\}", content, re.DOTALL)
            if json_match:
                judgment_data = json.loads(json_match.group())
                winner = judgment_data.get("winner", "tie")
                confidence = float(judgment_data.get("confidence", 0.5))
                reasoning = judgment_data.get("reasoning", "No reasoning provided")

                # Map to full judgment string
                judgment_map = {
                    "a": "a_wins",
                    "b": "b_wins",
                    "tie": "tie",
                    "both_fail": "both_fail",
                }
                judgment = judgment_map.get(winner, "tie")

                return judgment, confidence, reasoning

        except Exception as e:
            print(f"⚠️  LLM judge failed: {e}, falling back to automated judge")

        # Fallback to automated judge
        return self._automated_judge(result_a, result_b)

    def _automated_judge(self, result_a: dict, result_b: dict) -> tuple[str, float, str]:
        """Automated heuristic-based judging."""
        # Both failed
        if result_a["error"] and result_b["error"]:
            return "both_fail", 1.0, "Both models failed to generate responses"

        # One failed
        if result_a["error"]:
            return "b_wins", 1.0, f"Model A failed: {result_a['error']}"
        if result_b["error"]:
            return "a_wins", 1.0, f"Model B failed: {result_b['error']}"

        # Both succeeded - compare based on response quality heuristics
        len_a = len(result_a["response"])
        len_b = len(result_b["response"])

        # If one is significantly longer, it might be more complete
        if len_a > len_b * 1.5:
            return "a_wins", 0.6, "Response A is significantly more detailed"
        elif len_b > len_a * 1.5:
            return "b_wins", 0.6, "Response B is significantly more detailed"

        # Similar length - tie
        return "tie", 0.5, "Responses are similar in length and structure"

    def _generate_report(
        self,
        model_a_role: ModelRole,
        model_a_id: str,
        model_b_role: ModelRole,
        model_b_id: str,
        results: list[ABTestResult],
    ) -> ABTestReport:
        """Generate aggregate A/B test report."""
        a_wins = len([r for r in results if r.judgment == "a_wins"])
        b_wins = len([r for r in results if r.judgment == "b_wins"])
        ties = len([r for r in results if r.judgment == "tie"])
        both_fail = len([r for r in results if r.judgment == "both_fail"])

        total = len(results)
        a_win_rate = a_wins / total if total > 0 else 0.0
        b_win_rate = b_wins / total if total > 0 else 0.0
        tie_rate = ties / total if total > 0 else 0.0

        a_latencies = [r.model_a_latency_ms for r in results]
        b_latencies = [r.model_b_latency_ms for r in results]
        a_avg_latency = mean(a_latencies) if a_latencies else 0.0
        b_avg_latency = mean(b_latencies) if b_latencies else 0.0
        latency_speedup = b_avg_latency / a_avg_latency if a_avg_latency > 0 else 1.0

        a_tokens = [r.model_a_tokens for r in results]
        b_tokens = [r.model_b_tokens for r in results]
        a_avg_tokens = mean(a_tokens) if a_tokens else 0.0
        b_avg_tokens = mean(b_tokens) if b_tokens else 0.0

        return ABTestReport(
            model_a_role=model_a_role,
            model_a_id=model_a_id,
            model_b_role=model_b_role,
            model_b_id=model_b_id,
            total_queries=total,
            a_wins=a_wins,
            b_wins=b_wins,
            ties=ties,
            both_fail=both_fail,
            a_win_rate=a_win_rate,
            b_win_rate=b_win_rate,
            tie_rate=tie_rate,
            a_avg_latency_ms=a_avg_latency,
            b_avg_latency_ms=b_avg_latency,
            a_avg_tokens=a_avg_tokens,
            b_avg_tokens=b_avg_tokens,
            latency_speedup=latency_speedup,
            results=results,
        )

    def _save_report(self, report: ABTestReport) -> None:
        """Save A/B test report."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_ab_{report.model_a_role.value}_vs_{report.model_b_role.value}.json"
        filepath = self.results_dir / filename

        with open(filepath, "w") as f:
            json.dump(asdict(report), f, indent=2)

        print(f"\n📊 A/B Test report saved: {filepath}")

    def _print_report(self, report: ABTestReport) -> None:
        """Print formatted A/B test report."""
        print(f"\n{'=' * 80}")
        print("A/B TEST RESULTS")
        print(f"{'=' * 80}")
        print(f"Model A: {report.model_a_id} ({report.model_a_role.value})")
        print(f"Model B: {report.model_b_id} ({report.model_b_role.value})")
        print()
        print(f"Total Queries:  {report.total_queries}")
        print()
        print("Results:")
        print(f"  A Wins:       {report.a_wins} ({report.a_win_rate * 100:.1f}%)")
        print(f"  B Wins:       {report.b_wins} ({report.b_win_rate * 100:.1f}%)")
        print(f"  Ties:         {report.ties} ({report.tie_rate * 100:.1f}%)")
        print(f"  Both Fail:    {report.both_fail}")
        print()
        print("Performance:")
        print(f"  A Avg Latency:  {report.a_avg_latency_ms:.0f}ms")
        print(f"  B Avg Latency:  {report.b_avg_latency_ms:.0f}ms")
        print(f"  Speedup:        {report.latency_speedup:.2f}x")
        print()
        print(f"  A Avg Tokens:   {report.a_avg_tokens:.0f}")
        print(f"  B Avg Tokens:   {report.b_avg_tokens:.0f}")
        print()

        # Determine winner
        if report.a_win_rate > report.b_win_rate + 0.1:
            print(f"🏆 Model A ({report.model_a_id}) wins overall")
        elif report.b_win_rate > report.a_win_rate + 0.1:
            print(f"🏆 Model B ({report.model_b_id}) wins overall")
        else:
            print("⚖️  Models are roughly equivalent in quality")

        print(f"{'=' * 80}")


async def main():
    """Run A/B test from command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Run A/B test between two models")
    parser.add_argument("--model-a", type=str, required=True, help="Model A role (e.g., reasoning)")
    parser.add_argument(
        "--model-b", type=str, required=True, help="Model B role (e.g., reasoning_baseline)"
    )
    parser.add_argument("--queries", type=int, default=20, help="Number of test queries")
    parser.add_argument(
        "--no-judge", action="store_true", help="Disable LLM judge (use automated heuristics)"
    )

    args = parser.parse_args()

    # Load or generate test queries
    test_queries = [
        "What is 157 + 234?",
        "Solve for x: 3x - 7 = 14",
        "Write a Python function to find the maximum value in a list.",
        "Explain what a binary search tree is.",
        "Calculate the area of a circle with radius 5.",
        "What is the capital of France?",
        "How do you reverse a string in Python?",
        "What is the difference between TCP and UDP?",
        "Explain the concept of object-oriented programming.",
        "Write a function to check if a number is prime.",
    ] * (args.queries // 10 + 1)
    test_queries = test_queries[: args.queries]

    tester = ABTester()
    await tester.run_ab_test(
        model_a_role=ModelRole(args.model_a),
        model_b_role=ModelRole(args.model_b),
        queries=test_queries,
        use_judge=not args.no_judge,
    )


if __name__ == "__main__":
    asyncio.run(main())
