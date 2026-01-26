# E-007: Inference Server Evaluation

**Status**: Proposed
**Priority**: Phase 2-3 (Post-MVP)
**Date**: 2025-12-31
**Owner**: TBD

---

## Context

The current MVP uses **LM Studio** as the local inference server. While LM Studio is excellent for development and provides a user-friendly interface, it has a significant architectural limitation: **sequential request processing**.

### LM Studio Sequential Processing

LM Studio processes requests **one at a time per loaded model**:
- Request 1 starts → completes → Request 2 starts → completes → etc.
- Multiple concurrent requests queue internally
- No true parallelism even with multiple models loaded

**Impact on Agent Architecture:**
- **Parallel tool execution** (Phase 2) will be bottlenecked
- **Multi-agent coordination** (Phase 4) severely limited
- **Interleaved routing + reasoning** patterns impossible
- **Background tasks** (monitoring, pre-warming) block main flow

### Alternative Inference Servers

Three primary candidates support **true concurrent inference**:

1. **llama.cpp** (C++ native)
   - Supports parallel inference with `--parallel` flag
   - Lower latency than LM Studio
   - No GUI (CLI only)
   - Requires manual model quantization

2. **MLX** (Apple Silicon optimized)
   - Native Metal acceleration
   - Excellent M-series performance
   - True parallel inference
   - Python-native API

3. **vLLM** (Production-grade)
   - PagedAttention for efficient KV cache
   - Continuous batching for throughput
   - Industry standard for serving
   - Heavier resource requirements

---

## Hypothesis

**Switching from LM Studio to a parallelism-capable inference server will significantly improve agent throughput for concurrent workflows.**

### Specific Claims

1. **Parallel Tool Execution**: Running 3 tools simultaneously (each requiring LLM calls) will complete 2-3x faster with parallel-capable servers vs LM Studio
2. **Multi-Agent Workflows**: Coordinating 3+ specialized agents will be feasible with concurrent servers, infeasible with LM Studio
3. **Latency**: llama.cpp and MLX will show 10-30% lower p50 latency vs LM Studio for single requests
4. **Throughput**: Concurrent servers will handle 3-5x more requests/minute under parallel load

---

## Experiment Design

### Phase 1: Benchmark Single-Request Performance

**Workload**: Same prompts used in E-004 baseline
**Metrics**:
- p50, p95, p99 latency
- Tokens/second
- Memory usage
- CPU/GPU utilization

**Servers to test**:
- LM Studio (baseline)
- llama.cpp with same models
- MLX with same models

**Expected outcome**: llama.cpp and MLX show 10-30% lower latency

---

### Phase 2: Concurrent Request Performance

**Workload**: 3 simultaneous requests to same model
**Test cases**:
1. Router: 3 routing decisions in parallel
2. Reasoning: 3 reasoning tasks in parallel
3. Mixed: 1 router + 1 reasoning + 1 coding in parallel

**Metrics**:
- Total wall-clock time to complete all 3
- Queue time per request
- Throughput (requests/minute)
- Resource saturation

**Expected outcome**:
- LM Studio: Sequential processing → 3x single request time
- Parallel servers: Near-constant time (small overhead)

---

### Phase 3: Agent Workflow Simulation

**Workload**: Realistic agent scenarios
1. **Parallel Tool Execution**: Execute 3 tools, each requiring LLM for result parsing
2. **Routing + Execution**: Router decides → coding model executes (pipelined)
3. **Multi-Agent Coordination**: 3 specialist agents collaborate on task

**Metrics**:
- End-to-end workflow completion time
- Idle time (waiting for LLM)
- Resource efficiency

**Expected outcome**: Parallel servers enable 2-5x faster workflows

---

### Phase 4: Production Readiness

**Evaluation criteria**:
- Setup complexity (minutes to hours?)
- Model loading time
- Hot-swap capability (switch models without downtime)
- API compatibility (OpenAI-compatible?)
- Monitoring/observability
- Error handling and recovery

---

## Implementation Strategy

### Step 1: Abstract Inference Server (Week X)

Create `InferenceServerAdapter` interface:

```python
class InferenceServerAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def load_model(self, model_id: str, config: ModelConfig) -> None: ...
    async def unload_model(self, model_id: str) -> None: ...
    async def health_check(self) -> bool: ...
```

Implementations:
- `LMStudioAdapter` (current)
- `LlamaCppAdapter` (new)
- `MLXAdapter` (new)
- `VLLMAdapter` (future)

---

### Step 2: Configuration-Based Server Selection

```yaml
# config/inference.yaml
inference:
  server: "llama.cpp"  # or "lmstudio", "mlx", "vllm"

  llama_cpp:
    host: "localhost"
    port: 8080
    parallel: 4  # Enable parallel processing

  mlx:
    host: "localhost"
    port: 8081

  lmstudio:
    host: "localhost"
    port: 1234
```

---

### Step 3: Benchmark Harness

Extend `tests/evaluation/model_benchmarks.py`:

```python
async def benchmark_concurrent_load(
    server: InferenceServerAdapter,
    model_id: str,
    num_concurrent: int,
    prompt: str
) -> ConcurrentBenchmarkResult:
    """Run N requests in parallel, measure total time."""
    tasks = [
        server.generate(model_id, prompt)
        for _ in range(num_concurrent)
    ]
    start = time.time()
    results = await asyncio.gather(*tasks)
    duration = time.time() - start
    return ConcurrentBenchmarkResult(
        total_time=duration,
        per_request_times=[r.latency for r in results],
        throughput=num_concurrent / duration
    )
```

---

## Success Criteria

### Must Have (to switch from LM Studio)
- ✅ 2x+ faster for concurrent workloads (3+ parallel requests)
- ✅ API compatibility (minimal code changes)
- ✅ Stable under load (no crashes, graceful degradation)
- ✅ Setup time < 30 minutes

### Nice to Have
- Lower single-request latency
- Better resource efficiency
- Hot model swapping
- Production monitoring

---

## Decision Matrix

| Feature | LM Studio | llama.cpp | MLX | vLLM |
|---------|-----------|-----------|-----|------|
| **Concurrent inference** | ❌ Sequential | ✅ Parallel | ✅ Parallel | ✅ Parallel |
| **Setup complexity** | ✅ Easy (GUI) | ⚠️ Medium (CLI) | ⚠️ Medium | ❌ Complex |
| **Latency** | ⚠️ Medium | ✅ Low | ✅ Very Low | ✅ Low |
| **Apple Silicon** | ✅ Good | ✅ Good | ✅ Excellent | ⚠️ Okay |
| **Model hot-swap** | ✅ Yes | ❌ No | ❌ No | ✅ Yes |
| **OpenAI API** | ✅ Yes | ✅ Yes | ⚠️ Custom | ✅ Yes |
| **GUI** | ✅ Yes | ❌ No | ❌ No | ❌ No |
| **Production ready** | ⚠️ Dev only | ✅ Yes | ✅ Yes | ✅ Yes |

---

## Risks & Mitigations

### Risk 1: Setup Complexity
**Impact**: High - blocks adoption
**Mitigation**:
- Create setup scripts for each server
- Document step-by-step in `docs/INFERENCE_SERVERS.md`
- Provide Docker compose files

### Risk 2: API Incompatibility
**Impact**: Medium - requires code changes
**Mitigation**:
- Abstract behind adapter interface
- Keep LM Studio as fallback option
- Maintain compatibility layer

### Risk 3: Resource Requirements
**Impact**: Medium - may not run on all hardware
**Mitigation**:
- Benchmark resource usage
- Document minimum requirements
- Provide configuration presets

---

## Timeline

| Phase | Duration | Dependencies |
|-------|----------|--------------|
| Design adapter interface | 1 day | None |
| Implement llama.cpp adapter | 2 days | Interface |
| Implement MLX adapter | 2 days | Interface |
| E-007 Phase 1 (single request) | 1 day | Adapters |
| E-007 Phase 2 (concurrent) | 1 day | Phase 1 |
| E-007 Phase 3 (workflows) | 2 days | Phase 2, Orchestrator |
| E-007 Phase 4 (production) | 1 day | Phase 3 |
| **Total** | **10 days** | |

---

## Related Work

- **E-004**: Baseline performance (establishes comparison point)
- **ADR-0003**: Model stack (which models to serve)
- **ADR-0008**: Model stack course correction (optimized models)
- **Phase 2**: Parallel tool execution (primary driver for this work)
- **Phase 4**: Multi-agent network (requires concurrent inference)

---

## Open Questions

1. **Which server to prioritize?** llama.cpp (versatile) vs MLX (Apple-optimized)?
2. **Configuration complexity**: Single unified config or per-server configs?
3. **Migration path**: Big-bang switch or gradual rollout with A/B testing?
4. **Model format**: Can we reuse LM Studio's downloaded models or re-quantize?
5. **Development workflow**: Keep LM Studio for dev, use llama.cpp for prod?

---

## Next Steps

1. **Immediate (MVP)**: Continue with LM Studio - document this as technical debt
2. **Post-MVP**: Schedule E-007 for Phase 2 planning
3. **Design**: Create `InferenceServerAdapter` interface in advance
4. **Research**: Test llama.cpp and MLX setup on dev machine
5. **Roadmap**: Add "Inference Server Evaluation" to Phase 2 objectives

---

## References

- **llama.cpp**: https://github.com/ggerganov/llama.cpp
- **MLX**: https://github.com/ml-explore/mlx
- **vLLM**: https://github.com/vllm-project/vllm
- **Benchmark discussion**: https://github.com/ggerganov/llama.cpp/discussions/2326
- **MLX performance**: https://ml-explore.github.io/mlx/build/html/usage/performance.html
