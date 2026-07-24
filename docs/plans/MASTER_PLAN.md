# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-24 (ADR-0124 Amendment B merged — summariser conversation-only; FRE-956 in build)

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

## 0b. Session-summary workstream — Amendment B landed; Phase-0 producer change in build

ADR-0124 Amendment B is merged (PR #647): the summariser is **conversation-only** — the `tool_evidence`
basis and `status_contradiction` correction are removed, and tool metadata is removed from the
producer's input **entirely** (tool counts kept only as computed structured properties, never fed to the
generator). AC-10 is redefined over the three conversation bases (`user_statement`,
`assistant_reasoning`, `mixed`); the old payload-derived fixtures retire with it. Tool-derived
verification is relocated to the future **verification oracle** (research Lane 5 → Workstream 4), not the
summariser.

**Live head: FRE-956** (build1) — implements the conversation-only producer + rebuilds the AC-8 /
AC-10–13 fixtures; **gateway-rebuild deploy, ask-first**, when it lands. It **blocks FRE-948** (Phase 1),
which is held parked (unlabelled) until FRE-956 merges. Chain: FRE-956 → FRE-948 → FRE-949 (Phase 2a
replay) → FRE-950 (Phase 2 hydration) → FRE-951 (Phase 3); Phase 4 unfiled, gated on AC-24.

**AC-22 is the seam** master owns — the paired evaluation holds only once Phases 0, 1 and 2 have all
landed, so the ADR does not close when its last child merges. Standing condition at every gate: *do not
invent a consumer to justify an artifact* — if Phase 1 shows the digest conveys nothing useful,
stopping is correct.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **build1 building FRE-956** (ADR-0124
Amendment B producer, §0b); build2 idle (FRE-954 parked, Needs Approval). Awaiting approval and
unlabelled: FRE-927, FRE-932. Method:
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
