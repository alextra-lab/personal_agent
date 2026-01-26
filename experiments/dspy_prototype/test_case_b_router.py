"""Test Case B: Router Decision Logic.

Compares manual prompt approach vs DSPy signature for router decision making.

Metrics:
- Routing accuracy
- Code clarity
- Latency
- Debuggability
"""

import json
import time
from typing import Literal

import dspy

from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.orchestrator.types import RoutingDecision, RoutingResult
from personal_agent.telemetry.trace import TraceContext

from .setup_dspy import configure_dspy

# Sample test queries for routing
TEST_QUERIES = [
    ("Hello", "simple"),  # Should HANDLE
    ("What is Python?", "complex"),  # Should DELEGATE to STANDARD or REASONING
    ("Debug this code: def foo(): return 1/0", "code"),  # Should DELEGATE to CODING
    ("Think carefully about quantum mechanics", "reasoning"),  # Should DELEGATE to REASONING
    ("List files in /tmp", "tool"),  # Should DELEGATE to STANDARD (tool use)
]


# ============================================================================
# Manual Approach (Current Implementation)
# ============================================================================

ROUTER_PROMPT = """You are an intelligent task classifier for a personal AI agent with multiple specialized models.

**Your Models:**
- ROUTER (you): Fast 4B model, <1s response, 8K context
  → Use for: greetings, simple facts, basic Q&A
- STANDARD: Fast/normal model, moderate latency, can use tools
  → Use for: most questions, tool orchestration, straightforward analysis (avoid "thinking aloud")
- REASONING: Deep reasoning model, higher latency/cost
  → Use for: explicit deep thought, multi-step proofs/derivations, careful reasoning, research synthesis
- CODING: Devstral Small 2 model, 5-15s response, 32K context
  → Use for: code generation, debugging, software engineering tasks

**Decision Framework:**

1. **Check for code-specific keywords** → If yes, use CODING
   - Keywords: function, class, debug, implement, refactor, code, programming
   - IMPORTANT: Requests to use tools or inspect the filesystem (e.g., "list files", "read file", "check disk usage")
     are NOT coding tasks. Delegate those to STANDARD so the agent can use tools and then respond.

2. **If the user explicitly asks for deep thinking** → DELEGATE to REASONING
   - Signals: "think", "reason", "deeply", "carefully", "step-by-step reasoning", "prove", "derive",
     "chain-of-thought", "rigorously", "philosophical", "research", "strong argument"

3. **Default delegation**:
   - Most non-coding questions → DELEGATE to STANDARD (fast, tool-capable)

4. **If uncertain** (confidence <0.7) → DELEGATE to STANDARD

**IMPORTANT**: The router should ONLY handle extremely simple queries like "Hello" or "Hi". Any question that requires explanation, definition, or formatted output should be delegated to REASONING.

**Output JSON:**

**If HANDLE (router answers directly):**
{{
  "routing_decision": "HANDLE",
  "confidence": 0.0-1.0,
  "reasoning_depth": 1-10,
  "reason": "one sentence explanation",
  "response": "Your actual answer to the user's question here"
}}

**If DELEGATE (delegate to another model):**
{{
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD|REASONING|CODING",
  "confidence": 0.0-1.0,
  "reasoning_depth": 1-10,
  "reason": "one sentence explanation"
}}

**CRITICAL**: When routing_decision is "HANDLE", you MUST include a "response" field with your actual answer to the user's question. The "response" field should be a complete, helpful answer, not just the routing decision.
"""


async def manual_routing_decision(query: str) -> RoutingResult:
    """Manual routing decision using current approach.

    Returns:
        RoutingResult dictionary.
    """
    prompt = f"{ROUTER_PROMPT}\n\n**User Query**: {query}\n\nRespond with JSON:"

    llm_client = LocalLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    response = await llm_client.respond(
        role=ModelRole.ROUTER,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
        trace_ctx=TraceContext.new_trace(),
    )

    # Parse response
    content = response.get("content", "")

    # Extract JSON from markdown code blocks if present
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        # Fallback to STANDARD delegation
        return {
            "decision": RoutingDecision.DELEGATE,
            "target_model": ModelRole.STANDARD,
            "confidence": 0.5,
            "reasoning_depth": 5,
            "reason": f"Parse error: {e}, defaulting to STANDARD",
        }

    # Convert to RoutingResult
    result: RoutingResult = {
        "decision": RoutingDecision(data["routing_decision"]),
        "confidence": float(data.get("confidence", 0.5)),
        "reasoning_depth": int(data.get("reasoning_depth", 5)),
        "reason": data.get("reason", ""),
        "target_model": None,
        "response": None,
    }

    if "target_model" in data and data["target_model"]:
        try:
            result["target_model"] = ModelRole.from_str(data["target_model"])
        except (ValueError, AttributeError):
            result["target_model"] = ModelRole.STANDARD

    if "response" in data:
        result["response"] = data["response"]

    return result


# ============================================================================
# DSPy Approach
# ============================================================================


class RouteQuery(dspy.Signature):
    """Analyze query and decide which model to use for handling it.

    Decision Framework:
    1. Code keywords (function, class, debug, implement, refactor, code, programming) → DELEGATE to CODING
       IMPORTANT: Tool requests (list files, read file, check disk) are NOT coding → DELEGATE to STANDARD
    2. Explicit deep thinking requests ("think", "reason", "deeply", "carefully", "step-by-step") → DELEGATE to REASONING
    3. Most non-coding questions → DELEGATE to STANDARD (fast, tool-capable)
    4. Only extremely simple queries like "Hello" or "Hi" → HANDLE directly

    IMPORTANT: Any question requiring explanation, definition, or formatted output should be DELEGATE to REASONING or STANDARD.
    """

    query: str = dspy.InputField(desc="User's query to analyze and route")

    routing_decision: Literal["HANDLE", "DELEGATE"] = dspy.OutputField(
        desc="HANDLE only for extremely simple queries (greetings). DELEGATE for anything requiring explanation, code, tools, or reasoning."
    )
    target_model: str = dspy.OutputField(
        desc="If DELEGATE: STANDARD (most questions, tool use), REASONING (deep thinking requests), or CODING (code keywords). If HANDLE: empty string."
    )
    confidence: float = dspy.OutputField(desc="Confidence score 0.0-1.0")
    reasoning_depth: int = dspy.OutputField(
        desc="Complexity score 1-10 (1-3 simple, 4-6 moderate, 7-10 complex)"
    )
    reason: str = dspy.OutputField(desc="Brief one-sentence explanation of routing decision")
    response: str = dspy.OutputField(
        desc="Direct response text if HANDLE (complete answer), empty string if DELEGATE"
    )


def dspy_routing_decision(query: str) -> RoutingResult:
    """DSPy routing decision using signature.

    Returns:
        RoutingResult dictionary.
    """
    # Create predictor (not ChainOfThought - routing should be fast)
    router = dspy.Predict(RouteQuery)

    # Generate routing decision
    result = router(query=query)

    # Convert to RoutingResult
    routing_result: RoutingResult = {
        "decision": RoutingDecision(result.routing_decision),
        "confidence": float(result.confidence),
        "reasoning_depth": int(result.reasoning_depth),
        "reason": result.reason,
        "target_model": None,
        "response": None,
    }

    # Parse target_model
    if result.routing_decision == "DELEGATE" and result.target_model:
        try:
            routing_result["target_model"] = ModelRole.from_str(result.target_model)
        except (ValueError, AttributeError):
            routing_result["target_model"] = ModelRole.STANDARD

    if result.routing_decision == "HANDLE" and result.response:
        routing_result["response"] = result.response

    return routing_result


# ============================================================================
# Comparison Test
# ============================================================================


def evaluate_routing_accuracy(result: RoutingResult, expected_category: str) -> bool:
    """Evaluate if routing decision matches expected category.

    Args:
        result: RoutingResult from router
        expected_category: Expected category ("simple", "complex", "code", "reasoning", "tool")

    Returns:
        True if routing decision is appropriate for category.
    """
    decision = result["decision"]
    target = result.get("target_model")

    if expected_category == "simple":
        # Simple queries should be HANDLE or DELEGATE to STANDARD
        return decision == RoutingDecision.HANDLE or target == ModelRole.STANDARD
    elif expected_category == "code":
        # Code queries should DELEGATE to CODING
        return decision == RoutingDecision.DELEGATE and target == ModelRole.CODING
    elif expected_category == "reasoning":
        # Reasoning queries should DELEGATE to REASONING
        return decision == RoutingDecision.DELEGATE and target == ModelRole.REASONING
    elif expected_category in ("complex", "tool"):
        # Complex/tool queries should DELEGATE (usually to STANDARD)
        return decision == RoutingDecision.DELEGATE
    else:
        return True  # Unknown category, accept any decision


async def run_comparison() -> dict:
    """Run comparison between manual and DSPy routing approaches.

    Returns:
        Dictionary with comparison results.
    """
    print("=" * 70)
    print("Test Case B: Router Decision Logic Comparison")
    print("=" * 70)
    print()

    # Configure DSPy with router model
    config = load_model_config()
    router_model = config.models.get("router")
    if router_model:
        model_name = router_model.id
        print(f"📋 Using 'router' model: {model_name}")
    else:
        model_name = "qwen/qwen3-1.7b"  # Fallback
        print(f"⚠️  Using fallback model: {model_name}")

    configure_dspy(model_name=model_name)
    print()

    # Test manual approach
    print("Testing Manual Approach...")
    manual_results = []
    manual_times = []
    manual_correct = 0

    for query, category in TEST_QUERIES:
        try:
            start_time = time.time()
            result = await manual_routing_decision(query)
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            manual_results.append((query, result, category))
            manual_times.append(elapsed)

            is_correct = evaluate_routing_accuracy(result, category)
            if is_correct:
                manual_correct += 1

            status = "✅" if is_correct else "❌"
            print(
                f"  {status} '{query[:40]}...': {result['decision'].value}, target={result.get('target_model')}, {elapsed:.0f}ms"
            )
        except Exception as e:
            print(f"  ❌ '{query[:40]}...': Failed - {e}")

    print()

    # Test DSPy approach
    print("Testing DSPy Approach...")
    dspy_results = []
    dspy_times = []
    dspy_correct = 0

    for query, category in TEST_QUERIES:
        try:
            start_time = time.time()
            result = dspy_routing_decision(query)
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            dspy_results.append((query, result, category))
            dspy_times.append(elapsed)

            is_correct = evaluate_routing_accuracy(result, category)
            if is_correct:
                dspy_correct += 1

            status = "✅" if is_correct else "❌"
            print(
                f"  {status} '{query[:40]}...': {result['decision'].value}, target={result.get('target_model')}, {elapsed:.0f}ms"
            )
        except Exception as e:
            print(f"  ❌ '{query[:40]}...': Failed - {e}")

    print()
    print("=" * 70)
    print("Results")
    print("=" * 70)
    print()

    # Calculate metrics
    manual_avg_time = sum(manual_times) / len(manual_times) if manual_times else 0
    dspy_avg_time = sum(dspy_times) / len(dspy_times) if dspy_times else 0
    overhead = dspy_avg_time - manual_avg_time

    results = {
        "manual": {
            "correct_count": manual_correct,
            "total_count": len(TEST_QUERIES),
            "accuracy": manual_correct / len(TEST_QUERIES) if TEST_QUERIES else 0,
            "avg_latency_ms": manual_avg_time,
        },
        "dspy": {
            "correct_count": dspy_correct,
            "total_count": len(TEST_QUERIES),
            "accuracy": dspy_correct / len(TEST_QUERIES) if TEST_QUERIES else 0,
            "avg_latency_ms": dspy_avg_time,
        },
        "comparison": {
            "latency_overhead_ms": overhead,
            "latency_overhead_percent": (overhead / manual_avg_time * 100)
            if manual_avg_time > 0
            else 0,
        },
    }

    print("Manual Approach:")
    print(
        f"  Accuracy: {results['manual']['correct_count']}/{results['manual']['total_count']} ({results['manual']['accuracy'] * 100:.1f}%)"
    )
    print(f"  Avg Latency: {results['manual']['avg_latency_ms']:.0f}ms")
    print()
    print("DSPy Approach:")
    print(
        f"  Accuracy: {results['dspy']['correct_count']}/{results['dspy']['total_count']} ({results['dspy']['accuracy'] * 100:.1f}%)"
    )
    print(f"  Avg Latency: {results['dspy']['avg_latency_ms']:.0f}ms")
    print()
    print("Comparison:")
    print(
        f"  Latency Overhead: {results['comparison']['latency_overhead_ms']:.0f}ms ({results['comparison']['latency_overhead_percent']:+.1f}%)"
    )
    print()

    return results


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_comparison())
