# FRE-699 — Recall-path frequency, rerank load, and harness serialization

**Date:** 2026-07-05 · **Type:** Research / measurement (measure-don't-assert)
**Ticket:** FRE-699 (Memory Recall Quality) · **Backing:** ADR-0100; feeds ADR-0104 rollout + FRE-655
**Origin:** consolidated from the FRE-695/696 reranker thread; gated on FRE-698 (joinable rerank telemetry, merged 2026-06-30).
**Status of conclusions:** the mechanistic answers (Q1–Q3) are code-grounded and settled; the "widen/parallelize/nudge" recommendations (Q4/Q5) are **routed** into the already-planned ADR-0104 rollout rather than re-measured here (owner scope decision, 2026-07-05).

---

## TL;DR

1. **The ticket's premise holds for the *live* path, but the architecture moved under it.** In the
   current production config the reranker fires on **exactly one** recall path — the vector
   `search_memory` / `query_memory` path (`memory/service.py:2717`). The topic path
   (`recall_personal_history`), the proactive path (`suggest_relevant`), and the broad
   `MEMORY_RECALL` path do **not** rerank. This is exactly what the 2026-06-30 recall-as-retrieval
   note recorded. **But** the ADR-0104 multi-path fusion layer — which *does* rerank a fused set
   spanning all arms (`_rerank_fused_items`, `service.py:3380`) — has since **landed in code and
   sits flag-dark**. So "widen reranking to the other paths" is largely already *built*; the open
   question is whether to *enable* it, not whether to write it.

2. **The "measure per real prod trace, split local vs cloud" framing is data-starved.** Since the
   joinable telemetry landed (2026-06-30), production has emitted **14** `reranker_applied` events
   (9 with a real `trace_id`; the other 5 are pre-merge test traffic), **0** `reranker_failed`, and
   **6** topic-path recalls (never reranked). n≈5 joinable rerank-turns cannot support a statistical
   local-vs-cloud split. This scarcity is *itself a finding*: reranking is a **rare, low-volume**
   operation, because it rides only the least-fired recall path.

3. **Rerank load is small and always sequential.** One `search_memory` call ⇒ exactly one rerank
   (one `await`). Per-turn rerank count = the number of `search_memory` calls the primary model
   chooses to make (observed **1–3**). Multiple reranks in a turn run **strictly sequentially**,
   each separated by a full primary-model round-trip — the round-trip, not the rerank, is the
   serialization cost.

4. **The reranker is ~8–11% of turn latency; the primary model dominates.** In a worked local-primary
   trace, 3 reranks summed to ~3.5s of a ~32s turn; a single primary generation call (6.8s) exceeded
   all three reranks combined. Rerank cost is ~0.11s/candidate (25-cand ≈ 2.5–2.9s, 10-cand ≈ 1.1–1.2s),
   bounded by `reranker_input_cap=25` (FRE-696).

---

## 1. The live recall architecture (as running, not as the ticket assumed)

Verified against the running `cloud-sim-seshat-gateway` container (2026-07-05):

| Flag | Live value | Effect |
|------|-----------|--------|
| `reranker_enabled` | `true` (default) | reranker active on the vector path |
| `relevance_bounded_recall_enabled` | **`true`** (`.env` override) | ADR-0100 vector-first candidacy is **live** on `query_memory` |
| `recall_similarity_floor` | `0.0` (default) | no hard floor (soft signal only) |
| `multipath_recall_enabled` | `false` (default) | ADR-0104 fused+reranked core is **dark** |
| `lexical_arm_enabled` / `multiquery_arm_enabled` / `structural_arm_enabled` | `false` | all ADR-0104 arms dark |

So production runs **single-path, relevance-bounded** recall with the reranker on the vector path.
The ADR-0104 multi-path layer (FRE-707/722/723/724, merged) is complete but not wired live.

**`rerank()` is called from exactly two sites** (`grep 'await rerank('`):

- `service.py:2717` — inside `query_memory`, the `search_memory` tool path. **Live.**
- `service.py:3380` — inside `_rerank_fused_items`, the ADR-0104 fused-set reranker. **Dark**
  (only reachable when `multipath_recall_enabled=true`).

Every claim below about "the live path" refers to site 2717.

---

## 2. Q1 — how often each recall path fires (mechanism + the honest volumes)

**Mechanism (deterministic, from code + a live trace):**

| Recall path | Trigger | Reranks? | Candidate gen |
|-------------|---------|----------|---------------|
| Proactive (`suggest_relevant`) | **every turn** (background suggest) | **No** | vector-first entity candidacy (`suggest_proactive_raw`) |
| Vector `search_memory` (`query_memory`) | model-decided tool call | **Yes** (site 2717) | ADR-0100 relevance-bounded (vector top-k ∪ entity match) |
| Topic (`recall_personal_history`) | model-decided tool call | **No** | topic + time window |
| Broad (`MEMORY_RECALL` intent → `query_memory_broad`) | intent-classified | **No** (live) / Yes under multipath | recency/entity (single-path) |
| Multi-path fused (`_rerank_fused_items`) | `multipath_recall_enabled` | Yes | RRF over all arms — **dark** |

**Measured volumes** (ES `agent-logs-*`, `event_type` key, since 2026-06-30):

| Event | Count | Reranks |
|-------|-------|---------|
| `proactive_memory_suggest_complete` | ~1065 (all-time) | never |
| `reranker_applied` | 14 (9 joinable, 5 pre-merge test) | — |
| `reranker_failed` | 0 | — |
| `memory_recall` (ADR-0100 event, vector path) | 14 | — |
| `recall_personal_history_called` | 6 | never |
| multi-path fused rerank | 0 | flag-dark |

The load ratio all-time is **proactive : reranks ≈ 1065 : 109 ≈ 10 : 1**, and the 10× side never
reranks. **The path that reranks is the least-fired recall path.** The absolute rerank volume is
tiny because it is gated on the primary model choosing to call `search_memory` — most turns satisfy
recall from the always-on proactive suggestion and never invoke it.

**Local vs cloud split: under-powered, reported as directional only.** Across the 5 joinable
rerank-turns, the sole cloud-primary turn (Sonnet-4.6) fired **1** rerank; the four local-primary
turns (qwen3.6-35B) fired **1, 1, 3, 3**. Directionally consistent with the ticket's "local hedges,
cloud economical" hypothesis, but n=5 traces is not a measurement — see §6.

---

## 3. Q2 — rerank load per turn: sequential, one-per-call

**One `search_memory` call ⇒ one rerank.** Site 2717 issues a single `await rerank(...)` per
`query_memory` invocation over the top-N-by-vector-score candidates (bounded by
`reranker_input_cap=25`; `reranker_top_k=10`). There is no fan-out of reranks within a single
recall call.

**Per-turn rerank count = number of `search_memory` calls the model makes.** Observed distribution
across the 5 joinable turns: `{1: 3 turns, 3: 2 turns}`.

**Multiple reranks in a turn are strictly sequential**, interleaved with primary-model round-trips.
Trace `902d7786` (local): reranks at 17:55:50 → 17:55:59 → 17:56:06 (25/25/10 candidates). Trace
`1f10cbbd` (local): reranks at 12:10:05 → 12:10:15 → 12:10:24. In both, the ~9s gaps between reranks
are the primary model running (search → think → search). The reranker's own internal concurrency
setting is irrelevant on the live path — it only ever receives **one call at a time**.

**Per-rerank cost** (`duration_ms` from the events): 25-candidate reranks 2501–2915ms; 10-candidate
reranks 1139–1215ms — i.e. **~0.11s/candidate**, matching the FRE-696 4B-mxfp8 latency curve. The
`input_cap=25` bounds a single rerank to ~2.8s.

---

## 4. Q3 — where the turn serializes, and latency attribution

**Worked example — trace `1f10cbbd`** (2026-07-05, primary = local qwen3.6-35B, one Haiku
side-call). This single turn exercised **all three recall paths**:

| t (mm:ss.mmm) | event | dur | reranks? |
|---|---|---|---|
| 09:52.994 → 09:55.110 | proactive suggest (start→complete) | ~2.1s | no |
| 09:56.272 → 09:57.132 | primary model_call | 766ms | — |
| 09:57.529 → 10:00.694 | primary model_call | 3404ms | — |
| 10:00.866 → 10:01.061 | `recall_personal_history` (topic) | ~0.2s | **no** |
| 10:01.101 → 10:02.922 | primary model_call | 1847ms | — |
| 10:02.939 → 10:05.987 | `search_memory` #1 (incl. rerank 1193ms) | ~3.0s | yes |
| 10:06.060 → 10:12.876 | primary model_call (generation) | **6841ms** | — |
| 10:12.894 → 10:15.630 | `search_memory` #2 (incl. rerank 1139ms) | ~2.7s | yes |
| 10:15.695 → … 10:24 | primary + `search_memory` #3 (rerank 1187ms) | … | yes |

**Attribution:** 3 reranks summed to **~3.5s** of a **~32s** turn ≈ **11%** (confirming the ticket's
~8% order-of-magnitude). The **primary chat model dominates**: a single generation call (6.8s)
exceeded all three reranks combined; total primary time was ~13s+ across four+ calls.

**The serialization point is not the reranker.** It is the **primary round-trip between the model's
own sequentially-hedged `search_memory` calls** — up to 6.8s of primary generation sits between two
reranks. The reranker is small and fast relative to this. Any latency lever aimed at the reranker
would be optimizing the wrong ~11%; the lever that matters is *collapsing the sequential
search-think-search hedge into one retrieval* (§5a).

---

## 5. Q4/Q5 recommendations — routed into the ADR-0104 rollout

Per the 2026-07-05 owner scope decision, FRE-699 does **not** re-run the FRE-670 probe A/B; the
"widen / parallelize / nudge" questions converge on the **already-built, flag-dark ADR-0104
multi-path layer**, whose recall@k evidence is owned by **FRE-655's FRE-489/670 probe A/B** and
whose rollout gate is already specified (`multipath_recall_enabled` description: *"Enabled only
after master's FRE-489/670 live probe confirms the p50 latency ceiling ≤17s and the noise-guard
floor invariant hold"*).

### (a) Parallelism — the highest-value lever, already built
The real serialization (§4) is the **N−1 primary round-trips** between the model's sequentially-hedged
`search_memory` calls, not the reranks. **Server-side multi-query expansion (the ADR-0104 paraphrase
arm) collapses those N model-driven searches into one fused retrieval**, removing the intervening
primary round-trips (the 6.8s gaps). This is a stronger latency + recall play than any ad-hoc
parallelism on the legacy path. **Recommendation:** treat this as a reason to prioritize the ADR-0104
rollout, gated on FRE-655's probe + the existing p50-latency ceiling. Do **not** add bespoke
parallelism to the single-path legacy code — it would be superseded on rollout.

### (b) Widen reranking to the other paths — already built; enable, don't add
ADR-0104 **already reranks the fused set** (`_rerank_fused_items`, all arms). Enabling
`multipath_recall_enabled` widens rerank to broad/topic/structural candidates **by construction**.
Latency budget is bounded: the fused set is capped and reranked **once** per recall at ~0.11s/cand,
so widening adds ~one bounded rerank per recall, not one-per-path. The reranker stays a **soft
ordering signal**, never a filter (FRE-694/695: Youden's J = 0.785, ~88% recall @ ~9% FP; the
overlap is structural/topical-density, not tunable away). **Recommendation:** route to the ADR-0104
enablement decision + FRE-655; no separate "widen rerank" work is needed.

### (c) Nudge the local model toward the cloud "economical" pattern — defer, it's a workaround
The local model's sequential multi-search hedge is a **coping strategy for single-path recall** —
ask several ways because one vector query might miss. ADR-0104's **server-side** multi-query
expansion makes that client-side hedging **redundant**: once the retrieval layer expands paraphrases
itself, the local skill can be simplified to one query *and get better coverage*. Nudging the local
recall skill in isolation **now** would trade away recall on the legacy path for latency.
**Recommendation:** fold "simplify the local recall skill to a single query" into the ADR-0104
rollout (after multipath does the expansion), gated on the probe showing no recall@k regression —
**not** a standalone prompt change.

---

## 6. Limitations (honest scope)

- **n is tiny.** 14 `reranker_applied` events (9 joinable), 5 joinable rerank-turns, 1 cloud-primary
  turn, 6 topic recalls. Every local-vs-cloud statement here is **directional**, not measured. The
  reranker rides the least-fired path, so joinable volume will stay low until either recall usage
  rises or reranking widens (which is exactly the ADR-0104 question).
- **The non-rerank paths lack a uniform joinable "recall-load" event.** FRE-698 instrumented only
  the rerank path. Answering "recall-mix by primary profile" today requires hand-joining
  heterogeneous per-path events (`proactive_*`, `recall_personal_history_called`, `memory_recall`)
  to `model_call_completed.model` — done manually for one trace in §4. A uniform recall-path event
  would make this a query, not a spelunk — **but** ADR-0104's server-side expansion would make the
  client-side recall-mix largely moot, so this telemetry is **deliberately not filed as work now**
  (see §7); revisit only if the multipath rollout stalls and client-side recall-mix becomes a live
  tuning question.
- **Offline geometry / young corpus.** The FRE-694/695 separation conclusions this note leans on are
  n=54-probe, single-corpus, non-stationary (see the 2026-06-30 recall-as-retrieval note §4).

---

## 7. Follow-up disposition — no new tickets filed (intentional)

The three recommendations all route into **work that already exists**: the ADR-0104 multi-path
rollout (arms merged, flag-dark) and **FRE-655** (the FRE-489/670 probe A/B that owns the recall@k
evidence and the rollout gate). Filing new "parallelize" / "widen rerank" / "nudge local" tickets
would duplicate that machinery. Per the workspace preference (advance priorities before creating
work; don't file investigations), the actionable output of FRE-699 is:

- **Into ADR-0104 rollout / FRE-655:** (i) the sequential-hedge round-trip is the real latency cost,
  so the paraphrase-arm expansion carries a *latency* win on top of recall; (ii) enabling multipath
  widens rerank for free (bounded, soft-signal); (iii) once multipath expands server-side, simplify
  the local recall skill to a single query, gated on no recall@k regression.
- **Deferred (not filed):** uniform recall-path telemetry (§6) — only if the multipath rollout
  stalls.

If the owner wants any of these as a tracked ticket rather than a routed note, that's a one-line ask.

---

## References

- ADR-0100 — Relevance-Bounded Recall (backing; the live vector-path candidacy).
- ADR-0104 — Multi-Path Retrieval with Rank Fusion (Proposed; impl FRE-707/722/723/724 merged,
  flag-dark — the layer this note routes into).
- FRE-698 — joinable reranker telemetry (the instrument this measurement depends on; PR #288).
- FRE-655 — ADR-0100 floor calibration / FRE-489/670 probe A/B (owns the recall@k evidence + the
  ADR-0104 rollout gate).
- FRE-694 / FRE-695 — embedder / reranker separation (no clean floor; reranker = soft signal, J=0.785).
- `docs/research/2026-06-30-recall-as-retrieval-and-the-dual-domain.md` — the structure/semantic
  division; names FRE-699 (§ References): "reranker fires only on the vector `search_memory` path
  today; paths are siloed, not fused."
- Code: `memory/service.py:2717` (live rerank site), `:3380` (`_rerank_fused_items`, dark),
  `memory/reranker.py:68` (`rerank()` + FRE-698 join keys), `config/settings.py:533–676` (recall flags).
- Telemetry: ES `agent-logs-*`, `event_type` = `reranker_applied` / `memory_recall` /
  `recall_personal_history_called` / `proactive_memory_suggest_complete`; joined on `trace_id`.
