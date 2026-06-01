# ADR-0081 D2/D3 design brief — frozen append-only layout + cache-aware compaction scheduler

**Status**: design brief (input for the adr session to write ADR-0081 §D2/D3; NOT the ADR itself)
**Date**: 2026-06-01
**Author**: build session (FRE-433 spike)
**For**: adr session (writes ADR-0081 D2/D3) → then build implements from the approved ADR
**Evidence**: `docs/superpowers/plans/2026-06-01-fre-433-crossturn-kv-reuse-diagnostic.md` · FRE-433 · A/B harness `scripts/eval/fre433_cache_ab/`

## 0. Why this is one design, not two

ADR-0081 is titled *"cache-aware context **layout and compaction**."* FRE-433 proved the two halves
are inseparable for the local backend:
- **Layout (D2/D3):** volatile content must sit at the tail **and** prior turns must be **frozen
  append-only**, so each turn is a strict forward extension → local KV reuse.
- **Compaction:** once layout is deterministic, compaction is the **only** cache-reset event, and its
  timing becomes a **computable optimum**. Compaction is the release valve for the accumulation that
  freezing causes.

Treat them as a single cache-aware design.

## 1. Confirmed problem (from FRE-433)

Cross-turn KV reuse on the local SLM (`:8502`, Qwen3.6-35B-A3B) is 0 — full ~8k re-prefill every turn.
Root cause = gateway prompt **head-layout**: per-turn volatile content (recalled memory + selected
skill bodies) is appended **inside the system message** (head of the sequence), and changes every turn.
The local backend reuses **only an exact forward-extension prefix** (`--cache-reuse` architecturally
unavailable for this model), so any head/mid-sequence change → 0.

**A/B verdict:** the volatility-gradient relayout *alone* (move the block to a trailing message) fixes
**cloud** (reuse 13.9k→17–20k; does NOT break Sonnet — improves it) but **not local** (stays 0),
because an *ephemeral* trailing block isn't frozen into history → mid-sequence divergence next turn.

## 2. Part A — Layout: frozen append-only history (the D2/D3 requirement)

Local reuse requires **two** properties (the slm_server's raw-`:8502` GOOD construction proves both):

```
turn2 = [system STABLE][user VOL_V1+Q1][assistant g1][user VOL_V2+Q2]
        └ prefix byte-identical to turn-1's cached KV ┘ + new volatile tail
→ strict forward extension → cache_n 6771 / prompt_n 277 (vs 0 / 6799 head-layout)
```

1. **Volatile rides with its user turn** — recall + skill bodies attach to that turn's user message (or
   a per-turn message), never the system head.
2. **Frozen append-only history** — a past turn's volatile block stays **byte-identical in its original
   position**; only the newest turn gets fresh volatile. Turn N+1 must reproduce turn N's full sequence
   verbatim, then append.

**Gateway changes implied** (for the eventual build impl):
- Stop appending volatile to `system_prompt`; attach it to the per-turn user message.
- Persist the per-turn volatile into session history so it replays byte-identically (today the system
  prompt is re-derived each turn and recall is fresh-per-turn — that must become append-only).
- Anthropic `cache_control`: keep the breakpoint **before** the volatile tail (Codex's arm already does
  this — system + history-end + last-tool); the layout change benefits cloud regardless.

## 3. Part B — Cache-aware compaction scheduler (the "and compaction" half)

Once layout is deterministic, the sequence grows by a **known, bounded increment per turn** (new user
turn + its volatile + the assistant reply). So:

- `total_tokens(N)` is predictable; cross-turn reuse ≈ `total_tokens(N-1)`; per-turn prefill ≈ the new
  tail only. **Reuse becomes deterministic.**
- **Compaction is the only cache-reset event** (rewriting history breaks byte-identity). It flips from
  *every-turn reset* (today) to *rare, scheduled resets* — a **sawtooth**: long reuse run → one reset →
  long reuse run.

**Compress at the computed optimum**, not a reactive token threshold. Fire compaction when:
```
marginal (accumulation + quality) cost of NOT compacting  >  amortized reset cost over the next run
```
The three terms determinism lets us quantify:
1. **Reuse savings** — longer run = more banked prefill (monotonic).
2. **Accumulation cost** — frozen volatile piles up: token growth + stale recall denting quality
   (calibrate against FRE-407 per-turn ratings; the A/B harness produces this).
3. **Reset cost** — the one-time re-prefill when compaction rewrites history.

**Make compaction re-establish a NEW frozen prefix**, not a one-off summary:
`[stable system][compacted summary of old turns][recent frozen turns][new tail]` — so after the reset
the next run forward-extends again. Tune the cadence to the cost terms.

**Backend-asymmetric reset cost (same scheduler, backend-aware term):**
- **Cloud:** the stable system+tools segment survives a reset (breakpoint before history); a reset only
  re-creates the rewritten-history portion → **cheap** → compact **tighter/fresher**.
- **Local:** any mid-history change breaks the forward extension → **full re-prefill** of the new prefix
  → **expensive** → compact **looser** (let runs grow longer before paying the reset).

## 4. Decisions the ADR must settle

1. **What gets frozen** — recall only? skill bodies only? both? (Both accumulate; skill bodies may be
   re-selected per turn — freezing them may keep stale skill guidance in-context.)
2. **The accumulation-vs-reuse tradeoff** — how much in-context growth / staleness is acceptable before
   a reset; the quality ceiling (FRE-407) that bounds it.
3. **Compaction-trigger model** — replace/augment the reactive `executor.py:1377` (0.85 hard) /
   `compression_manager` (0.65 soft) thresholds with the cost/quality optimum above; backend-aware.
4. **Reconcile with `within_session_compression`** — it rewrites history today, which *is* a cache reset;
   it must become the **scheduled** reset that re-establishes a new frozen prefix, not an ad-hoc rewrite.
5. **Frozen-prefix re-establishment** — exact structure of the post-compaction sequence so the next run
   reuses.
6. **Disposition of Codex's arm** (`codex/fre-433-layout-tail-arm`) — validated **cloud-only** partial;
   subsume into the full D2/D3 layout or keep as a cloud-only flag? (Recommend subsume.)

## 5. Constraints / non-goals

- Backend reality is fixed: local reuses **only** exact forward extensions; `--cache-reuse` unavailable.
  Do not design around a backend knob.
- Must hold the **FRE-407 quality gate** (flat-or-up) — relocating skill bodies/recall out of the head
  must not degrade answers; this is the *primary* risk and gates any rollout.
- Gate behind a flag (`prefer_primitives_enabled` or a dedicated one), no-op when off, like prior ADR-0081 work.
- Not in scope: SLM-server changes (the backend is correct); cloud already wins from layout alone.

## 6. Verification (when build implements)

Reuse the FRE-433 A/B harness (`scripts/eval/fre433_cache_ab/`, both `--profile {local,cloud}`):
- **PASS:** local `cache_read_tokens > 0` on the first full-context call of turns ≥ 2 (today 0);
  turn-≥2 `prompt_n` drops to ~the new-tail size; cloud reuse ≥ the 17–20k arm-B baseline.
- **Quality:** FRE-407 per-turn ratings flat-or-up vs. head-layout baseline.
- **Determinism/scheduler:** token growth per turn matches the predicted increment; compaction fires at
  the computed optimum and re-establishes a reusable frozen prefix (reuse resumes on the next turn).

## 7. References

- FRE-433 findings (A/B matrix, GOOD-vs-arm construction, Test-3 method): `2026-06-01-fre-433-crossturn-kv-reuse-diagnostic.md`
- A/B harness + dataset: `scripts/eval/fre433_cache_ab/`
- Cloud-only partial arm: branch `codex/fre-433-layout-tail-arm` (flag `AGENT_CACHE_VOLATILE_TAIL_LAYOUT`)
- ADR-0081 (this extends §D2/D3) · FRE-431 (D4) · FRE-422 (D1) · FRE-405/406/407 (instruments)
