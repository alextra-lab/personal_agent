# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-24 (ADR-0124 Phases 0–1 live — conversation-only producer + digest surface deployed; paused before Phase 2)

## 0. ADR-0123 turn progress surface — merged, tickets pending

The transport models tool execution and **not inference** (verified: zero transport references to
planning/sub-agent/artifact_draft vs 14 for tool events). Every long silence measured today was an
inference step, so the system is silent precisely where it works longest. Silence → disengagement →
dropped socket → decision resolved without the user, which makes this **upstream of FRE-928**, not
parallel. Implementation tickets follow from the adrs seat.

Live condition for whoever implements: `turn_status` already carries `tool_iteration_max` and
`context_max`, but **both are currently emitted as `0`** between turns. Absent-vs-zero (ADR-0123 §5)
is a present defect, not a future principle.

## 0a. Compaction — reset-action gap (behavioural), unscheduled

The hard-gate ceiling shipped (FRE-942); what remains is behavioural: the frozen-reset **action**
(ADR-0092 item **#7**) is unreachable on gateway turns and never fires in production. Per-turn emits are
now live (FRE-944/945), so a decision can be based on measured headroom. **Decide whether to make the
reset action fire after real per-turn data accumulates** — unfiled by intent. Whole surface is latent
(assembled context ~400–6,000 tokens vs a 48,000 reset ceiling; budget trim never fired in 1,283
evals) — real only if sessions grow. **FRE-954** (Needs Approval, Sonnet) — a `build_frozen_reset`
sanitiser fixed-point defect, parked behind the never-firing reset action.

## 0b. Session-summary workstream — Phases 0–1 live; paused before Phase 2 on real digests

ADR-0124 through **Phase 1 is live** (both deployed 2026-07-24): Amendment B shipped the
**conversation-only producer** (FRE-956 — `tool_evidence`/`status_contradiction` retired, tool metadata
gone from the input entirely; AC-10 redefined over the three conversation bases) and the
**session-browser digest surface** (FRE-948 — generated label replaces the title hack, digest rendered,
cross-substrate read bounded + graceful-degrading). Tool-derived verification is relocated to the future
**verification oracle** (research Lane 5 → Workstream 4).

The chain now **pauses by design before Phase 2**: FRE-949 (Phase 2a offline analysis) → FRE-950
(Phase 2 hydration) → FRE-951 (Phase 3) are held **parked** on two conditions — a real conversation-only
**digest population must accumulate** (prod digests are budget-denied, so none exists yet), and the
**Phase-1 forcing-function read** (is the digest worth consuming?) must land first. Per the ADR, *if
Phase 1 shows the digest conveys nothing useful, stopping is correct* — do not invent a consumer to
justify the artifact.

**Standing post-deploy check master owns:** the Amendment B retired-value population scan (FRE-956) —
run once real digests exist, refusing an empty population. **AC-22 seam** (assembled Phases 0–2
evaluation) remains master's; it closes only once Phase 2 lands, not when a child merges.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **both build streams idle** — build1 free
after Phases 0–1 shipped (§0b, chain paused before Phase 2); build2 empty (FRE-954 parked, Needs
Approval). Awaiting approval and unlabelled: FRE-927, FRE-932. Method:
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

- **ADR-0120 cost governance** — Proposed. Impl chain (FRE-898/904/905; T0 = instrument OVH/Voyage/
  Perplexity into `api_costs`) unlocks on Proposed→Accepted. All cost work ask-first.
- **Backlog cull scope + gate** (see §1).
- **FRE-885** · **FRE-805** · **FRE-621** — Needs Approval.

## To fix, unscheduled

- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7) — behavioural gap; see §0a. Decide
  after per-turn headroom data accumulates. FRE-954 (sanitiser fixed-point) sits behind it.
- **FRE-912** — narrowed, not fixed; parked-Approved. See §2.
- **Worker seats strand on non-edit prompts** — see §2.

## Deploy queue

**FRE-938 (ask-first — gateway + PWA rebuild, owner-gated, NOT done)** — merged #617; handoff runbook
on the ticket, PWA cache bumped to v35. First operational pickup. · FRE-739 (needs FRE-740 + a
live non-owner request) · FRE-717 (needs organic outcome input).

_FRE-947 + FRE-953 (ADR-0124 Phase 0) deployed + Done 2026-07-23 (`e86386be`) — see §0b and git log._
