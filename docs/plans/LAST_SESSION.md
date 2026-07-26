# Last session — 2026-07-25/26 (shipped a wave, then found a cost incident and stopped everything)

## READ THIS FIRST — the environment is deliberately STOPPED

**The harness (`cloud-sim-seshat-gateway`) is STOPPED. The dispatch kill switch is ENGAGED. This is
intentional, owner-directed. Do not restart either without explicit instruction.**

- `docker stop cloud-sim-seshat-gateway` — run 2026-07-26 ~04:20Z. `/health` returns nothing by design.
- `telemetry/dispatch.disabled` present → dispatch daemon *and* the PR-gating watcher are both gated.
- Both build seats idle, both worktrees clean, 0 commits ahead. Nothing lost, nothing in flight.
- All datastores (postgres, ES, neo4j, redis, searxng, reranker, kibana, caddy, cloudflared) and the
  PWA shell are still up — they cost nothing and hold the evidence.

**Why:** a background process was spending real money on a loop that produced nothing, and cost
control turned out not to be trustworthy enough to leave running unattended. See §"The incident".

## Doing / discussing (≤5 sentences)

The first two-thirds of the session was ordinary delivery: gated and merged **PRs #667–#676** (seven
tickets), ran two bundled gateway+PWA deploys, and closed FRE-934/935/961/976/978/979/984 Done with
live evidence — the ADR-0123 turn-progress chain is now live end-to-end except its seam. The last
third was an incident: the owner asked why 321 budget denials existed when they had made ~67 turns,
and the answer overturned my analysis twice before landing on a **non-convergent retry loop** in the
ADR-0124 session-summary sweep. Investigating it exposed that **cost attribution is broadly
untrustworthy** — capped roles can bill to the wrong budget, three roles are uncapped and unmeasurable,
and a 100× cost jump ran three days unnoticed. The owner halted all development, then had me stop the
harness itself. **A new Linear project — "Cost, Process and Monitoring Audit" — now holds this work,
and each background stream will be studied objective-first in dedicated clean sessions.**

## Commits — the story behind the last ~10

- **PRs #667–#676** = seven merged tickets. FRE-978 (Stage-7 model-aware trim) · FRE-979 (ES cost
  skills → `api_cost_recorded`) · FRE-935 (absent-vs-zero client, verify-not-implement) · FRE-934
  (inference phases on the transport) · FRE-984 (safety hooks anchored to `$CLAUDE_PROJECT_DIR`) ·
  FRE-983 Phase-2 (durable field-limit + killed a clobbering template writer) · FRE-961 (server-side
  absent-not-zero) · FRE-936 (the live phase surface) · FRE-986 (phase-state projection).
- **#671** = ADR-0123 corrections: withdrew a stale liveness observation and recorded AC-2's operative
  gap-clock reading (the literal text is unsatisfiable given §3/§4 forbid filler events).
- **#678 = the incident report** — `docs/research/2026-07-25-captains-log-summarization-insights-cost-incident.md`.
  **STILL OPEN, no auto-merge, deliberately left for the owner.**
- **#677 = a cap raise I opened and then CLOSED myself.** It would have doubled `captains_log` to $10.
  Withdrawn once the denials proved to be one loop, not demand. Do not resurrect it.
- **Two outside factors the messages don't carry:** (1) codex was down (OpenAI circuit-open) during
  FRE-978, so that PR shipped without plan-review and I supplied the adversarial pass myself; (2) the
  dispatch daemon wedged mid-session on a ticket I canceled underneath it — which turned out to be the
  exact bug FRE-976 fixes, and restarting the daemon deployed the fix that then proved itself live.

## Worktrees — anything special

- **build (build1)** — idle, clean, on `fre-983-es-telemetry-lifecycle`. Was mid-FRE-983 when halted.
- **build2** — idle, clean, on `fre-986-phase-state-projection`.
- **Hooks are per-worktree:** the FRE-984 fix (hooks anchored to `$CLAUDE_PROJECT_DIR` instead of cwd)
  is live in the primary tree and build2 only. **build1, adrs and explore still run the old
  cwd-relative registrations** until each rebases on main. Not urgent while everything is stopped.

## Plan position + drift

MASTER_PLAN is **stale** — it still describes the 07-24 bug wave as the live thread and has no entry
for the audit. The ADR-0123 chain progressed far beyond what it records. **Deliberate drift, not
oversight:** the plan should not be rewritten around the audit until the owner has scoped it, and the
studies will reshape it. Treat MASTER_PLAN §0/§0b as historically-true-but-superseded until then.

## The incident (the thread to pick up)

**What happened.** The ADR-0124 session-summary idle sweep runs every 300s, regenerates each digest
**wholesale** (`f(all captures)`), and on failure leaves the session dirty and eligible **forever**.
The exclusion predicate requires *both* a terminal failure reason *and* the attempt ceiling, and
`budget_denied` is classified transient — so the ceiling is unreachable. Observed: attempt counter
**311** against a configured max of **2**; 358 attempts / 0 successes in 24h across 5 sessions.

**Cost.** 30-day total $25.51, **60% of it in the last three days**. `captains_log` = $10.52/14d,
more than all user inference. Delivered output over 14 days: **6 digests written, 2 merged proposals**.

**The correction that matters most:** the cap was never containment. It is a **daily allowance the
loop burns through** — it reset at midnight and had spent **$3.67 of $5.00 in four hours** with the
owner asleep and all dev halted. That is why the harness itself had to stop: the kill switch stops
dispatch and build seats, **not** the gateway's background schedulers.

**I was wrong twice, publicly, before getting there** — first blaming a backfill loop (disproved by
clock mismatch), then sizing a cap raise from denial volume treated as demand. Both were caught by the
owner asking a ratio question I should have asked myself. Worth remembering: *67 turns/week cannot
produce a 12-per-hour overnight pattern.*

## Answers for the fresh start

- **Do NOT restart the harness or clear the kill switch** without the owner saying so.
- **Do NOT raise any budget cap** as a remedy. A higher ceiling funds the loop. This is written into
  the audit project's posture on purpose.
- **The audit project is "Cost, Process and Monitoring Audit"** (Urgent, Needs Approval) covering
  process · reporting · monitoring · cost management. It holds **FRE-987** (the loop, root cause
  established), **FRE-989** (attribution audit, 5 findings), **FRE-988** (connection pooling).
  All three are **Needs Approval** — the owner has not approved them yet.
- **Next work is study, not build.** The owner's words: each stream gets a deep objective+implementation
  study in **new clean master sessions** — Captain's Log reflection, Summarization (session digest),
  and Insights, separately. Objective first. Do not patch.
- **Awaiting Deploy queue — 9 tickets, and the distinction matters.** **8 of 9 are already DEPLOYED**
  and are simply awaiting verification and an evidence-close: FRE-936 (deployed in the 19:51Z gateway +
  20:06Z PWA rebuild, UI behaviour never observed), and FRE-970/972/943/971/969/739/717 (deployed in
  earlier waves — the 06:35Z and 12:50Z rebuilds and older — never closed out).
  **FRE-986 is the ONLY one genuinely undeployed:** it merged at 20:29Z, *38 minutes after* the last
  gateway rebuild, so its code has never run anywhere. **It needs a deploy, not just verification**,
  whenever the harness comes back.
  Nothing here is verifiable while the harness is stopped — do not close any of them on inference.
- **PR #678** (incident report) is open with auto-merge deliberately OFF — the owner may want to read
  it before it lands.
- **A refinement to a standing fact:** a `fre-XXX` token in a docs PR **body** also triggers the Linear
  integration (it moved FRE-987 to In Progress). The known rule only covered branch and title.
