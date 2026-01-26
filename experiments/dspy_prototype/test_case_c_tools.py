"""Test Case C: Tool-Using Agent.

Compares manual orchestrator approach vs DSPy ReAct for tool-using agents.

Metrics:
- Code complexity
- Control (governance/telemetry integration feasibility)
- Tool selection accuracy
- Debugging capability
"""

import asyncio
import json
import time

import dspy

from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import ToolExecutionLayer, get_default_registry

from .setup_dspy import configure_dspy

# Test queries that require tools
TEST_QUERIES = [
    "What is the current CPU usage?",
    "Read the file at /tmp/test.txt and tell me what's in it",
]

# Note: This is a simplified comparison focusing on code complexity and control feasibility.
# Full integration would require governance/telemetry adapters for DSPy ReAct.


# ============================================================================
# Manual Approach (Simplified - shows orchestrator pattern)
# ============================================================================


async def manual_tool_agent(query: str) -> str:
    """Manual orchestrator approach (simplified pattern).

    This shows the pattern used in step_tool_execution + step_llm_call.
    """
    tool_layer = ToolExecutionLayer(get_default_registry())
    trace_ctx = TraceContext.new_trace()

    llm_client = LocalLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    messages = [{"role": "user", "content": query}]
    max_iterations = 3

    for _iteration in range(max_iterations):
        # Call LLM with tools
        from personal_agent.governance.models import Mode

        tools = tool_layer.registry.get_tool_definitions_for_llm(mode=Mode.NORMAL)
        response = await llm_client.respond(
            role=ModelRole.STANDARD,
            messages=messages,
            tools=tools,
            trace_ctx=trace_ctx,
        )

        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        # If no tool calls, return answer
        if not tool_calls:
            return content

        # Execute tools
        tool_results = []
        for tool_call in tool_calls:
            tool_name = tool_call.get("function", {}).get("name", "")
            arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))

            result = tool_layer.execute_tool(tool_name, arguments, trace_ctx)

            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "name": tool_name,
                    "content": json.dumps(result.output)
                    if result.success
                    else json.dumps({"error": result.error}),
                }
            )

        # Append tool results to messages
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        messages.extend(tool_results)

    return content or "Tool loop limit reached"


# ============================================================================
# DSPy ReAct Approach (Prototype - shows pattern)
# ============================================================================


def read_file_tool(path: str) -> str:
    """Read file contents (DSPy tool adapter)."""
    tool_layer = ToolExecutionLayer(get_default_registry())
    trace_ctx = TraceContext.new_trace()

    result = tool_layer.execute_tool("read_file", {"path": path}, trace_ctx)

    if result.success:
        output = result.output
        if isinstance(output, dict) and "content" in output:
            return output["content"]
        return str(output)
    return f"Error: {result.error}"


def system_metrics_tool() -> str:
    """Get system metrics (DSPy tool adapter)."""
    tool_layer = ToolExecutionLayer(get_default_registry())
    trace_ctx = TraceContext.new_trace()

    result = tool_layer.execute_tool("system_metrics_snapshot", {}, trace_ctx)

    if result.success:
        output = result.output
        # Format metrics as string for DSPy
        if isinstance(output, dict):
            metrics = []
            for key, value in output.items():
                metrics.append(f"{key}: {value}")
            return "\n".join(metrics)
        return str(output)
    return f"Error: {result.error}"


def dspy_react_agent(query: str) -> str:
    """DSPy ReAct approach for tool-using agent.

    Note: This is a prototype showing the pattern. Full integration would require:
    - Governance checks (currently bypassed in tool adapters)
    - Telemetry integration (would need DSPy callbacks)
    - Error handling and retries
    """
    # Create ReAct agent with tools
    agent = dspy.ReAct(
        "question -> answer: str",
        tools=[read_file_tool, system_metrics_tool],
        max_iters=3,
    )

    # Generate answer
    result = agent(question=query)
    return result.answer


# ============================================================================
# Comparison Test
# ============================================================================


async def run_comparison() -> dict:
    """Run comparison between manual and DSPy tool-using approaches.

    Returns:
        Dictionary with comparison results.
    """
    print("=" * 70)
    print("Test Case C: Tool-Using Agent Comparison")
    print("=" * 70)
    print()

    # Configure DSPy with standard model
    config = load_model_config()
    standard_model = config.models.get("standard")
    if standard_model:
        model_name = standard_model.id
        print(f"📋 Using 'standard' model: {model_name}")
    else:
        model_name = "qwen/qwen3-4b-2507"
        print(f"⚠️  Using fallback model: {model_name}")

    configure_dspy(model_name=model_name)
    print()

    # Test manual approach
    print("Testing Manual Approach...")
    manual_results = []
    manual_times = []
    manual_success = 0

    for query in TEST_QUERIES:
        try:
            start_time = time.time()
            result = await manual_tool_agent(query)
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            manual_results.append((query, result))
            manual_times.append(elapsed)

            # Simple success check: has content and not an error
            if result and "Error:" not in result:
                manual_success += 1

            print(f"  ✅ '{query[:50]}...': {len(result)} chars, {elapsed:.0f}ms")
        except Exception as e:
            print(f"  ❌ '{query[:50]}...': Failed - {e}")

    print()

    # Test DSPy approach
    print("Testing DSPy ReAct Approach...")
    dspy_results = []
    dspy_times = []
    dspy_success = 0

    for query in TEST_QUERIES:
        try:
            start_time = time.time()
            result = dspy_react_agent(query)
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            dspy_results.append((query, result))
            dspy_times.append(elapsed)

            # Simple success check: has content and not an error
            if result and "Error:" not in result:
                dspy_success += 1

            print(f"  ✅ '{query[:50]}...': {len(result)} chars, {elapsed:.0f}ms")
        except Exception as e:
            print(f"  ❌ '{query[:50]}...': Failed - {e}")

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
            "success_count": manual_success,
            "total_count": len(TEST_QUERIES),
            "avg_latency_ms": manual_avg_time,
        },
        "dspy": {
            "success_count": dspy_success,
            "total_count": len(TEST_QUERIES),
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
    print(f"  Success: {results['manual']['success_count']}/{results['manual']['total_count']}")
    print(f"  Avg Latency: {results['manual']['avg_latency_ms']:.0f}ms")
    print()
    print("DSPy ReAct Approach:")
    print(f"  Success: {results['dspy']['success_count']}/{results['dspy']['total_count']}")
    print(f"  Avg Latency: {results['dspy']['avg_latency_ms']:.0f}ms")
    print()
    print("Comparison:")
    print(
        f"  Latency Overhead: {results['comparison']['latency_overhead_ms']:.0f}ms ({results['comparison']['latency_overhead_percent']:+.1f}%)"
    )
    print()
    print("=" * 70)
    print("Control & Integration Assessment")
    print("=" * 70)
    print()
    print("Manual Approach:")
    print("  ✅ Governance: Integrated via ToolExecutionLayer")
    print("  ✅ Telemetry: Full trace context support")
    print("  ✅ Error handling: Comprehensive (per-tool errors, retries)")
    print("  ✅ State management: Explicit (messages, steps, iterations)")
    print()
    print("DSPy ReAct Approach:")
    print("  ⚠️  Governance: Requires adapter wrapper (bypassed in prototype)")
    print("  ⚠️  Telemetry: Requires DSPy callbacks (not shown in prototype)")
    print("  ⚠️  Error handling: Limited (DSPy handles retries internally)")
    print("  ⚠️  State management: Implicit (DSPy manages internally)")
    print()
    print("Assessment:")
    print("  - DSPy ReAct is simpler for basic tool use (declarative pattern)")
    print("  - Manual approach provides more control (governance, telemetry, state)")
    print("  - Full DSPy integration would require significant adapter code")
    print("  - For production systems requiring governance, manual approach may be better")
    print()

    return results


if __name__ == "__main__":
    asyncio.run(run_comparison())
