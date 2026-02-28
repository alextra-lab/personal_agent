# LangExtract Library Review for Personal Agent

**Date**: 2026-01-28
**Status**: Complete
**Related**: Second Brain (entity extraction), Captain's Log (reflection), ADR-0010 (Structured Outputs)
**Experiment**: E-018 (LangExtract Evaluation)

---

## Executive Summary

**LangExtract** is a Python library from Google for extracting structured information from unstructured text using LLMs. It provides schema enforcement, source grounding (character-level spans for extracted entities), multi-pass processing, and production-oriented chunking and parallelization.

**Relevance to Personal Agent**: We already perform LLM-based structured extraction in two places—**second_brain entity extraction** (conversation → entities/relationships JSON) and **Captain's Log reflection** (trace + metrics → reflection entry). LangExtract could reduce parse failures, add traceability via grounding, and simplify extraction code.

**Recommendation**: Document the review (this document), run **hypothesis-driven experiment E-018** to measure value (parse rate, code complexity, latency, optional grounding utility) before committing. Prioritize **entity extraction** as the first evaluation target; consider reflection only if E-018 shows clear benefit and local LLM compatibility is proven.

---

## 1. What is LangExtract?

### 1.1 Overview

- **Source**: Google (open-source, Apache-2.0). Available on [PyPI](https://pypi.org/project/langextract/) and [GitHub](https://github.com/google/langextract).
- **Purpose**: Turn unstructured text into structured data using LLMs, with consistent schema and optional source references.
- **Requirements**: Python 3.10+, LLM backend (Gemini, OpenAI, or **Ollama**—relevant for local SLM).

### 1.2 Key Features

| Feature | Description |
|--------|-------------|
| **Schema enforcement** | Output conforms to a declared structure; reduces malformed JSON and parse failures. |
| **Source grounding** | Each extracted item can be mapped to exact character positions in the source text (traceability, citations). |
| **Multi-pass processing** | Configurable passes to improve recall on long or complex documents. |
| **Model support** | Gemini, OpenAI, and **Ollama** (local models). Our stack uses OpenAI-compatible endpoints (LM Studio, etc.); Ollama compatibility suggests local SLM may work with appropriate configuration. |
| **Production features** | Parallel processing, chunking for large documents. |
| **Visualization** | Optional HTML output with highlighted entities (useful for debugging and demos). |

### 1.3 Typical Use Cases (from docs)

- Medical/clinical note structuring
- Contract and document analysis
- Entity and relationship extraction from text
- Literature processing (e.g., characters, plot)
- Customer feedback and email mining

Our use cases—**conversation → entities/relationships** and **trace + metrics → reflection entry**—align with “entity/relationship extraction” and “document → structured record.”

---

## 2. Fit with Personal Agent

### 2.1 Current Extraction Pipelines

1. **Second Brain: Entity Extraction** (`src/personal_agent/second_brain/entity_extraction.py`)
   - **Input**: User message + assistant response (conversation text).
   - **Output**: `{ summary, entities, relationships, entity_names }` (JSON).
   - **Method**: Single LLM call with a hand-written prompt; response parsed with `orjson`; markdown code fences stripped manually; fallback to default on parse failure.
   - **Pain points**: JSON parse failures, no source spans, custom prompt and parsing logic to maintain.

2. **Captain's Log: Reflection** (`src/personal_agent/captains_log/reflection.py`)
   - **Input**: User message, trace_id, steps_count, final_state, reply_length, telemetry summary, metrics_summary.
   - **Output**: `CaptainLogEntry` (rationale, proposed_change, supporting_metrics, impact_assessment, etc.).
   - **Method**: DSPy ChainOfThought (preferred) or manual prompt + JSON parse with fallbacks.
   - **Pain points**: Already improved by E-008 (DSPy); adding LangExtract would be a second structured-output path unless we compare or replace.

### 2.2 Where LangExtract Would Help

| Area | Benefit | Notes |
|------|---------|------|
| **Entity extraction** | Schema enforcement → fewer parse failures; optional grounding for “where did this entity come from?” | Highest leverage; no DSPy today. |
| **Entity extraction** | Multi-pass / chunking | Useful if we later process long conversations or batches. |
| **Reflection** | Alternative to DSPy or manual path with schema + grounding | Only if E-018 justifies and we want one less dependency or need grounding. |
| **Captain's Log metrics** | None | Metrics extraction is deterministic (no LLM); LangExtract not applicable. |

### 2.3 Trade-offs

| Benefit | Consideration |
|--------|----------------|
| Schema enforcement, fewer parse failures | We already use DSPy for reflection; LangExtract would be another path unless we replace or standardize. |
| Source grounding | Only useful if we expose citations or debugging UI; adds complexity if unused. |
| Multi-pass / chunking | More relevant for long documents; current flows are single-turn or moderate context. |
| Model support | Supports Ollama; we use a single OpenAI-compatible base URL—need to verify compatibility (e.g., Ollama or LM Studio). |
| Dependency | Additional Google library; need to align with governance and maintenance. |

---

## 3. Recommendations

1. **Document the review**
   - Keep this document in `docs/research/` as the single source of truth for the LangExtract review.

2. **Run a hypothesis-driven experiment (E-018)**
   - Define clear hypotheses (e.g., parse rate, code size, latency, optional grounding value).
   - Baseline: current entity extraction (and optionally reflection).
   - Treatment: LangExtract-based extraction with same schema.
   - Measure: parse success rate, lines of code, latency, and if applicable grounding usefulness.

3. **Prioritize entity extraction in E-018**
   - Entity extraction has no DSPy today and a single, clear “text → JSON” contract; easier to compare and get a clean signal.

4. **Defer reflection until E-018 results**
   - Only consider LangExtract for reflection if E-018 shows clear benefit and we want to consolidate on one structured-extraction approach or need grounding.

5. **Do not use LangExtract for metrics extraction**
   - Captain's Log metrics are intentionally deterministic and non-LLM; no fit.

---

## 4. Related Documents

- **Experiment**: [E-018: LangExtract Evaluation](../architecture_decisions/experiments/E-018-langextract-evaluation.md)
- **Implementation**: `src/personal_agent/second_brain/entity_extraction.py`, `src/personal_agent/captains_log/reflection.py`
- **Structured outputs**: ADR-0010 (Structured Outputs), E-008a (DSPy Prototype)
- **Entity extraction experiment**: E-017 (Entity Extraction Model Comparison)
- **External**: [LangExtract on PyPI](https://pypi.org/project/langextract/), [GitHub](https://github.com/google/langextract)

---

**Last Updated**: 2026-01-28
