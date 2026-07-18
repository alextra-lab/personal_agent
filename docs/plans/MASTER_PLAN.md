# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-18

## 0. Fix the dispatcher — nothing else until it lands

**FRE-913.** A dispatch must deliver work into the running session and never create or destroy one.
Design approved 2026-07-18 (in-session clear satisfies CLEAR; in-session model switch satisfies the
model contract; no ADR). Proof: claude PID identical before and after a dispatch (AC-1) + the seat stays
visible on the owner's mobile throughout (AC-5, owner-verified only).

Building on `cc-build`, hand-delivered. **Do not start the orchestrator** — it kills the seat to launch.

## 1. Reduce the backlog

80 Approved, 1 dispatchable; the rest is inventory, 24 of it predating the guardian role. Method:
verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Owner to settle scope (Approved only vs all open states) and gate (cancel directly
vs list-first).

## 2. Owner-led ADR debate on project direction

Runs in the `adr` seat, owner-driven. The ADR-0118/0119 artifact-builder + config-UI chain
(FRE-880/888/889/890/891/892, umbrellas 878/887) is **un-approved and held behind this**.

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
- **FRE-912** — deterministic session-id collides with a stale lock on relaunch. Re-scope to the create
  path or close once FRE-913 lands.
- **Worker seats strand on non-edit prompts** — FRE-911's `acceptEdits` covers file edits only; a bash
  approval still blocks a seat the owner may not be able to reach.
- **`cc-sessions` false `(self)`** — `SELF` is read from `tmux display-message` with no target, so from
  outside tmux it self-protects the wrong seat. One-line fix: only trust it when `$TMUX` is set.

## Deploy queue

FRE-884 · FRE-739 (needs FRE-740 + a live non-owner request) · FRE-866 · FRE-717 (needs organic outcome
input).
