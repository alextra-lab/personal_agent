# Technical Debt Register

**Purpose**: Track known limitations, shortcuts, and planned improvements.

**Status**: Living document - update as debt is added or resolved

---

## 🔴 High Priority (Blocking Future Features)

### TD-001: LM Studio Sequential Request Processing

**Component**: `llm_client`, inference infrastructure
**Impact**: **Critical** - Blocks parallel tool execution and multi-agent coordination
**Added**: 2025-12-31
**Status**: Documented, scheduled for Phase 2

**Problem**:
LM Studio processes requests **sequentially** - only one request per model at a time. Concurrent requests queue internally with no true parallelism.

**Impact**:

- **Parallel tool execution** (Phase 2): 3 tools requiring LLM calls will take 3x longer than optimal
- **Multi-agent coordination** (Phase 5): Infeasible with sequential processing - agents can't work concurrently
- **Background tasks**: Pre-warming, monitoring, etc. block main request flow
- **User experience**: Slow response times for multi-step workflows

**Current Workaround**:

- MVP accepts this limitation (single-threaded orchestrator doesn't need parallelism yet)
- Documented in E-007 for future evaluation

**Resolution Plan**:

- **Phase 2A**: Evaluate llama.cpp, MLX, and vLLM (E-007)
- **Phase 2A**: Implement `InferenceServerAdapter` abstraction
- **Phase 2A**: Benchmark concurrent throughput
- **Phase 2B**: Switch to parallel-capable server if justified by data

**Effort**: 10 days (design + implementation + evaluation)

**References**:

- `./experiments/E-007-inference-server-evaluation.md`
- `../plans/IMPLEMENTATION_ROADMAP.md` - Phase 2A

---

## 🟡 Medium Priority (Optimization Opportunities)

### TD-002: Logging Error from httpx through structlog

**Component**: `telemetry/logger.py`
**Impact**: **Low** - Doesn't affect functionality, but pollutes logs
**Added**: 2025-12-31
**Status**: Identified

**Problem**:

```
AttributeError: 'NoneType' object has no attribute 'name'
Call stack: telemetry/logger.py:87, in _add_component
```

Occurs when `httpx` library logs through structlog's configured handlers.

**Impact**:

- Logging error messages printed to stderr (not breaking)
- Clutters benchmark output
- Minor observability degradation

**Current Workaround**:

- Ignore - doesn't affect functionality
- Actual LLM client logs still work correctly

**Resolution Plan**:

- Fix `_add_component` processor to handle `None` logger gracefully
- Add conditional: `if logger and hasattr(logger, 'name'):`

**Effort**: 30 minutes

**References**:

- Error first seen in benchmark output: `tests/test_llm_client/benchmark_response_times.py`

---

### TD-003: No Concurrency Enforcement in LocalLLMClient

**Component**: `llm_client/client.py`
**Impact**: **Medium** - Could cause resource exhaustion with parallel orchestrator
**Added**: 2025-12-31
**Status**: Config defined but not enforced

**Problem**:
`max_concurrency` parameter exists in `config/models.yaml` but is not enforced in `LocalLLMClient`.

```yaml
models:
  router:
    max_concurrency: 4  # ← Defined but ignored
```

**Impact**:

- No protection against overwhelming local inference server
- Potential OOM or slowdown with many parallel requests
- Not critical for MVP (single-threaded orchestrator)

**Current Workaround**:

- Sequential orchestrator naturally limits concurrency
- LM Studio queues excess requests (doesn't crash)

**Resolution Plan**:

- Add semaphore-based rate limiting per model role
- Implement in `LocalLLMClient.__init__()` and `respond()` method

**Effort**: 1-2 hours

**References**:

- User conversation: 2025-12-31 re: concurrency parameter

---

## 🟢 Low Priority (Nice to Have)

### TD-004: Simple Test Query in Benchmarks

**Component**: `tests/test_llm_client/benchmark_response_times.py`
**Impact**: **Very Low** - Benchmark accurate but not realistic
**Added**: 2025-12-31
**Status**: Acknowledged

**Problem**:
Benchmark uses trivial prompt: `"Say 'OK' and nothing else."`

This measures latency but not realistic workload characteristics.

**Impact**:

- Coding model shows artificially low token generation (2 tokens)
- Doesn't test context handling, reasoning quality, or tool use
- Fine for initial latency baseline

**Current Workaround**:

- Use E-004 comprehensive benchmarks for realistic testing

**Resolution Plan**:

- Enhance benchmark suites with realistic prompts:
  - Router: Actual routing decisions with tool choices
  - Reasoning: Math, logic, multi-step problems
  - Coding: Function generation, debugging tasks

**Effort**: 2-3 hours (prompt design + validation)

**References**:

- `./experiments/E-004-baseline-model-performance.md`

---

## 📊 Debt Metrics

| Priority | Count | Total Effort |
|----------|-------|--------------|
| High     | 1     | 10 days      |
| Medium   | 2     | 3-4 hours    |
| Low      | 1     | 2-3 hours    |
| **Total** | **4** | **~10 days** |

---

## 🔄 Resolved Debt (Archive)

### ~~TD-000: Example Resolved Item~~

**Status**: ✅ Resolved (YYYY-MM-DD)
**Resolution**: Brief description of how it was fixed
**Commit**: `abc123f`

---

## Maintenance

**How to add debt**:

1. Assign next TD-XXX number
2. Include: Component, Impact, Added date, Problem, Impact, Workaround, Plan, Effort
3. Update metrics table
4. Link from relevant ADRs/specs if architectural

**How to resolve debt**:

1. Move to "Resolved Debt" section
2. Update status to ✅ with date
3. Add commit hash
4. Update metrics table

**Review cadence**: Monthly or when planning new phases
