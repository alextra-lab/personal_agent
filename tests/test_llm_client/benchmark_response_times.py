"""Benchmark response times for all 3 models.

Run with: python tests/test_llm_client/benchmark_response_times.py
"""

import asyncio
import time
from statistics import mean, median
from typing import Any

from personal_agent.config import load_model_config
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext


async def benchmark_model(role: ModelRole, model_id: str, num_runs: int = 3) -> dict[str, Any]:
    """Benchmark a single model."""
    client = LocalLLMClient()
    latencies: list[float] = []
    token_stats: list[dict[str, Any]] = []

    print(f"\n🔄 Benchmarking {role.value} model ({model_id})...")
    print(f"   Running {num_runs} requests...")

    for i in range(num_runs):
        trace_ctx = TraceContext.new_trace()
        start_time = time.time()

        try:
            response = await client.respond(
                role=role,
                messages=[{"role": "user", "content": "Say 'OK' and nothing else."}],
                trace_ctx=trace_ctx,
            )

            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            latencies.append(elapsed)

            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
            total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

            # Extract reasoning tokens if available
            reasoning_tokens = 0
            output_details = usage.get("output_tokens_details", {})
            if isinstance(output_details, dict):
                reasoning_tokens = output_details.get("reasoning_tokens", 0)

            # Calculate tokens per second
            tokens_per_sec = (output_tokens / elapsed * 1000) if elapsed > 0 else 0

            token_stats.append(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "total_tokens": total_tokens,
                    "tokens_per_sec": tokens_per_sec,
                }
            )

            reasoning_info = f", {reasoning_tokens} reasoning" if reasoning_tokens > 0 else ""
            print(
                f"   Run {i + 1}: {elapsed:.0f}ms | "
                f"{output_tokens} output tokens{reasoning_info} | "
                f"{tokens_per_sec:.1f} tok/s"
            )

        except Exception as e:
            print(f"   Run {i + 1}: ERROR - {e}")

    if latencies and token_stats:
        # Aggregate token stats
        avg_input = mean([s["input_tokens"] for s in token_stats])
        avg_output = mean([s["output_tokens"] for s in token_stats])
        avg_reasoning = mean([s["reasoning_tokens"] for s in token_stats])
        avg_total = mean([s["total_tokens"] for s in token_stats])
        avg_tokens_per_sec = mean([s["tokens_per_sec"] for s in token_stats])

        # Calculate stats excluding first run (warmup)
        warmup_latency = latencies[0] if len(latencies) > 1 else None
        warmup_tokens_per_sec = token_stats[0]["tokens_per_sec"] if len(token_stats) > 1 else None

        # Stats after warmup (excluding first run)
        post_warmup_latencies = latencies[1:] if len(latencies) > 1 else latencies
        post_warmup_tokens = token_stats[1:] if len(token_stats) > 1 else token_stats

        post_warmup_mean_latency = (
            mean(post_warmup_latencies) if post_warmup_latencies else mean(latencies)
        )
        post_warmup_mean_tokens_per_sec = (
            mean([s["tokens_per_sec"] for s in post_warmup_tokens])
            if post_warmup_tokens
            else avg_tokens_per_sec
        )

        return {
            "min": min(latencies),
            "max": max(latencies),
            "mean": mean(latencies),
            "median": median(latencies),
            "runs": len(latencies),
            "warmup_latency": warmup_latency,
            "post_warmup_mean": post_warmup_mean_latency,
            "warmup_tokens_per_sec": warmup_tokens_per_sec,
            "post_warmup_tokens_per_sec": post_warmup_mean_tokens_per_sec,
            "avg_input_tokens": avg_input,
            "avg_output_tokens": avg_output,
            "avg_reasoning_tokens": avg_reasoning,
            "avg_total_tokens": avg_total,
            "avg_tokens_per_sec": avg_tokens_per_sec,
        }
    return {}


async def main() -> None:
    """Benchmark all 3 models."""
    # Load model config from models.yaml
    try:
        config = load_model_config()
        model_configs = config.models

        # Extract model IDs from config
        models = []
        for role in [ModelRole.ROUTER, ModelRole.REASONING, ModelRole.CODING]:
            role_config = model_configs.get(role.value)
            model_id = role_config.id if role_config else None
            if model_id:
                models.append((role, model_id))
            else:
                print(f"⚠ Warning: No model ID found for {role.value} in models.yaml")
    except Exception as e:
        print(f"⚠ Error loading models.yaml: {e}")
        print("   Falling back to hardcoded model IDs")
        # Fallback to hardcoded values
        models = [
            (ModelRole.ROUTER, "qwen/qwen3-4b-2507"),
            (ModelRole.REASONING, "deepseek-r1-distill-qwen-14b"),
            (ModelRole.CODING, "mistralai/devstral-small-2-2512"),
        ]

    print("=" * 70)
    print("Response Time Benchmark - All 3 Models")
    print("=" * 70)
    print(f"Models loaded from config: {', '.join([f'{r.value}={m}' for r, m in models])}")

    results: dict[str, dict[str, float]] = {}

    for role, model_id in models:
        stats = await benchmark_model(role, model_id, num_runs=3)
        if stats:
            results[role.value] = stats

    # Summary - Response Times
    print("\n" + "=" * 100)
    print("SUMMARY - Response Times (milliseconds)")
    print("=" * 100)
    print(f"{'Model':<15} {'Min':<10} {'Max':<10} {'Mean':<10} {'Median':<10} {'Runs':<6}")
    print("-" * 100)

    for role_name, stats in results.items():
        print(
            f"{role_name:<15} "
            f"{stats['min']:<10.0f} "
            f"{stats['max']:<10.0f} "
            f"{stats['mean']:<10.0f} "
            f"{stats['median']:<10.0f} "
            f"{stats['runs']:<6.0f}"
        )

    # Summary - Token Generation Stats
    print("\n" + "=" * 100)
    print("SUMMARY - Token Generation Statistics")
    print("=" * 100)
    print(
        f"{'Model':<15} "
        f"{'Input':<8} "
        f"{'Output':<8} "
        f"{'Reasoning':<10} "
        f"{'Total':<8} "
        f"{'Tok/s (all)':<12} "
        f"{'Tok/s (warm)':<12} "
        f"{'Efficiency':<12}"
    )
    print("-" * 100)

    for role_name, stats in results.items():
        reasoning_pct = (
            (stats["avg_reasoning_tokens"] / stats["avg_output_tokens"] * 100)
            if stats["avg_output_tokens"] > 0
            else 0
        )
        # Use post-warmup speed for efficiency rating
        speed_for_rating = stats.get("post_warmup_tokens_per_sec", stats["avg_tokens_per_sec"])
        efficiency = (
            "High" if speed_for_rating > 50 else "Medium" if speed_for_rating > 20 else "Low"
        )

        warmup_info = ""
        if stats.get("warmup_latency") and stats["warmup_latency"] > stats["post_warmup_mean"] * 2:
            warmup_info = f" (warmup: {stats['warmup_latency']:.0f}ms)"

        print(
            f"{role_name:<15} "
            f"{stats['avg_input_tokens']:<8.0f} "
            f"{stats['avg_output_tokens']:<8.0f} "
            f"{stats['avg_reasoning_tokens']:<10.0f} "
            f"{stats['avg_total_tokens']:<8.0f} "
            f"{stats['avg_tokens_per_sec']:<12.1f} "
            f"{stats.get('post_warmup_tokens_per_sec', stats['avg_tokens_per_sec']):<12.1f} "
            f"{efficiency:<12}"
        )
        if warmup_info:
            print(f"   └─ Warmup detected{warmup_info}")
        if reasoning_pct > 0:
            print(f"   └─ {reasoning_pct:.1f}% of output tokens are reasoning tokens")

    print("=" * 100)

    # Find fastest and slowest (using post-warmup stats if available)
    if results:
        # Use post-warmup mean for comparison if available, otherwise use regular mean
        fastest = min(results.items(), key=lambda x: x[1].get("post_warmup_mean", x[1]["mean"]))
        slowest = max(results.items(), key=lambda x: x[1].get("post_warmup_mean", x[1]["mean"]))

        fastest_latency = fastest[1].get("post_warmup_mean", fastest[1]["mean"])
        fastest_speed = fastest[1].get(
            "post_warmup_tokens_per_sec", fastest[1]["avg_tokens_per_sec"]
        )
        slowest_latency = slowest[1].get("post_warmup_mean", slowest[1]["mean"])
        slowest_speed = slowest[1].get(
            "post_warmup_tokens_per_sec", slowest[1]["avg_tokens_per_sec"]
        )

        print(
            f"\n⚡ Fastest (after warmup): {fastest[0]} ({fastest_latency:.0f}ms, {fastest_speed:.1f} tok/s)"
        )
        print(
            f"🐌 Slowest (after warmup): {slowest[0]} ({slowest_latency:.0f}ms, {slowest_speed:.1f} tok/s)"
        )
        print(f"📊 Speed ratio: {slowest_latency / fastest_latency:.2f}x")

        # Analysis
        print("\n" + "=" * 100)
        print("ANALYSIS")
        print("=" * 100)
        for role_name, stats in results.items():
            if stats["avg_reasoning_tokens"] > 0:
                print(
                    f"{role_name}: Generates {stats['avg_reasoning_tokens']:.0f} reasoning tokens "
                    f"({stats['avg_reasoning_tokens'] / stats['avg_output_tokens'] * 100:.1f}% of output), "
                    f"totaling {stats['avg_output_tokens']:.0f} output tokens. "
                    f"Generation speed: {stats['avg_tokens_per_sec']:.1f} tok/s. "
                    f"Higher latency ({stats['mean']:.0f}ms) is due to generating ~{stats['avg_output_tokens'] / 2:.0f}x more tokens than others."
                )
            else:
                print(
                    f"{role_name}: Generates {stats['avg_output_tokens']:.0f} output tokens "
                    f"at {stats['avg_tokens_per_sec']:.1f} tokens/sec. "
                    f"Response time: {stats['mean']:.0f}ms."
                )


if __name__ == "__main__":
    asyncio.run(main())
