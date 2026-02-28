# E-018: LangExtract Evaluation

Hypothesis-driven experiments to determine whether Google's **LangExtract** library adds value for structured extraction in Personal Agent (entity extraction and, conditionally, reflection).

## Links

- **Experiment spec**: [E-018-langextract-evaluation.md](../../docs/architecture_decisions/experiments/E-018-langextract-evaluation.md)
- **Research review**: [langextract_library_review_2026-01-28.md](../../docs/research/langextract_library_review_2026-01-28.md)

## Status

**Date started**: 2026-01-28
**Status**: Planned

## Hypotheses (Summary)

| ID | Claim | Metric | Success |
|----|-------|--------|---------|
| **H-018a** | LangExtract reduces parse failures vs current entity extraction | Parse success rate on fixed dataset | ≥ baseline (or +10% if baseline &lt;95%) |
| **H-018b** | LangExtract reduces extraction code size | LOC (extraction + parsing) | ≥20% reduction |
| **H-018c** | LangExtract does not materially increase latency | P95 latency per call | P95 ≤ baseline × 1.15 |
| **H-018d** | Source grounding adds traceability value | Feasibility + one use case | Documented and feasible |
| **H-018e** | (Conditional) LangExtract for reflection | Parse rate, LOC, latency vs DSPy | Deferred until Phase 1 complete |

## Experiment Design

### Phase 1: Entity Extraction (Primary)

1. **Dataset**: 50–100 conversation pairs (user_message, assistant_response). Reuse E-017 format or synthetic samples.
2. **Baseline**: Run `extract_entities_and_relationships()` (current implementation) on dataset; record parse success, latency, counts.
3. **Treatment**: Run LangExtract-based extractor with same output schema on same dataset and model; record same metrics.
4. **Analysis**: Compare parse rate (H-018a), LOC (H-018b), P95 latency (H-018c); document grounding (H-018d).

### Phase 2: Reflection (Conditional)

- Run only if Phase 1 supports adoption and we choose to evaluate reflection.
- Compare LangExtract vs current reflection (DSPy + manual fallback).

## Prerequisites

- Python 3.10+
- LangExtract: `uv add langextract` or `pip install langextract`
- Local LLM (Ollama or OpenAI-compatible) used for entity extraction in config
- Dataset: real captures or E-017-style samples

## Running the Experiment

### 1. Install LangExtract

```bash
uv add langextract
# or: pip install langextract
```

### 2. Verify local LLM

Ensure the same endpoint and model as `entity_extraction` in config (e.g., LM Studio or Ollama). LangExtract supports Ollama; for other OpenAI-compatible servers, verify compatibility per LangExtract docs.

### 3. Run baseline + treatment (when implemented)

```bash
# From repo root
uv run python -m experiments.langextract_evaluation.run_comparison
```

When `run_comparison.py` is implemented it will:

- Load or generate dataset
- Run baseline (`personal_agent.second_brain.entity_extraction.extract_entities_and_relationships`)
- Run LangExtract treatment (same schema)
- Output metrics (parse rate, latency percentiles) for E-018 results document

### 4. Document results

Write findings to `docs/architecture_decisions/experiments/E-018-langextract-results.md` (hypotheses, metrics, pass/fail, recommendation).

## Directory Layout

```
experiments/langextract_evaluation/
├── README.md                 # This file
├── __init__.py
├── run_comparison.py         # (To implement) Baseline vs treatment runner
└── HYPOTHESES.md             # (Optional) Expanded hypothesis text
```

## References

- Current entity extraction: `src/personal_agent/second_brain/entity_extraction.py`
- E-017 (entity extraction model comparison): dataset and format
- ADR-0010: Structured outputs
- LangExtract: https://pypi.org/project/langextract/, https://github.com/google/langextract
