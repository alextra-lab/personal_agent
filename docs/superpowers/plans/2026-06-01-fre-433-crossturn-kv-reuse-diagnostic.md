# FRE-433 — Cross-turn KV reuse ≈0 post-ADR-0081-D4: root-cause diagnostic

**Status**: findings — ROOT CAUSE CONFIRMED (diagnostic spike; fix is a separate ticket)
**Date**: 2026-06-01
**Ticket**: [FRE-433](https://linear.app/frenchforest/issue/FRE-433)
**Relates**: [FRE-431](https://linear.app/frenchforest/issue/FRE-431) (ADR-0081 D4, PR #125) · [FRE-422](https://linear.app/frenchforest/issue/FRE-422) (D1) · [FRE-405](https://linear.app/frenchforest/issue/FRE-405)/406/407 (instruments)
**ADR**: `docs/architecture_decisions/ADR-0081-cache-aware-context-layout-and-compaction.md` (this is **D2/D3** territory)

## TL;DR

**Root cause (confirmed): gateway prompt LAYOUT.** The gateway assembles the system message as
`[stable prefix][volatile tail][history…]`, where the volatile tail (recalled memory + selected
skill bodies, appended by ADR-0081 D4) lives **inside the system message — i.e. in the HEAD of
the token sequence, before the conversation history.** It changes every turn. The `:8502` backend
(llama.cpp, Qwen3.6-35B-A3B) reuses KV cache **only on an exact / forward-extension prefix match**,
and the partial-reuse knob (`--cache-reuse`) is **architecturally unavailable for this model**. So a
head change at offset ~N invalidates the **entire** KV cache for the turn → `cache_reuse=0`, full
~8k re-prefill, every turn.

**The fix is gateway-side**: move the per-turn volatile content out of the head to the **tail**, with
the prior turns **frozen append-only** so each turn is a strict forward extension (slm_server Test 3:
turn-2 `prompt_n` 6799→277, `cache_n` 0→6771). **The end-to-end A/B (see §A/B validation) refines
this: the relayout *alone* fixes cloud but NOT local** — local additionally requires the frozen
append-only history (ADR-0081 **D2/D3**), because the local whole-sequence slot only reuses an exact
forward extension. Cloud reuse rose 13,916→17–20k; local stayed at 0 until the volatile block is
frozen in place.

**Both earlier leads were wrong and are retracted:** it is NOT inter-request slot eviction, and NOT
`mmproj`/multimodal (the backend control reused *with* mmproj + spec-decode on). My initial
"server-side / mmproj" call over-read a stable `static_prefix_hash` — that hash covers only the
system-prompt **string**, not the `tools` array or the full wire sequence, so it could not see the
head-layout problem. The slm_server's direct wire tests settled it.

## Evidence

### A. Gateway-side (ES `agent-logs-2026.06.01`, this repo's code)
- Per turn-first SLM call: `static_prefix_hash` constant (`5cae73c3636cec4d`, 1 distinct / 41 calls)
  but **`dynamic_hash` changes every turn** (`185a25…` → `22aa64…` → `97e3c2…`), constant *within* a
  turn. Decoded: the system message's **volatile tail changes every turn** while its stable head
  does not. Tool list is stable (`tools_count=23`, registration-ordered dict).
- Code: `executor.py:2270` captures `inner_system_before_memory` (hashed as `static_prefix`), then
  `executor.py:2275-2285` appends skill bodies + memory to the **same system message** (the volatile
  tail). `prompt_identity.py` hashes only that system string — never `tools` or the wire sequence.

### B. Backend (slm_server, raw `:8502`, llama.cpp b9370) — the decisive tests
**Test 1 — does the backend do partial reuse across a mid-sequence divergence?**

| Scenario | cache_n |
|---|---|
| Identical full prefix | **8758** ✓ |
| Forward extension (prior prompt + appended turn) | **8758** ✓ |
| Mid-sequence divergence (identical 5k head, then differ) | **0** ✗ |

→ Backend reuses **only** exact/forward prefixes. **Any** mid-prefix change zeroes the whole cache —
*including the identical head before the divergence point.*

**Test 2 — can `--cache-reuse` (KV-shift partial reuse) rescue it?** Tried across **7 configs**
(real flags q8_0/kv-unified/draft-mtp; f16 KV; minimal; `--spec-type none`; `--swa-full`;
`--ctx-checkpoints 0 --no-kv-unified`; and the **non-MTP** `Qwen3.6-35B-A3B-GGUF`). Every case logged:
`cache_reuse is not supported by this context, it will be disabled`. → It's the **Qwen3.6-35B-A3B
attention architecture**; no flag enables partial reuse. Backend cannot help.

**Test 3 — proof the layout fix works** (A/B; only difference is *where the volatile block sits*):

| Turn-2 layout | prompt_n | cache_n |
|---|---|---|
| Volatile block in **system head** (current) | 6799 | **0** |
| Volatile block moved to **tail** (after history) | **277** | **6771** |

→ ~24× less prefill from reordering alone. This is the entire fix.

## Hypothesis verdict

| # | Hypothesis | Verdict |
|---|---|---|
| 1 | History/summary reconstruction breaks prefix (→ D2/D3) | **CONFIRMED (refined)** — it's specifically the volatile memory/skill-body block in the system HEAD, plus a backend that only reuses exact/forward prefixes |
| 2 | Single-slot eviction by another SLM request | **Refuted** — no evictor; backend reuses forward extensions fine |
| 3 | `mmproj` (vision projector) disables KV reuse | **Refuted** — backend control reused with mmproj on |
| 4 | `--cache-reuse 0` / spec-decode | **Refuted as fixable** — `--cache-reuse` is architecturally unavailable for this model; non-MTP behaves identically |

## The fix (gateway-side; routed to ADR-0081 D2/D3)

Restructure prompt assembly so each turn is a **forward extension** of the previous one:
1. **System message = stable content only** — static instructions + the fixed 23-tool list. Nothing
   per-turn in it.
2. **Move recalled memory + selected skill bodies to the TAIL** — attach to (or immediately before)
   the current user message, the newest position in the sequence.
3. **Freeze history** — past turns stay byte-identical in their historical position; never rewrite
   earlier turns (append-only). **Must reconcile with within-session compression**
   (`within_session_compression.py`), which rewrites history and would otherwise break the invariant.

After this, turn N's prefix `[stable system][turn 1…N-1 history]` is byte-identical to what turn N-1
cached → backend reuses it, prefilling only the small new tail.

### Open design decisions (why this is an ADR, not a drive-by edit)
- **Skill-body placement vs quality.** Skill bodies are behavioral instructions; moving them out of
  the system prompt to the sequence tail may change adherence. This partially reverses D4's just-shipped
  "skill bodies in the system-message tail" decision. Gate on FRE-407 quality (flat-or-up).
- **History-freeze vs within-session compression.** The append-only freeze conflicts with compaction
  that rewrites history. This is the core of ADR-0081 **D2/D3** (frozen append-only compaction).
- **Memory recall semantics.** Per-turn recall legitimately changes; the append-only model must define
  whether a past turn's recall is frozen in place or only the newest recall is live.

## Expected result after fix
- `cache_reuse > 0` (ES `cache_read_tokens > 0`) on the **first** model call of each new turn (now 0).
- Turn ≥2 `prompt_n` drops from ~8k to a few hundred (just the new tail).
- First-token latency drops from 9–35s toward ~1.5–4s — finally realizing D4's intended win.

## A/B validation (2026-06-01) — relayout alone is cloud-only; local needs frozen append-only

An end-to-end A/B ran the same multi-turn dataset (`scripts/eval/fre433_cache_ab/`) through the live
gateway under two layouts × two backends. Arm B = branch `codex/fre-433-layout-tail-arm` +
`AGENT_CACHE_VOLATILE_TAIL_LAYOUT=true` (volatile block relocated from the system head to a trailing
message). Cross-turn `cache_read` on the first full-context call of turns ≥ 2:

| arm | backend | cross-turn cache_read | cache_create/turn |
|---|---|---|---|
| A (head) | cloud (Sonnet) | 13,916 (constant) | ~1.9–7.4k |
| A (head) | local (Qwen :8502) | 0 | — |
| **B (tail)** | **cloud** | **17.3–20.2k** ✅ | **~0.1–2.2k** |
| **B (tail)** | **local** | **0** ❌ | — |

**Verdict: the relayout fixes cloud but NOT local.** Cloud reuse rises and re-creation collapses
(and confirms it does *not* break Sonnet caching — it improves it). Local stays at 0.

**Why — confirmed by the slm_server's exact raw-`:8502` Test 3 construction:**

```
GOOD (reuses): turn2 = [system STABLE][user VOL_V1+Q1][assistant g1][user VOL_V2+Q2]
               prefix [STABLE][VOL_V1+Q1][g1] is BYTE-IDENTICAL to turn-1's cached KV
               → strict forward extension → cache_n 6771, prompt_n 277
```

Two properties make local reuse work, and **arm B has only the first**:
1. Volatile rides with its user turn (out of the system head). ✅ arm B does this.
2. **Frozen append-only history** — a past turn's volatile block stays byte-identical in its original
   position; only the newest turn gets fresh volatile. ❌ arm B appends an *ephemeral* trailing message
   that is **not persisted into history**, so turn N's volatile sits in the cached slot between `userN`
   and `assistantN`, and turn N+1 (which has `assistantN` there instead) **diverges mid-sequence** →
   the whole-sequence local slot reuses 0. Cloud survives only because the cache breakpoint sits before
   the tail.

**Therefore the local fix = ADR-0081 D2/D3 (frozen append-only history)**, not the relayout alone.
Test-3 method (for reproduction): raw `POST :8502/v1/chat/completions` (or `:8000` via router),
`stream:true max_tokens:16 temperature:0`, metric `timings.cache_n`/`prompt_n` from the final SSE
chunk, run turn 1 → ~1s pause → turn 2 (single slot retains turn-1 KV).

**Tradeoff the ADR must settle:** freezing each turn's recalled memory + skill bodies into history buys
the reuse but means they **accumulate** in-context (monotonic token growth + stale recall persisting),
vs. today's fresh-per-turn recall. Codex's arm is a validated **cloud-only** partial — hold it pending
the D2/D3 ADR, which subsumes it.

## Verification (mirror Test 3 in staging)
≥2-turn session sharing a prefix. **PASS** = `cache_n > 0` / `cache_read_tokens > 0` on the first call
of turn 2+. **Do NOT** verify with a within-turn continuation (already reuses → false PASS); must be
two *separate* turns.

## Non-issues (ruled out)
- **Not mmproj/multimodal** (backend reused with it on; a final mmproj re-test is pending only for the
  vision-enablement decision, which is orthogonal to this bug).
- **Not telemetry** — `cache_reuse` now reads `timings.cache_n` correctly (a prior bug read a
  nonexistent `tokens_cached` and logged null); the 0s are real.
- **Not speculative decoding / MTP** — non-MTP model behaves identically.

## Build-owned cleanup shipped with this note
`adapters.py:655` comment corrected: current llama.cpp defaults `cache_prompt` true (not false), and
annotated that the flag governs within-turn reuse only — cross-turn reuse is a layout matter.
