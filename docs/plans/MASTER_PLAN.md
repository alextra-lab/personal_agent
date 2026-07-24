# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-24 pm (a live turn surfaced a bug wave — sub-agent routing + config-design; ADR-0123 accepted)

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

## 0c. Model selection — sub-agent routing + the primary/sub pairing design

A live turn exposed that **sub-agent delegation was silently broken**: since ADR-0121 T5 (FRE-920)
rebound `sub_agent` to cloud `claude_sonnet`, the enforced-expansion path dialed a dead local endpoint,
so every sub-agent died and the primary ground the whole task solo. **FRE-958** fixes the routing
(merged; **deploy HELD** to bundle with FRE-963). The deeper design, owner-settled: **`sub_agent` has a
per-primary *default* (companion — qwen-thinking→qwen-instruct, sonnet→sonnet), open-ended override by
model *name* via the Config UI; selection is by name, not location** (owner accepts mismatch latency).
- **FRE-963** (build1, Urgent) — stopgap re-bind `sub_agent` → `qwen3.6-35b-instruct` + open. **Deploys
  with FRE-958 in one gateway rebuild** (ask-first) once its PR lands.
- **FRE-964** (adrs, queued behind FRE-957) — ADR-0121 amendment: the per-primary default *map* + a
  must-define-on-add guard + the Config UI override surface.
- **FRE-959** (SIGPIPE reported to the model as a tool failure) · **FRE-960** (query-paraphrasing has the
  same routing bug, fails-open → recall degraded to single-query since FRE-920; re-scoped as a routing
  bug, not egress) — both Needs Approval.

## 1. Reduce the backlog

~80 Approved; most carry no stream label (parked). Live queue: **build1 building FRE-963** (§0c),
**adrs building FRE-957** (§0, then FRE-964 queued); build2 empty (FRE-954 parked). Awaiting approval and
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

**FRE-958 + FRE-963 (ask-first — gateway rebuild, BUNDLED — TOP PICKUP)** — FRE-958 (sub-agent routing)
merged + Awaiting-Deploy-held; deploy together with FRE-963 (the re-bind) in ONE rebuild once FRE-963's
PR lands + is gated (§0c). Re-stop embeddings + verify after. · **FRE-938** (gateway + PWA, owner-gated)
— merged #617, PWA cache v35. · FRE-739 (needs FRE-740 + a live non-owner request) · FRE-717 (needs
organic outcome input).
