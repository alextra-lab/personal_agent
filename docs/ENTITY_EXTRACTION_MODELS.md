# Entity Extraction Model Comparison

## Overview

Phase 2.2 supports multiple models for entity extraction from conversations. This document compares the options and provides guidance for selection.

## Available Models

### 1. Qwen 3 8B (Reasoning) - **DEFAULT**

**Configuration**: `AGENT_ENTITY_EXTRACTION_MODEL=qwen3-8b`

**Characteristics**:
- **Model Role**: REASONING
- **Quality**: High - designed for complex reasoning
- **Speed**: ~150-300ms per extraction
- **Cost**: Zero (local SLM Server)
- **Memory**: ~5GB when loaded

**Best For**:
- Default choice for balanced quality/speed
- Complex conversations with multiple entities
- Nuanced relationship detection
- Production use

**Limitations**:
- Slower than LFM 1.2B
- Higher memory usage

---

### 2. LFM 2.5 1.2B - **FAST EXPERIMENT**

**Configuration**: `AGENT_ENTITY_EXTRACTION_MODEL=lfm2.5-1.2b`

**Characteristics**:
- **Model Role**: ROUTER
- **Quality**: Unknown (needs benchmarking)
- **Speed**: ~50ms per extraction (⚡ 3-6x faster)
- **Cost**: Zero (local SLM Server)
- **Memory**: ~1.5GB when loaded

**Best For**:
- High-throughput consolidation
- Simple entity extraction (names, keywords)
- Quick experiments
- Resource-constrained environments

**Limitations**:
- May struggle with complex relationships
- Unproven for structured JSON output
- Smaller context window

**Experiment**: E-017 - Entity Extraction Model Comparison
- Test on 50-100 real captures
- Compare: entities found, relationships, JSON parse rate
- Measure: latency, accuracy, throughput

---

### 3. Claude Sonnet 4.5 - **PRODUCTION QUALITY**

**Configuration**: `AGENT_ENTITY_EXTRACTION_MODEL=claude`

**Characteristics**:
- **Model Role**: Cloud API
- **Quality**: Highest - world-class reasoning
- **Speed**: ~500-1000ms per extraction (includes API latency)
- **Cost**: $3/1M input tokens, $15/1M output tokens
- **Memory**: Zero (cloud)

**Best For**:
- Production deployments requiring highest quality
- Complex multi-entity conversations
- Critical applications
- When local resources limited

**Limitations**:
- API cost ($5/week budget default)
- Requires internet connection
- Network latency
- Requires API key

---

## Performance Comparison (Estimated)

| Model | Latency | Quality | Cost | Memory | Throughput |
|-------|---------|---------|------|--------|------------|
| **Qwen 8B** | 150-300ms | High | $0 | 5GB | ~200-400/hour |
| **LFM 1.2B** | ~50ms | ? | $0 | 1.5GB | ~1200-2000/hour |
| **Claude 4.5** | 500-1000ms | Highest | ~$0.05/100 | 0GB | ~100-200/hour |

## Selection Guide

### Choose Qwen 8B (Default) When:
- ✅ Balanced quality and speed needed
- ✅ Complex conversations
- ✅ Standard use case
- ✅ Don't want to experiment

### Experiment with LFM 1.2B When:
- 🧪 Have many captures to process (>100)
- 🧪 Speed is critical
- 🧪 Want to test small model capabilities
- 🧪 Willing to accept lower accuracy for speed

### Use Claude When:
- 💎 Quality is paramount
- 💎 API cost acceptable
- 💎 Have API key
- 💎 Internet connection reliable

## Experiment: LFM 1.2B Capability Assessment

**Hypothesis**: LFM 2.5 1.2B can extract basic entities (names, places, topics) with 60-80% accuracy compared to Qwen 8B, with 3-6x speed improvement.

**Test Plan**:
1. Process 50 captures with Qwen 8B (baseline)
2. Process same 50 captures with LFM 1.2B
3. Compare:
   - Entity count (should be similar)
   - Entity accuracy (manual review)
   - Relationship quality
   - JSON parse success rate
   - Latency
   - Throughput

**Success Criteria**:
- ✅ LFM 1.2B finds ≥70% of entities Qwen 8B finds
- ✅ JSON parse rate ≥90%
- ✅ Latency ≤100ms (3x faster than Qwen)
- ✅ No crashes or errors

**Failure Criteria**:
- ❌ Entity accuracy <60%
- ❌ JSON parse rate <80%
- ❌ Frequent hallucinations

**Outcome**:
- If successful: Use LFM 1.2B for bulk consolidation, Qwen 8B for important conversations
- If failed: Stick with Qwen 8B default

## Implementation

Current implementation supports all three models via configuration:

```python
# src/personal_agent/second_brain/entity_extraction.py

if settings.entity_extraction_model == "lfm2.5-1.2b":
    model_role = ModelRole.ROUTER  # LFM 1.2B
elif settings.entity_extraction_model == "claude":
    # Use Claude client if provided
elif settings.entity_extraction_model == "qwen3-8b":
    model_role = ModelRole.REASONING  # Qwen 8B (default)
```

## Recommendation

**Start with Qwen 8B** (default), then:
1. Let system run and collect captures
2. Run E-017 experiment with 50-100 real captures
3. Compare LFM 1.2B vs Qwen 8B results
4. Make data-driven decision
5. Consider hybrid: LFM for simple, Qwen for complex

**Don't use Claude** until local models prove insufficient.
