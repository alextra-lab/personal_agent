# FRE-656 — Embedder/Reranker Quality-Ceiling Benchmark (Qwen3-4B vs 0.6B)

**Ticket:** FRE-656 (Approved → In Progress) · **Initiative:** Memory Recall Quality (FRE-435) · **ADR:** ADR-0100 (relevance-bounded recall) · **Backing:** FRE-655 A/B + floor calibration (merged, PR #271)

## Owner decision (locked, 2026-06-28)
- **Embedder:** `Qwen/Qwen3-Embedding-4B` (GGUF Q4_K_M, native **2560-dim**) replacing `Qwen3-Embedding-0.6B` (1024-dim).
- **Reranker:** `Voodisss/Qwen3-Reranker-4B` (Q4_K_M) replacing `Qwen3-Reranker-0.6B`.
- Both served from the **Mac SLM gateway** and reachable from the VPS the same way the LLMs are: `https://slm.example.com/v1`, Cloudflare-Access-gated.

## What I verified live (this session)
| Fact | Result |
|---|---|
| `GET slm.example.com/v1/models` (CF token) | lists `Qwen/Qwen3-Embedding-4B` (:8505, embeddings) + `Voodisss/Qwen3-Reranker-4B` (:8506, rerank) |
| `POST /v1/embeddings` (4B) | **dim = 2560** ✅ |
| `POST /v1/rerank` (4B) | HTTP 200, ranks cooking docs over k8s doc ✅ |
| Test substrate 7688/9201/5433 | all up ✅ |
| Local 0.6B :8503 / reranker :8504 | up (baseline path) ✅ |

## The one necessary code change (surfaced — contradicts the "no instrument change" premise)
The master/owner notes said the benchmark needs **no code change** — that assumed reaching the embedder on an unauthenticated local tunnel port. The owner then redirected to the **Access-gated** `slm.example.com`. CF-Access header injection currently lives **only** in `llm_client/client.py:400-405` (`_SLM_TUNNEL_HOSTNAME` check → `CF-Access-Client-Id/Secret` from `settings`). The two memory paths do **not** inject it:
- `memory/embeddings.py:135 _call_embeddings_api` — plain `openai.AsyncOpenAI`, no headers.
- `memory/reranker.py:98` — raw `httpx.AsyncClient`, no headers.

Both **degrade silently** on failure (zero-vector / passthrough), so against the Access challenge they would return garbage instead of erroring — the benchmark would silently measure noise. So a small, faithful CF-injection change is **required**, and it is **forward-correct**: the eventual prod 4B migration serves the embedder from the Mac via the same gateway, so this auth is a prerequisite for that migration regardless.

## Plan (revised per codex adversarial review, 2026-06-28)

Codex confirmed: the singleton trap is real (separate processes sidestep it); a **shared CF helper already exists** (`service/cf_service_token.py:20`) — reuse it, don't add one; the driver's `os.environ.setdefault` means a **stray prod `AGENT_NEO4J_URI` would survive and `ensure_vector_index` would drop+recreate the *prod* index at 2560** (catastrophic) → force-set env + hard substrate assert; the `dimensions=` param and the reranker's hardcoded 30s timeout are silent-degradation traps → preflight + detection.

### Step 1 — Inject CF-Access headers into embedding + reranker clients (TDD)
Reuse the existing `personal_agent.service.cf_service_token.cf_access_service_token_headers()`, **gated by hostname** at the call site (match `client.py`'s `slm.example.com` gate):
- `memory/embeddings.py` `_call_embeddings_api`: `headers = cf_access_service_token_headers() if _SLM_TUNNEL_HOSTNAME in endpoint else {}`; pass `default_headers=headers` to `AsyncOpenAI`. **Fix the singleton:** key `_openai_client` on `endpoint` (dict cache) so the slm endpoint actually gets a header-bearing client (today it binds once to the first endpoint).
- `memory/reranker.py`: pass `headers=` (same gated call) on the `httpx` POST.
- Add `_SLM_TUNNEL_HOSTNAME = "slm.example.com"` constant in each file (mirrors `client.py:58`; importing a private cross-module is worse).
- **Failing tests first** (`tests/personal_agent/memory/`): mocked-transport asserting CF headers are sent when endpoint is the slm host, absent for `http://embeddings:8503/v1`, absent when creds unset; plus a singleton test asserting two different endpoints get two clients.
- Verify red → green: `make test-k K=embedding` / `K=rerank`. (Leave `client.py` untouched — surgical; codex agreed.)

### Step 2 — Benchmark model config (artifact, no logic)
- `config/models.benchmark-4b.yaml` = copy of `models.cloud.yaml` with `embedding` → `id: Qwen/Qwen3-Embedding-4B`, `endpoint: https://slm.example.com/v1`; `reranker` → `id: Voodisss/Qwen3-Reranker-4B`, same endpoint. Comment notes the CF-Access gating.

### Step 3 — Committed safe runner with preflight asserts (new artifact; instrument unchanged)
`scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh` — encodes the safety codex demanded, so the run is reproducible and prod-safe:
- **Force-export** (not setdefault) all `_TEST_SUBSTRATE_ENV` keys + `AGENT_MODEL_CONFIG_PATH` + `AGENT_EMBEDDING_DIMENSIONS` + test neo4j creds, so no stray value survives.
- **Preflight python guard** before any run: assert `settings.neo4j_uri` ends `:7688`; `generate_embedding("probe", mode="query")` returns **non-zero** and `len(vec) == settings.embedding_dimensions` (catches the `dimensions=`/auth/native-width traps); echo a header line {model id, endpoint, dims, neo4j_uri}. Exit non-zero on any mismatch — never seed against a misconfigured substrate.
- Runs **one CLI process per (embedder × mode)** — sidesteps the module-global `_openai_client` / `settings` / `lru_cache` traps.

### Step 4 — Run the A/B on the TEST substrate (instrument unchanged)
Same FRE-489 probe, same harness as FRE-655 — apples-to-apples:
- **0.6B baseline** (same-session control): `models.cloud.yaml`, `AGENT_EMBEDDING_DIMENSIONS=1024`, `--mode calibrate` then `--mode ab`.
- **4B**: `models.benchmark-4b.yaml`, `AGENT_EMBEDDING_DIMENSIONS=2560`, `--mode calibrate` then `--mode ab`.
- **`--distractor-background 0` for the calibrate runs** — codex: `fetch_live_distractors` reads live Neo4j (7687) and drifts between runs; the co-seeded calibration is the primary, drift-free separation metric. The `ab` recall pass records the distractor-background it used.
- `ensure_vector_index()` auto-drops+recreates the **test** index on the 1024→2560 mismatch — no manual step (guarded by the :7688 assert above).

### Step 5 — Latency probe (standalone, no instrument change)
- Small timing script: N serial `generate_embedding(mode="query")` + N `rerank()` through the prod client path against the 4B; report p50/p95. Watch for reranker passthrough (hardcoded 30s timeout, `reranker.py:98`) — flag if any call degrades. **Caveat recorded:** this is the **Mac-GPU-via-tunnel** steady-state path, *not* VPS-CPU. The 4B is GPU-served, so the ticket's original "VPS-CPU latency" framing does not apply to the locked decision; VPS-CPU-fallback latency (laptop-offline) is the separate, deferred tiered-embedding question.

### Step 6 — Floor proposal + recommendation writeup
- Feed the 4B separated distributions to `calibration.propose_floor` → candidate `recall_similarity_floor`.
- `docs/research/2026-06-28-fre-656-embedder-benchmark.md`: 4B-vs-0.6B separation table (positive/negative cosine ranges + gap), recall@5, latency p50/p95, RAM/resource note, the floor proposal, and the **keep / upgrade-local / cloud** recommendation with the privacy + hot-path-latency trade explicit. Hand the floor back to FRE-655 for rollout.
- Raw JSON artifacts: curated summary committed; no raw dumps in git (per policy).

## Acceptance criteria → proof (master gate input)
| Criterion (from ticket + FRE-655 residual) | Proof |
|---|---|
| 4B **separates** the positive/negative cosines the 0.6B overlapped (baseline pos 0.655–0.874 / neg 0.625–0.807) | both `calibrate-*.json` + computed separation gap in the writeup |
| recall@5 maintained/improved | both `ab-*.json` |
| latency measured on the real (gateway) path | latency-probe output, p50/p95, with the GPU-via-tunnel caveat |
| floor proposed on separated scores | `propose_floor` output in the writeup |
| recommendation (keep/upgrade-local/cloud) w/ privacy + latency trade | the research doc |
| embedding+reranker reach slm host authenticated | unit tests (CF headers injected) + the live 200s observed this session |

## Out of scope (follow-on)
- **No prod embedder swap / prod KG re-embed** (one-way door) — that is FRE-655 rollout + its own follow-on after this recommendation.
- Tiered laptop-GPU-primary / VPS-CPU-fallback architecture — deferred contingency (owner, 2026-06-28), gated on this benchmark.

## Risk tier: **Standard** (touches `src/personal_agent/memory/` + a security-sensitive auth header) → codex plan-review + owner approval before coding.
