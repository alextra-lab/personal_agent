# Pre-Eval Infrastructure: Embedding Migration, Reranker, & Naming Convention

## Context

Slice 3 implementation (Chunks 1–9) is complete and committed. Before running evaluation tests, we identified four infrastructure gaps:

1. **Embeddings hardcoded to OpenAI** — `memory/embeddings.py` calls `api.openai.com` with `text-embedding-3-small` (1536d). We chose **Qwen3-Embedding-0.6B** (768d) via slm_server as the embedding model. This is a one-way door — model cannot change without rebuilding all vectors.
2. **No reranker in retrieval pipeline** — hybrid search returns candidates scored by keyword + graph + vector, but no cross-attention reranking. **Qwen3-Reranker-0.6B** is now deployed on slm_server port 8504.
3. **Neo4j vector index** — `ensure_vector_index()` is never auto-called; needs wiring into startup with 768d.
4. **Eval naming convention** — current runs use inconsistent naming. Adopting `EVAL-{NN}-{slug}` continuing from EVAL-08.

slm_server is already updated:
- Embeddings: `Qwen/Qwen3-Embedding-0.6B` on port **8503** (`/v1/embeddings`)
- Reranker: `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` on port **8504** (`/v1/rerank`)

---

## Step 1: Add embedding & reranker models to config/models.yaml + update settings.py

### 1a. config/models.yaml — model identity (follows ADR-0031 pattern)

Add new model entries alongside existing LLM models:

```yaml
  # ── Embedding Model ─────────────────────────────────────────────
  embedding:
    id: "Qwen/Qwen3-Embedding-0.6B"
    provider_type: "local"
    endpoint: "http://localhost:8503/v1"
    context_length: 32768
    max_concurrency: 1
    default_timeout: 60

  # ── Reranker Model ──────────────────────────────────────────────
  reranker:
    id: "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
    provider_type: "local"
    endpoint: "http://localhost:8504/v1"
    context_length: 8192
    max_concurrency: 1
    default_timeout: 120
```

### 1b. settings.py — runtime knobs only (not model identity)

**File:** `src/personal_agent/config/settings.py` (~line 300)

Changes:
- `embedding_model` → remove (moves to models.yaml)
- `embedding_dimensions`: default `768` (was 1536)
- `embedding_batch_size`: keep as-is (runtime knob)
- Add `reranker_enabled: bool` — default `True`
- Add `reranker_top_k: int` — default `10` (re-score top K candidates)
- Update descriptions to reflect Qwen3/slm_server (remove OpenAI references)

### 1c. .env.example — add embedding/reranker section

```bash
# =============================================================================
# EMBEDDING & RERANKER (Seshat Memory)
# =============================================================================
# Embedding vector dimensions (must match model output; 768 for Qwen3-Embedding-0.6B)
# AGENT_EMBEDDING_DIMENSIONS=768

# Max items per embedding API call
# AGENT_EMBEDDING_BATCH_SIZE=20

# Enable reranker in memory query pipeline
# AGENT_RERANKER_ENABLED=true

# Number of top candidates to re-score
# AGENT_RERANKER_TOP_K=10
```

**Model identity** (which model, which endpoint) comes from `config/models.yaml`.
**Runtime params** (dimensions, batch size, reranker toggle) come from `.env` via settings.
**No `openai_api_key` dependency for embeddings** — local provider doesn't need an API key.

---

## Step 2: Rewrite embeddings.py — route to slm_server via models.yaml config

**File:** `src/personal_agent/memory/embeddings.py`

Changes:
- Remove `EmbeddingProvider` enum (dead code, no longer needed)
- Load model config from `config/models.yaml` `embedding` entry (id + endpoint)
- `_call_openai_embeddings()` → use `embedding.endpoint` as `base_url` for `openai.AsyncOpenAI`
- Remove `api_key` requirement — pass `api_key="unused"` (llama.cpp doesn't check it, but the OpenAI client requires a non-empty string)
- Remove `dimensions=` parameter from API call (Qwen3-Embedding handles this natively, not an OpenAI truncation feature)
- Remove the `if not settings.openai_api_key` guard in `generate_embedding()` — no API key needed for local
- Add instruction prefix support: for entity text, prefix with `"Instruct: Given a document, retrieve relevant passages\nQuery: "`, for query text, prefix with `"Instruct: Given a query, retrieve relevant passages\nQuery: "` (Qwen3-Embedding instruction format — verify exact format from HuggingFace model card)
- Update docstrings to reflect Qwen3/slm_server

The `generate_embedding()` signature gains an optional `mode: Literal["document", "query"] = "document"` parameter. Callers:
- `service.py:create_entity()` calls with default `"document"`
- `service.py:query_memory()` calls with `mode="query"`

**How model config is loaded:** Follow the same pattern used by `llm_client/` for loading model definitions from `config/models.yaml`. The embedding entry provides `id` and `endpoint`; dimensions come from `settings.embedding_dimensions`.

---

## Step 3: Add reranker module

**New file:** `src/personal_agent/memory/reranker.py`

Implements:
```python
async def rerank(
    query: str,
    documents: list[str],
    top_k: int | None = None,
) -> list[RerankResult]:
    """Re-score documents using cross-attention reranker.

    Calls slm_server's /v1/rerank endpoint (OpenAI-compatible).
    Returns results sorted by relevance score descending.
    """
```

Loads reranker config from `config/models.yaml` `reranker` entry (id + endpoint).
Uses `httpx.AsyncClient` to call `reranker.endpoint + "/rerank"` with:
```json
{
  "model": reranker.id,
  "query": query,
  "documents": documents
}
```

Returns `list[RerankResult]` where `RerankResult` is a frozen dataclass with `index: int`, `score: float`, `document: str`.

Graceful degradation: if reranker is down or `reranker_enabled=False`, return documents in original order with default scores. Log warning, don't crash.

---

## Step 4: Wire reranker into query_memory

**File:** `src/personal_agent/memory/service.py` (~line 645-670)

After the existing hybrid scoring (keyword + graph + vector), and before returning `MemoryQueryResult`:

1. If `settings.reranker_enabled` and `query_text` and `len(conversations) > 1`:
2. Build document strings from conversations: `f"{c.summary or c.user_message}"` for each
3. Call `rerank(query=query_text, documents=docs, top_k=settings.reranker_top_k)`
4. Use reranker scores to re-weight the final relevance scores (blend with existing multi-factor score)
5. Emit telemetry: `reranker_applied` event with `candidate_count`, `top_k`, `duration_ms`

Weight redistribution when reranker active:
- Recency: 0.20 (was 0.30)
- Entity match: 0.20 (was 0.30)
- Entity importance: 0.10 (was 0.15)
- Vector similarity: 0.15 (was 0.25)
- Reranker score: 0.35 (new)

---

## Step 5: Wire ensure_vector_index() into startup

**File:** `src/personal_agent/service/app.py` (lifespan function, ~line 122-173)

Add call to `memory_service.ensure_vector_index()` during app startup, after Neo4j connection is established. This ensures the 768d vector index exists on every boot. The DDL uses `IF NOT EXISTS` so it's idempotent.

---

## Step 6: Update tests

### 6a. Update existing embedding tests
**File:** `tests/personal_agent/memory/test_embeddings.py`

- Update mocks to reflect new `base_url` approach (no API key guard)
- Test instruction prefix: `mode="document"` adds `"document: "` prefix, `mode="query"` adds `"query: "` prefix
- Update expected dimensions from 1536 → 768

### 6b. Add reranker tests
**New file:** `tests/personal_agent/memory/test_reranker.py`

- Test successful rerank call (mock httpx)
- Test graceful degradation when reranker is down
- Test `reranker_enabled=False` bypasses call
- Test empty document list returns empty

### 6c. Update hybrid search tests
**File:** `tests/personal_agent/memory/test_hybrid_search.py`

- Add tests for reranker weight redistribution
- Test that reranker failure falls back to existing weights

### 6d. Update settings tests (if any)
- Verify new settings fields have correct defaults

---

## Step 7: Establish eval naming convention

### 7a. Rename existing eval result directories
**Directory:** `telemetry/evaluation/`

Current → New:
- `run-foundation-baseline/` → `EVAL-01-foundation-baseline/`
- `run-02-subagent-fix/` → `EVAL-02-subagent-fix/`
- `run-03-three-fixes/` → `EVAL-03-three-fixes/`
- `run-04-fixes-and-searchxng/` → `EVAL-04-fixes-searchxng/`
- `eval-03-memory-promotion/` → `EVAL-05-memory-promotion/`
- `eval-04-context-budget/` → `EVAL-06-context-budget/`
- `graphiti/` → `EVAL-07-graphiti-experiment/`

(Note: this re-numbers from 01 for the telemetry results, separate from the EVAL-0N research docs which track findings, not runs)

### 7b. Update harness run.py output path
**File:** `tests/evaluation/harness/run.py`

Update the CLI to default output directory naming to `EVAL-{NN}-{slug}` pattern, or accept `--run-id` parameter.

---

## Step 8: Verify end-to-end

1. `uv run pytest tests/personal_agent/memory/test_embeddings.py -v` — embedding tests pass
2. `uv run pytest tests/personal_agent/memory/test_reranker.py -v` — reranker tests pass
3. `uv run pytest tests/personal_agent/memory/test_hybrid_search.py -v` — hybrid search tests pass
4. `uv run pytest tests/personal_agent/ -v` — all unit tests pass
5. `uv run mypy src/personal_agent/memory/` — type check
6. `uv run ruff check src/personal_agent/memory/ src/personal_agent/config/` — lint
7. Manual smoke test: start services, hit `/chat` endpoint, confirm embedding telemetry shows Qwen3 model and 768d vectors

---

## Files Modified

| File | Change |
|------|--------|
| `config/models.yaml` | Add embedding + reranker model entries |
| `.env.example` | Add embedding/reranker runtime params section |
| `src/personal_agent/config/settings.py` | Update embedding defaults (768d), add reranker_enabled/top_k, remove embedding_model |
| `src/personal_agent/memory/embeddings.py` | Route to slm_server, instruction prefixes, remove OpenAI deps |
| `src/personal_agent/memory/reranker.py` | **New** — reranker client |
| `src/personal_agent/memory/service.py` | Wire reranker into query_memory, ensure_vector_index on startup path |
| `src/personal_agent/service/app.py` | Call ensure_vector_index() in lifespan |
| `tests/personal_agent/memory/test_embeddings.py` | Update for new config/behavior |
| `tests/personal_agent/memory/test_reranker.py` | **New** — reranker tests |
| `tests/personal_agent/memory/test_hybrid_search.py` | Add reranker weight tests |
| `tests/evaluation/harness/run.py` | Naming convention support |
| `telemetry/evaluation/` | Rename existing run directories |

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Embedding model | Qwen3-Embedding-0.6B (768d) | Higher MTEB than nomic (64.33 vs ~62), instruction-aware, Matryoshka, Qwen ecosystem |
| Embedding server | slm_server port 8503 | Already deployed, OpenAI-compatible API |
| Reranker | Qwen3-Reranker-0.6B | Companion to embedding model, GGUF available, served via /v1/rerank |
| Reranker server | slm_server port 8504 | Already deployed |
| Vector dimensions | 768 | Good quality/size balance for entity text |
| Eval naming | EVAL-{NN}-{slug} | Continues from eval phase, single sequence |
| Neo4j clear | Not needed | Changes are additive (IF NOT EXISTS, SET properties) |
| ES index rebuild | Not needed | dynamic: true handles new events |
