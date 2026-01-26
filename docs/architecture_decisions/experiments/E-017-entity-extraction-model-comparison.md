# E-017: Entity Extraction Model Comparison

**Date**: 2026-01-22
**Phase**: 2.2 (Memory & Second Brain)
**Status**: Planned - Ready to Execute
**Type**: Performance & Quality Comparison

## Hypothesis

**H-009**: Small fast models (LFM 2.5 1.2B) can extract basic entities from conversations with acceptable accuracy (≥70% vs reasoning model) while providing significant speed improvements (3-6x faster).

## Background

Phase 2.2 implements entity extraction for building the knowledge graph. Three model options are available:

1. **Qwen 3 8B** (reasoning): High quality, moderate speed (~200ms)
2. **LFM 2.5 1.2B** (router): Unknown quality, very fast (~50ms)
3. **Claude 4.5** (cloud): Highest quality, slow + cost (~800ms + $0.05/100)

**Question**: Can LFM 1.2B provide good enough extraction for bulk consolidation?

## Method

### Test Setup

**Dataset**: 50-100 real task captures from production usage

**Models to Test**:
- Baseline: Qwen 8B (REASONING)
- Experiment: LFM 1.2B (ROUTER)
- Reference: Claude 4.5 (if API key available)

**Metrics**:
1. **Entity Accuracy**: % of entities found (vs Qwen 8B baseline)
2. **Relationship Quality**: Correctness of relationship types
3. **JSON Parse Rate**: % of valid JSON responses
4. **Latency**: Average extraction time
5. **Throughput**: Captures processed per minute

### Test Procedure

1. **Collect Captures**: Run agent for 1-2 days to generate real captures
2. **Sample Selection**: Select 50 diverse captures (various topics, lengths)
3. **Parallel Extraction**: Process same captures with both models
4. **Manual Review**: Validate 10 randomly selected extractions from each
5. **Statistical Analysis**: Compare metrics

### Implementation

```python
# Test script: tests/experiments/test_e017_entity_extraction.py

import asyncio
from personal_agent.captains_log.capture import read_captures
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships
from personal_agent.llm_client import LocalLLMClient
from personal_agent.config import settings

async def run_extraction_comparison():
    # Read recent captures
    captures = read_captures(limit=50)

    results = {
        "qwen8b": [],
        "lfm12b": [],
    }

    for capture in captures:
        # Test Qwen 8B
        settings.entity_extraction_model = "qwen3-8b"
        qwen_result = await extract_entities_and_relationships(
            capture.user_message,
            capture.assistant_response or "",
        )
        results["qwen8b"].append(qwen_result)

        # Test LFM 1.2B
        settings.entity_extraction_model = "lfm2.5-1.2b"
        lfm_result = await extract_entities_and_relationships(
            capture.user_message,
            capture.assistant_response or "",
        )
        results["lfm12b"].append(lfm_result)

    # Analyze results
    return analyze_results(results)
```

## Success Criteria

### Must Pass (All Required)
- ✅ LFM 1.2B finds ≥70% of entities found by Qwen 8B
- ✅ JSON parse success rate ≥90%
- ✅ Latency improvement ≥2x (target: 3-6x)
- ✅ No systematic hallucinations or errors

### Nice to Have
- ⭐ Entity accuracy ≥80% (vs Qwen 8B)
- ⭐ Relationship accuracy ≥60%
- ⭐ Latency improvement ≥4x

## Failure Criteria

**Abandon LFM 1.2B if**:
- ❌ Entity accuracy <60%
- ❌ JSON parse rate <80%
- ❌ Frequent hallucinations (made-up entities)
- ❌ Relationship extraction meaningless

## Expected Results

### Scenario A: LFM 1.2B Success (70-90% accuracy)
**Action**: Use hybrid approach
- **Bulk consolidation**: LFM 1.2B (fast)
- **Important conversations**: Qwen 8B (quality)
- **Critical extraction**: Claude (optional)

**Benefits**:
- 3-6x faster consolidation
- Same memory usage (model already loaded as router)
- Good enough for knowledge graph building

### Scenario B: LFM 1.2B Partial Success (60-70% accuracy)
**Action**: Use LFM for simple tasks only
- **Single entity conversations**: LFM 1.2B
- **Multi-entity conversations**: Qwen 8B
- Implement complexity detector

### Scenario C: LFM 1.2B Failure (<60% accuracy)
**Action**: Stick with Qwen 8B default
- Don't use LFM 1.2B for extraction
- Document learnings
- Revisit when better small models available

## Deliverables

1. ✅ Model comparison implementation
2. ⏳ Test results document
3. ⏳ Statistical analysis
4. ⏳ Manual review samples
5. ⏳ Decision and recommendation

## Timeline

- **Setup**: 1 hour (test script)
- **Data collection**: 1-2 days (agent usage)
- **Execution**: 2-3 hours (extraction + analysis)
- **Review**: 1 hour (manual validation)

**Total**: ~2-3 days from Phase 2.2 completion

## Related

- **ADR-0016**: Service architecture with second brain
- **Phase 2.2**: Memory & Second Brain implementation
- **E-008**: DSPy evaluation (structured outputs)
- **H-009**: Small model capabilities hypothesis

## Notes

- LFM 2.5 1.2B is already loaded as router in SLM Server (zero additional cost)
- Could enable "free" extraction if quality acceptable
- Worst case: Stick with proven Qwen 8B
- Best case: 3-6x consolidation speed boost
