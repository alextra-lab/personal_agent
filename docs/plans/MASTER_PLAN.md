# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-21

## 0. Gate PR #613 — first action, it has been waiting

FRE-928 (all three no-card mechanisms). CI fully green, **ungated for hours** — the watcher sent
`/master 613` at 16:47 and the send was swallowed while the ledger recorded delivery (FRE-939, the
second such loss today). **Run `gh pr list` rather than waiting for a trigger.**

## 0a. Decide the model caps before running AC-7

The catalog declares `max_tokens` far below real provider ceilings — `claude_sonnet` 32768 vs **128K**,
`claude_haiku` 4096 vs **64K**. Harmless until today; **FRE-931 now clamps to those numbers**, so a
Haiku pick yields a ~16×-smaller artifact and the plan is sized to it. Correcting it touches the
planning note, which lives in the system prompt — and the owner has ruled out prompt-cache-affecting
changes pending their decision. **Not ticketed. Owner's call on approach first.**

Running AC-7 before this produces a needlessly small artifact that will read as a sizing bug.

## 0b. ADR-0122 — AC-7 is the only thing left

T4–T6 (FRE-929/930/931) shipped and deployed 2026-07-21; verified in the running container. **FRE-921
stays `Verify Failed`** until a live run passes. One artifact-build turn closes AC-7 **and** answers the
open cache question (see `LAST_SESSION.md`) — same turn, same cost. Owner fires it; master verifies from
telemetry.

## 0c. ADR-0123 turn progress surface — merged, tickets pending

The transport models tool execution and **not inference** (verified: zero transport references to
planning/sub-agent/artifact_draft vs 14 for tool events). Every long silence measured today was an
inference step, so the system is silent precisely where it works longest. Silence → disengagement →
dropped socket → decision resolved without the user, which makes this **upstream of FRE-928**, not
parallel. Implementation tickets follow from the adrs seat.

Live condition for whoever implements: `turn_status` already carries `tool_iteration_max` and
`context_max`, but **both are currently emitted as `0`** between turns. Absent-vs-zero (ADR-0123 §5)
is a present defect, not a future principle.

## 0b. ADR-0121 / FRE-887 — AC-9 still open

Needs **one owner turn**: picker renders real candidates → switch model → next turn runs on it →
survives reload → survives WS reconnect. Closes FRE-920. Unaffected by the above.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: build1 on FRE-928 → FRE-938 → FRE-926;
**build2 is idle — its eligible set is empty**, so it needs work labelled or it stays parked. Awaiting
approval and unlabelled: FRE-927, FRE-932, FRE-939. Method:
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

FRE-739 (needs FRE-740 + a live non-owner request) · FRE-717 (needs organic outcome
input).
