"""Test Case A: Captain's Log Reflection Generation.

Compares manual prompt approach vs DSPy ChainOfThought for generating
Captain's Log reflection entries.

Metrics:
- Code complexity (lines of code)
- Parse failure rate
- Latency
- Code clarity/maintainability
"""

import json
import time

import dspy

from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext

from .setup_dspy import configure_dspy

# Sample test data
SAMPLE_USER_MESSAGE = "What is Python?"
SAMPLE_TRACE_ID = "test-trace-001"
SAMPLE_STEPS_COUNT = 3
SAMPLE_FINAL_STATE = "COMPLETED"
SAMPLE_REPLY_LENGTH = 150


# ============================================================================
# Manual Approach (Current Implementation)
# ============================================================================

REFLECTION_PROMPT = """You are a personal AI agent analyzing your own task execution to generate insights and improvement proposals.

## Task Context
- **User Message**: {user_message}
- **Trace ID**: {trace_id}
- **Steps Completed**: {steps_count}
- **Final State**: {final_state}
- **Reply Length**: {reply_length} characters

## Your Task
Analyze this task execution and generate a structured reflection entry with:

1. **Rationale**: What happened? Key observations about the execution.
2. **Supporting Metrics**: Specific metrics that stand out (e.g., "llm_call_duration: 2.3s", "tool_executions: 3")
3. **Proposed Change** (if any): Concrete, actionable improvement suggestion based on evidence
   - What to change
   - Why it would help
   - How to implement it
4. **Impact Assessment**: Expected benefits if the change is implemented

Respond with ONLY valid JSON in this exact format:
{{
  "rationale": "string",
  "proposed_change": {{
    "what": "string",
    "why": "string",
    "how": "string"
  }} | null,
  "supporting_metrics": ["metric1: value1", "metric2: value2"],
  "impact_assessment": "string" | null
}}

Do not include markdown formatting, explanations, or any text outside the JSON object."""


async def manual_reflection_generation(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
) -> dict:
    """Manual reflection generation using current approach.

    Returns:
        Parsed reflection data dictionary.
    """
    prompt = REFLECTION_PROMPT.format(
        user_message=user_message[:200],
        trace_id=trace_id,
        steps_count=steps_count,
        final_state=final_state,
        reply_length=reply_length,
    )

    llm_client = LocalLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    response = await llm_client.respond(
        role=ModelRole.REASONING,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=3000,
        reasoning_effort="medium",
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
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse reflection response: {e}") from e


# ============================================================================
# DSPy Approach
# ============================================================================


class GenerateReflection(dspy.Signature):
    """Generate structured reflection on task execution to propose improvements."""

    user_message: str = dspy.InputField(desc="The user's original message")
    trace_id: str = dspy.InputField(desc="Trace ID for the task execution")
    steps_count: int = dspy.InputField(desc="Number of orchestrator steps executed")
    final_state: str = dspy.InputField(desc="Final task state")
    reply_length: int = dspy.InputField(desc="Length of the agent's reply in characters")

    rationale: str = dspy.OutputField(desc="Analysis of what happened, key observations")
    proposed_change_what: str = dspy.OutputField(
        desc="What to change (empty string if no change proposed)"
    )
    proposed_change_why: str = dspy.OutputField(
        desc="Why it would help (empty string if no change proposed)"
    )
    proposed_change_how: str = dspy.OutputField(
        desc="How to implement it (empty string if no change proposed)"
    )
    supporting_metrics: str = dspy.OutputField(
        desc="Comma-separated list of metrics (e.g., 'metric1: value1, metric2: value2')"
    )
    impact_assessment: str = dspy.OutputField(
        desc="Expected benefits if change implemented (empty string if none)"
    )


def dspy_reflection_generation(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
) -> dict:
    """DSPy reflection generation using ChainOfThought.

    Returns:
        Parsed reflection data dictionary.
    """
    # Create ChainOfThought module
    reflection_generator = dspy.ChainOfThought(GenerateReflection)

    # Generate reflection
    result = reflection_generator(
        user_message=user_message[:200],
        trace_id=trace_id,
        steps_count=steps_count,
        final_state=final_state,
        reply_length=reply_length,
    )

    # Convert DSPy output to dictionary format
    proposed_change = None
    if result.proposed_change_what and result.proposed_change_what.strip():
        proposed_change = {
            "what": result.proposed_change_what,
            "why": result.proposed_change_why or "",
            "how": result.proposed_change_how or "",
        }

    # Parse supporting_metrics from comma-separated string
    metrics_list = []
    if result.supporting_metrics:
        metrics_list = [m.strip() for m in result.supporting_metrics.split(",") if m.strip()]

    impact = (
        result.impact_assessment
        if result.impact_assessment and result.impact_assessment.strip()
        else None
    )

    return {
        "rationale": result.rationale,
        "proposed_change": proposed_change,
        "supporting_metrics": metrics_list,
        "impact_assessment": impact,
    }


# ============================================================================
# Comparison Test
# ============================================================================


async def run_comparison(num_tests: int = 5) -> dict:
    """Run comparison between manual and DSPy approaches.

    Args:
        num_tests: Number of test runs for each approach.

    Returns:
        Dictionary with comparison results.
    """
    print("=" * 70)
    print("Test Case A: Captain's Log Reflection Generation Comparison")
    print("=" * 70)
    print()

    # Configure DSPy with REASONING model (same as manual approach)
    config = load_model_config()
    reasoning_model = config.models.get("reasoning")
    if reasoning_model:
        model_name = reasoning_model.id
        print(f"📋 Using 'reasoning' model for fair comparison: {model_name}")
    else:
        model_name = "qwen/qwen3-8b"  # Fallback
        print(f"⚠️  Using fallback model: {model_name}")

    configure_dspy(model_name=model_name)
    print()

    # Test manual approach
    print("Testing Manual Approach...")
    manual_results = []
    manual_times = []
    manual_failures = 0

    for i in range(num_tests):
        try:
            start_time = time.time()
            result = await manual_reflection_generation(
                SAMPLE_USER_MESSAGE,
                f"{SAMPLE_TRACE_ID}-manual-{i}",
                SAMPLE_STEPS_COUNT,
                SAMPLE_FINAL_STATE,
                SAMPLE_REPLY_LENGTH,
            )
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            manual_results.append(result)
            manual_times.append(elapsed)
            print(f"  Test {i + 1}/{num_tests}: ✅ Success ({elapsed:.0f}ms)")
        except Exception as e:
            manual_failures += 1
            print(f"  Test {i + 1}/{num_tests}: ❌ Failed: {e}")

    print()

    # Test DSPy approach
    print("Testing DSPy Approach...")
    dspy_results = []
    dspy_times = []
    dspy_failures = 0

    for i in range(num_tests):
        try:
            start_time = time.time()
            result = dspy_reflection_generation(
                SAMPLE_USER_MESSAGE,
                f"{SAMPLE_TRACE_ID}-dspy-{i}",
                SAMPLE_STEPS_COUNT,
                SAMPLE_FINAL_STATE,
                SAMPLE_REPLY_LENGTH,
            )
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            dspy_results.append(result)
            dspy_times.append(elapsed)
            print(f"  Test {i + 1}/{num_tests}: ✅ Success ({elapsed:.0f}ms)")
        except Exception as e:
            dspy_failures += 1
            print(f"  Test {i + 1}/{num_tests}: ❌ Failed: {e}")

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
            "success_count": len(manual_results),
            "failure_count": manual_failures,
            "avg_latency_ms": manual_avg_time,
        },
        "dspy": {
            "success_count": len(dspy_results),
            "failure_count": dspy_failures,
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
    print(f"  Success: {results['manual']['success_count']}/{num_tests}")
    print(f"  Failures: {results['manual']['failure_count']}/{num_tests}")
    print(f"  Avg Latency: {results['manual']['avg_latency_ms']:.0f}ms")
    print()
    print("DSPy Approach:")
    print(f"  Success: {results['dspy']['success_count']}/{num_tests}")
    print(f"  Failures: {results['dspy']['failure_count']}/{num_tests}")
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

    asyncio.run(run_comparison(num_tests=5))
