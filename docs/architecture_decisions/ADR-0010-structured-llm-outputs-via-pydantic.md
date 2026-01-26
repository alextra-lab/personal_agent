# ADR-0010: Structured LLM Outputs via Pydantic Models

**Status:** Accepted (Modified)  
**Date Proposed:** 2026-01-14  
**Date Accepted:** 2026-01-17  
**Decision Owner:** Project Owner  
**Related Specs:** `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md`  
**Related ADRs:** ADR-0009 (Streaming), ADR-0003 (Model Stack)  
**Related Experiments:** E-008 (DSPy Prototype Evaluation)

---

## 1. Context

### 1.1 Current State

The agent currently uses **manual JSON parsing** for structured LLM outputs:

```python
# Current approach in reflection.py:
prompt = """Return ONLY valid JSON with these fields:
{
  "rationale": "string",
  "proposed_change": {"what": "string", "why": "string", "how": "string"},
  ...
}
"""
raw_response = llm.respond(prompt)
data = json.loads(raw_response.content)  # Manual parsing
entry = CaptainLogEntry(**data)  # Manual construction
```

**Problems with this approach:**

1. **Brittle prompt engineering**: Schema must be manually described in natural language
2. **No validation until after generation**: LLM may produce invalid JSON/missing fields
3. **Manual error handling**: Must catch `json.JSONDecodeError`, `ValidationError`, etc.
4. **Schema drift**: Pydantic model changes don't automatically update prompts
5. **No retries**: If LLM produces invalid output, manual fallback required
6. **Verbose code**: Repetitive parsing/validation boilerplate

**Current usage locations:**
- `captains_log/reflection.py`: Generating reflection entries (critical use case)
- Potential future use cases: Planning outputs, tool schemas, governance decisions

### 1.2 Modern Alternatives

#### Option A: `instructor` Library
[Instructor](https://github.com/jxnl/instructor) wraps LLM clients to return validated Pydantic models:

```python
import instructor
from openai import AsyncOpenAI

client = instructor.from_openai(AsyncOpenAI())

entry = await client.chat.completions.create(
    model="gpt-4",
    response_model=CaptainLogEntry,  # Pydantic model
    messages=[{"role": "user", "content": prompt}],
    max_retries=3,  # Auto-retry on validation failure
)
# `entry` is a validated CaptainLogEntry instance
```

**Pros:**
- ✅ Automatic schema generation (from Pydantic model)
- ✅ Built-in retries on validation failures
- ✅ Supports partial responses, streaming, async
- ✅ Works with OpenAI, Anthropic, local models (via OpenAI-compatible APIs)
- ✅ Handles complex types (unions, nested models, optional fields)

**Cons:**
- ❌ External dependency (adds ~15KB, depends on `pydantic`, `openai`)
- ❌ Requires OpenAI-compatible API (LM Studio supports this)
- ❌ May not support tool calling + structured outputs simultaneously (known issue)

#### Option B: Native OpenAI Structured Outputs
OpenAI's [Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs) API:

```python
completion = await client.beta.chat.completions.parse(
    model="gpt-4-turbo",
    messages=[...],
    response_format=CaptainLogEntry,  # Pydantic model
)
entry = completion.choices[0].message.parsed
```

**Pros:**
- ✅ Native API support (no wrapper library)
- ✅ Guaranteed schema adherence (model trained for structured outputs)
- ✅ Lower latency (optimized by provider)

**Cons:**
- ❌ OpenAI-only (no local model support yet)
- ❌ **Known issue with tool calling**: Cannot use `response_format` + `tools` simultaneously
- ❌ Requires newer API versions (`beta.chat.completions.parse`)

#### Option C: Custom Implementation
Build our own wrapper around `LocalLLMClient.respond()`:

```python
async def respond_structured(
    self,
    messages: list[dict],
    response_model: Type[BaseModel],
    max_retries: int = 3,
) -> BaseModel:
    schema = response_model.model_json_schema()
    enhanced_prompt = f"{messages[-1]['content']}\n\nReturn JSON: {json.dumps(schema)}"
    
    for attempt in range(max_retries):
        response = await self.respond(enhanced_prompt)
        try:
            return response_model.model_validate_json(response.content)
        except ValidationError as e:
            if attempt == max_retries - 1:
                raise
            # Retry with error feedback
    
    raise ValueError("Max retries exceeded")
```

**Pros:**
- ✅ Full control over implementation
- ✅ No external dependencies
- ✅ Works with any LLM backend
- ✅ Can customize retry logic, error handling

**Cons:**
- ❌ Maintenance burden (we own the code)
- ❌ Less battle-tested than `instructor`
- ❌ Must implement retries, streaming, async ourselves

### 1.3 OpenAI Async Responses Endpoint

OpenAI's [Async Responses API](https://platform.openai.com/docs/api-reference/async-responses) allows:
- Submit request, get `response_id`
- Poll for completion (or webhook callback)
- Useful for long-running tasks (>60s)

**Trade-offs:**
1. **Not needed for most tasks**: Captain's Log reflection takes ~30s, within normal timeout
2. **Complexity**: Requires job tracking, polling, or webhooks
3. **Tool calling issues**: As user noted, there have been problems with async + tool use
4. **Local models don't support it**: LM Studio, Ollama, etc. don't have async response APIs

**Recommendation:** Defer async responses until we have a clear need (e.g., multi-minute reasoning tasks).

---

## 2. Decision

### 2.1 Adopt DSPy for Captain's Log Reflection (Modified Decision)

**Update (2026-01-17)**: After E-008 prototype evaluation, we've modified this ADR to use **DSPy ChainOfThought** specifically for Captain's Log reflection, instead of `instructor` for all structured outputs.

**Rationale**: E-008 Test Case A demonstrated:
- 100% reliability (0 parse failures in 5 tests)
- ~30-40% code reduction
- +21% latency overhead (acceptable: 2.5s absolute increase)
- Cleaner, more maintainable signature-based approach

### 2.2 DSPy for Captain's Log Reflection

We will use **DSPy ChainOfThought** for reflection generation:

1. **Add `dspy` dependency** to `pyproject.toml`
2. **Create DSPy signature** for reflection generation:
   ```python
   class GenerateReflection(dspy.Signature):
       """Generate structured reflection on task execution to propose improvements."""
       user_message: str = dspy.InputField(desc="The user's original message")
       trace_id: str = dspy.InputField(desc="Trace ID for the task execution")
       steps_count: int = dspy.InputField(desc="Number of orchestrator steps executed")
       final_state: str = dspy.InputField(desc="Final task state")
       reply_length: int = dspy.InputField(desc="Length of the agent's reply in characters")
       
       rationale: str = dspy.OutputField(desc="Analysis of what happened, key observations")
       proposed_change_what: str = dspy.OutputField(desc="What to change (empty string if no change proposed)")
       # ... additional fields
   ```
3. **Configure DSPy** with LM Studio and REASONING model
4. **Use DSPy ChainOfThought** in Captain's Log reflection:
   ```python
   reflection_generator = dspy.ChainOfThought(GenerateReflection)
   result = reflection_generator(user_message=msg, trace_id=trace_id, ...)
   ```
5. **Keep manual approach as fallback** for robustness

### 2.3 Why DSPy Instead of `instructor`?

Based on E-008 prototype evaluation (Test Case A):

**DSPy Advantages**:
1. ✅ **Zero parse failures**: 5/5 successful reflections (100% reliability)
2. ✅ **Significant code reduction**: ~30-40% reduction in reflection code
3. ✅ **Cleaner maintainability**: Signature-based approach easier to modify
4. ✅ **Acceptable latency**: +21% overhead (2.5s absolute increase) is acceptable for reflection
5. ✅ **LM Studio compatibility**: Confirmed working with OpenAI-compatible endpoint

**`instructor` Still Valid For**:
- Simple structured outputs (may adopt post-MVP)
- Use cases where DSPy overhead not justified
- Fallback if DSPy integration issues arise

### 2.4 Scope: Captain's Log Only (Selective Adoption)

**DSPy will be used for**:
- ✅ Captain's Log reflection generation (Test Case A validated)

**Manual approach retained for**:
- ❌ Tool execution workflows (Test Case C: +237% latency overhead unacceptable)
- Router decisions (manual approach works well, DSPy optional)
- Other LLM interactions (evaluate case-by-case)

### 2.5 Keep Manual Fallback

If DSPy fails (configuration issues, model incompatibility), fall back to manual JSON parsing:

```python
try:
    # Try DSPy approach
    reflection_generator = dspy.ChainOfThought(GenerateReflection)
    result = reflection_generator(user_message=msg, trace_id=trace_id, ...)
    entry = _convert_dspy_result_to_entry(result)
except Exception as e:
    log.warning("dspy_reflection_failed_fallback_manual", error=str(e))
    # Fallback to manual prompt + JSON parsing
    entry = _generate_reflection_manual(...)
```

---

## 3. Decision Drivers

### 3.1 Why DSPy for Captain's Log? (Updated Decision)

Based on E-008 Test Case A results:

1. **Proven reliability**: 100% success rate (0 parse failures in 5 tests)
2. **Code reduction**: ~30-40% reduction with cleaner, signature-based structure
3. **Acceptable latency**: +21% overhead (2.5s) is reasonable for reflection tasks
4. **Better maintainability**: Signature changes simpler than prompt template modifications
5. **LM Studio compatible**: Works with OpenAI-compatible endpoint via `dspy.LM`
6. **Framework alignment**: ChainOfThought pattern fits reflection use case well

### 3.2 Why Not `instructor`?

While `instructor` is excellent, E-008 showed DSPy has advantages for Captain's Log:

1. **ChainOfThought reasoning**: DSPy's module adds explicit reasoning steps (helpful for reflection quality)
2. **Proven in prototype**: Test Case A validated DSPy for this specific use case
3. **Signature-based approach**: More declarative than `instructor`'s wrapper pattern
4. **Future optimizers**: DSPy's MIPROv2 can improve reflection quality post-MVP

**Note**: `instructor` may still be adopted for simpler structured outputs post-MVP.

### 3.3 Why Not DSPy ReAct for Tools?

Test Case C (E-008) showed DSPy ReAct is NOT suitable:

1. **Latency overhead**: +237% (6.6s) is unacceptable for tool workflows
2. **Control trade-offs**: Governance/telemetry integration requires significant adapter code
3. **Manual orchestrator better**: Provides better control and performance

**Scope**: DSPy adoption limited to Captain's Log reflection only.

### 3.4 Why Selective Adoption?

**Option B (Selective Adoption)** balances benefits and risks:

1. **Use DSPy where proven**: Captain's Log (Test Case A validated)
2. **Keep manual where better**: Tool execution (Test Case C showed DSPy issues)
3. **Low risk**: Fallback to manual approach if DSPy fails
4. **Reversible**: Can replace DSPy with `instructor` later if needed
5. **Flexible**: Evaluate other use cases individually (router, planning)

---

## 4. Consequences

### 4.1 Positive

1. ✅ **Cleaner code**: `CaptainLogEntry` generation reduced from ~20 lines to ~5
2. ✅ **Better reliability**: Auto-retries eliminate manual fallback logic
3. ✅ **Type safety**: Schema changes automatically propagate to prompts
4. ✅ **Easier maintenance**: Schema defined once in Pydantic model
5. ✅ **Extensible**: Can use for planning outputs, routing decisions, governance

### 4.2 Negative

1. ❌ **New dependency**: Adds `instructor` (~15KB + transitive deps)
2. ❌ **Learning curve**: Team must understand `instructor` API
3. ❌ **Abstraction layer**: One more layer between agent and LLM
4. ❌ **Potential tool conflicts**: May not work if we combine with tool calling (needs testing)

### 4.3 Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| `instructor` breaks with LM Studio | Keep manual JSON parsing fallback |
| Tool calling + structured outputs conflict | Use structured outputs only for tool-free calls (reflection, planning) |
| Model doesn't follow JSON schema | Increase `max_retries`, improve prompts |
| Performance regression | Benchmark before/after, optimize if needed |

---

## 5. Implementation Plan

### Phase 1: DSPy Setup (Week 5, Day 28) ✅
- ✅ E-008 prototype evaluation completed
- ✅ DSPy configuration validated with LM Studio
- [ ] Add `dspy` to `pyproject.toml` (if not already present)
- [ ] Document DSPy usage patterns in `captains_log/AGENTS.md`

### Phase 2: Captain's Log Migration (Week 5, Day 31-32)
- [ ] Create `captains_log/reflection_dspy.py` with DSPy ChainOfThought
- [ ] Define `GenerateReflection` signature (from Test Case A)
- [ ] Configure DSPy with REASONING model (`qwen/qwen3-8b`)
- [ ] Refactor `captains_log/reflection.py` to use DSPy implementation
- [ ] Remove manual JSON parsing logic
- [ ] Add fallback to manual approach if DSPy fails
- [ ] Integrate telemetry logging for DSPy module
- [ ] Verify existing tests pass
- [ ] Measure parse failure rate (target: <5%, prototype achieved 0%)

### Phase 3: Future Structured Outputs (Week 6+) - Deferred
- [ ] Evaluate DSPy or `instructor` for other use cases (planning, routing)
- [ ] Consider DSPy optimizers (MIPROv2) for reflection quality improvement
- [ ] Revisit router with DSPy (optional, Test Case B showed 100% accuracy with enhanced signature)

---

## 6. Examples

### Before (Current):
```python
# reflection.py (current)
prompt = f"""Analyze this task execution...

Return ONLY valid JSON with these exact fields:
{{
  "rationale": "string describing what happened",
  "proposed_change": {{"what": "string", "why": "string", "how": "string"}},
  "supporting_metrics": ["metric1:value1", "metric2:value2"],
  "impact_assessment": "string or null"
}}
"""

response = await llm.respond(messages=[{"role": "user", "content": prompt}], role="reasoning")

try:
    content = response.content
    if not content:
        content = response.raw['choices'][0]['message'].get('reasoning_content', '')
    
    reflection_data = json.loads(content)
    entry = CaptainLogEntry(
        title=f"Task: {user_message[:50]}",
        rationale=reflection_data.get("rationale", ""),
        proposed_change=ProposedChange(**reflection_data["proposed_change"]) 
            if reflection_data.get("proposed_change") else None,
        supporting_metrics=reflection_data.get("supporting_metrics", []),
        impact_assessment=reflection_data.get("impact_assessment"),
        # ... more fields
    )
except (json.JSONDecodeError, KeyError, ValidationError) as e:
    log.warning("reflection_parse_failed", error=str(e))
    return _create_basic_reflection_entry(...)
```

### After (With DSPy ChainOfThought):
```python
# reflection_dspy.py (with DSPy)
import dspy
from personal_agent.config import settings

class GenerateReflection(dspy.Signature):
    """Generate structured reflection on task execution to propose improvements."""
    user_message: str = dspy.InputField(desc="The user's original message")
    trace_id: str = dspy.InputField(desc="Trace ID for the task execution")
    steps_count: int = dspy.InputField(desc="Number of orchestrator steps executed")
    final_state: str = dspy.InputField(desc="Final task state")
    reply_length: int = dspy.InputField(desc="Length of the agent's reply in characters")
    
    rationale: str = dspy.OutputField(desc="Analysis of what happened, key observations")
    proposed_change_what: str = dspy.OutputField(desc="What to change (empty string if no change)")
    proposed_change_why: str = dspy.OutputField(desc="Why it would help")
    proposed_change_how: str = dspy.OutputField(desc="How to implement it")
    supporting_metrics: str = dspy.OutputField(desc="Comma-separated metrics")
    impact_assessment: str = dspy.OutputField(desc="Expected benefits")

# Configure DSPy once at module level
reasoning_model = load_model_config().models.get("reasoning")
lm = dspy.LM(
    model=f"openai/{reasoning_model.id}",
    api_base=settings.llm_base_url,
    api_key="lm-studio",
)
dspy.configure(lm=lm)

# reflection.py (refactored to use DSPy)
def generate_reflection(user_message, trace_id, steps_count, final_state, reply_length):
    try:
        # DSPy ChainOfThought
        reflection_generator = dspy.ChainOfThought(GenerateReflection)
        result = reflection_generator(
            user_message=user_message[:200],
            trace_id=trace_id,
            steps_count=steps_count,
            final_state=final_state,
            reply_length=reply_length,
        )
        
        # Convert to CaptainLogEntry
        proposed_change = None
        if result.proposed_change_what and result.proposed_change_what.strip():
            proposed_change = ProposedChange(
                what=result.proposed_change_what,
                why=result.proposed_change_why or "",
                how=result.proposed_change_how or "",
            )
        
        return CaptainLogEntry(
            title=f"Task: {user_message[:50]}",
            rationale=result.rationale,
            proposed_change=proposed_change,
            supporting_metrics=[m.strip() for m in result.supporting_metrics.split(",") if m.strip()],
            impact_assessment=result.impact_assessment if result.impact_assessment and result.impact_assessment.strip() else None,
            # ... additional fields
        )
    except Exception as e:
        log.warning("dspy_reflection_failed_fallback", error=str(e))
        return _create_basic_reflection_entry(...)
```

**Result**: 40 lines → ~25 lines (DSPy signature + usage), no manual JSON parsing, structured fields, type-safe.

**E-008 Test Results**:
- 100% success rate (0 parse failures in 5 tests)
- +21% latency overhead (acceptable: 11.8s → 14.3s)
- ~30-40% code reduction vs manual approach

---

## 7. Success Metrics

**For Captain's Log DSPy Integration** (Day 31-32):

- ✅ **Code reduction**: ≥30% reduction (prototype achieved ~30-40%)
- ✅ **Reliability**: <5% parse failures (prototype achieved 0%)
- ✅ **Performance**: Latency overhead <30% (prototype: +21%)
- [ ] **Maintainability**: Signature changes simpler than prompt template modifications
- [ ] **Integration**: Telemetry logging works with DSPy modules

**Post-MVP Evaluation** (Week 6+):

- [ ] Consider DSPy or `instructor` for other use cases (routing, planning)
- [ ] Evaluate DSPy optimizers (MIPROv2) for reflection quality
- [ ] Adoption in additional components (if justified by use case)

---

## 8. References

- [DSPy Documentation](https://dspy.ai/)
- [DSPy GitHub](https://github.com/stanfordnlp/dspy)
- [Instructor Documentation](https://python.useinstructor.com/) (alternative for future use cases)
- [Pydantic JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)
- **E-008 Prototype Evaluation**: `./experiments/E-008-dspy-prototype-evaluation.md`
- **E-008 Executive Summary**: `experiments/dspy_prototype/E-008_EXECUTIVE_SUMMARY.md`
- **DSPy Test Case A**: `experiments/dspy_prototype/test_case_a_reflection.py`
- ADR-0009: Streaming vs Non-Streaming Responses
- ADR-0003: Model Stack
- `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md`

---

## 9. Decision Log

| Date | Change | Rationale |
|------|--------|-----------|
| 2026-01-14 | Proposed with `instructor` | Initial decision based on Captain's Log implementation experience |
| 2026-01-17 | **Modified to use DSPy** | E-008 prototype evaluation (Test Case A) demonstrated DSPy superiority for Captain's Log: 100% reliability, ~30-40% code reduction, acceptable latency. Selective adoption: DSPy for Captain's Log only. |

---

## 10. Open Questions

1. ✅ **Tool calling compatibility**: E-008 Test Case C showed DSPy ReAct has issues (+237% latency)
   - **Resolution**: Use DSPy only for non-tool workflows (Captain's Log reflection)
   
2. **Streaming structured outputs**: Can we stream partial Pydantic models with DSPy?
   - **Action**: Defer until streaming is implemented (ADR-0009)
   
3. ✅ **Performance impact**: E-008 measured latency overhead
   - **Resolution**: +21% overhead acceptable for Captain's Log reflection
   
4. **DSPy optimizers**: Should we use MIPROv2 to improve reflection quality?
   - **Action**: Evaluate post-MVP (Week 6+) with production data
   
5. **Router with DSPy**: Should we adopt DSPy for routing decisions?
   - **Status**: Optional - E-008 Test Case B showed 100% accuracy with enhanced signature
   - **Action**: Evaluate if signature design effort is worthwhile (manual approach works well)

---

## 11. Implementation Status

**Current Status** (2026-01-17):

- ✅ E-008 prototype evaluation complete
- ✅ Decision made: Option B (Selective Adoption)
- ✅ ADR-0010 updated to reflect DSPy for Captain's Log
- [ ] Day 31-32: Implement DSPy ChainOfThought for reflection
- [ ] Validate with production workloads
- [ ] Measure parse failure rate and code complexity

**Next Steps**:
1. ✅ Review and approve modified ADR (DSPy instead of `instructor`)
2. [ ] Add `dspy` to `pyproject.toml`
3. [ ] Implement Day 31-32 tasks (Captain's Log refactor)
4. [ ] Document DSPy patterns in `captains_log/AGENTS.md`
