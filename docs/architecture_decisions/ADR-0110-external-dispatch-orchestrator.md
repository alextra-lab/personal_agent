# ADR-0110: External Dispatch Orchestrator for build/adr Worker Sessions

**Status:** Proposed (dispatch/transport half superseded by ADR-0116 event-driven actuation — poll + send-keys + idle-scrape → capability-gateway + MCP-channel push; the Remote Control execution substrate and the Linear-native dispatch contract are retained)
**Date:** 2026-07-04
**Deciders:** Owner (dispatch/autonomy posture), adr session (Opus, design)
**Tags:** dev-process, orchestration, claude-code, remote-control, dispatch, FRE-783

---

## Context

**What is the issue we're addressing?**

Seshat runs three worker streams — `build` (Stream 1), `build2` (Stream 2), and `adr` — each as a
persistent Claude Code session in its own git worktree (`.claude/worktrees/{build,build2,adrs}`). The
`prime-worker` skill runs a 20-minute monitor loop in each session that resolves the stream's **NEXT**
ticket from Linear (the Linear-native dispatch contract: `Approved` + `stream:<s>` + no open `blocked by`,
priority-ordered, oldest-first, with a busy guard on `In Progress`/`In Review`) and surfaces a one-line
dispatch card. It **advises only** — it does not launch work.

The **owner is the actuator**: for each dispatch the owner reads the card and manually (1) switches the
session's model tier to the ticket's `Tier-*` label, (2) `/clear`s for a fresh context (unless the ticket
carries `context:keep`), and (3) types `/build N` or `/adr`. Then the owner watches the session in the
terminal and answers permission and decision (`AskUserQuestion`) prompts as they arise.

That per-ticket **switch → clear → type** cycle carries no judgment. Both approval gates already live
elsewhere: the **owner** decides *whether* work proceeds (marks a ticket `Approved`), and **master**
decides *when/where* (applies the `stream:*` label) and gates the merge (review → merge → deploy →
verify → close). The dispatch mechanics in between are toil. The owner wants that toil removed **without**
giving up the two things they value in the current loop: **seeing the development happen** and **answering
permission/decision prompts themselves** — now from any device, not only the VPS terminal.

**What needs to be decided:** whether and how to externalize worker *dispatch* — the resolve-NEXT +
launch mechanics — while (a) keeping the human in the loop for permissions and decisions, (b) keeping
master's role and both approval gates exactly intact, and (c) respecting the shared-VPS concurrency
ceiling (one pytest lock, one test substrate on ports 7688/9201/5433, one SLM/embedder on 8502/8503/8504).

Two leads were investigated per FRE-783: an external script driving `claude`, and the Linear agents API.

### Findings (FRE-783 research)

- **The two leads collapse into one substrate.** Linear's agents capability does **not** run the worker.
  A Linear agent is an OAuth app (`actor=app`, scopes `app:assignable` / `app:mentionable`); assigning or
  @mentioning it fires an `AgentSessionEvent` webhook to an **external service you host**, which must ack
  with a `thought` activity within ~10 s and stream activity back via GraphQL. Linear is a
  notification/coordination surface, not an execution engine — an external launcher is still required
  either way. "Linear agents" is therefore a *possible future trigger/UX front-end*, not a distinct
  dispatch substrate. (Assessed during FRE-783 from linear.app/docs/agents-in-linear and
  linear.app/developers — a doc-sourced research finding, not an independently re-verified guarantee.)

- **Headless skill invocation works — proven empirically (the ticket's pivotal question).** A spike ran
  `claude -p "/spike-probe"` against a throwaway skill whose SKILL.md body contained a nonce the model
  could not otherwise produce. Result (v2.1.201):
  - `result` returned the exact nonce → print mode **loads and executes a skill's SKILL.md body**; it does
    not treat `/spike-probe` as literal prompt text.
  - `num_turns: 3` → a **full multi-step agentic loop** ran (two sequential Bash calls + final), not a
    single-shot response.
  - `--model haiku` was honored; process **exit code 0**; `--output-format json` returned `session_id`,
    `result`, `total_cost_usd`, and `terminal_reason: "completed"`.
  - Under `--permission-mode dontAsk` + `--allowedTools "Bash(echo:*)"`, `permission_denials: []` — the
    recommended *unattended* permission config runs with zero prompts and zero denials.
  This confirms the execution core (skill + model tier + agentic loop) is available to a non-interactive
  launcher. (Doc-corroborated: code.claude.com/docs/en/headless.md.)

- **But the owner does not want the fully-unattended path yet.** The owner's decision (2026-07-04):
  keep the loop **"same as now"** — *see the dev take place* and *answer decision/permission questions* —
  but reachable from any device via **Remote Control**. Fully-unattended `dontAsk`/`bypassPermissions`
  removes the human from the loop; on a shared VPS with `git push` and prod-adjacent credentials, the
  owner chose to keep answering prompts for now. So the execution layer must be **interactive and
  remotely answerable**, which print mode is not — print mode with default (asking) permissions would
  simply fail on the first prompt with no one to answer it. **Remote Control** is the primitive that makes
  a VPS-hosted interactive session observable and answerable from claude.ai web or the mobile app.

---

## Decision

Build a small **external dispatch orchestrator** on the VPS that removes the actuation toil, paired with a
**Remote Control execution substrate** that keeps the owner watching and answering — master's role and
both approval gates unchanged.

**1. Execution substrate — Remote Control server on the VPS.**
Run Claude Code in Remote Control **server mode** as a long-lived, no-TTY daemon under systemd
(`Restart=always`), reachable by the owner from claude.ai/code or the Claude mobile app. It requires the
claude.ai OAuth subscription (Pro/Max/Team/Enterprise), not an API key, and the Anthropic endpoint
(Remote Control is disabled when `ANTHROPIC_BASE_URL` points off-Anthropic). Sessions run against the
**existing per-stream worktrees** (`build`, `build2`, `adrs`) — not ephemeral `--spawn worktree` clones —
so each stream keeps its stable branch and working tree. Permission mode stays **default (asks)**; the
owner answers permission and `AskUserQuestion` prompts from their device.

**2. Dispatch orchestrator — resolve NEXT + prepare/launch.**
A small script/service (Python, `scripts/dispatch/`) that, per stream:
- Resolves the stream's NEXT via the **Linear GraphQL API using an API key** (`AGENT_LINEAR_API_KEY`,
  `https://api.linear.app/graphql`) — deliberately **not** the Linear MCP, which is
  claude.ai-OAuth-authenticated, WAF-sensitive to CLI-shaped tokens, and of uncertain availability in a
  headless context. It reuses the dispatch contract verbatim: busy guard (`In Progress`/`In Review` on
  `stream:<s>` → occupied), then the head of `Approved` + `stream:<s>` ordered by priority (desc) then
  oldest-created, skipping any ticket with an open `blocked by` relation (open = blocker not yet
  `Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`; a relation to an already-terminal blocker is treated
  as satisfied per lifecycle-rules § Dispatch).
- Reads the ticket's **model tier** (`Tier-1:Opus`/`Tier-2:Sonnet`/`Tier-3:Haiku` → `opus`/`sonnet`/`haiku`)
  and **context flag** (`context:keep` present → KEEP; absent → CLEAR).
- **Launches by context contract — two distinct paths, never conflated:**
  - **CLEAR ticket** (default): the orchestrator may create/prepare a **fresh** RC worker session for that
    stream, set the model to the resolved tier, and seed or surface `/build N` / `/adr`. A fresh session
    satisfies CLEAR by construction (no prior task context).
  - **KEEP ticket** (`context:keep`): the orchestrator must target the stream's **existing warm** session
    only — never a fresh/cleared session, since KEEP means "preserve the warm context, do not `/clear`." If
    it cannot prove the target RC session *is* that stream's existing warm session, it **does not launch**;
    it surfaces the dispatch card and requires owner/manual continuation. KEEP is deliberately not
    machine-auto-launched.
  The exact no-TTY seed-and-launch incantation, and whether RC exposes programmatic session-create with a
  chosen model, are implementation spikes (see Risks); the v1 design does **not depend** on zero-tap
  auto-seed — see §4.
- **Concurrency — grounded on the existing guard, not a new one.** The single-critical-section protection
  is the **existing `check-pytest-lock` PreToolUse hook**, which denies a second concurrent `pytest`
  (exit 2), instructs the worker to wait and retry, and records the collision to
  `telemetry/pytest_lock_blocks.log`. The orchestrator's obligations are therefore concrete: (a) it never
  launches workers in a mode that strips hooks (`--safe-mode`/`--bare`) — the hook must remain live; and
  (b) it never itself initiates a second worker for an already-occupied stream (busy guard). It does not
  claim to serialize the workers' later `make test` calls — the hook does that, and CI re-runs tests at the
  master gate regardless, so a mishandled local block cannot reach a green PR.

**3. The human loop is preserved, the toil is removed.**
What the orchestrator removes: resolving NEXT, switching the model tier, `/clear`ing for fresh context,
and typing the command — the parts with no judgment. What the owner keeps: watching the session live and
answering every permission and decision prompt, now from any device via Remote Control. This is the
owner's stated "same as now, but I can respond and monitor" — a **monitored** loop, not an unattended one.

**4. Graceful degradation (v1 does not hinge on the two undocumented mechanics).**
**Three** Remote-Control mechanics are undocumented and LOW-confidence: (a) auto-seeding a slash command as
a session's first turn from a no-TTY launch; (b) **programmatic session creation with a chosen model** (set
the tier at create-time without a human picking it); and (c) programmatic completion detection for RC
sessions. The design is layered so v1 is valuable even when these spikes fail, and — critically — the
fallback never *claims* a state it cannot prove:
- **Auto-seed + programmatic model-set work →** fully machine-initiated launch: fresh session, correct
  model, skill running; owner monitors + answers only. Full toil removal.
- **Auto-seed fails but model-set works →** orchestrator prepares a correctly-modeled fresh session and
  surfaces the exact command; owner taps and sends `/build N`. Model-switch + clear + resolve toil gone.
- **Model-set also fails →** the fallback is **advisory parity with `prime-worker`, not full toil removal**:
  the orchestrator resolves NEXT and surfaces a device-visible dispatch card with the exact model and
  command, and the owner performs the model switch + dispatch. It **must not silently launch at an unknown
  model** — if it cannot prove the tier, it reports `manual-model-required`. This is honest degradation: it
  still removes the resolve-NEXT toil and delivers the card to any device, but does not pretend to have set
  a model it did not.
- **Completion detection works →** the orchestrator advances to the next dispatch automatically.
- **Completion detection fails →** advance is triggered by the durable signal the worker already produces —
  an open PR + the Linear ticket at `In Review` (GitHub integration) — polled exactly as `prime-worker`
  does today. This is the robust default; auto-detection is only a latency optimization. Note this signal
  proves *success*, not *liveness*: a stalled or failed run (prompt left unanswered, worker crashed, PR
  never opened) is caught by a **stall timeout** + push-notification on a pending prompt, not by advancing.

**5. Boundary — master is untouched.** The orchestrator handles worker **dispatch only**. It never merges,
deploys, closes tickets, or edits MASTER_PLAN. The worker sessions still stop at "push branch + open PR"
(the `/build` and `/adr` skills already enforce this). Master remains the sole gate to `main`.

Linear agents (webhook-triggered dispatch + in-issue agent-session UX) is **assessed and deferred** as an
optional future front-end that could replace the orchestrator's *poll* with a *push* — it does not change
the substrate and is not built in v1.

---

## Alternatives Considered

### Option 1: Status quo — `prime-worker` advises, owner fully actuates
**Description:** Keep today's loop: the monitor surfaces a card, the owner switches model, `/clear`s, and
types the command in the VPS terminal.
**Pros:**
- Zero new moving parts; already working and understood.
- Human fully in the loop by construction.
**Cons:**
- The toil the ticket exists to remove remains: per-ticket switch/clear/type is manual and tethered to the
  VPS terminal.
- No mobility — the owner must be at (or SSH'd into) the machine to dispatch and to answer prompts.
**Why Rejected:** It preserves precisely the judgment-free toil FRE-783 targets. Removing that toil loses
no gate, since approval (owner) and merge (master) live elsewhere.

### Option 2: Fully-unattended headless (`claude -p --permission-mode dontAsk` + allowlist)
**Description:** The orchestrator launches each worker with `claude -p`, a curated `--allowedTools`/
`permissions.allow` allowlist, and no human in the run. Spike-proven feasible (zero denials).
**Pros:**
- Maximum toil removal — no human touch per dispatch.
- Deterministic model tier and fresh context by construction; `--output-format json` gives a clean
  success/exit signal for the advance loop.
**Cons:**
- Removes the human from permission/decision prompts entirely. On a shared VPS with `git push` and
  prod-adjacent credentials, a mis-scoped or hallucinated action has real blast radius and no live veto.
- A too-narrow allowlist silently denies mid-build and stalls; a too-wide one approaches `bypassPermissions`.
**Why Rejected (for now):** The owner explicitly chose to keep watching and answering prompts. Documented
here as the **future escalation path**: once the monitored loop has built trust and an allowlist is proven,
individual streams (e.g. low-risk Tier-3 work) could graduate to `dontAsk`. Not v1.

### Option 3: Linear agents webhook app as the dispatch substrate
**Description:** Register a Linear agent app; assigning/mentioning it fires `AgentSessionEvent` to a hosted
endpoint that drives the worker and streams agent-session activity back into the issue.
**Pros:**
- Event-driven (push, not poll); dispatch state and activity visible natively inside Linear.
**Cons:**
- Linear does not execute the worker — a hosted launcher is still required, so this is *additive* surface,
  not a replacement: an OAuth app, a public webhook endpoint, the 10-second ack contract, and
  agent-session GraphQL mutations.
- Larger attack/ops surface (public ingress, app credentials) for marginal UX over the existing poll.
**Why Rejected (as substrate):** It solves triggering, not execution, and the execution launcher is the
hard part. Kept as an optional future front-end that can wrap the same orchestrator.

### Option 4: Claude Agent SDK orchestrator (Python/TypeScript)
**Description:** Build the orchestrator on the Claude Agent SDK for programmatic control (structured
results, tool-approval hooks) instead of shelling out to the `claude` CLI.
**Pros:**
- Fine-grained programmatic control; natural fit if the loop later needs result-based routing.
**Cons:**
- More code up front; re-implements what `--model` + skill-seed + Remote Control already provide.
- The SDK path is oriented at unattended programmatic runs, not at the owner-answers-remotely model the
  owner asked for; Remote Control is a CLI/claude.ai feature, not an SDK primitive.
**Why Rejected (for v1):** The CLI already exposes every lever the design needs. Revisit if a future
fully-programmatic (Option 2) loop wants richer control than shell + JSON output.

---

## Consequences

### Positive Consequences
- The judgment-free dispatch toil (resolve NEXT, switch model, `/clear`, type, pre-position worktree) is
  removed; the owner keeps full visibility and veto over every permission/decision, from any device.
- Model tier and the ticket's context contract (**fresh** for CLEAR, **warm** for KEEP) become correct
  **by construction** — the orchestrator sets the tier and honors the context flag — eliminating the class
  of errors where a session runs the wrong tier, is `/clear`ed when it should stay warm, or carries stale
  context when it should be fresh.
- Reuses the proven Linear-native dispatch contract unchanged — one dispatch semantics across
  `prime-worker` and the orchestrator, resolved from durable Linear state.
- Master's role and both approval gates are structurally untouched; the orchestrator is dispatch-only.
- Poll-based and API-key-based: no new public ingress, no dependence on headless MCP OAuth.

### Negative Consequences
- A new long-lived component (systemd RC server + orchestrator) to run and maintain, plus a dependency on
  the Remote-Control research-preview feature and its entitlement/endpoint constraints.
- `context:keep` tickets are not machine-auto-launched: KEEP means "preserve the warm context," so the
  orchestrator targets the existing warm session or, if it cannot prove that target, surfaces the card for
  manual continuation (Decision §2, KEEP path). This is a deliberate scope limit, not a contradiction.
- A machine-initiated launch can leave a permission prompt waiting on an absent owner — the worker stalls
  until answered (no worse than today's terminal, but now the trigger is automatic, so a stall is
  unattended by default). Requires push-notification on prompt + a stall timeout; the durable
  PR-open/`In Review` signal proves success, not liveness, so a stall/failure must be detected separately.
- Three load-bearing RC mechanics (auto-seed first turn; programmatic session-create with a chosen model;
  completion detection) are undocumented and must be spiked; v1 degrades gracefully if they fail
  (Decision §4) — but the fallback is bounded to advisory parity with `prime-worker` and never claims a
  model/context state it cannot prove.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Auto-seeding `/build N` as the first turn from a no-TTY launch is undocumented and may not work | Medium | Spike T2 before building the loop; v1 falls back to owner-taps-prepared-session (Decision §4) — the resolve-NEXT toil removed is unchanged either way |
| Programmatic RC session-create at a chosen model (without a human picking it) is undocumented | Medium | Spike T2; if unavailable, the fallback is advisory parity — orchestrator reports `manual-model-required` and never silently launches at an unknown model (Decision §4, AC-7) |
| No programmatic completion signal for RC sessions | Medium | Advance on the durable signal the worker already emits (open PR + ticket `In Review`), polled as `prime-worker` does today; auto-detect is an optimization, not a dependency |
| A stalled/failed run (unanswered prompt, crash, PR never opens) looks idle, not done | Medium | Stall timeout + push-notification on a pending prompt; the orchestrator advances only on positive PR/`In Review` evidence, never on silence (AC-7) |
| RC server process dies on extended network outage with no auto-restart | Medium | systemd `Restart=always` + a liveness check in the orchestrator that refuses to dispatch when the RC server is down |
| Orchestrator double-dispatches or picks a blocked ticket (dispatch-contract drift) | High | The resolver is the single source; a dry-run mode asserts parity with the `prime-worker` Step-4 selection before it is allowed to launch (AC-1, AC-6) |
| Two orchestrated workers run `pytest` against the single test substrate at once | High | The existing `check-pytest-lock` PreToolUse hook denies the second `pytest` (exit 2) and logs the collision; the orchestrator preserves the hook (never `--safe-mode`/`--bare`) and never dispatches into an occupied stream; CI re-runs tests at the master gate (AC-5) |
| Remote Control not entitled / disabled off-Anthropic-endpoint | Low | One-time `claude auth login` (claude.ai) + entitlement check at server start; documented precondition, fail-fast if unmet |
| Orchestrator oversteps into master's lane (merge/deploy/close) | High | Hard boundary: the orchestrator has no merge/deploy/close code path and no write scope beyond Linear state reads + launching sessions; worker skills already stop at PR (AC-4) |

---

## Implementation Notes

**Components**
- `scripts/dispatch/next_resolver.py` — the dispatch contract as a library + CLI: given a stream, return
  its NEXT (or none) from Linear via `AGENT_LINEAR_API_KEY`. Pure, dry-runnable, unit-testable against
  fixture board states. Mirrors `scripts/reconcile_board.py`'s Linear-API approach.
- `scripts/dispatch/launcher.py` — the RC session launch primitive: given (stream, worktree, model, skill
  command), initiate/seed the worker session. Encapsulates the T2 spike outcome and its fallback.
- `scripts/dispatch/orchestrator.py` — the loop: for each stream, if idle and a NEXT exists, launch;
  hold the concurrency mutex; advance on completion (PR-open/`In Review`). Structlog with `trace_id`.
- `deploy/systemd/claude-remote-control.service` — the RC server unit (`Restart=always`).

**Dependencies / preconditions**
- Remote Control entitlement on the claude.ai account; `claude auth login` completed on the VPS.
- `AGENT_LINEAR_API_KEY` present (already a documented setting).
- No Alembic; no schema changes. No `src/` behavior change — this is dev-process tooling under `scripts/`.

**Testing strategy**
- `next_resolver` unit tests over fixture board states (busy guard, priority order, blocked-head skip,
  wrong-stream exclusion) — no live Linear.
- A parity test asserting the resolver's pick equals the `prime-worker` Step-4 selection for the same board.
- RC launch + owner-answers-prompt + completion is validated in a live, owner-in-loop dispatch (T3/seam),
  since Remote Control inherently requires the owner's device.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — The orchestrator's NEXT equals the contract's NEXT.** For a given Linear board state, the
  resolver returns exactly the ticket `prime-worker` Step 4 would (correct stream, priority order,
  blocked-head skipped, busy guard honored). **Check:** parity test over ≥5 fixture boards covering: a
  higher-priority-but-blocked head (skipped), a wrong-stream decoy (excluded), an occupied stream (busy
  guard → no candidate), a no-candidate board (empty), and a **stale-but-satisfied blocker** — a `blockedBy`
  relation to an already-terminal blocker (`Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`), which must
  **not** be treated as blocking (per lifecycle-rules § Dispatch). *Fails if* it returns a genuinely-blocked,
  lower-priority, or wrong-stream ticket, a ticket when the stream is busy, or **skips** a ticket whose only
  blocker is already terminal.
- **AC-2 — Dispatch preserves the ticket's context contract and model tier.** For a **CLEAR** ticket, the
  launched session has no prior task context and runs the labeled skill (e.g. `/build` executing its Step 0
  reset) at the ticket's `Tier-*` model. For a **KEEP** ticket, the orchestrator either targets the existing
  warm stream session without clearing, or refuses auto-launch and surfaces manual continuation — **never** a
  fresh/cleared session. **Check:** one CLEAR dispatch (fixture or live) and one KEEP dispatch (simulated or
  live) inspect the session's context disposition/identity, its first substantive action, and its model.
  *Fails if* a KEEP ticket is launched into a fresh/cleared session, a CLEAR ticket's session carries prior
  task context, the `/build`/`/adr` text is echoed as literal input rather than invoking the skill, or the
  model differs from the `Tier-*` label.
- **AC-3 — The owner can answer a prompt from the remote surface and the run proceeds.** During a dispatched
  run, a permission or `AskUserQuestion` prompt appears on claude.ai web/mobile, and answering it there lets
  the worker continue. **Check:** observe one live dispatched run; approve a real prompt from a device other
  than the VPS terminal; confirm the tool then executes. *Fails if* remote answers do not reach the session
  or the session cannot proceed without the local TTY.
- **AC-4 — Master's gate is untouched.** After a dispatched run completes, the outcome is an **open PR** with
  the ticket at `In Review` — never a merge to `main`, a deploy, or a `Done`/closed ticket performed by the
  orchestrator or worker. **Check:** after one dispatched build, `gh pr view` shows an open PR and
  `git log origin/main` is unchanged by the worker; Linear shows `In Review`, set by the GitHub integration,
  not the orchestrator. *Fails if* any orchestrated run mutates `main`, deploys, or closes a ticket.
- **AC-5 — Orchestrated dispatch cannot put two `pytest` runs on the substrate at once.** When one worker is
  running `pytest`, a second orchestrated worker that reaches `make test` is denied by the `check-pytest-lock`
  PreToolUse hook (exit 2) **before** a second `python -m pytest` process starts, and the collision is
  recorded in `telemetry/pytest_lock_blocks.log`. **Check:** an instrumented run starts two orchestrated
  workers that both reach the test phase; assert only one `python -m pytest` process is ever live, the hook
  logs a block for the other, and neither worker was launched with hooks stripped (`--safe-mode`/`--bare`).
  *Fails if* two `pytest` processes run concurrently, the protection is only a post-hoc log with no actual
  denial, or a worker ran with the hook disabled.
- **AC-6 — No double-dispatch.** Given a stream whose `stream:<s>` label already has an `In Progress` or
  `In Review` ticket, the orchestrator launches nothing for that stream. **Check:** dry-run the orchestrator
  against a board with an occupied stream; assert zero launches for it. *Fails if* it initiates a second
  worker for an occupied stream.
- **AC-7 — Fallbacks are explicitly bounded and actually work.** *(a)* With auto-seed / programmatic
  model-set forced off, the orchestrator either prepares a correctly-modeled session and surfaces the
  command, or emits `manual-model-required` with the exact model + command — it **never silently launches at
  an unknown model**. *(b)* With RC completion detection forced off, it advances a stream **only** after
  durable PR-open + Linear `In Review` evidence, and never on silence. **Check:** force each mechanic off and
  run a dispatch; assert (a) the emitted fallback state names the required owner action and the resolved
  model, and no launch occurs at an unproven model; (b) advance happens only after the PR/`In Review` signal,
  and a simulated stall (no PR) triggers the stall path, not an advance. *Fails if* a fallback implies a
  model/context state it cannot prove, or a stream advances without durable PR/`In Review` evidence.

**Seam owner (decomposed ADR):** **master owns final seam verification** on the FRE-783 umbrella /
orchestrator-integration ticket (T3) — asserting the **assembled** intent (resolve → launch → owner-monitored
run → end-at-PR → advance) holds end-to-end for one real ticket per stream with master's gate intact. The ADR
does not close because its last child merged; it closes when master has demonstrated that end-to-end monitored
dispatch once for a build stream and once for adr.

---

## References

- Linear issue: FRE-783 — Research: external orchestrator for build/adr dispatch (loop-equivalent) + Linear agents API
- FRE-781 — worker follows master PR bounces (two-channel comment contract) — the prior dispatch carve-out
- `.claude/skills/prime-worker/SKILL.md` — the current in-session monitor + dispatch card
- `.claude/skills/lifecycle-rules.md` § Dispatch (Linear-native) — the dispatch contract this reuses
- `scripts/reconcile_board.py` — existing Linear-API board reconciler (pattern for the resolver)
- Claude Code headless mode — https://code.claude.com/docs/en/headless.md
- Claude Code Remote Control — https://code.claude.com/docs/en/remote-control.md
- Claude Code permission modes — https://code.claude.com/docs/en/permission-modes.md
- Linear agents (product) — https://linear.app/docs/agents-in-linear
- Linear developer platform (agents/OAuth `actor=app`) — https://linear.app/developers
- Spike evidence: `claude -p "/spike-probe"` v2.1.201 — `result` == skill nonce, `num_turns: 3`,
  `--model haiku` honored, exit 0, `permission_denials: []` under `dontAsk` + allowlist (this session, 2026-07-04)

---

## Status Updates

### 2026-07-04 - Proposed
**Changed By:** adr session (Opus), FRE-783
**Reason:** Research complete (headless-skill spike run and reported; Linear agents assessed as a
coordination surface, not an execution substrate). Owner set the autonomy posture: monitored via Remote
Control, human answers permissions/decisions, master's role unchanged. Recommendation: external poll-based
orchestrator + RC execution substrate, decomposed into implementation tickets.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
