# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-23

## 0. ADR-0123 turn progress surface — merged, tickets pending

The transport models tool execution and **not inference** (verified: zero transport references to
planning/sub-agent/artifact_draft vs 14 for tool events). Every long silence measured today was an
inference step, so the system is silent precisely where it works longest. Silence → disengagement →
dropped socket → decision resolved without the user, which makes this **upstream of FRE-928**, not
parallel. Implementation tickets follow from the adrs seat.

Live condition for whoever implements: `turn_status` already carries `tool_iteration_max` and
`context_max`, but **both are currently emitted as `0`** between turns. Absent-vs-zero (ADR-0123 §5)
is a present defect, not a future principle.

## 0a. Compaction — a full review is owed; visibility now exists to base it on

**There is no working compaction path.** Proven, not suspected: the soft trigger was dead by design
under the frozen layout and FRE-941 deleted it; the hard gate fires but provably shrinks nothing
(`tokens_saved == 0` — any tool response large enough to cross the threshold is itself larger than the
24k tail floor, so it is swept wholesale into the protected tail); and the frozen-reset scheduler that
was to supersede both **had never once evaluated** — its per-turn emit was unreachable behind
`step_init`'s gateway-branch return. Sources: `docs/research/2026-07-17-fre-908-compression-gate-proof.md`
(executable proof) and ADR-0092 open item 7.

This is **latent, not live** — assembled context runs ~400–6,000 tokens against a 48,000 reset ceiling
and a 120,000 budget ceiling, and the budget trim has never fired in 1,283 evaluations. It becomes real
only if session lengths grow.

Forward: FRE-944/FRE-945 restored the per-turn emits (live 2026-07-23), so the review can be based on
measured headroom rather than code reading. **FRE-942 is parked** — its premise (hard≈soft trigger
parity) rested on stale pre-June counts and named modules FRE-941 deleted; it needs an owner retarget
onto the design change FRE-908 identifies (exempt oversized trailing messages from the tail floor, or
target the scheduler directly), not a threshold tweak. Settle ADR-0061's status in the same decision —
it still reads Implemented while FRE-908 proved it inert.

## 0b. Session-summary workstream — ADR-0124 Accepted, chain live

**ADR-0124** (Accepted 2026-07-23) supersedes the measure-first hold that previously sat here: the gate
now applies to **Phase 4 only**, and Phases 0–3 are unblocked. Chain on build1, relations written at
dispatch: **FRE-947** Phase 0 producer correction (AC-1–14) → **FRE-948** Phase 1 session-browser
surface (AC-15) → **FRE-949** Phase 2a offline replay → **FRE-950** Phase 2 hydration, model-visible
(AC-17–21) → **FRE-951** Phase 3 anti-re-litigation (AC-22 to build, AC-23 to surface). Phase 4 is
deliberately **unfiled**, gated on AC-24.

Two things master owns here. **AC-22 is the seam** — the paired evaluation holds only once Phases 0, 1
and 2 have all landed, so the ADR does not close because its last child merges. And the ADR carries a
standing condition worth enforcing at every gate: *do not invent a consumer to justify an artifact* —
if Phase 1 shows the digest conveys nothing useful, stopping is the correct outcome.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **build1 = the ADR-0124 chain, head
FRE-947** (PR #636 open at the gate); **build2 = FRE-942** (compaction decision, retargeted 07-23).
Awaiting approval and unlabelled: FRE-927, FRE-932. Method:
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

- **ADR-0061 status is known-false** — reads "Implemented"; proven inert by FRE-908. Fix or retire.
- **FRE-912** — narrowed, not fixed; parked-Approved. See §2.
- **Worker seats strand on non-edit prompts** — see §2.

## Deploy queue

**FRE-938 (ask-first — gateway + PWA rebuild, owner-gated, NOT done)** — merged #617; handoff runbook
on the ticket, PWA cache bumped to v35. First operational pickup. · FRE-739 (needs FRE-740 + a live
non-owner request) · FRE-717 (needs organic outcome input).
