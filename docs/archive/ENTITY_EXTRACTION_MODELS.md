# Entity Extraction Model Comparison

## Overview

Phase 2.2 supports multiple models for entity extraction from conversations. This document compares the options and provides guidance for selection.

## Available Models

### 1. Qwen 3 8B (Reasoning) - **DEFAULT**

**Configuration**: `entity_extraction_role: reasoning` in `config/models.yaml`

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

**Configuration**: `entity_extraction_role: router` in `config/models.yaml`

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

**Configuration**: `entity_extraction_role: claude` in `config/models.yaml`

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

## How the model is chosen (what you see in logs)

The model is selected by the top-level key **`entity_extraction_role`** in `config/models.yaml` (no env var). It accepts:

- A **role name** that exists under `models:` in the same file (e.g. `reasoning`, `router`, `standard`, `coding`). The actual model id and endpoint come from that role's block. Example: `entity_extraction_role: reasoning` uses the **reasoning** model (e.g. `qwen3.5-9b-mlx-mxfp8`); `entity_extraction_role: router` uses the **router** model (e.g. `lfm2.5-1.2b-instruct-mlx`). So the log line `[lfm2.5-1.2b-instruct-mlx] Generated prediction` appears when `entity_extraction_role: router`.
- **`claude`** — use Claude API instead of a local model (no role block; requires Claude client and API key).

To use the fast LFM model for entity extraction, set `entity_extraction_role: router` in `config/models.yaml`. To use the higher-quality reasoning model, set `entity_extraction_role: reasoning`.

## Implementation

Entity extraction reads the role from model config:

```python
# src/personal_agent/second_brain/entity_extraction.py

from personal_agent.config import load_model_config

model_config = load_model_config()
entity_extraction_role = model_config.entity_extraction_role

if entity_extraction_role == "claude":
    # Use Claude API if claude_client is provided
else:
    # Use local model: role name must match a key under models: in config/models.yaml
    model_role = ModelRole.from_str(entity_extraction_role) or ModelRole.REASONING
    # ... call LocalLLMClient.respond(role=model_role, ...)
```

## Current Configuration (2026-04-30)

`entity_extraction_role: reasoning` — `qwen3.6-35b-a3b` (8bit, 35B MoE)

This replaced the original `qwen3.5-9b-mlx-mxfp8` which crashed on LM Studio
due to an mxfp8 quantization issue at the 9B size. The 4B standard model was
used temporarily and confirmed the improved prompt works, but relationship
quality was poor (wrong directionality, semantic errors). The 35B model
produces significantly better entity deduplication and relationship reasoning.

**Model call settings for extraction:**
- `max_tokens: 6000` — headroom after 3000 thinking tokens
- `default_timeout: 180s` — 35B generates slowly; ~60-90s per capture
- `thinking_budget_tokens: 3000` — bounded reasoning for structured output

**Known issues:**
- ~30% empty response rate — model occasionally exhausts budget on thinking.
  Fallback guard skips these; they retry next consolidation run.

## Extraction Prompt — What to Exclude

The following categories consistently produce noise and are explicitly excluded
in the prompt (`entity_extraction.py`):

| Category | Example | Reason |
|---|---|---|
| Conversation participants | `User`, `Assistant` | Always present, zero recall value |
| MCP tool bindings | `mcp_perplexity_ask`, `mcp_docker` | Implementation detail — extract the service |
| Ephemeral data values | `7°C`, `53°F`, `March 6, 2026` | Transient, not knowledge |
| Test/placeholder text | `Test message`, `Quick test` | Synthetic artifacts |
| Generic meta-concepts | `Topic`, `Invalid routing` | Not real-world knowledge |

## Recommendation

**Use `reasoning` (35B)** for all entity extraction. The quality difference in
relationship correctness and entity deduplication over the 4B standard model
is significant enough to justify the slower throughput. Extraction runs in the
background scheduler — latency is not user-facing.

**Don't use Claude** unless local models prove insufficient for a specific task.
The current 35B model quality is sufficient for the knowledge graph use case.
