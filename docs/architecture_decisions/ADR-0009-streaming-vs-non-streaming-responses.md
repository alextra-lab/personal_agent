# ADR-0009: Streaming vs Non-Streaming LLM Responses

**Status:** Accepted
**Date:** 2025-01-01
**Decision Owner:** Project Owner
**Related Specs:** `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md`, `../architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md`

---

## 1. Context

The Local LLM Client specification (LOCAL_LLM_CLIENT_SPEC_v0.1.md) defines two methods for LLM interaction:

1. **`respond()`**: Returns complete `LLMResponse` after generation finishes
2. **`stream_respond()`**: Yields `LLMStreamEvent` objects as tokens arrive

Both methods are specified, but the decision of **when to use each** affects:

- **User experience**: Progressive token display vs. waiting for complete response
- **Internal agent logic**: Complete responses needed for parsing/validation vs. streaming for display
- **Implementation priority**: Which method to implement first
- **Architecture clarity**: Clear boundaries between user-facing and internal communication

**Current state:**

- `respond()` is implemented and used throughout the orchestrator
- `stream_respond()` is specified but not yet implemented
- No clear guidance on when to use streaming vs. non-streaming

**Key considerations:**

- **Routing decisions** require complete JSON responses for parsing
- **Tool calls** must be fully extracted before execution
- **Internal planning/reasoning** needs complete responses for validation
- **User-facing output** benefits from progressive display (better perceived responsiveness)
- **Error handling** requires complete responses for retry logic

---

## 2. Decision

### 2.1 Use Streaming for User-Facing Output Only

**Use `stream_respond()` when:**

- The output is **directly displayed to the end user** (e.g., final chat response in CLI/UI)
- Progressive token display improves **perceived responsiveness**
- The complete response is **not needed before display begins**

**Use `respond()` (non-streaming) when:**

- **Internal agent communication** (routing decisions, tool calls, planning)
- Response must be **parsed/validated** before proceeding (e.g., JSON routing decisions)
- Response is used as **input to subsequent steps** (e.g., tool arguments, instructions)
- Complete response is required for **error handling or retry logic**

### 2.2 Implementation Boundaries

**Orchestrator layer:**

- **Always uses `respond()`** (non-streaming) for all internal LLM calls
- Routing decisions, tool call generation, planning, and synthesis all require complete responses
- Orchestrator returns complete `OrchestratorResult` to UI layer

**UI layer:**

- **Uses `stream_respond()`** for final user-facing responses when available
- Falls back to `respond()` if streaming not implemented or unavailable
- Displays tokens progressively as they arrive from `stream_respond()`

**Examples:**

- ✅ **Streaming**: Final user-facing response in `Channel.CHAT` → UI calls `stream_respond()` and displays tokens as they arrive
- ✅ **Non-streaming**: Router making routing decision → Orchestrator calls `respond()` to get complete JSON for parsing
- ✅ **Non-streaming**: Tool call generation → Orchestrator calls `respond()` to extract complete tool calls before execution
- ✅ **Non-streaming**: Internal reasoning/planning → Orchestrator calls `respond()` to get complete plan before validation

---

## 3. Decision Drivers

### Why Streaming for User-Facing Only?

1. **Validation requirements**: Internal agent logic (routing, tool calls) requires complete, parseable responses
2. **Error handling**: Retry logic and error recovery need complete responses to determine success/failure
3. **Simplicity**: Orchestrator logic is simpler when it always receives complete responses
4. **UX benefit**: Users perceive faster responses when tokens stream, but internal logic doesn't benefit from streaming

### Why Non-Streaming for Internal Communication?

1. **Parsing needs**: Routing decisions are JSON that must be fully parsed before proceeding
2. **Tool extraction**: Tool calls must be completely extracted before validation and execution
3. **State machine clarity**: Orchestrator state transitions are clearer when responses are complete
4. **Error recovery**: Complete responses enable better error handling and retry logic

### Trade-offs

**Positive:**

- ✅ Clear separation of concerns: orchestrator logic vs. user experience
- ✅ Simpler orchestrator implementation (always complete responses)
- ✅ Better UX for end users (progressive display)
- ✅ Implementation can be deferred: streaming not needed for MVP core functionality

**Negative:**

- ⚠️ Two code paths to maintain (`respond()` and `stream_respond()`)
- ⚠️ UI layer must handle both streaming and non-streaming (with fallback)
- ⚠️ Streaming implementation adds complexity to LLM client

---

## 4. Implementation Plan

### Phase 1: Current State (MVP)

- ✅ `respond()` implemented and used throughout orchestrator
- ✅ All internal agent communication uses non-streaming
- ✅ UI displays complete responses (no streaming yet)

### Phase 2: Streaming Implementation (Post-MVP)

- [ ] Implement `stream_respond()` in `LocalLLMClient`
- [ ] Add streaming support for responses API and chat_completions API
- [ ] Update UI layer to use `stream_respond()` for final responses
- [ ] Add fallback to `respond()` if streaming unavailable
- [ ] Test streaming with various model endpoints

### Phase 3: Optional Enhancements

- [ ] Streaming for reasoning traces (if user wants to see "thinking" in real-time)
- [ ] Streaming for tool call progress (if tools take time to execute)
- [ ] Progressive display of multi-step workflows

---

## 5. Consequences

### Positive

✅ **Clear architectural boundaries**: Internal logic vs. user experience clearly separated
✅ **Simpler orchestrator**: Always works with complete responses, no partial parsing needed
✅ **Better UX**: Users see responses faster with progressive display
✅ **Deferred complexity**: Streaming can be implemented after MVP is stable
✅ **Flexibility**: UI can choose streaming or non-streaming based on context

### Negative / Trade-offs

⚠️ **Two code paths**: Must maintain both `respond()` and `stream_respond()` implementations
⚠️ **UI complexity**: UI layer must handle streaming events and fallback to non-streaming
⚠️ **Testing complexity**: Need to test both streaming and non-streaming paths
⚠️ **Implementation effort**: Streaming adds complexity to LLM client adapters

### Risks

- **Streaming bugs**: Partial responses, connection drops during streaming
- **Fallback complexity**: UI must gracefully handle streaming failures
- **Performance**: Streaming may have different latency characteristics than non-streaming

---

## 6. Open Questions & Future Work

- **Streaming for reasoning traces**: Should users see "thinking" in real-time? (Future enhancement)
- **Streaming for tool calls**: Should tool execution progress be streamed? (Future enhancement)
- **Streaming performance**: Does streaming actually improve perceived latency? (Measure in experiments)
- **Error handling in streams**: How to handle errors mid-stream? (Implementation detail)

---

## 7. References

- `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md` — LLM client specification (Section 2.2.1)
- `../architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md` — Orchestrator specification (Section 3.1)
- `../plans/IMPLEMENTATION_ROADMAP.md` — Implementation roadmap (Day 8-9)

---

## 8. Acceptance Criteria

This ADR is accepted when:

1. ✅ Decision documented in ADR-0009
2. ✅ Spec files updated to reference this ADR
3. ✅ Implementation roadmap updated with streaming implementation plan
4. ✅ Current implementation uses `respond()` for all internal communication
5. ✅ Future `stream_respond()` implementation will be used only in UI layer

---

**Next steps**: Implement `stream_respond()` in Phase 2, update UI layer to use streaming for final responses.
