# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-19

## 0. Owner decision — accept ADR-0121 + ADR-0122, then approve the build chain

The config-UI debate ran in `cc-adrs` and landed: **ADR-0121** (model catalog and selection layer —
providers → deployments → role bindings, one catalog, **Path removed**, the user selects the model by
name) and **ADR-0122** (build-time artifact-builder selection on the ADR-0076 DecisionCard). Merged as
PR #581, both **Proposed** — publishing is not acceptance.

Two gates, both the owner's:
1. **Accept or amend the two ADRs.** ADR-0121 removes Path entirely and inherits ADR-0079's eleven
   invariants for the selection store; that is the consequential call.
2. **Approve the implementation chain.** FRE-887 (ADR-0121 umbrella, T1–T5, seam AC-9) and FRE-878
   (ADR-0122 umbrella, FRE-881/882/921, seam AC-7) — all children sit **Needs Approval**. Nothing is
   dispatchable until approved and stream-labelled.

Superseded and already closed out: ADR-0118/0119, FRE-880 (canceled with cause), FRE-883, and the
FRE-888–892 chain. FRE-894 holds the deferred Phase-2 scope.

## 1. Reduce the backlog

80 Approved, **0 currently dispatchable — no Approved ticket carries a stream label**; the rest is inventory, 24 of it predating the guardian role. Method:
verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Owner to settle scope (Approved only vs all open states) and gate (cancel directly
vs list-first).

Note: the board reconciler now reads Linear (FRE-915), so drift is detectable automatically — run it
before culling. It already found FRE-432 and FRE-875 shipped-but-stale.

## 2. Questions for the owner — raise at the ADR debate

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
Inference. Re-sequence after 0–2.

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

FRE-884 · FRE-739 (needs FRE-740 + a live non-owner request) · FRE-866 · FRE-717 (needs organic outcome
input).
