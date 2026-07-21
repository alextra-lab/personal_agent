# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-21

## 0. DISPATCH IS PAUSED — owner-directed 2026-07-21

`telemetry/dispatch.disabled` is engaged (proven detected: the launch-block predicate returns
`kill-switch`). It blocks **new launches only** — in-flight seats are untouched — and it **also pauses
the gating watcher**, so master gates open PRs manually. **Delete the file to resume.** The file
documents its own intent.

## 1. Two PRs at the gate — first action of the next session

- **#600 — FRE-923** dispatch delivery atomicity (bounded retry + swallowed-Enter repair)
- **#599 — FRE-921** ADR-0122 T3, PWA card rendering — **the AC-7 assembled seam**

Both pushed and awaiting master's gate. No watcher trigger will arrive (see §0).

## 2. Close the two ADR seams — the only thing keeping them open

- **ADR-0121 / FRE-887** — everything merged + deployed live. Open solely on **AC-9**, which needs an
  **owner-driven PWA check**: picker renders real candidates → switch model → next turn runs on it →
  survives reload → survives WS reconnect. FRE-920 stays Awaiting Deploy until it passes.
- **ADR-0122 / FRE-878** — open on **AC-7**, owned by FRE-921 (PR #599 above). Needs the deployed stack.

FRE-881/882 are merged but undeployed — they ride the next gateway rebuild, and have no live effect
until the FRE-921 card UI ships.

## 3. Reduce the backlog

~80 Approved; most carry no stream label (parked). Dispatch queue behind the pause: build1 FRE-925 → FRE-926. Method:
verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Owner to settle scope (Approved only vs all open states) and gate (cancel directly
vs list-first).

Note: the board reconciler now reads Linear (FRE-915), so drift is detectable automatically — run it
before culling. It already found FRE-432 and FRE-875 shipped-but-stale.

## 4. Questions for the owner

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

## 5. Then, in order

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

FRE-739 (needs FRE-740 + a live non-owner request) · FRE-717 (needs organic outcome
input).
