# DSPy Prototype Evaluation (E-008)

This directory contains the prototype evaluation comparing DSPy vs manual approaches for:
1. Captain's Log reflection generation
2. Router decision logic
3. Tool-using agent

## Status

**Date Started**: 2026-01-17
**Status**: ⏳ In Progress

## Setup

### Prerequisites

1. LM Studio running on `localhost:1234` (verified ✅)
2. DSPy installed: `uv add dspy` (completed ✅)
3. Models available: qwen3-1.7b, qwen3-4b-2507, qwen3-8b, devstral-small-2-2512 (verified ✅)

### Configuration Challenge

DSPy uses LiteLLM under the hood for OpenAI-compatible endpoints. Configuration requires:
- Correct API base URL format
- Dummy API key ("lm-studio" or empty)
- Model identifier format

**Current Issue**: DSPy configuration with LM Studio endpoint needs refinement. Working on resolving configuration.

## Test Cases

### Test Case A: Captain's Log Reflection

**Location**: `test_case_a_reflection.py`

**Manual Approach**: See `src/personal_agent/captains_log/reflection.py`
- ~100 lines of code
- Manual prompt construction
- JSON parsing with error handling
- Fallback logic

**DSPy Approach**: Planned implementation
- DSPy ChainOfThought signature
- Expected ~50-70 lines
- Automatic parsing
- Comparison: code complexity, parse failures, latency

### Test Case B: Router Decision Logic

**Location**: `test_case_b_router.py`

**Manual Approach**: See `src/personal_agent/orchestrator/prompts.py` and `executor.py`
- Manual prompt with JSON schema
- Custom parsing logic
- Fallback handling

**DSPy Approach**: Planned implementation
- DSPy signature for routing decisions
- Comparison: routing accuracy, code clarity, debuggability

### Test Case C: Tool-Using Agent

**Location**: `test_case_c_tools.py`

**Manual Approach**: See `src/personal_agent/orchestrator/executor.py` (step_tool_execution)
- Manual tool call parsing
- Tool execution loop
- State management

**DSPy Approach**: Planned implementation
- DSPy ReAct module
- Comparison: code complexity, control integration, debugging

## Running Tests

```bash
# Setup DSPy (once)
uv run python -m experiments.dspy_prototype.setup_dspy

# Run Test Case A
uv run python -m experiments.dspy_prototype.test_case_a_reflection

# Run Test Case B
uv run python -m experiments.dspy_prototype.test_case_b_router

# Run Test Case C
uv run python -m experiments.dspy_prototype.test_case_c_tools
```

## Results

Results will be documented in:
- `architecture_decisions/experiments/E-008-dspy-prototype-evaluation.md`
- This README will be updated with findings

## Next Steps

1. ✅ Install DSPy
2. ⏳ Configure DSPy with LM Studio (in progress)
3. ⏳ Implement Test Case A
4. ⏳ Implement Test Case B
5. ⏳ Implement Test Case C
6. ⏳ Run comparisons and document findings
7. ⏳ Make adoption decision (Option A/B/C)
