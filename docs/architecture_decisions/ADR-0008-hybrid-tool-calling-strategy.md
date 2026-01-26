# ADR-0008: Hybrid Tool Calling Strategy for Reasoning Models

**Status**: Accepted
**Date**: 2026-01-01
**Deciders**: Architecture Team
**Related**: ADR-0007 (Configuration), Day 18-19 Implementation

## Context

The agent uses multiple model roles (Router, Reasoning, Coding) to handle different types of tasks. While this multi-model architecture provides flexibility and cognitive specialization, it introduces a fundamental challenge: **reasoning models like DeepSeek-R1 excel at complex multi-step reasoning but do not support native OpenAI-style function calling**.

### The Problem

- **Reasoning models** (DeepSeek-R1, R1-Distill) provide exceptional reasoning capabilities through explicit chain-of-thought processes
- These models were trained primarily for research and reasoning tasks, NOT for structured function calling
- They cannot generate the standardized `tool_calls` JSON format that frameworks expect
- However, they CAN express tool needs in natural language or text-based formats

### Requirements

1. **Must support tool execution** - The agent needs to interact with external tools (filesystem, APIs, etc.)
2. **Must leverage reasoning** - Complex tasks require the deep reasoning capabilities of models like DeepSeek-R1
3. **Must be model-agnostic** - Should work with both function-calling and non-function-calling models
4. **Must be maintainable** - Should not require model-specific hacks throughout the codebase

## Decision

We implement a **hybrid tool calling strategy** that supports BOTH native function calling AND text-based tool protocols:

### 1. **Dual Protocol Support**

The `LLMResponse` adapter layer detects and normalizes tool calls from multiple formats:

**Native Function Calling** (OpenAI format):
```json
{
  "output": [
    {
      "type": "function_call",
      "call_id": "call_123",
      "name": "list_directory",
      "arguments": "{\"path\": \"/tmp\"}"
    }
  ]
}
```

**Text-Based Tool Calls** (Reasoning model format):
```text
[TOOL_REQUEST]{"name":"list_directory","arguments":{"path":"/tmp"}}[END_TOOL_REQUEST]
```

### 2. **Text-Based Tool Call Parser**

Created `src/personal_agent/llm_client/tool_call_parser.py` that implements multiple parsing strategies:

- **Strategy 1**: `[TOOL_REQUEST]{...}[END_TOOL_REQUEST]` - Primary format for reasoning models
- **Strategy 2**: `<tool_call>{...}</tool_call>` - XML-style format
- **Strategy 3**: `Tool: tool_name(arg1=value1)` - Function-style format

The parser extracts tool calls from free-form text and normalizes them into the standard `ToolCall` format.

### 3. **Transparent Integration**

The parsing happens automatically in the adapter layer:
1. Check for native `function_call` items in response (preferred)
2. If none found, parse message content for text-based tool calls
3. Normalize both formats into the same `ToolCall` structure
4. Orchestrator sees consistent tool calls regardless of source

### 4. **Stateful Conversation with `/v1/responses` API**

For LM Studio's `/v1/responses` endpoint:
- Track `response_id` from each LLM response
- Pass `previous_response_id` in subsequent requests for stateful conversation
- Format tool results as `function_call_output` items for synthesis
- This enables multi-turn tool-using conversations without manual message history management

## Consequences

### Positive

✅ **Model Flexibility**: Can use reasoning models (DeepSeek-R1) AND function-calling models (Mistral, GPT-4) interchangeably
✅ **Cognitive Advantages**: Leverage superior reasoning capabilities where they matter most
✅ **Graceful Degradation**: If native function calling fails, text parsing provides fallback
✅ **Future-Proof**: As reasoning models improve function calling support, we automatically benefit
✅ **Clean Architecture**: Parsing logic isolated in adapter layer, orchestrator remains model-agnostic

### Negative

⚠️ **Parsing Complexity**: Text-based parsing is inherently less reliable than structured function calls
⚠️ **Model Dependency**: Reasoning models must be prompted to use the expected text format
⚠️ **Debugging Difficulty**: Text parsing failures are harder to diagnose than structured format violations

### Neutral

- **Performance**: Text parsing adds minimal latency (<1ms per response)
- **Token Efficiency**: Text-based formats can be more compact than full JSON schemas
- **Maintenance**: Parser requires updates if new text formats emerge

## Implementation Details

### File Structure

```
src/personal_agent/llm_client/
├── tool_call_parser.py      # NEW: Text-based tool call parsing
├── adapters.py               # MODIFIED: Calls parser for text-based tools
├── types.py                  # MODIFIED: Added response_id field
└── client.py                 # MODIFIED: Added previous_response_id support
```

### Orchestrator Flow

```
1. step_llm_call()
   ├─> LocalLLMClient.respond(previous_response_id=ctx.last_response_id)
   ├─> Adapter detects tool calls (native OR text-based)
   ├─> Store response_id in ctx.last_response_id
   └─> Transition to TOOL_EXECUTION if tool_calls present

2. step_tool_execution()
   ├─> Extract tool_calls from last assistant message
   ├─> Execute each tool via ToolExecutionLayer
   ├─> Append results as "tool" role messages
   └─> Transition back to LLM_CALL for synthesis

3. step_llm_call() [synthesis]
   ├─> Send tool results with previous_response_id
   ├─> Model synthesizes final response
   └─> Transition to SYNTHESIS/COMPLETED
```

### Prompting Strategy

Tool definitions are provided in the `tools` parameter of LLM requests. For reasoning models that generate text-based tool calls, the model learns the format from:
1. Tool descriptions in the prompt
2. Examples in system prompts (future enhancement)
3. Error feedback if parsing fails (retry logic)

## Alternatives Considered

### 1. **Separate Tool-Calling Model**
Use a dedicated function-calling model (e.g., GPT-3.5) for tool orchestration while reasoning model handles planning.

**Rejected because**:
- Adds complexity of multi-model coordination
- Increases latency (two model calls per tool cycle)
- Reasoning model loses direct control over tool execution

### 2. **Constrained Decoding**
Force reasoning models to output valid JSON schemas using constrained decoding (vLLM, Outlines).

**Rejected because**:
- Requires specific inference engine support (not all LM Studio versions support it)
- May interfere with reasoning model's natural generation patterns
- Adds deployment complexity

### 3. **Prompt Engineering Only**
Rely entirely on prompting to get reasoning models to generate parseable tool calls.

**Rejected because**:
- Too brittle - models hallucinate formats
- No fallback if format changes
- Requires model-specific prompt tuning

### 4. **ReAct Pattern with Explicit Thought/Action**
Implement strict ReAct loop where model alternates between "Thought:" and "Action:" markers.

**Partially adopted**: Our text parser supports this format as Strategy 3, but we don't enforce it as the only format.

## Future Enhancements

1. **Model Capability Metadata**: Add `supports_function_calling` flag to model configs
2. **Adaptive Prompting**: Adjust system prompts based on model capabilities
3. **Retry with Format Correction**: If parsing fails, feed back error and ask model to retry
4. **Constrained Decoding Support**: Add optional constrained decoding for guaranteed format compliance
5. **Multi-Model Coordination**: Router delegates planning to reasoning model, tool execution to function-calling model

## References

- [LM Studio Responses API Docs](https://lmstudio.ai/docs/developer/openai-compat/responses)
- [DeepSeek-R1 Model Card](https://github.com/deepseek-ai/DeepSeek-R1)
- [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)
- [LangGraph Multi-Model Patterns](https://docs.langchain.com/oss/python/langgraph/)
- Perplexity Research: "AI Agent Frameworks and Reasoning Model Integration" (2026-01-01)

## Acceptance Criteria

✅ Can execute tools with reasoning models (DeepSeek-R1)
✅ Can execute tools with function-calling models (Mistral, GPT-4)
✅ Orchestrator code is model-agnostic
✅ Text parsing handles multiple formats
✅ Stateful conversation via `previous_response_id`
✅ Tool execution and synthesis work end-to-end

**Test Cases**:
1. **Reasoning Model (DeepSeek-R1)**: "List files in /tmp" → Uses `/v1/chat/completions` → Generates text-based tool call → Executes `list_directory` → Synthesizes response ✅
2. **Function-Calling Model (Qwen3-4B)**: "List files in /tmp" → Uses `/v1/responses` exclusively → Native function call → Executes `list_directory` → Synthesizes response ✅
3. **Environment Variable Expansion**: "What files are in $HOME/Dev?" → Correctly expands `$HOME` → Lists 54 entries ✅

**Status**: ✅ PASSING (2026-01-01)
