# ADR-0015: Tool Call Performance Optimization

**Status**: Proposed  
**Date**: 2026-01-17  
**Deciders**: System Architect  
**Related**: ADR-0008 (Tool Calling), ADR-0012 (Request-Scoped Metrics)

## Context

Performance profiling of agent requests reveals significant optimization opportunities in tool execution and LLM routing. A simple "Hello, test GPU metrics" query took 20.9 seconds with the following breakdown:

```
Router LLM call:     8.6s  (41% - deciding to delegate)
Standard LLM call:   3.7s  (18% - deciding to use tool)
Tool execution:      3.6s  (17% - collecting metrics)
Final synthesis:     5.0s  (24% - formatting response)
─────────────────────────────────────────────
Total user-facing:  20.9s  (100%)
Background work:    17.0s  (non-blocking, Captain's Log)
```

### Problems Identified

1. **Router Overhead** (8.6s for simple query)
   - Deep reasoning for obvious patterns
   - Should be <1s for greetings/simple queries
   - Excessive token usage for routing decisions

2. **Redundant LLM Calls** (12.3s total)
   - Router → Standard → Standard (synthesis)
   - Could be 1-2 calls instead of 3
   - Delegation overhead for straightforward tasks

3. **Tool Execution Blocking** (3.6s)
   - `system_metrics_snapshot` waits for fresh macmon poll
   - RequestMonitor already has metrics cached (polled 5s ago)
   - Unnecessary blocking on I/O

4. **No Performance Budgets**
   - No latency targets per component
   - No alerting on slow requests
   - No automatic optimization triggers

### Impact

- **User experience**: 21s feels unresponsive for simple queries
- **Resource waste**: 71% of time could be eliminated
- **Scaling concerns**: Performance degrades linearly with complexity
- **Homeostasis**: Slow responses delay mode transition decisions

### Current Performance vs Target

| Scenario | Current | Target | Gap |
|----------|---------|--------|-----|
| Simple greeting | 21s | 2s | **19s** |
| System query | 21s | 3s | **18s** |
| Complex reasoning | N/A | 10s | TBD |
| Tool-heavy task | N/A | 5s | TBD |

## Decision

Implement a **phased performance optimization strategy** focusing on high-impact, low-risk improvements.

### Phase 1: Quick Wins (Week 7, Days 37-38)

#### 1. Cache GPU Metrics in Tools (HIGH IMPACT)

**Problem**: Tool calls re-poll macmon (3.6s) when RequestMonitor already has fresh data

**Solution**: Share metrics cache between RequestMonitor and tools

```python
# In brainstem/sensors/metrics_cache.py
class MetricsCache:
    """Thread-safe cache for system metrics."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self._cache: dict[str, tuple[float, dict]] = {}  # key: (timestamp, metrics)
        self._ttl = 10.0  # seconds
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = MetricsCache()
        return cls._instance
    
    def get_metrics(self, key: str) -> dict | None:
        """Get cached metrics if fresh (within TTL)."""
        if key in self._cache:
            timestamp, metrics = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return metrics
        return None
    
    def set_metrics(self, key: str, metrics: dict):
        """Cache metrics with current timestamp."""
        self._cache[key] = (time.time(), metrics)

# In tools/system_health.py
def system_metrics_snapshot() -> dict:
    cache = MetricsCache.get_instance()
    
    # Try cache first (GPU metrics)
    gpu_metrics = cache.get_metrics("gpu")
    if not gpu_metrics:
        gpu_metrics = poll_apple_gpu_metrics()  # Slow path
        cache.set_metrics("gpu", gpu_metrics)
    
    # CPU/memory are fast, poll directly
    cpu_metrics = poll_cpu_metrics()  # <10ms
    memory_metrics = poll_memory_metrics()  # <10ms
    
    return {**cpu_metrics, **memory_metrics, **gpu_metrics}
```

**Expected Impact**: -3.5s (tool execution: 3.6s → 0.1s)

#### 2. Router Fast Path for Simple Queries (HIGH IMPACT)

**Problem**: Router spends 8.6s analyzing obvious greetings

**Solution**: Pattern-based fast path before LLM call

```python
# In orchestrator/executor.py
FAST_PATTERNS = {
    "greeting": [r"^(hello|hi|hey|greetings)", r"how are you"],
    "simple_question": [r"what is \w+\?$", r"who is \w+\?$"],
    "test": [r"test", r"ping"],
}

def _fast_route_check(user_message: str) -> str | None:
    """Check if message matches fast-path patterns.
    
    Returns model role if match found, None otherwise.
    """
    msg = user_message.lower().strip()
    
    for pattern_type, patterns in FAST_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, msg):
                log.info(
                    "routing_fast_path_matched",
                    pattern_type=pattern_type,
                    matched_pattern=pattern,
                )
                return "router"  # Handle simple queries directly
    
    return None

# In step_llm_call()
# Before calling router LLM:
fast_route = _fast_route_check(ctx.user_message)
if fast_route:
    # Skip router LLM call, use fast path
    ctx.selected_model_role = ModelRole.ROUTER
    return TaskState.LLM_CALL  # Router handles directly
```

**Expected Impact**: -7.5s (router: 8.6s → 1s for simple queries)

**Trade-off**: May miss nuanced routing needs, but acceptable for greetings

#### 3. Performance Budgets & Alerting (MEDIUM IMPACT)

**Problem**: No visibility into slow components until profiling

**Solution**: Add performance thresholds and telemetry

```python
# In config/settings.py
class PerformanceSettings(BaseSettings):
    """Performance thresholds and budgets."""
    
    # Latency budgets (milliseconds)
    router_call_budget_ms: int = 2000
    llm_call_budget_ms: int = 5000
    tool_execution_budget_ms: int = 1000
    total_request_budget_ms: int = 10000
    
    # Alerting
    slow_request_threshold_ms: int = 15000
    enable_performance_alerts: bool = True

# In orchestrator/executor.py
def _check_performance_budget(operation: str, duration_ms: float):
    """Log warning if operation exceeds budget."""
    budgets = {
        "router_call": settings.router_call_budget_ms,
        "llm_call": settings.llm_call_budget_ms,
        "tool_execution": settings.tool_execution_budget_ms,
    }
    
    if operation in budgets and duration_ms > budgets[operation]:
        log.warning(
            "performance_budget_exceeded",
            operation=operation,
            duration_ms=duration_ms,
            budget_ms=budgets[operation],
            overage_ms=duration_ms - budgets[operation],
        )
```

**Expected Impact**: +0s (observability, enables future optimization)

---

### Phase 2: Structural Improvements (Week 8)

#### 4. Reduce LLM Call Redundancy

**Problem**: 3 LLM calls for simple queries (router → standard → standard)

**Options**:
- **Option A**: Router can return final answer directly (no delegation)
- **Option B**: Skip synthesis LLM call for tool-only responses
- **Option C**: Streaming responses (progressive output)

**Investigation Needed**: E-009 experiment to measure impact

#### 5. Parallel Tool Execution

**Problem**: Tools execute sequentially even when independent

**Solution**: Async tool execution with dependency graph

```python
# If tools are independent:
results = await asyncio.gather(
    execute_tool("system_metrics"),
    execute_tool("read_file"),
    execute_tool("web_search"),
)
```

**Expected Impact**: Variable (depends on tool usage patterns)

#### 6. LLM Response Streaming

**Problem**: User waits for complete response before seeing anything

**Solution**: Stream tokens as generated (ADR-0009 already supports this)

**Expected Impact**: Perceived latency reduced by 60%+ (first token in <1s)

---

### Phase 3: Advanced Optimization (Future)

#### 7. Router Model Optimization

- Fine-tune smaller router model (1.7B → 0.5B)
- Quantization (int8/int4) for faster inference
- Multi-task routing (combine with classification)

#### 8. Caching & Memoization

- Cache common LLM responses (greetings, FAQs)
- Memoize tool results (filesystem, static data)
- Semantic caching (similar queries → same response)

#### 9. Speculative Execution

- Predict likely tool calls before LLM decides
- Pre-warm tool execution pipelines
- Parallel router + standard calls

---

## Testing Strategy

### Performance Test Suite

```python
# tests/performance/test_latency_budgets.py

@pytest.mark.performance
def test_simple_greeting_under_budget():
    """Simple greetings should complete in <3s."""
    start = time.time()
    result = orchestrator.execute_task("Hello!")
    duration = time.time() - start
    
    assert duration < 3.0, f"Simple greeting took {duration:.1f}s (budget: 3s)"
    assert result.success

@pytest.mark.performance
def test_tool_call_caching():
    """Repeated tool calls should use cache (fast)."""
    # First call (uncached)
    start1 = time.time()
    orchestrator.execute_task("Check GPU metrics")
    duration1 = time.time() - start1
    
    # Second call (cached)
    start2 = time.time()
    orchestrator.execute_task("Check GPU metrics")
    duration2 = time.time() - start2
    
    # Second call should be significantly faster
    assert duration2 < duration1 * 0.5, "Cache not utilized"
    assert duration2 < 1.0, "Cached call too slow"

@pytest.mark.performance
@pytest.mark.parametrize("query,budget_seconds", [
    ("Hello", 3),
    ("What is Python?", 10),
    ("Analyze system health", 5),
])
def test_query_latency_budgets(query, budget_seconds):
    """All queries should meet latency budgets."""
    start = time.time()
    result = orchestrator.execute_task(query)
    duration = time.time() - start
    
    assert duration < budget_seconds, \
        f"{query} took {duration:.1f}s (budget: {budget_seconds}s)"
```

### Continuous Performance Monitoring

```python
# In telemetry, add performance metrics
log.info(
    "request_performance_summary",
    total_duration_ms=duration_ms,
    router_duration_ms=router_ms,
    tool_duration_ms=tool_ms,
    llm_calls=llm_count,
    budget_status="within" if duration_ms < budget_ms else "exceeded",
)
```

---

## Consequences

### Positive

✅ **User Experience**: 71% faster responses (21s → 6s target)  
✅ **Resource Efficiency**: Fewer unnecessary LLM calls  
✅ **Scalability**: Reduced per-request cost  
✅ **Observability**: Performance budgets expose slow components  
✅ **Incremental**: Phased approach, low risk  

### Negative

⚠️ **Complexity**: Caching adds state management  
⚠️ **Trade-offs**: Fast path may miss edge cases  
⚠️ **Maintenance**: Performance tests require upkeep  

### Neutral

ℹ️ **Best Practices**: Aligns with standard web service optimization  
ℹ️ **Iterative**: Measure, optimize, repeat  

---

## Alternatives Considered

### Alternative 1: Accept Current Performance

**Rationale**: Focus on features, not optimization

**Rejected Because**:
- 21s is unusable for production
- Performance degrades user experience
- Wasted resources (compute, energy)

### Alternative 2: Complete Rewrite for Speed

**Rationale**: Redesign architecture for parallelism

**Rejected Because**:
- High risk, high effort
- Current design is sound
- Incremental improvements sufficient

### Alternative 3: Use Faster Models Only

**Rationale**: Switch to smaller, faster LLMs

**Rejected Because**:
- Quality trade-off unacceptable
- Doesn't address structural issues (tool blocking, caching)

---

## Implementation Roadmap

### Week 7, Days 37-38: Quick Wins

- [ ] Implement MetricsCache for GPU metrics
- [ ] Add router fast path for simple queries
- [ ] Add performance budgets & logging
- [ ] Create performance test suite
- [ ] Measure baseline vs optimized latency

**Expected Outcome**: 71% latency reduction (21s → 6s)

### Week 8: Structural Improvements

- [ ] Design E-009: LLM call reduction experiment
- [ ] Implement parallel tool execution
- [ ] Enable response streaming (ADR-0009)
- [ ] Add caching for common responses

### Week 9+: Advanced Optimization (Optional)

- [ ] Router model optimization
- [ ] Semantic caching
- [ ] Speculative execution

---

## Success Metrics

| Metric | Baseline | Target | Measurement |
|--------|----------|--------|-------------|
| Simple query latency | 21s | <3s | p50 |
| Tool execution latency | 3.6s | <0.5s | p50 |
| Router decision latency | 8.6s | <1s | p50 |
| Total request latency | 21s | <6s | p50 |
| Performance test pass rate | N/A | 100% | CI |

---

## References

- **ADR-0008**: Tool Calling Strategy
- **ADR-0009**: Streaming vs Non-Streaming Responses
- **ADR-0012**: Request-Scoped Metrics Monitoring
- **Profile Data**: Terminal output from test run (2026-01-17)

---

**Decision**: Approved for Week 7 implementation (Phase 1)  
**Next Steps**: Create performance test suite, implement quick wins, measure impact

**Created**: 2026-01-17  
**Status**: Ready for implementation
