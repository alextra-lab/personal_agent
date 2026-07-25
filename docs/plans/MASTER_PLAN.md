# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-25 (the 2026-07-24 bug wave SHIPPED — 7 fixes deployed; pipeline-hardening family filed)

## 0. ADR-0123 turn progress surface — accepted; adrs seat filing the impl chain (FRE-957)

Accepted 2026-07-24 after a live turn reproduced the harm: a ~19-minute qwen turn showed **zero** UI
activity. The transport models tool execution but **not inference**, so it's silent through the longest
steps; and a mid-turn WebSocket drop meant even the tool events (which *are* modelled) never reached the
UI. **FRE-957** (adrs) is filing the sequenced impl chain — scope: transport emits inference/planning
progress · the turn-progress UI surface (phase model incl. a Thinking phase, unknown-looks-unknown) ·
**event-stream replay on reconnect** (by sequence number) · the `turn_status`-emits-`0` defect (ADR-0123
§5) · the `/api/inference/status` 404 that blinds the liveness poll. Upstream of FRE-928. Impl tickets
land Needs-Approval for the owner.

## 0a. Compaction — reset-action gap (behavioural), unscheduled

The hard-gate ceiling shipped (FRE-942); what remains is behavioural: the frozen-reset **action**
(ADR-0092 item **#7**) is unreachable on gateway turns and never fires in production. Per-turn emits are
now live (FRE-944/945), so a decision can be based on measured headroom. **Decide whether to make the
reset action fire after real per-turn data accumulates** — unfiled by intent. Whole surface is latent
(assembled context ~400–6,000 tokens vs a 48,000 reset ceiling; budget trim never fired in 1,283
evals) — real only if sessions grow. **FRE-954** (Needs Approval, Sonnet) — a `build_frozen_reset`
sanitiser fixed-point defect, parked behind the never-firing reset action.

## 0b. Session-summary workstream — paused before Phase 2

ADR-0124 Phases 0–1 are live (conversation-only producer + session-browser digest surface, deployed
2026-07-24). The chain **pauses by design before Phase 2**: FRE-949 (Phase 2a) → FRE-950 (Phase 2
hydration) → FRE-951 (Phase 3) are parked on two conditions — a real digest population must accumulate
(prod digests budget-denied, none yet) and the **Phase-1 forcing-function read** (is the digest worth
consuming?) must land first. Per the ADR, if Phase 1 shows nothing useful, **stopping is correct** —
don't invent a consumer.

**Standing checks master owns:** the Amendment B retired-value population scan (FRE-956) once digests
exist; the **AC-22 seam** (assembled Phases 0–2) closes only when Phase 2 lands, not when a child merges.

## 0c. The 2026-07-24 bug wave — SHIPPED; two follow-on threads

The wave (sub-agent routing, Anthropic-primary prefill, compaction window, 524 salvage, OVH/Voyage cost
+ migration 0022, legacy digest, budget right-size) is **deployed** (2026-07-25, main `cc019fde`) — see
git log + `LAST_SESSION.md`. Forward from here:
- **Finish wave verification** — FRE-970/971/972/969 are deployed + TDD-proven but their *behavioural*
  ACs are not yet live-confirmed (qwen turns 524 on the tunnel before completing). Fire a **sonnet**
  budget turn to seal them, then close Done. FRE-974/973 already Done.
- **Config chain FRE-966→967→968** (ADR-0121 Addendum A — per-primary sub_agent map · guard · resolver ·
  Config-UI seam) — **parked** (labels off, relations wired); resumes on owner's word.
- **Follow-ups (Needs Approval):** FRE-978 (Stage-7 static-window trim, sibling of 972) · FRE-979 (ES
  skill → `api_cost_recorded` for total spend, sibling of 970/974) · FRE-980 (raise CF tunnel timeout —
  **Mac-side terraform**, not build-pipeline). Also still open from the wave: FRE-959 (SIGPIPE) · FRE-960
  (paraphrase routing).

## 0d. Pipeline-hardening family — filed, held for the explore study

Surfaced by this session's own dispatch failures. **FRE-976** (Linear-reconciled dispatch — the daemon
stall-loops on a Done-directly ticket; Tier-1) · **FRE-975** (gate master on a review-complete signal,
not PR-open+CI-green) · **FRE-977** (explore first-class dispatch — master owns explore hand-offs). All
Needs Approval. **Hold until the explore pipeline-architecture study lands** (owner-started) — it may
fold them into a unified design. Open owner decision: dispatch 975+977 to build2 now (hold 976)?

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **build1 + build2 idle** (wave done); the
pipeline-hardening family (§0d) is the next candidate for build2, held for the explore study. Awaiting
approval and unlabelled: FRE-927, FRE-932. Method:
verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Owner to settle scope (Approved only vs all open states) and gate (cancel directly
vs list-first).

Note: the board reconciler now reads Linear (FRE-915), so drift is detectable automatically — run it
before culling. It already found FRE-432 and FRE-875 shipped-but-stale.

## 2. Questions for the owner

- **FRE-909 residual / seat hygiene** — none. Closed, all five criteria met.
- **FRE-432 · FRE-875** — merged PRs, stale board state. Close with evidence, or is something unfinished?
- **The unverified handoff claim (#577)** — the build asserted a scope cut "was discussed with the owner
  in-session". Master could not confirm it. Did that happen? If not, a worker citing owner approval is a
  governance hole worth closing.
- **ADR-Implemented drift check** — retired with the MASTER_PLAN parser (it read plan prose). Re-source
  it from each ADR's own Status header, or accept the loss? Not filed.
- **FRE-912** — narrowed by FRE-913 (no termination path), not eliminated: the deterministic session-id
  can still collide on the absent-seat relaunch. Schedule, or accept as residual risk?
- **Bash-prompt stranding** — FRE-911's `acceptEdits` covers file edits only. Broader mode, allowlist,
  or detect-and-surface?

## 3. Then, in order

Memory Recall · Telemetry residuals · Configuration Management · Linear async feedback · Seshat
Inference. Re-sequence after §0.

---

## Awaiting an owner decision

- **ADR-0120 cost governance** — Proposed. Note: FRE-974 already delivered the **OVH/Voyage half of T0**
  (both now in `api_costs`); Perplexity + the caps/consent layer remain, unlock on Proposed→Accepted.
  All cost work ask-first.
- **Backlog cull scope + gate** (see §1).
- **FRE-885** · **FRE-805** · **FRE-621** — Needs Approval.

## To fix, unscheduled

- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7) — behavioural gap; see §0a. Decide
  after per-turn headroom data accumulates. FRE-954 (sanitiser fixed-point) sits behind it.
- **FRE-912** — narrowed, not fixed; parked-Approved. See §2.
- **Worker seats strand on non-edit prompts** — see §2.

## Deploy queue

The 2026-07-24 wave is **deployed** (2026-07-25 bundled rebuild + migration 0022; embeddings re-stopped).
Remaining:
- **FRE-970 · 971 · 972 · 969** — deployed; **behavioural verification pending** (fire a sonnet budget
  turn — see §0c / `LAST_SESSION.md`). Close Done once confirmed.
- **FRE-943 · 739 · 717** — rode the wave rebuild → now live; verify ACs + close.
- **FRE-938** (gateway + PWA, owner-gated) — merged #617, PWA cache v35.
