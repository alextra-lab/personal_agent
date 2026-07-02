# FRE-724 — Multi-path recall: assemble the pipeline and prove it live behind a flag

**Ticket:** FRE-724 (Approved → In Progress) · **Backing:** ADR-0104 (seam owner), design spec
`docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md`, operating point `docs/specs/RECALL_OPERATING_POINT_DECISION.md`
**Depends (all merged):** FRE-722 (`memory/fusion.py`, RRF+dedup) · FRE-723 (lexical + multi-query arms) · FRE-706 (operating-point sign-off)
**Branch:** `fre-724-multi-path-recall-seam`

---

## 1. What this ticket delivers

Assemble the arms + fusion + rerank-on-the-fused-set into live recall **behind a default-off flag**,
and prove ADR-0104 **AC-1, AC-3, AC-5, AC-6** hold with the flag on. ADR-0104 does not reach
Implemented until this ships and master proves the ACs live.

The pieces already exist in isolation:
- `memory/fusion.py` — `reciprocal_rank_fusion()` + `dedup_arm_ranking()` (pure; FRE-722).
- `lexical_recall_arm()`, `multi_query_recall_arm()`, `_dense_vector_search_ranked()` — arms returning
  `list[RankedResult]`, flag-dark (FRE-723).
- `_select_rerank_candidates()` + `reranker_input_cap=25` — rerank-input bounding, today **only** inside
  `query_memory()` (FRE-672/696).
- Operating-point values — `recall_similarity_floor=0.60` signed off (FRE-706).

This ticket builds the **shared fuse→rerank→operating-point core** they feed, wires the three siloed
recall paths through it, and proves the tail-case win.

## 2. Scope interpretation (codex-reviewed 2026-07-02)

"Converge the entity-name and proactive paths onto the same shared core" is read as: **the shared core
is the fusion + rerank-on-fused-set + input-cap + operating-point machinery** (spec §6: "generalize the
existing reranker-input-cap selection helper … into the shared core, selecting by fused rank"). Each
path assembles its own arms and calls the one shared core; each adapts the fused+reranked output back to
its own return shape. This is the spec's "cheap once the core exists." The **broad/MEMORY_RECALL path is
the primary AC-proof surface** (where the "no prior discussions" symptom lives and which does not rerank
today).

**All three paths fully converge — no fallback.** (Codex finding 1: a "share only the rerank-selection
helper" fallback would leave `query_memory` single-path, contradicting Build 3's "not left single-path."
So each path runs the multi-arm core — dense + lexical + multi-query — behind the flag; none is left
dense-only.)

**v1 arm set = dense + lexical + multi-query** (spec §2). Structural/graph deferred to FRE-707
(`structural_recall_arm` returns `EntityNode`, not `RankedResult` — not in the v1 fusion interface).

## 3. Acceptance criteria carried (ADR-0104) → how proven

| AC | Statement | Proof in this PR |
|----|-----------|------------------|
| **AC-1** | ≥2 independent arms, fused by RRF **rank** (not score) | core runs dense+lexical+multi_query; emits `arms_ran`; fusion via `reciprocal_rank_fusion` (rank-only). Test: recall trace lists ≥2 arms; unit test that the core calls `reciprocal_rank_fusion`. |
| **AC-3** | Lived-tail win: an OOV-for-dense item present with flag **on**, absent with flag **off** | integration test on the test substrate: a Turn/Entity whose token is out-of-vocab for the dense arm but a lexical/paraphrase hit — present multipath-on, absent multipath-off. |
| **AC-5** | No arm/fused/reranked set filtered-to-empty on a score threshold; reranker orders, never gates | core returns material whenever fused set non-empty after noise guard; reranker reorders only. Test: a below-rerank-score fused set still returns all items (no drop-to-empty). |
| **AC-6** | (a) `fused_set_size ≤ cap`; (b) numeric ceiling (17s, in spec); (c) measured p50 ≤ 17s | (a) core caps to `reranker_input_cap` before rerank + asserts in telemetry (unit test). (b) spec has it. (c) **master-owned live A/B** (deploy-gated) — documented in handoff, not provable in-session. |

AC-2 (fusion agreement) proven by FRE-722. AC-4 (structural on enforced type) is FRE-707.

Operating-point (FRE-706 / ADR-0103 AC-1): `recall_similarity_floor` set to **0.60**; the noise guard
must never drop a true positive — validated on the FRE-489/670 probe **at enactment by master**
(live/deploy-gated). In-session we prove the mechanism (floor filters below-floor, keeps ≥floor) and the
"empty-after-noise-guard ⇒ no prior discussions" rule.

## 4. Design — the shared core

**Item identity carries `kind` (codex finding 2).** `RankedResult` gains `kind: str = "entity"` (frozen,
default keeps every FRE-722/723 construction valid). `FusedResult` gains `kind`. `reciprocal_rank_fusion`
still keys by `item_id` (turn_ids and Entity elementIds are globally unique — no cross-kind collision)
but propagates `kind` onto each `FusedResult`. Only `lexical_recall_arm` is updated to stamp `kind`
(`"turn"` for Turn nodes, `"entity"` for Entity nodes — its Cypher already branches on the label); dense
/ multi-query are entity-only and keep the default. This is what lets the core resolve/rerank/expand a
heterogeneous fused set unambiguously.

New internal method on `MemoryService` (service.py):

```
async def _multipath_fused_recall(
    self, query_text, *, path, entity_type_filter=None, anchor_names=None,
    limit, trace_id, session_id, user_id, authenticated,
) -> MultiPathRecallResult
```

`MultiPathRecallResult` (new frozen dataclass in `memory/fusion.py`):
- `items: list[FusedResult]` — ordered post-rerank (each carries `item_id`, `kind`, `arm_count`)
- `arms_executed: list[str]`, `arms_failed: list[str]`, `per_arm_counts: dict[str,int]` (incl. zero-count
  executed arms), `fused_set_size: int`, `path: str`

Core steps:
1. **Assemble arms** (parallel `asyncio.gather`, each already fails-open to `[]`):
   - dense: `dense_recall_arm(query_text)` **only when `multiquery_arm_enabled` is off** (multi-query
     subsumes the original-query dense pass — avoids double-counting agreement); else the multi-query arm.
   - `multi_query_recall_arm(query_text)` when `multiquery_arm_enabled`.
   - `lexical_recall_arm(query_text)` when `lexical_arm_enabled`.
   - **`arms_executed` = every arm invoked** (not just non-empty ones); `arms_failed` = those that raised;
     `per_arm_counts` includes zero-count arms (codex finding 6 — AC-1 verifiability).
2. **Fuse**: `reciprocal_rank_fusion([...], k=multipath_rrf_k)` → dedup-by-construction fused list.
3. **Cap**: take fused top-`reranker_input_cap` (already rank-ordered — the generalized
   `_select_rerank_candidates`, now "top-N by fused rank"). `fused_set_size = len(capped) ≤ cap`.
4. **Rerank the fused set**: resolve each capped `item_id` → doc text by kind (one Cypher batch: entities
   by `elementId` → `name + ' ' + description`, turns by `turn_id` → `summary`|`user_message`), call
   `rerank(query_text, docs, top_k=len(docs))` so **every** capped item is scored, then reorder by rerank
   score; any item the reranker omits keeps fused order **after** scored ones. Reranker failure/disabled
   ⇒ keep fused order. **Never drops items** (codex finding 7 → AC-5).
5. **Operating point** (§5): the noise guard is the `recall_similarity_floor` applied to the **dense** ANN
   *inside the arms* (below-floor entities never enter fusion). This is the ADR-0103 §4 / ADR-0100
   per-arm noise guard, **not** the AC-5 gate: AC-5 forbids a score threshold on the *fused/reranked* set,
   which we never apply. If the fused set is empty **after all arms**, the path emits its "no prior
   discussions" equivalent (empty payload). A dense-below-floor item is still recoverable by lexical /
   multi-query — so an empty result means *every* arm missed, never a single arm's hard gate (codex
   finding 4).
6. **Telemetry**: `log.info("multipath_recall", arms_executed=…, arms_failed=…, per_arm_counts=…,
   fused_set_size=…, path=…, reranked=bool, trace_id=…, session_id=…)` (ADR-0074 identity threading).

New small helpers:
- `dense_recall_arm(query_text, …) -> list[RankedResult]` — embed + `_dense_vector_search_ranked`, with
  the noise-guard floor applied (below-floor rows dropped before ranking). Public arm parity with
  lexical/multi_query.
- Apply the noise-guard floor inside `_dense_vector_search_ranked` (filter `score >= recall_similarity_floor`
  before ranking). With the floor's **current** default 0.0 this is a no-op for existing FRE-723 tests;
  the floor value question is §6 (owner decision).

## 5. Wiring the three paths (all behind `multipath_recall_enabled`, default off)

- **broad / `query_memory_broad`** (PRIMARY): flag on + `query_text` ⇒ call core with `path="broad"`;
  resolve fused items → entities (entity items directly by elementId; turn items expanded to their
  `key_entities`), then build the existing `{entities, sessions, turns_summary}` shape (sessions/turns_summary
  stay recency-based as today). Flag off ⇒ **today's path byte-for-byte** (no code motion on the off branch).
- **entity-name / `query_memory`**: flag on ⇒ additionally feed lexical+multi_query arms into the **same
  core** and use the fused rank for `_select_rerank_candidates`; map fused turn/entity items back to its
  `TurnNode` candidate set (entity items → their most-recent turns via existing expansion). Flag off ⇒
  unchanged (existing vector+rerank path).
- **proactive / `suggest_relevant`**: flag on ⇒ the candidate **ranking** is produced by the shared
  fuse+rerank core (multi-arm), then handed to the existing `build_proactive_suggestions`. Flag off ⇒
  unchanged. **Scope note (codex finding 8):** proactive's own downstream filters —
  `proactive_memory_min_score` and the diminishing-returns budget in `build_proactive_suggestions` — are
  an intentional *product* gate (don't inject weak proactive context, ADR-0039) and remain in place.
  They are **not** part of AC-5's "no prior discussions" decision, which lives on the explicit recall
  (broad) surface. So proactive converges on *candidate generation via the multi-arm core*, while keeping
  its suggestion-budget filtering — documented, not silently dropped.

**Full convergence, no fallback** (codex finding 1). The entity-name (`query_memory`) path runs the same
multi-arm core behind the flag: its entity-name hints become additional retrieval seeds, the fused set's
turn items map straight to `TurnNode` candidates and entity items expand to their most-recent turns; the
generalized fused-rank cap replaces the vector-score `_select_rerank_candidates`. Flag off ⇒ the existing
vector+rerank path unchanged.

## 6. Config (settings.py)

- **New** `multipath_recall_enabled: bool = False` — master gate for routing the recall paths through the
  core. Default off reproduces single-path recall exactly.

### Noise-guard floor value — OWNER DECISION (codex finding 3)

FRE-706 signed off `recall_similarity_floor = 0.60`. But that setting is **already consumed** by the
ADR-0100 relevance-bounded `query_memory` / `query_memory_broad` paths (gated by
`relevance_bounded_recall_enabled`), and `tests/test_memory/test_relevance_bounded_recall.py` asserts the
`0.0` default. So the value change is **not** isolated behind `multipath_recall_enabled`. Two options:

- **Option A — change the code default `0.0 → 0.60` now** (literal FRE-706). Simple, single config knob.
  Cost: raises the floor on the ADR-0100 paths too on the next deploy, *before* master's live
  FRE-489/670 floor-invariant probe runs; requires updating the ADR-0100 test's asserted default.
- **Option B (recommended) — keep the code default `0.0`; the multipath core reads the same setting, and
  master sets `AGENT_RECALL_SIMILARITY_FLOOR=0.60` in deploy config at the verified rollout** (flag on +
  probe passes). Honors FRE-706's value, keeps the ADR-0100 live behavior unchanged until master
  deliberately enables it, and keeps the floor invariant (AC-1) probe-gated per the operating-point doc.

Recommendation: **Option B** — it matches the owner's "observable-first, don't clamp round 1 / confirm
before shared-gateway behavior changes" posture and keeps the change behind master's deploy gate, while
still enacting the signed-off 0.60 at rollout.

**DECIDED (owner, 2026-07-02): Option B.** `recall_similarity_floor` code default stays `0.0` (ADR-0100
test unchanged); the multipath core reads the same setting; **master sets `AGENT_RECALL_SIMILARITY_FLOOR=0.60`
at the verified rollout** after the FRE-489/670 floor-invariant probe passes. Called out in the handoff.

## 7. TDD test plan (each AC gets an outcome test)

New: `tests/personal_agent/memory/test_multipath_core.py` (unit, mocked arms/rerank) +
`tests/test_memory/test_multipath_recall_integration.py` (test substrate, `@pytest.mark.integration`).

1. **AC-1** — core with two stubbed arms emits `arms_executed` of length ≥2 (incl. an executed-but-empty
   arm) and the fused order equals `reciprocal_rank_fusion` of the stubs (rank-only). Fails if only dense
   runs, or if an executed arm is hidden from telemetry.
2. **AC-3 — mechanism (in-session, codex finding 5)** — unit/integration with the **dense arm forced to
   miss** (mock/seed so the target is OOV for dense) and the **lexical or multi-query arm hitting**:
   target present with `multipath_recall_enabled=True`, absent with it off (dense-only). This proves the
   mechanism; the **full lived-tail proof on the real corpus/embedder is master-owned** (FRE-489/670,
   deploy-gated) — not claimed as complete acceptance in-session.
3. **AC-5** — (a) a capped fused set whose rerank scores are all low still returns **every** item
   (reranker reorders, never drops to empty); (b) dense drops a below-floor item **but lexical still
   returns material** ⇒ non-empty result (no per-arm hard gate); (c) **all** arms miss ⇒ empty by the
   documented noise-guard rule, not a fused-set threshold.
4. **AC-6(a)** — arms whose union > cap ⇒ `fused_set_size == reranker_input_cap` and the reranked input
   length ≤ cap.
5. **Operating point / floor mechanism** — floor filters below-floor entities inside the dense arm, keeps
   ≥floor (mechanism for ADR-0103 AC-1; the live lowest-true-positive probe is master-owned).
6. **Off-path parity** — `multipath_recall_enabled=False` ⇒ `query_memory_broad` / `query_memory` /
   `suggest_relevant` produce the identical result to pre-change (byte-for-byte off branch).

Full regression: `make test` (memory module then full) incl. the FRE-722/723 suites unbroken.

## 8. Steps (atomic)

1. `MultiPathRecallResult`/`FusedItem` dataclass (fusion.py) → `make test-file` green.
2. `dense_recall_arm` + floor in `_dense_vector_search_ranked` (floor default still 0.0 → no-op) → arm tests green.
3. `_multipath_fused_recall` core + telemetry + generalized cap selection → unit tests 1,3,4,5.
4. Wire broad path behind flag + entity resolution → off-parity + AC-3 integration.
5. Converge entity-name + proactive paths through the core behind the flag → off-parity for both.
6. Settings: `multipath_recall_enabled` + floor 0.0→0.60.
7. Docs: mark design spec §8 Build 3 as built; note in ADR-0104 references (no status change — master).
8. Quality gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## 9. Master handoff notes (for the ticket comment)

- AC-1/3/5/6 mechanism proofs = the tests above. **AC-6(c) live p50 ≤ 17s and the AC-1 floor probe
  (lowest FRE-489/670 true positive clears 0.60) are master-owned, deploy-gated** — cannot run in-session
  (need the live corpus + prod embedder).
- Deploy is a `seshat-gateway` rebuild (always-ask class) — **flag stays off** on deploy; master runs the
  FRE-489/670 A/B with the flag on, checks p50 and the floor invariant, then decides rollout.
- The `recall_similarity_floor` 0.0→0.60 default change affects the ADR-0100 relevance-bounded paths when
  their flag is on — verify the probe before enabling.
