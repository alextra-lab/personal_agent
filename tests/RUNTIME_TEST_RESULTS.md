# Phase 2.2 Runtime Test Results

**Date**: 2026-01-23
**Test**: Entity extraction with local SLM models

## Test Environment

- ✅ Docker infrastructure running (PostgreSQL, Elasticsearch, Neo4j)
- ✅ SLM Server running on port 8000 (MLX backend)
- ✅ Personal Agent Service running on port 9000
- ✅ Models loaded: LFM 1.2B (router), Qwen 8B (reasoning)

## Initial Test Results

### Test 1: Simple Python Conversation

**User**: "Tell me about Python programming"
**Assistant**: "Python is a high-level programming language created by Guido van Rossum..."

#### Qwen 8B (Reasoning Model)
- ✅ **Status**: Success (first attempt)
- ⏱️ **Latency**: 27,020ms (~27 seconds)
- 📊 **Tokens**: 261 prompt, 1426 completion
- 🔍 **Entities Found**: 6
- 🔗 **Relationships Found**: 5
- ⚠️ **Issue**: Subsequent calls timed out at 60s

#### LFM 1.2B (Fast Model)
- ✅ **Status**: Success
- ⏱️ **Latency**: 1,318ms (~1.3 seconds) **⚡ 20x faster**
- 📊 **Tokens**: 276 prompt, 292 completion
- 🔍 **Entities Found**: 4
- 🔗 **Relationships Found**: 1
- ✅ **No timeouts**

## Identified Issues

### 1. Qwen 8B Timeout Problem ❌

**Symptoms**:
```
backend_timeout: model_id=qwen/qwen3-8b timeout=60
```

**Root Causes**:
1. **No token limit**: Entity extraction wasn't setting `max_tokens`
2. **Model generating too many tokens**: 1426 tokens for simple conversation
3. **60s timeout too tight**: Barely enough for first call, not enough for retries
4. **Sequential calls slower**: Model might be context-loaded on first call

**Fixes Applied**:
- ✅ Added `max_tokens=2000` to extraction calls
- ✅ Increased reasoning model timeout from 60s → 90s in `models.yaml`

### 2. LFM 1.2B Works Well! ✅

**Observations**:
- **20x faster** than Qwen 8B
- Successfully extracts entities (4 vs 6)
- Generates fewer relationships (1 vs 5)
- No timeout issues
- Compact output (292 tokens vs 1426)

**Quality Trade-off**:
- Finds 67% of entities Qwen 8B finds (4/6)
- Fewer relationships extracted
- Still provides useful summary
- **Good enough for bulk consolidation?** → Needs E-017 testing

## Performance Summary

| Model | Latency | Speed | Entities | Relationships | Tokens | Timeout Issues |
|-------|---------|-------|----------|---------------|--------|----------------|
| **Qwen 8B** | ~27s | 1x | 6 | 5 | 1426 | ❌ Yes (60s) |
| **LFM 1.2B** | ~1.3s | **20x** | 4 | 1 | 292 | ✅ None |

## Recommendations

### Immediate (Phase 2.2)
1. ✅ **Use LFM 1.2B as default** for entity extraction
   - 20x faster with acceptable quality
   - No timeout issues
   - Already loaded (zero extra cost)

2. ⏳ **Fix Qwen 8B timeouts** then test quality
   - Applied `max_tokens=2000` limit
   - Increased timeout to 90s
   - Re-test after fixes

3. 📊 **Run E-017 experiment** with real captures
   - Compare LFM vs Qwen quality
   - Measure on 50+ real conversations
   - Make data-driven decision

### Future (Phase 2.3)
1. **Hybrid approach**: Use LFM for bulk, Qwen for important conversations
2. **Optimize prompts**: Shorter prompts for faster extraction
3. **Batch processing**: Extract multiple conversations in parallel with LFM

## Next Steps

1. ⏳ Re-test Qwen 8B with fixes (max_tokens + 90s timeout)
2. ⏳ Test LFM 1.2B with complex multi-entity conversations
3. ⏳ Integration test: Full capture → extraction → Neo4j flow
4. ⏳ Run E-017 experiment with real data

## Conclusion

**LFM 1.2B is surprisingly capable!**
- 20x faster than Qwen 8B
- Acceptable entity extraction quality (67% coverage)
- Zero timeout issues
- **Recommendation**: Default to LFM 1.2B for Phase 2.2, optimize Qwen 8B for Phase 2.3

## Updated Configuration

```python
# .env
# Entity extraction: set entity_extraction_role in config/models.yaml (e.g. router for LFM, reasoning for Qwen)
```

```yaml
# config/models.yaml
reasoning:
  default_timeout: 90  # Increased from 60s
```

```python
# entity_extraction.py
max_tokens=2000  # Added to prevent runaway generation
```
