# ADR-0116: Event-Driven Dispatch Actuation (capability-gateway + MCP Channels)

**Status:** Accepted
**Date:** 2026-07-13
**Deciders:** Owner (dispatch/autonomy posture, delivery-contract design), adr session (Opus, design + de-risk spike)
**Tags:** dev-process, orchestration, claude-code, mcp-channels, dispatch, remote-control, FRE-852

---

## Context

**What is the issue we're addressing?**

Seshat's development loop dispatches turns into persistent Claude Code seats — `cc-master`, and the
worker seats `cc-build` / `cc-build2` / `cc-adrs` — when a gating event fires. Today this is **poll +
terminal injection + an idle-scrape**, implemented by the external `gating_watcher` (FRE-823):

1. The watcher **polls** GitHub (`gh`) and Linear on a fixed interval.
2. On a gating event it injects a turn by writing to the seat's terminal with `tmux send-keys`
   (`gating_watcher.py`): a ready CI-green PR pokes `cc-master` to gate it; a red-CI PR pokes the owning
   `cc-<stream>` worker to correct it.
3. Before injecting into a **worker** seat it runs a `tmux capture-pane` **idle-detection scrape** — "is
   this seat idle at the prompt, safe to type into?" — to avoid interrupting an active turn.

The scrape is the fragile part. A screen-shape heuristic over a rendered TUI, it has been **repeatedly,
expensively wrong**: FRE-825 (it *never* matched real Remote Control panes) and FRE-845 (it
*false-flagged an idle master as busy*, dropping a dispatch — since fixed by making master delivery
unconditional: `gating_watcher.py` gates on the scrape for **worker** triggers only,
`require_idle = trigger.kind != "master"`). It still gates every worker
delivery, and every RC/TUI rendering change risks re-breaking it. **Retiring the scrape is the prize.**

> **FRE-939 (2026-07-23).** Master delivery remains unconditional — the scrape still never *gates* it.
> But the master path now *reads* the pane as evidence: a send into a busy pane is recorded as
> `queued` (issued, receipt unobserved) rather than booked as a confirmed delivery. Assuming delivery
> is what let PR 602 sit ungated for nine hours with nothing to surface and nothing to retry. This
> makes the scrape's *unreliability* cheap on the master path in both directions: a false-busy costs
> at most a duplicate poke, never a lost one — which is a further argument for the channel cutover
> below, where delivery is confirmed rather than inferred from a rendered TUI at all.

**The mechanism that removes it now exists.** Claude Code **MCP Channels** (research preview, ≥ v2.1.80)
push an external event straight into a *running* session as a `<channel source="…">` tag — purpose-built
for exactly this (the docs name "CI results" as the use case). A channel is a small MCP server, spawned
as a subprocess of the seat, that turns an inbound HTTP POST into an in-session turn. If a pushed event
triggers a turn in an **idle** seat, the scrape is unnecessary: the channel *owns* delivery. Remote
Control, the Agent SDK, and headless (`-p`) mode were assessed and offer **no** session-targeted
injection into a live named session (capability assessment, 2026-07-10).

**What needs to be decided:** whether to replace the fragile *delivery* path (send-keys + scrape) with a
channel push; under what architecture; carrying what payload; and within what authority boundary — given
that the whole thesis rested on one unproven claim, now settled by a de-risk spike (below).

### The de-risk spike settled the load-bearing unknown (2026-07-13)

The docs demonstrate idle-seat triggering only for a **vanilla interactive** `claude` session. Our seats
are **not** vanilla: `launcher.py` starts them as `claude --remote-control <name> --model <tier> …` —
Remote-Control-steerable, at an assigned model tier. Nothing in the docs shows a channel composing with
that substrate. The spike (spec: FRE-852 / PR #484; results appended to
`docs/research/2026-07-11-event-driven-dispatch-actuation-spike.md`) proved all three load-bearing
hypotheses on this box (CC 2.1.207, Claude Max):

- **H1 entitlement — PASS.** Claude Max is an individual plan → Channels enabled, no org toggle.
- **H2 primitive — PASS.** A `curl` POST fired a turn in an idle vanilla seat, no keystrokes.
- **H3 substrate (load-bearing) — PASS.** A `curl` POST fired a turn in an idle
  `claude --remote-control <name> --model haiku …` seat — the **`launcher.py` interactive seat form**,
  which is the process that actually receives dispatch turns (the tmux `cc-*` pane the watcher targets) —
  **push→react ≈ 2.2 s**. A seat is simultaneously RC-steerable (phone/web) *and* channel-pushable, at
  its assigned tier, with no conflict. *Scope note:* `rc_server.py`'s server-mode daemon
  (`claude remote-control --spawn session --name seshat-<stream>`, no `--model`) is a **distinct** process
  and **not** a channel target; H3 does not cover it, and a build ticket must prove any channel need there
  separately before relying on it.

The spike also surfaced the **decisive implementation detail** that shapes the decision: the channel
flag is real but *undocumented* in `--help`, and the local-development flag
(`--dangerously-load-development-channels`) fires an **interactive per-launch consent** that a
non-interactive `launcher.py` start would block on. The binary exposes the intended production surface —
an **approved-allowlist** path (`--channels <servers…>` + `channelsEnabled` + `allowedChannelPlugins`) —
which is the route this ADR mandates for production.

---

## Decision

Adopt **event-driven dispatch actuation**: replace the send-keys + idle-scrape **delivery** hop with a
**per-seat MCP Channel push**, under a **capability-gateway** architecture, within a strict
**actuation-only** boundary. Concretely:

1. **Architecture — capability-gateway (openclaw platform model).** A long-running gateway daemon owns
   routing, per-seat targeting, dedup, the trigger ledger, permissioning, and logging. We already have
   its **embryo**: `gating_watcher` + `next_resolver` + `launcher` + `trigger_ledger` +
   `send_keys_whitelist`. **MCP Channels is one *reach surface* the gateway uses for the last hop
   (event → warm seat) — not the organizing principle.** This avoids the "pure MCP" failure mode of
   pushing orchestration and security into every session.

2. **Delivery — per-seat channel via the approved `--channels` allowlist path.** Each seat runs its own
   channel MCP server on its own port (`SESHAT_CHANNEL_PORT`); the gateway POSTs the event to the right
   seat's port instead of typing it. Production uses the **approved-allowlist** path
   (`--channels` + `channelsEnabled: true` + `allowedChannelPlugins`, channel packaged as an allowlisted
   plugin), **not** `--dangerously-load-development-channels` — because the dev flag carries a per-launch
   interactive consent that a non-interactive launch cannot answer. The first build ticket's gate is to
   prove the allowlist path launches a seat with the channel live and **zero blocking consent prompts**.

3. **Payload — structured PR state, not a pre-baked imperative.** The channel delivers the PR's **live
   state**: identity, mergeable/blocked, per-check **CI results**, and **dependabot status** — replacing
   today's terse `"PR #N failed CI checks - correct them"` string. The **warm seat reasons over that
   state and determines + applies the correction itself** (a code/config fix pushed to its own branch),
   rather than executing a generic instruction. Per-seat routing is unchanged: a worker seat corrects its
   PR; `cc-master` receives "green + dependabot-clean → ready" for its gate.

4. **Scope — Phase 1 (delivery swap) only.** This ADR swaps *delivery* and *enriches the payload*. The
   **poll stays as the event *detector*** (the poll is not the broken part — only the scrape is), now
   also reading dependabot status. **Replacing the poll with GitHub webhook ingress is explicitly
   Phase 2** and out of scope here.

5. **Cutover — per-seat mode flag, send-keys retained as fallback.** Each seat is *either* channel-mode
   *or* send-keys-mode, **never both** (to preclude double-fire). Seats flip **one at a time**;
   `send-keys` + the scrape remain live for not-yet-cutover seats. The scrape is **deleted only after the
   last seat cuts over** (the assembled seam).

6. **Boundary — actuation only (the ADR-0113 lesson).** "Apply the correction" means **author and push a
   fix to the session's own worker branch/PR** — nothing else. The session **never** pushes to, merges,
   approves, closes, or deploys a branch/PR it does **not** own. A **dependabot** PR is therefore analyzed
   only: the session may propose a *separate, worker-owned* follow-up PR, but must **never** push to the
   dependabot branch, nor merge/approve/close/deploy it. Merge/approve/deploy remain **master + human**
   gates, exactly as now. This ADR introduces **zero** new authority; it is a transport swap. The
   merge-authority invariant is **not restated here** — it is owned by `lifecycle-rules.md` § Session
   boundary ("build & adr sessions stop at push branch + open PR … master alone merges"), which this ADR
   cites as the normative source per that file's own single-source rule.

**Why this over the alternatives:** the scrape is a recurring, silent-failure tax on the delivery path;
the spike proves the channel push removes it *in our exact substrate today*. The capability-gateway
framing keeps orchestration and security in one owned daemon rather than smeared across seats, and the
`--channels` allowlist path avoids baking a "dangerous" dev flag with an interactive consent into the
systemd launch.

---

## Alternatives Considered

### Option 1: Harden the idle-scrape; keep poll + send-keys (status quo, repaired)

**Description:** Do not adopt channels. Instead make the `tmux capture-pane` idle detector robust —
tighten the pane-shape regex, add RC-pane awareness, add a settle delay — and keep the proven
poll + send-keys delivery.

**Pros:**
- No new dependency; no research-preview surface.
- Smallest change; the delivery path is already understood.

**Cons:**
- The scrape is heuristic over a *rendered TUI that Anthropic changes without notice* — it broke twice
  (FRE-825, FRE-845) and will break again on the next RC/TUI rendering change. Hardening reduces but
  does not remove a whole class of silent-drop failures.
- Keeps the fragile thing whose retirement is the entire point.

**Why Rejected:** it treats the symptom, not the class. The spike shows a *categorically* more robust
delivery (an explicit push with a file-write-confirmable turn, no screen-scraping) is available and works
in our substrate. Retained only as the do-nothing baseline; not chosen.

### Option 2: Pure-MCP — make each seat an MCP-server-driven autonomous node

**Description:** Push routing, targeting, and permissioning *into each session* via MCP, dissolving the
external gateway — the "it's all just MCP" model.

**Pros:**
- Conceptually uniform; no separate daemon.

**Cons:**
- Smears orchestration and (critically) **security/permissioning** across N sessions with no single
  owner of dedup, the trigger ledger, or the send-keys whitelist — the openclaw "pure-protocol" failure
  mode.
- An ungated inbound channel is a **prompt-injection vector**; centralizing the sender-gating in one
  daemon is far safer than N per-seat policies.

**Why Rejected:** the capability-gateway model (Option chosen) keeps exactly one owner of routing and
policy, using MCP only as a reach surface. Channels-as-org-principle inverts that and multiplies the
attack surface.

### Option 3: Ship on the `--dangerously-load-development-channels` dev flag

**Description:** Use the flag the spike proved working end-to-end, wired directly into the systemd seat
launch.

**Pros:**
- Proven today (H2/H3 both used it); zero additional build work.

**Cons:**
- Fires an **interactive per-launch consent** ("I am using this for local development") that a
  non-interactive `launcher.py` start cannot answer → the seat blocks on startup.
- Carries "dangerous / do not run channels from the internet" semantics unfit for a standing production
  launch, and maximal research-preview churn exposure.

**Why Rejected as the production path** (kept as a *documented, proven contingency* only): the approved
`--channels` allowlist path is the intended production surface and avoids the consent gate. Option 3 is
the fallback if the allowlist path hits a wall — it is **not wired** into the launch.

### Option 4: Gate on Channels reaching General Availability

**Description:** Accept the design but do not build until MCP Channels leaves research preview.

**Pros/Cons:** Removes API-churn risk, but indefinitely defers a proven, purpose-fit win while the scrape
keeps costing silent drops. **Why Rejected:** the fallback (send-keys) stays live throughout cutover, so
preview churn cannot regress live dispatch — the risk GA would remove is already mitigated by the per-seat
mode flag. Building now with a fallback dominates waiting.

---

## Consequences

### Positive Consequences

- **The idle-scrape class of bug is eliminated** on cutover seats — delivery becomes an explicit push
  with a turn-confirmable side-effect, not a screen-shape guess.
- **Richer corrections.** The seat sees real PR state (CI + dependabot) and reasons, instead of executing
  a generic "correct them" string — better fixes, and it can distinguish a flaky check from a real
  failure from a dependabot-flagged dependency.
- **Owner keeps mobile/web steering** — a seat is RC-steerable *and* channel-pushable at once (spike H3).
- **Security consolidates** in one daemon (sender-gating, dedup, ledger) rather than per-seat.
- **The spike artifact is the production embryo** — `webhook.mjs` hardens straight into the seat channel.

### Negative Consequences

- **A research-preview dependency** on the delivery path (mitigated by the retained send-keys fallback
  and per-seat cutover).
- **Two live delivery transports during cutover** (channel + send-keys) — more surface until the last
  seat flips and send-keys is retired.
- **New moving parts**: a per-seat channel process + port per seat, and a dependabot-status read added to
  the gateway's detection.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Approved `--channels` allowlist path can't launch headless (consent/other block) | High | First build ticket's **gate** is to prove it with zero blocking prompts; Option 3 dev-flag is the documented proven fallback |
| Double-fire (a seat gets both a channel push and a send-keys for one event) | High | Per-seat **mode flag** — channel-mode XOR send-keys-mode; `trigger_ledger` enforces exactly-once |
| Research-preview API churn breaks delivery | Medium | send-keys + scrape retained as fallback until every seat is proven; cutover one seat at a time |
| Channel notification dropped silently if a seat isn't listening | Medium | Gateway confirms delivery via the ledger; unacked → fall back to send-keys for that event |
| Ungated inbound channel = prompt-injection vector | High | localhost-only bind + shared-secret sender header, owned centrally by the gateway |
| "Apply corrections" creeps toward autonomous merge | High | Boundary AC (below) proves the channel-triggered turn produces commits/pushes only; merge stays master/human per `lifecycle-rules.md` |

---

## Implementation Notes

- **Files in play:** `scripts/dispatch/gating_watcher.py` (detector + delivery — swap send-keys for a
  channel POST on cutover seats; add dependabot-status read), `scripts/dispatch/launcher.py` (add the
  `--channels` allowlist flag + settings to the **interactive `--remote-control … --model` seat launch** —
  the seat that receives dispatch), a new per-seat channel server hardened from the spike's `webhook.mjs`,
  and `trigger_ledger` (add a `transport` field; exactly-once). `rc_server.py`'s server-mode daemon is
  **out of scope for Phase 1** — it is not a channel target; any future channel need there is a separate,
  gated build proof.
- **Settings:** the spike found `enableAllProjectMcpServers: true` suppresses the MCP-server consent but
  **not** the dev-channel consent; production must use `channelsEnabled` + `allowedChannelPlugins` — the
  first build ticket records the exact working settings key for the systemd launch.
- **Doc-drift reconciled by this PR:** ADR-0110's Status line cited the now-Superseded ADR-0113 as the
  superseder of its dispatch half; **this PR updates it** to point to ADR-0116 (0110's transport half is
  superseded by ADR-0116; its RC substrate + Linear-native dispatch contract + gateway embryo are
  retained).
- **`trigger_ledger` gains a `transport` field** (`channel` | `send_keys`) as build work — today's
  `LedgerEntry` records timing only; AC-4's exactly-once check depends on it.
- **Settled inputs:** FRE-846 (resolver wiring) and FRE-844 (dry-run-inert) merged 2026-07-10 — no longer
  open reconciliation items.
- **Testing strategy:** each build ticket proves its slice with an observable side-effect (a file-write
  turn, a ledger row, a headless launch with the channel live), mirroring the spike's measure-don't-assert
  discipline; no new test infrastructure required.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 (scrape retired on cutover seats)** — a dispatch event delivered via the channel fires a turn in
  an idle production seat **with the `capture-pane` idle-scrape provably not consulted** for that seat.
  · **Check:** run channel-mode delivery under an injected `CommandRunner`/probe that **fails the test on
  any `tmux capture-pane` invocation** for that seat, then drive a real gating event carrying a unique
  nonce; confirm the seat acts on the nonce **and** the ledger records exactly one channel delivery for
  it. · *Fails if* the probe observes any `capture-pane` for the seat, or the seat does not act on the
  push.
- **AC-2 (production allowlist path, no consent gate)** — a seat launches **non-interactively** (via
  `launcher.py`, no human at a TTY) on the `--channels` allowlist path with the channel **registered and
  live**. · **Check:** launch headless; assert the startup notice shows the channel injecting **and** no
  interactive consent/trust prompt blocked startup (`/mcp` reports the channel connected). · *Fails if*
  any blocking consent prompt appears, or the channel is not live after launch.
- **AC-3 (payload drives the action)** — the channel-triggered turn's action **is derived from the
  specific delivered PR state**, not a canned response. · **Check:** deliver payloads carrying unique
  failing-check names, failing-log excerpts, head SHA, and dependabot state; require the resulting
  commit message / PR comment to **reference the relevant check or dependency** and verify the changed
  files address **that exact payload**. Separately, a payload with **no actionable failure** must produce
  **no code change**. · *Fails if* the seat acts identically across distinct payloads, edits files
  unrelated to the delivered failure, or produces a change for a no-failure payload.
- **AC-4 (no double-fire)** — a channel-mode seat is **never** also send-keys'd for the same event.
  · **Check:** requires a build-added `transport` field on `trigger_ledger` (today's `LedgerEntry` has
  none). For one event id: assert the ledger records **exactly one** consumed delivery with
  `transport=channel`, **no** consumed/pending `transport=send_keys` entry for that same event id, **and**
  command-level instrumentation observes **zero `tmux send-keys`** calls to the channel-mode target.
  · *Fails if* two deliveries are recorded, a `send_keys` entry exists for the event, or any `send-keys`
  hits the channel-mode seat.
- **AC-5 (fallback intact)** — a **not-yet-cutover** seat still dispatches via send-keys, and a
  channel-mode seat whose channel is down **falls back** to send-keys rather than silently dropping.
  · **Check:** leave one seat in send-keys-mode and dispatch to it; separately, kill a channel-mode seat's
  channel and confirm the event still lands via fallback. · *Fails if* a send-keys-mode seat stops
  dispatching, or a downed channel drops the event with no fallback.
- **AC-6 (boundary — no new authority)** — a channel-triggered correction turn produces **commits/pushes
  to the session's own worker branch only**. · **Check:** for the event nonce, audit **git remote refs,
  the GitHub PR timeline (commits / reviews / merge / close), Linear issue history, and deploy logs**;
  the only mutations by the session are commits on its own worker branch — **no** PR approvals, merges, or
  closes; **no** Linear `Done`/`Awaiting Deploy` flips; **no** deploy invocations; and **no push to a
  branch it does not own** (dependabot included). · *Fails if* the session approves/merges/closes any PR,
  flips a Linear state, deploys, or pushes to a non-owned branch.

**Seam owner (assembled intent):** the **build stream** owns the assembled seam — every live seat is
channel-mode XOR send-keys-mode, and **the idle-scrape is deleted only after the last seat is cut over**.
The ADR (via master at the gate) does not close on the last child ticket merging; it closes when AC-1 and
AC-6 hold across **all** cutover seats with the scrape removed. This is the criterion no single child
ticket proves alone.

---

## References

- `docs/research/2026-07-10-event-driven-dispatch-actuation-capability-assessment.md` — capability assessment (MCP Channels vs RC/SDK/headless; openclaw platform-vs-protocol framing)
- `docs/research/2026-07-11-event-driven-dispatch-actuation-spike.md` — de-risk spike spec **+ appended 2026-07-13 results** (H1/H2/H3 PASS, flag + consent findings)
- `docs/architecture_decisions/ADR-0110-external-dispatch-orchestrator.md` — dispatch substrate + Linear-native contract retained; its transport half is superseded by this ADR
- `docs/architecture_decisions/ADR-0113-*.md` — Superseded; the autonomy-overreach lesson (actuation only) that bounds this ADR
- `.claude/skills/lifecycle-rules.md` § Session boundary — normative source for the master-only-merge invariant this ADR cites (does not restate)
- `scripts/dispatch/gating_watcher.py` — poll + send-keys + idle-scrape; the delivery path this ADR swaps
- `scripts/dispatch/launcher.py` — the interactive `--remote-control <name> --model` seat launch argv the channel flag is added to (the seat that receives dispatch)
- `scripts/dispatch/rc_server.py` — the distinct `remote-control --spawn` server-mode daemon; out of scope for Phase 1 (not a channel target)
- Claude Code docs — Channels (`code.claude.com/docs/en/channels.md`) and Channels reference (`…/channels-reference.md`)
- FRE-825 · FRE-845 — idle-scrape bugs (the prize) · FRE-846 · FRE-844 — settled inputs (merged 2026-07-10)
- FRE-852 — this ADR's tracking ticket · PR #484 — the spike spec

---

## Status Updates

### 2026-07-13 - Accepted
**Changed By:** Owner + adr session (Opus)
**Reason:** De-risk spike passed all three load-bearing hypotheses (H1/H2/H3) in our exact substrate on
2026-07-13, removing the one unproven assumption the design rested on. Owner settled the delivery contract
(structured PR + CI + dependabot state; session corrects, master merges) and selected the approved
`--channels` allowlist path as the production target. Accepted for the Phase-1 delivery-swap increment;
GitHub-webhook ingress deferred to Phase 2.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
