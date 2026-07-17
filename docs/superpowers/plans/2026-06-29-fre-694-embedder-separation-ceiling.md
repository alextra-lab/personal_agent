# FRE-694 — Embedder separation ceiling: 0.6B-f16 vs 8B-f16 (+ Voyage) on the FRE-670 probe

**Ticket:** FRE-694 (Approved, Tier-1:Opus, "Memory Recall Quality"). Continuation of FRE-670/656,
same build session (context kept). **Backing:** ADR-0087 §D (recall measurement) + ADR-0100 (the
floor that separation gates) + ADR-0099 (single-source config). **Branch:** `fre-694-embedder-separation-ceiling`.
Eval + docs only — no production deploy, no prod-embedder swap.

## The question (binary, decisive)

Can a higher-quality embedder open a **clean floor** on the FRE-670 probe — a cosine cutoff that
separates true matches (positives) from no-record (negatives) — or do even the best embedders leave
the distributions overlapping? FRE-670 found overlap (0.6B positives median ~0.78, negatives up to
~0.79; the 4B arm no better) **but that 4B arm ran at Q4**, which perturbs the fine cosine geometry
separation depends on — so "size doesn't help separation" is precision-confounded and unconfirmed.
This ticket re-tests at **f16 across the board** so size is the only variable.

- If a clean floor opens → the embedder is the lever; a re-embed is justified.
- If even the ceiling overlaps → the embedder is not the lever; the fix is downstream (reranker / retrieval).

## Acceptance criteria (definition of done)

- **AC1 — separation per arm** on the FRE-670 probe (test-isolated): positive vs negative cosine
  distributions (min/median/max), **overlap counts** (negatives above the lowest positive; positives
  below the highest negative), and the recall-vs-false-positive tradeoff at a swept floor. Recall@1 +
  register delta are secondary.
- **AC2 — dimension sweep** per MRL-capable arm (256 / 512 / 1024 / native): does adding dimensions
  help separation on our data, or just saturate?
- **AC3 — config drift fixed** (ADR-0099): a benchmark config whose arm label, model id, precision,
  and dimension match the *served* model (8B f16, 4096-dim) — the stale 4B-q4 config is corrected.
- **AC4 — verdict + recommendation:** a clear yes/no on whether any arm opens a clean floor, feeding
  the FRE-655 floor calibration and the embedder/re-embed decision.
- **AC5 — curated research doc** (no PII); raw run JSON gitignored.

## Verified facts (probed 2026-06-29)

- **0.6B is f16** — served `Qwen3-Embedding-0.6B-f16.gguf` on the prod embedder (:8503), 1024-dim.
  No re-run needed for precision.
- **8B is f16, native 4096** — `Qwen/Qwen3-Embedding-8B`, llamacpp, port 8505, quantization f16, on
  the Access-gated `slm.example.com` tunnel. Precision confound eliminated.
- **Voyage is reachable** — the key lives in the `pass` store (`pass show VOYAGEAI_API_KEY`, not
  `.env`, which is why it first read as absent). `voyage-4-large` confirmed live, **native dim 1024**.
- `generate_embedding` hardcodes `api_key="unused"` and sends `dimensions=`; Voyage needs a bearer
  key and `output_dimension` → the Voyage arm needs a dedicated eval-only path.
- **Native dims:** 0.6B = 1024, 8B = 4096, voyage-4-large = 1024.

## Design

### D1 — Offline cosine separation harness (no substrate)

Separation is purely cosine geometry between query and note embeddings — no graph, no recency gate.
The FRE-670 `calibrate` mode measured it through the Neo4j vector index, which is **single-dimension**:
a dim sweep (AC2) would require dropping/recreating the index per dim per arm, and a cloud arm (Voyage)
does not fit the seed-into-Neo4j model. So the harness computes cosines **offline** (embed → truncate
→ renormalize → cosine), touching **no substrate at all** — strictly satisfying "never the live prod
KG." It **reuses** `probes.load_probe_set`, `calibration.propose_floor`/`sweep_floor` (the floor math),
and `embeddings.cosine_similarity`. *(Deviation from the literal "reuse calibrate mode" — flagged for
owner approval; the Neo4j path cannot do the dim sweep.)*

### D2 — Arms

- **0.6B-f16** — `generate_embedding` via `config/models.yaml` (prod embedder :8503), native 1024.
- **8B-f16** — `generate_embedding` via a **new** `config/models.benchmark-8b.yaml` (slm tunnel :8505,
  `Qwen/Qwen3-Embedding-8B`, native 4096), CF-Access token already injected for that host.
- **Voyage voyage-4-large** — dedicated eval-only call (`httpx` POST to the Voyage API, `input_type`
  query/document, `output_dimension` for the sweep). Bearer key read from `pass show VOYAGEAI_API_KEY`
  at run time (never written to disk, never logged). Native 1024. (Optional Gemini arm deferred unless
  the owner wants it.)

### D3 — Matryoshka dim sweep (server-side where available, else MRL client-trunc)

Probed (2026-06-29): **llama.cpp ignores the OpenAI `dimensions=` param** (returns native 1024/4096
regardless), so the Qwen arms reduce **client-side** — first-N components + **L2 renormalize** (valid
for MRL-trained Qwen3-Embedding). **Voyage honors `output_dimension`** server-side (its own MRL), so
the Voyage sweep requests each dim server-side (its native is **2048**, not 1024 — the 1024 my first
probe saw was Voyage's *default*, not native). Per-arm native: 0.6B 1024, 8B 4096, Voyage 2048; common
sweep dims 256/512/1024 plus each arm's native. The run record states, per arm, whether reduction was
server-side or client-side (codex axis 3 — they are each the *provider's* intended MRL reduction).

**Fail-loud (codex axis 3/4):** set `AGENT_EMBEDDING_DIMENSIONS=native` per Qwen arm, and **assert each
returned vector is non-zero and exactly the expected native length before scoring** — never let
`generate_embedding`'s zero-vector fallback or `cosine_similarity`'s length-mismatch-returns-0.0 path
silently corrupt an arm (e.g. a stale `2560` env leak).

### D4 — Separation metric (the headline)

Per arm × dim, over the FRE-670 probe:
- **positive cosine** = query ↔ each of its true notes, scored **per expected entity** (NOT max over a
  compound case — codex axis 2: max hides a failed supporting fact; every true entity that must surface
  has to clear the floor, so each is its own positive sample).
- **negative cosine** = for every query (positives *and* controls), the strongest cosine to a
  *non-expected* note (the hardest distractor the floor must reject).
- **overlap counts**: `neg_above_min_pos` and `pos_below_max_neg` (zero on both = a clean floor), plus
  **robust percentiles** (p5 positive vs p95 negative) because n=44+10 makes extrema outlier-sensitive
  (codex axis 2); the report lists the specific outlier cases alongside min/median/max.
- **sweep** via `calibration.sweep_floor` (recall / false-positive at each candidate floor) + `propose_floor`.

**Instruction handling (codex axis 2/4):** each arm runs in its *native* retrieval mode — Qwen gets the
`Instruct: …Query:` prefix on queries (raw documents) via `generate_embedding` query-mode; Voyage uses
`input_type=query|document`. So an arm difference blends model quality and provider prompt template; the
verdict is framed as **"best native retrieval mode per arm,"** explicitly not instruction-controlled.

### D6 — Instrument validation (codex axis 1)

The offline harness answers *"does the exact embedding geometry contain a clean floor?"*, **not** *"will
the production Neo4j HNSW path retrieve at that floor?"* — stated as a caveat. To trust the new harness,
a **parity check**: run 0.6B at native 1024 offline and confirm its positive/negative cosine
medians match the FRE-670 `calibrate` output (pos median ~0.776, neg max ~0.792) before trusting
cross-arm numbers.

### D5 — Config drift fix (ADR-0099)

Add `config/models.benchmark-8b.yaml` (8B f16, 4096) as the truthful benchmark config; annotate/retire
`config/models.benchmark-4b.yaml` as precision-confounded (do not delete — provenance). Update
`run_embedder_benchmark.sh` to add the `8b` arm + a `separation` mode that drives the new harness.

## Plan (atomic steps, TDD)

1. **Pure helpers + tests** — `tests/evaluation/test_fre694_separation.py` (RED): `truncate_renormalize`
   (first-N + L2, zero-safe + raises on a zero/short vector), `overlap_counts(positives, negatives)`,
   `percentile`, and the per-arm separation aggregation (per-expected-entity positives; top-non-match
   negatives). Implement in `scripts/eval/fre435_memory_recall/separation_report.py`. *(No embedder —
   synthetic vectors.)*
2. **Offline harness** — `scripts/eval/fre435_memory_recall/separation_benchmark.py`: load the FRE-670
   probe; embed notes (document) + queries (query) for the selected arm; **assert each vector is
   non-zero and the expected native length** (fail loud); sweep dims (Qwen client first-N+renorm,
   Voyage server-side `output_dimension`); compute D4 metrics; reuse `propose_floor`/`sweep_floor`;
   log a per-arm run record (model id, endpoint, requested/returned dim, mode, reduction path, CF
   headers active); write gitignored JSON + print the separation table. Arms: `--arm 0.6b|8b|voyage`.
3. **Config + wrapper** — `config/models.benchmark-8b.yaml` (8B f16, 4096); `run_embedder_benchmark.sh`
   gains the `8b` embedder (DIMS=4096) + a `separation` mode; annotate `models.benchmark-4b.yaml` as
   precision-confound-retired (keep for provenance — ADR-0099).
4. **Parity check + run arms** (test-isolated, no substrate writes): first validate 0.6B@1024 offline
   medians against the FRE-670 calibrate output (D6); then run 0.6B-f16, 8B-f16, and Voyage across the
   sweep. **Verdict** = clean floor (zero overlap)? per arm × dim.
5. **Research doc** — `docs/research/2026-06-29-fre-694-embedder-separation.md` (curated aggregates,
   no PII): separation tables, overlap verdict, recommendation feeding FRE-655 + the re-embed decision.
6. **Quality gates** — `make test` · `make mypy` · ruff · pre-commit. PR.

## Risks / halt conditions

- 8B endpoint + Voyage are remote — read-only embedding inference; only the *paraphrased* committed
  probe (no PII) is sent. The Voyage key is read from `pass` at run time, never persisted or logged.
- recall@5 saturates — separation (overlap + floor sweep) is the discriminator, not recall.
- If even the 8B/Voyage ceiling overlaps as badly as 0.6B, that is a *real result* (embedder is not
  the lever), not a failure — report it; do not hunt for a separating config.

## Out of scope

- Any production embedder swap / KG re-embed (one-way door; FRE-655/656 own the decision this feeds).
- The reranker arm (downstream lever) — a distinct end-to-end measurement (the FRE-670 follow-up).
