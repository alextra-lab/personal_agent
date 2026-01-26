"""Manual E2E validation script for router routing logic.

This script tests the router with 20 diverse queries to validate:
1. Simple queries are handled by router
2. Complex queries are delegated to REASONING
3. Code queries are delegated to CODING
4. Routing overhead is <200ms
5. Telemetry shows correct routing decisions

Usage:
    python tests/evaluation/manual_routing_validation.py

Requirements:
    - LM Studio running on localhost:1234
    - All models loaded (router, reasoning, coding)
    - Virtual environment activated

Related:
    - Implementation Plan: docs/plans/router_routing_logic_implementation_plan.md
    - Test Suite: tests/test_orchestrator/test_routing.py
"""

import asyncio
import time
from typing import Any

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


# ============================================================================
# Test Queries
# ============================================================================

TEST_QUERIES = [
    # Simple queries (Router should HANDLE)
    {
        "query": "Hello",
        "expected_decision": "HANDLE",
        "expected_model": "ROUTER",
        "category": "greeting",
    },
    {
        "query": "Hi there",
        "expected_decision": "HANDLE",
        "expected_model": "ROUTER",
        "category": "greeting",
    },
    {
        "query": "What is 2+2?",
        "expected_decision": "HANDLE",
        "expected_model": "ROUTER",
        "category": "simple_math",
    },
    # Moderate queries (Router should DELEGATE to REASONING)
    {
        "query": "What is Python?",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "explanation",
    },
    {
        "query": "Explain how neural networks work",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "explanation",
    },
    {
        "query": "Compare Python and JavaScript",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "comparison",
    },
    {
        "query": "What are the pros and cons of microservices?",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "comparison",
    },
    # Complex queries (Router should DELEGATE to REASONING)
    {
        "query": "Explain the philosophical implications of quantum mechanics",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "deep_analysis",
    },
    {
        "query": "Analyze the economic impact of AI on the job market",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "deep_analysis",
    },
    {
        "query": "What are the ethical considerations of autonomous vehicles?",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "deep_analysis",
    },
    # Code queries (Router should DELEGATE to CODING)
    {
        "query": "Write a Python function to calculate factorial",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",
        "category": "code_generation",
    },
    {
        "query": "Debug this code: def divide(a, b): return a/b",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",
        "category": "code_debugging",
    },
    {
        "query": "Implement a binary search algorithm in Python",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",
        "category": "code_generation",
    },
    {
        "query": "Refactor this function to be more efficient",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",
        "category": "code_refactoring",
    },
    {
        "query": "Write a class for a binary tree in Python",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",
        "category": "code_generation",
    },
    # Edge cases
    {
        "query": "How do I code a neural network?",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",  # Has "code" keyword
        "category": "edge_case",
    },
    {
        "query": "What is the meaning of life?",
        "expected_decision": "DELEGATE",
        "expected_model": "REASONING",
        "category": "edge_case",
    },
    {
        "query": "Tell me a joke",
        "expected_decision": "HANDLE",
        "expected_model": "ROUTER",
        "category": "edge_case",
    },
    {
        "query": "Explain Python decorators with code examples",
        "expected_decision": "DELEGATE",
        "expected_model": "CODING",  # Has "code" keyword
        "category": "edge_case",
    },
    {
        "query": "What's the weather like?",
        "expected_decision": "HANDLE",
        "expected_model": "ROUTER",
        "category": "edge_case",
    },
]


# ============================================================================
# Validation Logic
# ============================================================================


async def run_validation() -> dict[str, Any]:
    """Run manual E2E validation with 20 test queries.

    Returns:
        Validation results with statistics.
    """
    orchestrator = Orchestrator()
    results = []

    print("\n" + "=" * 80)
    print("ROUTER ROUTING LOGIC - MANUAL E2E VALIDATION")
    print("=" * 80 + "\n")

    for i, test_case in enumerate(TEST_QUERIES, 1):
        query = test_case["query"]
        expected_decision = test_case["expected_decision"]
        expected_model = test_case["expected_model"]
        category = test_case["category"]

        print(f"\n[{i}/20] Testing: {category}")
        print(f'Query: "{query}"')
        print(f"Expected: {expected_decision} → {expected_model}")

        start_time = time.time()

        try:
            result = await orchestrator.handle_user_request(
                session_id=f"validation-{i}",
                user_message=query,
                mode=Mode.NORMAL,
                channel=Channel.CHAT,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Extract routing info from steps
            routing_step = None
            final_model = None

            for step in result.get("steps", []):
                if step["type"] == "llm_call":
                    final_model = step["metadata"].get("model_role")
                    if not routing_step:
                        routing_step = step

            # Determine if routing was correct
            correct = final_model == expected_model.lower()

            result_entry = {
                "query": query,
                "category": category,
                "expected_decision": expected_decision,
                "expected_model": expected_model,
                "actual_model": final_model,
                "correct": correct,
                "duration_ms": duration_ms,
                "reply_preview": result["reply"][:100] if result.get("reply") else None,
                "steps_count": len(result.get("steps", [])),
            }

            results.append(result_entry)

            # Print result
            status = "✅ PASS" if correct else "❌ FAIL"
            print(f"Result: {status}")
            print(f"Actual Model: {final_model}")
            print(f"Duration: {duration_ms}ms")
            print(f"Reply: {result['reply'][:100]}...")

        except Exception as e:
            print(f"❌ ERROR: {str(e)}")
            results.append(
                {
                    "query": query,
                    "category": category,
                    "expected_decision": expected_decision,
                    "expected_model": expected_model,
                    "actual_model": None,
                    "correct": False,
                    "duration_ms": None,
                    "error": str(e),
                    "steps_count": 0,
                }
            )

    # Calculate statistics
    total = len(results)
    passed = sum(1 for r in results if r.get("correct", False))
    failed = total - passed
    pass_rate = (passed / total) * 100 if total > 0 else 0

    avg_duration = sum(r["duration_ms"] for r in results if r.get("duration_ms")) / total
    max_duration = max((r["duration_ms"] for r in results if r.get("duration_ms")), default=0)

    # Print summary
    print("\n" + "=" * 80)
    print("VALIDATION SUMMARY")
    print("=" * 80)
    print(f"\nTotal Tests: {total}")
    print(f"Passed: {passed} ({pass_rate:.1f}%)")
    print(f"Failed: {failed}")
    print("\nPerformance:")
    print(f"  Average Duration: {avg_duration:.0f}ms")
    print(f"  Max Duration: {max_duration}ms")

    # Breakdown by category
    print("\nBreakdown by Category:")
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "passed": 0}
        categories[cat]["total"] += 1
        if r.get("correct", False):
            categories[cat]["passed"] += 1

    for cat, stats in sorted(categories.items()):
        cat_pass_rate = (stats["passed"] / stats["total"]) * 100
        print(f"  {cat}: {stats['passed']}/{stats['total']} ({cat_pass_rate:.0f}%)")

    # Check routing overhead
    print("\nRouting Overhead Check:")
    if avg_duration < 200:
        print(f"  ✅ Average routing overhead ({avg_duration:.0f}ms) is <200ms")
    else:
        print(f"  ❌ Average routing overhead ({avg_duration:.0f}ms) exceeds 200ms target")

    print("\n" + "=" * 80 + "\n")

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "avg_duration_ms": avg_duration,
        "max_duration_ms": max_duration,
        "results": results,
        "categories": categories,
    }


# ============================================================================
# Main
# ============================================================================


if __name__ == "__main__":
    print("\n🚀 Starting Router Routing Logic Validation...")
    print("⚠️  Make sure LM Studio is running with all models loaded!\n")

    try:
        validation_results = asyncio.run(run_validation())

        # Exit code based on pass rate
        if validation_results["pass_rate"] >= 80:
            print("✅ Validation PASSED (≥80% pass rate)")
            exit(0)
        else:
            print(
                f"❌ Validation FAILED ({validation_results['pass_rate']:.1f}% pass rate, need ≥80%)"
            )
            exit(1)

    except KeyboardInterrupt:
        print("\n\n⚠️  Validation interrupted by user")
        exit(130)
    except Exception as e:
        print(f"\n\n❌ Validation failed with error: {str(e)}")
        import traceback

        traceback.print_exc()
        exit(1)
