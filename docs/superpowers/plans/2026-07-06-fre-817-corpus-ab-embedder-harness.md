# FRE-817 — ADR-0112 Corpus A/B Embedder Harness (nDCG, pre-registered margin) [AC-4]

**Backing:** ADR-0112 §D4 + AC-4. Independent measurement, no seam dependency;
decides the embedder for the FRE-821 adoption ticket.

## Acceptance criteria (verbatim, ADR-0112 AC-4)

- A recorded corpus A/B artifact exists (fixed query set, nDCG@k) and the
  selected embedder is its measured winner.
- If a closed/API-only model is selected, its nDCG exceeds the best
  open-weight candidate by the pre-registered margin; else the open-weight
  spine is retained. **Fails if** there is no A/B artifact, or a closed model
  is adopted on a noise-level win.

## What already exists (reuse, do not duplicate)

- `scripts/eval/fre435_memory_recall/metrics.py::ndcg_at_k` — pure nDCG@k,
  binary gains, `None` when nothing relevant (excluded from aggregates). Reuse
  directly.
- `scripts/eval/fre435_memory_recall/probes.py::load_probe_set` /
  `ProbeCase` / `ENTITY_NS` — the fixed real-query corpus loader. **Use
  `semantic_probe.yaml` (FRE-670, 54 cases), not `bespoke_probe.yaml`.**
  Codex review flagged this: `bespoke_probe.yaml` is documented as "lexical
  masked as semantic" — the README records that 0.6B and 4B embedders
  **already score identically on it** (FRE-656), so it cannot discriminate
  embedder quality at all. `semantic_probe.yaml` is purpose-built
  vocabulary-divergent (imagery/paraphrase queries) so keyword shortcuts fail
  and the vector path must do real semantic matching — the correct fixed
  real-query set for an embedder-quality A/B.
- `scripts/eval/fre435_memory_recall/separation_benchmark.py` — prior art for
  an **offline, no-substrate** embed→cosine→rank pattern across arms
  (`_build_corpus`, `_embed_local`, `_embed_voyage`, `_score`/`_unit`,
  `_assert_vectors` fail-loud checks). This ticket's driver mirrors that
  shape but scores **nDCG@k** (ranking quality), not separation (clean-floor
  geometry) — a different question, same harness family.
- `scripts/eval/fre720_insights_separation/decision.py::decide_branch` — prior
  art for a small pure decision function consumed by a downstream ticket.
  This ticket's `decide_embedder` follows the same shape.
- Credentials already in `pass`: `seshat/AGENT_OVH_AI_BASE_URL`,
  `seshat/AGENT_OVH_EMBEDDING_TOKEN` (read at run time only, mirrors
  `separation_benchmark.py::_voyage_key`, never logged/persisted).

## Scope

New package `scripts/eval/fre817_corpus_ab_embedder/`:

1. **`decision.py`** (pure, no I/O) — the margin decision, structurally unable
   to violate AC-4's "fails if" clause:
   - `EmbedderCandidate(name: str, kind: Literal["open_weight", "closed"], mean_ndcg: float)`
     — `mean_ndcg` is **nDCG@5 only** (the pre-registered decision metric;
     codex flagged that reporting both @1 and @5 without designating one as
     authoritative makes "the measured winner" ambiguous/post-hoc if they
     disagree). @1 is still computed and reported in the artifact/writeup for
     context, but never fed into `decide_embedder`.
   - `EmbedderDecision(winner: str, winner_kind: ..., margin_cleared: bool | None, reasoning: str)`
   - `PRE_REGISTERED_MARGIN_NDCG: float = 0.05` — declared here, before any
     run. **Revised rationale** (the original ±0.012 citation was wrong — that
     figure is a cosine-space docstring observation in
     `separation_benchmark.py`, not the enforced tolerance, and cosine parity
     is a different quantity from nDCG variance anyway; codex caught this).
     Grounded instead in the probe set's own granularity:
     `semantic_probe.yaml` has 54 cases, so one case flipping moves the
     aggregate mean nDCG by ≈ 1/54 ≈ 0.019; 0.05 is comfortably wider than a
     single-case flip (≈ 2.6 cases' worth of movement), so a margin "clear"
     cannot be a one-case fluke — the "not a noise-level win" bar AC-4 asks
     for.
   - `decide_embedder(candidates, margin) -> EmbedderDecision`:
     - raises `ValueError` if `candidates` is empty (no artifact possible), if
       there is no open-weight candidate (AC-4's retained default requires
       one), or **if `margin <= 0`** (codex flagged: a caller-supplied
       zero/negative margin would let a closed model win on a tie or a loss
       while still satisfying `delta >= margin` — defense in depth on top of
       the driver never exposing a `--margin` CLI override, so the only
       margin ever used end-to-end is the pre-registered constant).
     - winner is the best open-weight candidate UNLESS a closed candidate's
       `mean_ndcg` exceeds it by `>= margin` — in which case the closed
       candidate wins and `margin_cleared=True`. This makes "a closed model
       adopted without clearing the margin" a structurally unreachable branch,
       not just a runtime check.
   - For *this* run neither candidate is closed (OVH-hosted Qwen3-Embedding-8B
     is open-weight per D4, just managed-hosted) — `margin_cleared` will
     record `None`, and the winner is simply the higher-nDCG open-weight arm.
     The closed branch is still fully implemented and unit-tested (extensible
     to a future Voyage-class contender) so the acceptance criterion is a real
     code path, not dead code.

2. **`corpus_ab.py`** — the driver + a pure scoring core:
   - `score_arm(cases, note_names, note_vecs, query_vecs, ks) -> dict[int, float | None]`
     — pure function: unit-normalizes vectors, ranks notes by cosine per
     query, namespaces retrieved ids (`entity:` prefix, matching
     `ProbeCase.relevant_ids`), calls `metrics.ndcg_at_k` per case per k,
     aggregates with `mean_optional`. No embedding I/O — directly unit-testable
     with hand-built vectors.
   - `_embed_local(texts, mode)` — reuses `generate_embeddings_batch` for the
     currently-deployed 0.6B arm (mirrors `separation_benchmark.py`).
   - `_embed_ovh(texts, mode, base_url, token)` — new: POST to
     `f"{base_url}/embeddings"` (OpenAI-compatible embeddings API), model
     `Qwen/Qwen3-Embedding-8B`; query mode prepends the Qwen instruction
     prefix client-side (`_QWEN_QUERY_PREFIX`, copied constant — matches the
     existing MLX-arm precedent in `separation_benchmark.py`), document mode
     sends the raw `"{name}: {description}"` text (matches
     `service.create_entity` production text, same as every other arm).
     Fail-loud: non-2xx raises; response cardinality is checked
     (`len(data) != len(texts)` raises — codex flagged that the existing
     Voyage/local embed helpers only ever `extend()` the response list with no
     count check, unlike the reranker path's explicit truncation guard); rows
     are re-sorted by their `index` field before extraction (never trust
     response order) — the same input-order discipline
     `parse_rerank_response` already enforces on the reranker side, just
     applied to the embeddings response; vector-length/non-zero assertions
     reuse the `_assert_vectors` pattern.
   - **Instrument sanity check for the OVH arm** (codex flagged: fail-loud
     length/non-zero checks alone can't catch a *content* mismatch — wrong
     model id, or a query-prefix the OVH endpoint doesn't interpret as
     intended — because a wrong-but-valid response still looks like a normal
     vector). Before scoring, run one fixed known-relevant/known-irrelevant
     pair (reuse the `_SANITY_QUERY` / `_SANITY_RELEVANT` /
     `_SANITY_IRRELEVANT` triple already defined in `separation_benchmark.py`
     for the reranker path) through `_embed_ovh` and assert the relevant
     text's cosine to the query beats the irrelevant text's — mirrors
     `_sanity_check`'s rank-order gate, applied to the embedding arm. Abort
     with a clear message if this fails, before spending on the full corpus.
   - `main()` — CLI: `--probe` (default `bespoke_probe.yaml`), `--out`
     (default `telemetry/evaluation/fre817-corpus-ab/`, gitignored). Builds
     the corpus once, embeds both arms, computes nDCG@1/@5 per arm via
     `score_arm`, builds `EmbedderCandidate` for each, calls
     `decide_embedder`, prints + writes a JSON run record (raw, gitignored)
     containing: probe path, per-arm nDCG@1/@5, the pre-registered margin
     value, and the decision.

3. **`run_corpus_ab.sh`** — thin wrapper (mirrors
   `run_embedder_benchmark.sh`): reads `AGENT_OVH_AI_BASE_URL` /
   `AGENT_OVH_EMBEDDING_TOKEN` from `pass show seshat/...` at run time (never
   written to disk), force-exports `AGENT_MODEL_CONFIG_PATH=config/models.yaml`
   (0.6B arm, host-reachable per the existing convention), runs a preflight
   (non-zero vector, correct 1024-dim for the 0.6B arm) before touching
   anything, then execs `corpus_ab.py`.

4. **`docs/research/2026-07-06-fre-817-corpus-ab-embedder.md`** — the durable,
   committed writeup that IS the "recorded corpus A/B artifact" AC-4 asks
   for: run record (probe set, n cases, n notes), per-arm nDCG@1/@5 table,
   the pre-registered margin (stated before interpreting results), the
   decision + one-paragraph interpretation. This is the artifact cited as
   AC-4 proof in the ticket close-out comment — the gitignored JSON under
   `telemetry/evaluation/` is the raw backing data, never committed (repo
   convention).

5. **Tests** (TDD, pure, no live network/substrate — `make test-k K=fre817`):
   - `tests/test_eval/test_fre817_margin_decision.py`:
     - two open-weight candidates, differing nDCG → winner = higher, `margin_cleared is None`.
     - a closed candidate clears the margin → winner = closed, `margin_cleared is True`.
     - a closed candidate falls short of the margin → winner = best open-weight, `margin_cleared is False`.
     - exact-margin boundary (`delta == margin`) → clears (`>=`, not `>`).
     - empty candidate list → raises.
     - all-closed, no open-weight candidate → raises (AC-4 needs a retained default).
     - `margin <= 0` (zero and negative) → raises, regardless of candidates.
   - `tests/test_eval/test_fre817_ovh_embed.py` (or inline in
     `test_fre817_corpus_ab.py`): a fake httpx transport returning
     out-of-order `index` values asserts `_embed_ovh` re-sorts to input order;
     a response with fewer rows than requested texts asserts it raises.
   - `tests/test_eval/test_fre817_corpus_ab.py`:
     - `score_arm` against 3 small hand-built cases (orthogonal-ish vectors,
       known ranking) asserts nDCG@1/@5 match hand-computed values.
     - a control case (empty `expected`) contributes `None` and is excluded
       from the mean (matches `metrics.py` convention — assert the mean isn't
       skewed).

## Known limitation (offline vs. live Neo4j retrieval path)

Codex review flagged this directly: an offline embed→cosine→rank pattern
measures embedding-geometry ranking quality, not "will the production Neo4j
HNSW path retrieve this" — `separation_benchmark.py`'s own docstring makes
exactly this distinction and only trusts cross-arm numbers after its 0.6B@1024
parity gate against the FRE-670 Neo4j calibrate medians (`_parity_check`,
Δ ≤ 0.02, already passing). This ticket's 0.6B arm reuses that same
already-validated construction (`_entity_text`, same config, same dimension),
so its offline nDCG numbers inherit that established parity. The OVH-8B arm
has no equivalent live-Neo4j reference to check against (there is no
production Neo4j index at 4096-dim today) — the offline geometry comparison
is therefore the only measurement AC-4 can practically ask for at this stage
(hitting the real HNSW path would mean standing up a second vector index at a
different dimension, which is re-embed-adjacent work, explicitly out of scope
per D6/FRE-821). **This is stated as an explicit limitation in the research
writeup**, not silently assumed away: the A/B measures embedding-quality
ranking, and production-path parity is asserted for 0.6B (inherited from
FRE-694) but not proven for OVH-8B until FRE-821's adoption seam lands.

## Explicitly out of scope

- No change to `config/substrate.yaml` / `settings.py` — this is a benchmark
  script, not runtime adoption. Wiring `managed_embedding_endpoint` to the OVH
  arm for serving traffic is FRE-821 (AC-5/AC-6), not this ticket.
- No re-embed of the production corpus.
- No change to `MemoryService` / the Neo4j vector index.

## Live run (owner-authorized one-off exception to ADR-0112 D3)

D3 says corpus-A/B jobs run off the serving host by default. The owner
explicitly authorized running this one from the current VPS session as a
one-off exception (small, cheap, real embedding calls — not a GPU batch job).
After the harness + tests are green, `run_corpus_ab.sh` is run for real
against `bespoke_probe.yaml`, and the result is written into the docs/research
writeup as the committed AC-4 artifact.

## Verification

- `make test-k K=fre817` — new pure-unit tests pass.
- `make mypy` / `make ruff-check` / `make ruff-format` clean.
- The live run produces a JSON record with non-zero, correctly-sized vectors
  for both arms (fail-loud preflight) and a `decide_embedder` verdict; the
  research doc records it.
