# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-23 (ADR-0124 Phase 0 deployed + Done — FRE-947 + FRE-953)

## 0. ADR-0123 turn progress surface — merged, tickets pending

The transport models tool execution and **not inference** (verified: zero transport references to
planning/sub-agent/artifact_draft vs 14 for tool events). Every long silence measured today was an
inference step, so the system is silent precisely where it works longest. Silence → disengagement →
dropped socket → decision resolved without the user, which makes this **upstream of FRE-928**, not
parallel. Implementation tickets follow from the adrs seat.

Live condition for whoever implements: `turn_status` already carries `tool_iteration_max` and
`context_max`, but **both are currently emitted as `0`** between turns. Absent-vs-zero (ADR-0123 §5)
is a present defect, not a future principle.

## 0a. Compaction — hard gate repaired; the reset-action gap remains

**The hard gate now works.** FRE-942 (merged #640, deployed + verified 2026-07-23) gave the tail band a
**ceiling** (`within_session_max_tail_ratio`, 0.35): it had a floor but no ceiling, so a run of large
trailing tool results accumulated an unbounded verbatim tail — measured at 44% zero-or-negative net
reduction across 289 real compactions, worst case 2.65× the window. The FRE-908 zero-reduction proof test
is now inverted to assert real reduction. The ADR-0085 intra-turn digest was weighed and **rejected** on
data (no current tool emits a result above the tail floor; largest since 2026-06-01 is 19,848 tokens).
ADR-0061 status **corrected** (§D3 amendment) and ADR-0092 open item #6 **Resolved** — both closed with
that ticket. The soft trigger stays retired (FRE-941).

**Still open — behavioural, not doc:** the frozen-reset *action* (ADR-0092 item **#7**) is unreachable on
gateway turns, and FRE-908's finding that the reset path never fires in production stands. FRE-944/945
restored the per-turn emits (live 2026-07-23), so there is now measured headroom to base a decision on.
Whether to make the reset action fire is unscheduled and unfiled — decide after real per-turn data
accumulates.

This whole surface is **latent, not live** — assembled context runs ~400–6,000 tokens against a 48,000
reset ceiling and a 120,000 budget ceiling, and the budget trim has never fired in 1,283 evaluations. It
becomes real only if session lengths grow. **FRE-954** (Needs Approval, Sonnet) — a `build_frozen_reset`
sanitiser fixed-point defect surfaced during FRE-942, guarded by a strict xfail, latent behind the
never-firing reset action.

## 0b. Session-summary workstream — Phase 0 LIVE (conversation-scoped); Phase 1 gated on AC-10

**Phase 0 is deployed and Done** (2026-07-23, gateway `e86386be`). The producer summarises the
**conversation** — full user + assistant text plus tool **metadata only** (name, status, error), no
payloads, no arguments — with corrections narrowed to two payload-free kinds (`self_correction` +
`status_contradiction`; ADR-0124 Amendment A #638, reconciled #643). FRE-947 (#636) shipped the
producer/clobber-fix/schema/sweep/role; FRE-953 (#642) narrowed it to conversation scope; both
deployed together, AC-14 no-op verified, 59 multi-turn count intact across the first sweep. Tool
payloads continue to be **captured and stored** (disk + `agent-captains-captures-*`, full `output` in
`_source`, `index: false`) for a future verification oracle; only their delivery to the summariser
stopped.

**Phase 1 (FRE-948) is shut on AC-10**, and Amendment A did not dissolve it: its fixtures were
payload-derived (now invalid) *and* its harness scores agreement by token overlap assuming ~one emitted
item per label, against a digest bounded at ~250 tokens. **Redesign is owner-led and unfiled by
intent** — do not file a measurement ticket for a criterion whose subject may still move. This is the
one genuinely open item in the workstream. Chain behind it: FRE-948 → FRE-949 (Phase 2a replay) →
FRE-950 (Phase 2 hydration) → FRE-951 (Phase 3 anti-re-litigation); Phase 4 unfiled, gated on AC-24.

**AC-22 is the seam** master owns — the paired evaluation holds only once Phases 0, 1 and 2 have all
landed, so the ADR does not close when its last child merges. Standing condition at every gate: *do not
invent a consumer to justify an artifact* — if Phase 1 shows the digest conveys nothing useful,
stopping is correct.

**Live residual:** the first prod sweep budget-denied all 11 eligible sessions (`captains_log` lane
exhausted), so no payload-free digest has been *observed* in prod yet — pending budget reset. The
producer is eval-proven (AC-8/12/13 passed live). Not a risk, a pending observation.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **both build streams idle** — build1's
ADR-0124 chain is parked on the owner-led AC-10 redesign (§0b); build2 empty (FRE-954 parked, Needs
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
