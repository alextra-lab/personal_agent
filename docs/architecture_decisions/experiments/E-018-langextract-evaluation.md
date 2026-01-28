# E-018: LangExtract Evaluation

**Status**: Planned  
**Date Created**: 2026-01-28  
**Phase**: 2.2 (Memory & Second Brain) / Framework Adoption  
**Related Research**: [LangExtract Library Review](../../research/langextract_library_review_2026-01-28.md)  
**Related ADRs**: ADR-0010 (Structured Outputs), E-017 (Entity Extraction Model Comparison), E-008a (DSPy Prototype)

---

## Objective

Determine methodically whether Google's **LangExtract** library adds value for structured extraction in Personal Agent by testing **hypotheses** with baseline-vs-treatment experiments and clear success criteria.

---

## Hypotheses

### H-018a: Entity Extraction Parse Reliability

**Claim**: Using LangExtract for second_brain entity extraction will **reduce parse failures** compared to the current prompt + manual JSON parse approach.

- **Metric**: Parse success rate (valid schema-compliant output) on a fixed dataset.
- **Baseline**: Current `extract_entities_and_relationships()` (manual prompt + orjson).
- **Treatment**: LangExtract-based extraction with the same output schema (summary, entities, relationships).
- **Success**: LangExtract parse rate ≥ baseline and statistically non-inferior; or ≥10% absolute improvement if baseline &lt;95%.

### H-018b: Entity Extraction Code Complexity

**Claim**: LangExtract will **reduce code size and parsing boilerplate** for entity extraction while preserving behavior.

- **Metric**: Lines of code (and/or cyclomatic complexity) for extraction + parsing logic.
- **Baseline**: `entity_extraction.py` (prompt construction, response handling, fence stripping, orjson, fallback).
- **Treatment**: LangExtract schema + extract call; no manual fence stripping or try/except parse.
- **Success**: ≥20% reduction in extraction-related LOC (excluding shared types/config).

### H-018c: Entity Extraction Latency

**Claim**: LangExtract will **not materially increase** end-to-end extraction latency (same model, same hardware).

- **Metric**: P95 latency (ms) per extraction call.
- **Baseline**: Current entity extraction (single LLM call).
- **Treatment**: LangExtract extraction (single pass, same model).
- **Success**: Treatment P95 ≤ baseline × 1.15 (≤15% overhead).

### H-018d: Source Grounding Utility (Optional)

**Claim**: Character-span grounding for extracted entities **adds traceability value** (e.g., debugging, future citation UI).

- **Metric**: Qualitative assessment + feasibility (can we get spans without breaking existing API?).
- **Method**: Implement grounding in LangExtract variant; review usefulness for second_brain and Captain's Log.
- **Success**: Grounding is obtainable and at least one concrete use case (debugging or citation) is documented.

### H-018e: Reflection Pipeline (Conditional)

**Claim**: If H-018a–c are positive, LangExtract could **optionally** improve or simplify Captain's Log reflection (schema enforcement, fewer parse failures) as an alternative or complement to DSPy.

- **Scope**: Deferred until after entity extraction results; run only if E-018 Phase 1 (entity extraction) succeeds and we want to compare reflection paths.
- **Metric**: Parse rate, code size, latency vs current DSPy + manual fallback.

---

## Method

### Phase 1: Entity Extraction (Primary)

1. **Environment**
   - Same LLM backend as production (e.g., LM Studio / Ollama) and same model used for entity extraction (e.g., qwen3-8b or lfm2.5-1.2b per config).
   - Install: `pip install langextract` (Python 3.10+).
   - Verify LangExtract works with local endpoint (Ollama or OpenAI-compatible).

2. **Dataset**
   - Reuse or subset E-017 dataset: 50–100 conversation pairs (user_message, assistant_response) from real captures or synthetic but realistic samples.
   - Ensure variety: short/long, single/multiple entities, edge cases (empty, markdown, code snippets).

3. **Baseline**
   - Run current `extract_entities_and_relationships()` on dataset.
   - Record: parse success (yes/no), latency per call, entity/relationship counts.
   - Compute: parse rate, P50/P95 latency.

4. **Treatment**
   - Implement LangExtract-based extractor with **same** output schema (summary, entities, relationships, entity_names).
   - Run on same dataset, same model.
   - Record: parse success, latency, entity/relationship counts; if supported, grounding spans.
   - Compute: parse rate, P50/P95 latency.

5. **Analysis**
   - Compare parse rate (H-018a): proportion test or McNemar if paired.
   - Compare LOC (H-018b): count extraction+parse code only.
   - Compare P95 latency (H-018c): non-inferiority or equivalence test.
   - Document grounding (H-018d): how to get spans, one use case.

### Phase 2: Reflection (Conditional)

- Only if Phase 1 supports adoption and we decide to evaluate reflection.
- Compare LangExtract vs current reflection path (DSPy + manual fallback) on parse rate, code size, latency.
- Document in E-018 results; decision in ADR or TECH debt doc.

### Deliverables

1. **E-018 results document** (`docs/architecture_decisions/experiments/E-018-langextract-results.md`): hypotheses, metrics, pass/fail, recommendation.
2. **Experiment code**: `experiments/langextract_evaluation/` (scripts to run baseline + treatment, aggregate metrics).
3. **Optional**: Minimal LangExtract-based entity extractor (branch or spike) for comparison.

---

## Success Criteria and Decision

| Hypothesis | Pass | Fail / Inconclusive |
|------------|-----|---------------------|
| H-018a Parse reliability | Parse rate ≥ baseline (or +10% if baseline &lt;95%) | No improvement or regression |
| H-018b Code complexity | ≥20% LOC reduction | &lt;20% or increased complexity |
| H-018c Latency | P95 ≤ baseline × 1.15 | &gt;15% overhead |
| H-018d Grounding | Feasible + 1 use case documented | Not feasible or no clear use |

**Decision rules**:

- **Adopt for entity extraction**: H-018a and H-018c pass; H-018b pass preferred.
- **Defer**: H-018a or H-018c fail; or local LLM integration blocked.
- **Revisit reflection**: Only if we adopt for entity extraction and want to consolidate structured extraction (E-018e).

---

## Dependencies and Prerequisites

- **E-017** (Entity Extraction Model Comparison): Shared dataset or capture format can be reused.
- **ADR-0010**: Defines structured output expectations; LangExtract schema must align.
- **Local LLM**: Same endpoint and model as used for entity extraction in config.

---

## References

- Research: [LangExtract Library Review](../../research/langextract_library_review_2026-01-28.md)
- Implementation: `src/personal_agent/second_brain/entity_extraction.py`
- Experiment harness: `experiments/langextract_evaluation/`
- External: [LangExtract PyPI](https://pypi.org/project/langextract/), [GitHub](https://github.com/google/langextract)
