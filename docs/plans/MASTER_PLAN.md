# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-21

## 0. ADR-0122 — AC-7 FAILED live; amendment in flight

**FRE-921 is `Verify Failed`.** The first real run (2026-07-21) produced no card: the constraint
waiter bypasses its own timeout when no socket is registered, so a momentary drop became a permanent
silent default. Routing itself was correct end to end.

**Owner has made the design call: the card moves to the START of the turn**, so the chosen builder's
max-output and context are inputs to planning (cf. FRE-478, where the builder hit an output cap
mid-generation). Measured: the card check is deterministic and takes <1ms, but sat 117s into the turn
behind research tools — by which point the user had disengaged. **ADR-0122 amendment is with the adrs
seat**; it decides the early-ask trigger, false-positive/negative handling, and preference interaction.

- **FRE-928** (timeout bypass + stale-constant status bar) — Needs Approval, **held by master** pending
  the amendment, which may reshape it.
- **AC-7 re-run** is blocked until the amendment lands and ships.

## 0b. ADR-0121 / FRE-887 — AC-9 still open

Needs **one owner turn**: picker renders real candidates → switch model → next turn runs on it →
survives reload → survives WS reconnect. Closes FRE-920. Unaffected by the above.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: build1 on FRE-925 → FRE-926; **build2 is
idle — its eligible set is empty**, so it needs work labelled or it stays parked. Method:
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
