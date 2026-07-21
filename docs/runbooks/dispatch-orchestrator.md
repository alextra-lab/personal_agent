# Runbook — Dispatch Orchestrator (ADR-0110)

Operational guide for the external dispatch orchestrator and its Remote Control
(RC) execution substrate. Backing design: `docs/architecture_decisions/ADR-0110-external-dispatch-orchestrator.md`.
The orchestrator removes the judgment-free dispatch toil (resolve NEXT, switch
model, `/clear`, type the command); the owner keeps watching and answering every
permission/decision prompt from any device via Remote Control.

## Master's role and both approval gates are unchanged

**The orchestrator handles worker dispatch only. It never merges, deploys,
closes tickets, or edits MASTER_PLAN.** Both approval gates are exactly as
before: the **owner** decides *whether* work proceeds (marks a ticket
`Approved`), and **master** decides *when/where* (applies the `stream:*` label)
and gates the merge (review → merge → deploy → verify → close). Worker sessions
still stop at "push branch + open PR" (the `/build` and `/adr` skills enforce
this). Master remains the sole gate to `main`. Automating the *actuation* between
those gates changes none of them.

## Enable-once precondition (fails fast when unmet)

Remote Control is entitlement-gated and endpoint-sensitive. Complete these once
on the VPS before enabling the units. The orchestrator's `--preflight` checks the
statically-verifiable subset and **exits non-zero** when unmet, so the systemd
unit refuses to start (`ExecStartPre`).

| Precondition | How to satisfy | Checked by |
|---|---|---|
| claude.ai subscription (Pro/Max/Team/Enterprise), **not** an API key | `claude auth login` → choose claude.ai; unset `ANTHROPIC_API_KEY` | `claude doctor` (manual) |
| Full-scope login present | `claude auth login` (a `setup-token`/`CLAUDE_CODE_OAUTH_TOKEN` is inference-only and cannot establish RC) | `claude doctor` (manual) |
| Workspace trust accepted | run `claude` once in each worktree (`build`, `build2`, `adrs`) and accept the trust dialog | `claude doctor` (manual) |
| Endpoint on Anthropic | `ANTHROPIC_BASE_URL` **unset** or `https://api.anthropic.com`; RC is disabled off-endpoint | `--preflight` (fails fast: `rc-endpoint-off-anthropic`) |
| Linear API key present | export `AGENT_LINEAR_API_KEY` | `--preflight` (fails fast: `linear-api-key-missing`) |
| Mobile push for pending prompts | in `claude`, `/config` → enable **Push when actions required** (and optionally **Push when Claude decides**) | manual |

Verify the whole set: `/opt/seshat/.venv/bin/python -m scripts.dispatch.orchestrator --preflight`
(prints `preconditions ok; remote-control reachable=<bool>`; exit 0 only when RC is reachable).
Diagnose entitlement failures with `claude doctor`.

Note: `--preflight` covers the deterministic config (Linear key, RC endpoint) plus
**global RC reachability** — it does not prove any specific stream's session is up.
Auth/entitlement/subscription are the manual `claude doctor` rows above; they are
never conflated into the fail-fast config check.

## Install and enable

```
# from /opt/seshat on the VPS
sudo install -m 644 infrastructure/systemd/claude-remote-control@.service /etc/systemd/system/
sudo install -m 644 infrastructure/systemd/seshat-dispatch-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload

# one RC server per stream (server mode, on the existing worktree)
sudo systemctl enable --now claude-remote-control@build1
sudo systemctl enable --now claude-remote-control@build2
sudo systemctl enable --now claude-remote-control@adr

# the orchestrator daemon (preflights, then --loop --execute)
sudo systemctl enable --now seshat-dispatch-orchestrator
```

## Guardrails

- **RC restarts on failure.** RC exits after a prolonged (~10 min) network
  outage; `Restart=always` (`RestartSec=10`) brings each server back.
- **Liveness — refuse to dispatch when RC is down.** Before launching, the
  orchestrator probes global RC reachability (`claude agents --json --all`, exit
  0). When RC is down it launches nothing and emits `dispatch_blocked`
  (`reason=rc-down`) — no stream is falsely marked in-flight.
- **Kill switch.** `touch telemetry/dispatch.disabled` halts **all** dispatch
  immediately (next tick logs `dispatch_blocked reason=kill-switch` and launches
  nothing). Remove the file to resume. Use this to stand dispatch down without
  stopping the daemon.
- **Stall timeout + push.** A launched run with no PR past the stall timeout
  (default 1 h) emits `dispatch_stall` once (surfaced in journald). Pending
  permission/decision prompts reach the owner's phone via **native RC push**
  (enabled in the precondition table) — the durable open-PR + `In Review` signal
  proves *success*; the stall path + push cover *liveness*.
- **Concurrency.** The orchestrator never launches into an occupied stream and
  never strips hooks, so the `check-pytest-lock` PreToolUse hook stays live and
  CI re-runs tests at the master gate regardless.
- **Persistent seats — the dispatcher never terminates a seat (FRE-913).** Seats
  are long-lived: a dispatch *prepares* a seat, it never destroys one. The
  launcher owns **no** tmux termination code at all (no `kill-session`,
  `kill-pane`, or `respawn-pane`), enforced by a source-level test.

  Between 2026-07-08 (commit 377d0646) and this fix, every dispatch killed the
  seat and immediately recreated it. The new `claude` could not reclaim its
  Remote Control name before the old registration was released, so it silently
  registered under a fallback name (observed live: a seat launched as
  `cc-build` came up as `build-41`) — alive and working, but invisible on the
  owner's mobile RC view. Seat lifecycle belongs to `cc-sessions`, not the
  dispatcher; that overlap is where the regression came from.

  Three seat states, only two of which act:

  | state | meaning | action |
  |---|---|---|
  | `live` | tmux session exists, pane runs `claude` | **reuse** — `/clear`, `/model <tier>`, `/build <FRE-id>` typed in-session; process untouched |
  | `absent` | no tmux session | **create** — the only path that starts a seat |
  | `unhealthy` | session exists, pane is not `claude` | **surface only** — recover it with `cc-sessions` |

  Delivery is **send-keys**, not the ADR-0116 channel: `/clear` and `/model` are
  Claude Code client commands the TTY interprets, so channel-delivered text
  would be inert prose. The channel keeps its own job (structured PR/CI gating
  events the seat reasons over) unchanged.

  A created seat is verified to hold the requested RC name **and** the session id
  it was launched with; a seat that takes a fallback name is reported
  (`registration-unverified`) and **left running**, never killed and retried.

## Surface and recover a stalled or failed run

```
journalctl -fu seshat-dispatch-orchestrator     # dispatch decisions, stalls, blocks
journalctl -fu claude-remote-control@build1      # the RC server for a stream
systemctl status seshat-dispatch-orchestrator
```

- **`dispatch_blocked reason=rc-down`** → an RC server is down. Check
  `systemctl status claude-remote-control@<stream>`; `journalctl` it; restart if
  needed (`sudo systemctl restart claude-remote-control@<stream>`). Diagnose
  entitlement with `claude doctor`.
- **`dispatch_blocked reason=kill-switch`** → the kill switch is set; remove
  `telemetry/dispatch.disabled` to resume.
- **`dispatch_stall`** → a launched run produced no PR in time. Attach to the
  session (`claude.ai/code` / the mobile app, or locally `tmux attach -t
  cc-<stream>`), answer any waiting prompt, or investigate a crash. The
  orchestrator never advances on silence — it advances only on the durable
  open-PR + `In Review` evidence.
- **`dispatch_held_too_long`** (FRE-924) → a **surfaced manual card** (a KEEP /
  manual-model-required continuation, or a `delivery-failed` / `seat-unhealthy`
  card) has awaited the owner past the escalation threshold (default 30 min;
  `--held-escalation-timeout`). The stream is stalled on a card that never
  self-clears. Act on the card (attach to the seat and continue it, or clear the
  stream's record from the state file after resolving), then the next tick's
  non-hold decision drops the one-shot latch. Fired **once per episode** — the
  per-tick `card-already-surfaced` decision log remains the continuous trail.
  Escalation surfaces only: it never clears the record or kills a process. The
  in-memory latch resets on daemon restart, so a still-held card re-escalates once.
- **Emergency stop:** `touch telemetry/dispatch.disabled` (halts dispatch), or
  `sudo systemctl stop seshat-dispatch-orchestrator` (stops the daemon).

## Open seam (master's live verification — ADR-0110 §345)

ADR §1 describes a server-mode RC daemon on the *existing* worktrees; the shipped
T2 launcher (`scripts/dispatch/launcher.py`) instead creates per-session RC in
tmux. These units ship the server-mode substrate as the ADR names it. **Which
substrate the assembled seam uses — server-mode daemon vs. per-session tmux
launcher — is master's live verification (resolve → launch → owner-monitored run
→ end-at-PR → advance, once per stream).** The orchestrator's guardrails
(liveness, precondition, kill switch, stall) are correct under either.

**RESOLVED 2026-07-05 — per-session tmux launcher, proven live end-to-end.** The
assembled seam ran once for a build stream: the orchestrator resolved build1's NEXT
(FRE-472), the T2 launcher spawned a fresh per-session RC tmux session (`cc-build`)
seeded `/build FRE-472` at Opus, the owner answered a permission prompt from a device
(AC-3 live), the worker opened PR #393, master merged, and a follow-up `--once` tick
cleared the record on the terminal merge state (`kind=clear reason=merged` — advance
only on merge, never on silence). The **per-session tmux launcher is the proven
substrate**; the server-mode `claude-remote-control@` daemon units are shipped but
NOT yet proven live. Still outstanding: the same demo once for **adr** (no Approved
adr ticket existed at demo time), and **AC-5** (the two-worker pytest-lock collision).

## Relationship to the prime-worker monitor

The orchestrator **owns dispatch** — it resolves each stream's NEXT (the
Linear-native contract, `.claude/skills/lifecycle-rules.md` § Dispatch), sets the
model tier, and launches the worker with the resolved ticket seeded
(`/build <FRE-id>` / `/adr <FRE-id>`). The `prime-worker` skill is now a **pure
PR-feedback monitor** (FRE-806): it no longer resolves NEXT or advises a command
— that was duplicated logic. After a build opens a PR, its only job is the watch
loop over its own PR, self-fixing on a master bounce **or** a red CI, until the
PR merges. The build/adr skills arm this monitor early so it runs even when the
orchestrator (not a manual `/prime-worker`) was the launch entry point.

## Gating watcher (FRE-823) — event-driven send-keys triggers

The **gating watcher** (`scripts/dispatch/gating_watcher.py`,
`seshat-gating-watcher.service`) is the event-driven replacement for the polling
`/loop` crons removed 2026-07-06 (they re-read a session's context every tick and
blew the 5-min prompt-cache TTL — an uncached-cost blowup, FRE-822). It runs
**outside** every CC session and holds **no LLM context**: it polls `gh`/Linear/
`tmux` only, so a short poll interval is cheap. It only *actuates* the trigger —
master's gate and both approval gates are unchanged (like the orchestrator, it
never merges, deploys, closes tickets, or edits MASTER_PLAN).

**Two PR triggers + one context-pressure nudge.** For each open PR (read once via
`gh pr view` so a tick is internally consistent):
- **Master ← new PR.** CI green, not `CONFLICTING`, and no unacked
  `## Master gate — BOUNCE` → `tmux send-keys -t cc-master "/master <PR#>"`.
  Dedup on `(PR#, head SHA)`: a bounce+push mints a new SHA → re-sent when it
  re-greens.
- **Worker ← bounce / red CI.** An unacked `## Master gate — BOUNCE` **or** a
  failed CI check on the head SHA with no `Ack: addressing red CI at <sha>` →
  `tmux send-keys -t cc-<stream> "/prime-worker"`. The stream is resolved from
  the PR branch (`fre-<id>`) → the ticket's `stream:*` label →
  `cc-build`/`cc-build2`/`cc-adrs`. The ack markers prime-worker already posts
  are the dedup key.

**Master ← context pressure (FRE-848).** After the PR-trigger loop, the watcher
reads master's own live context% in-process (imports `resolve_jsonl`/
`read_context` from `scripts.dispatch.context_probe`, FRE-847 — no subprocess
wrapper). If usage is at/over `--context-pressure-threshold` (env
`AGENT_CONTEXT_PRESSURE_THRESHOLD`, default **70**) it reuses `send_to_session`
(same idle/existence gate as the PR triggers) to type a plain-text nudge into
`cc-master`: *"Context at `<pct>`% — checkpoint MASTER_PLAN + run the
prime-master pre-reset gate; consider `/clear` at the next clean boundary."*
This does not act on master's behalf — it only puts the suggestion in front of
the human operator supervising that session (Remote Control), who decides
whether to act. Dedup key is `ctxpressure:cc-master` (session-scoped, no PR/SHA
component — deliberately 2 colon-parts so it never collides with `prune_state`'s
open-PR pruning, which only strips a `<pr>` component from `master:`/`worker:`
keys), TTL-only, reusing the master TTL below. A `context_pressure` log line is
emitted every tick regardless of `--execute`, so pressure is observable in
dry-run. Like the two PR triggers, the actual send is also wired through the
trigger ledger below (`source="context-pressure"`, `ticket="cc-master"`) for
the same crash-safety guarantee — `prune_ledger`'s open-PR eviction is scoped
to numeric (PR) tickets only, so this session-keyed entry ages out by
retention alone, never by a PR-closure check that could never apply to it.

**Injection safety.** A command is sent only into a session that **exists**
(`tmux has-session`) AND is **idle at a prompt** (`capture-pane` shows the input
prompt and no busy/permission marker — a session waiting on an owner decision is
treated as busy and never interrupted). A missing or busy target is skipped +
logged, never crashed. The idle check is best-effort — the watcher only actuates;
master's and worker's own gates re-read live state and are authoritative. (Known
residual: an unsent human draft in the input box would receive the appended
command — inherent to the capture-pane approach.)

**Kill switch (shared).** `touch telemetry/dispatch.disabled` halts **both** the
orchestrator and the watcher immediately; remove it to resume. The watcher pokes
local tmux, so it does not depend on Remote-Control reachability.

**Dedup TTLs.** State is `telemetry/gating_watcher_state.json`
(`{"sent": {"<kind>:<pr>:<sha>": epoch}}` for the two PR triggers, plus
`"ctxpressure:<session>": epoch` for the context-pressure nudge; atomic write,
pruned each tick). Because `send-keys` is at-least-once, dedup is timestamped,
not permanent: master entries carry a long re-arm TTL (a genuinely stuck send
re-nudges once instead of being suppressed forever); worker entries carry a
short in-flight lease over the send→pre-ack window (the ack markers remain the
primary key); the context-pressure entry reuses the master TTL (one nudge per
~6h pressure episode).

Enable it alongside the orchestrator (single instance — one serial `--loop`):
```
sudo install -m 644 infrastructure/systemd/seshat-gating-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seshat-gating-watcher
```
Preflight by hand: `/opt/seshat/.venv/bin/python -m scripts.dispatch.gating_watcher --preflight`
(checks the Linear key + gh; exit 0 when both are present).

### Trigger ledger (FRE-829) — durable, crash-safe actuation

Every actual `tmux send-keys` the watcher performs is also recorded in the **trigger ledger**
(`scripts/dispatch/trigger_ledger.py`, state at `telemetry/trigger_ledger.json`) — the durable
substrate ADR-0113's self-driving delivery loop builds on. This is separate from the dedup TTLs
above: the TTL dict decides *whether to attempt* an actuation this tick; the ledger makes the
*attempt itself* crash-safe and gives a future `prime-master` (FRE-832) a durable source to
reconstruct in-flight actuation from after a `/clear` — never from conversation.

**Write ordering — ledger-before-send, consumed-after.** For each actuation: write a pending entry
→ mark send-started → call `tmux send-keys` → mark sent (only after a confirmed `"sent"` result) →
mark consumed. A crash lands in one of three distinguishable places, each handled differently:

- **Never attempted** (no send-started marker) → safe to retry — the next tick's `reconcile()`
  resends it and closes it out. Exactly one net actuation.
- **Ambiguous, crashed mid-send** (send-started marked, but never confirmed sent) → *never*
  auto-retried (a blind replay here could double-send a not-fully-idempotent action) — surfaced in
  the ledger (`surfaced_at` set) for owner intervention instead.
- **Known sent** (confirmed before the crash) → the next tick just closes the bookkeeping; never
  replayed.

A busy/absent target (the existing injection-safety skip) is recorded as **abandoned**, not
ambiguous — it is immediately eligible for a fresh attempt next tick, same as before this ticket.

**Reconciliation runs every tick**, immediately after the kill-switch check and before any new
board decision — so "restart" and "the tick after a crash" are the same code path. Duplicate or
replayed events dedupe against the ledger itself (folding in the trigger's own TTL window), so a
lost write to the TTL dict above can never cause a same-tick double-send.

**Retention.** `--ledger-retention-days` (default 7) prunes only *consumed* entries past that age
or whose PR has closed (PR-ticketed entries only — a session-keyed entry like the context-pressure
nudge's, FRE-848, has no PR to close against, so it ages out by the retention window alone). An
unconsumed entry — pending or surfaced — is **never** pruned regardless of age; a `surfaced_at`
entry sits in the ledger file until an owner clears it (no automated clearing mechanism yet — that
is out of scope for FRE-829, tracked under FRE-832).

Override the path with `--ledger-file` (systemd unit unchanged — defaults are fine).

### send-keys whitelist wrapper (FRE-831) — not yet wired into any live sender

`scripts/dispatch/send_keys_whitelist.py` is the mechanically-enforced boundary ADR-0113 §2 calls
for: a **non-LLM grammar parser + pane attestation** in front of `tmux send-keys`, so master's own
future actuation toward workers cannot rationalize a free-form instruction into a whitelisted
command. It is a self-contained library today — **neither `gating_watcher.py` nor any master skill
calls it yet**; that consolidation is a follow-up ticket, kept separate so this module's own PR
stayed one phase.

**Closed grammar** — exactly `/build <1|2|FRE-<digits>>` and bare `/prime-worker`, nothing else.
Matched with `str.fullmatch` over ASCII-only character classes (never the Unicode-permissive `\d`
regex shorthand), so a lookalike digit, an embedded newline, or a trailing newline all fail
outright — the grammar has no character class that admits them. **No `/master` entry, by design**:
this wrapper targets *LLM-driven* actuation specifically (master deciding to poke a worker); the
watcher's own `/master <PR#>` trigger is emitted by a dumb, contextless sensor, not an LLM, so it
has no role here and keeps its existing crash-safety from the trigger ledger unchanged.

**Pane attestation is command-role-aware, not just membership.** Every attested pane is derived from
`launcher.topology_for` (`cc-build`, `cc-build2`, `cc-adrs`), never a hand-duplicated literal — but
`/build` is only valid at a build pane (`cc-build`/`cc-build2`); `cc-adrs` runs `/adr`, not `/build`,
so a `/build` sent there is refused even though `cc-adrs` is a real, known pane. `/prime-worker` is
valid at any of the three.

**A refusal never touches the ledger or `tmux` (ADR-0113 AC-10).** Grammar and pane checks both run
before any side effect; a refused send is logged (`send_keys_whitelist_refused`, with the rejected
text truncated to 200 characters — never logged unbounded, since a free-form send is exactly the
attacker-controlled-content case). An approved send is ledger-integrated: `record_pending` →
`mark_send_started` → `tmux send-keys` → `mark_sent`/`mark_consumed` (or `mark_consumed` alone if
the target pane is busy/absent) — the same ledger-before-send/consumed-after shape
`gating_watcher.run_once` already uses, so a future caller inherits crash-safety for free instead of
re-deriving it. `event_id` is a caller-supplied idempotency key (mirror the watcher's own
`<kind>:<pr>:<sha>` pattern); the wrapper trusts it rather than deriving one, since it has no
visibility into the caller's own dedup-relevant context. An optional `kill_switch_engaged` predicate
provides defense-in-depth on top of (not instead of) a caller's own tick-level kill-switch check.

## Production cutover — settled posture + phased checklist

**Settled autonomy posture (owner, 2026-07-05 — do NOT re-open).** An orchestrated
session may run ONLY the kick-off skills `prime-worker` / `build` / `adr` (plus
`loop`, prime-worker's monitor-arm) — the entire allowlist, committed to
`.claude/settings.json` `permissions.allow` (PR #395). **Everything else inside a
build the owner approves remotely via RC, in real time.** `--dangerously-skip-permissions`
is never used, now or planned. Master's gate and both approval gates are unchanged.
There is no "how autonomous" dial — this is the ceiling.

**Status:** the seam is proven live end-to-end (see *Open seam* above — FRE-472 via the
per-session tmux launcher). The allowlist is merged to `main`. What remains is a
deliberate, phased cutover — not a single flip, because it changes the owner's
workflow and lets a daemon launch real (Opus) builds.

Phased checklist:

- [ ] **Prereq — allowlist reaches the worktrees.** PR #395 is on `main`; each worker
  worktree must **sync main** (fresh branch off `origin/main`) so kick-off runs
  promptless. Until synced, orchestrated launches still prompt on `loop`/`build`/`adr`.
- [ ] **Prereq — the orchestrator owns the worker sessions.** The owner stops manually
  opening `cc-build`/`cc-build2`/`cc-adrs` (the tmux names collide — both can't
  coexist); the orchestrator launches them. Kill any stale manual session for a stream
  before its first orchestrated launch.
- [ ] **Phase A — supervised `--once` (build confidence).** Run
  `orchestrator --once --execute --streams <one>` on demand a few times across streams;
  owner monitors via RC. Prove **AC-5** here: two concurrent orchestrated workers both
  reach the test phase → the `check-pytest-lock` hook blocks the second (one live
  pytest, a logged block, no hook-stripping). Prove **adr** here too (needs an Approved
  adr-stream ticket).
- [ ] **Phase B — supervised `--loop` in tmux (not systemd).** Run
  `orchestrator --loop --execute` in a watched tmux window, kill-switch handy, for a
  session — proves poll cadence + advance + stall handling under real load.
- [ ] **Phase C — official (systemd enable).**
  `sudo systemctl enable --now seshat-dispatch-orchestrator`. New default: keep
  Approved+`stream:*` tickets flowing, monitor via RC, answer build-time prompts.
  Retire the manual "master briefs → owner primes" step and update MASTER_PLAN's
  Dispatch section.

**Halt at any point:** `touch telemetry/dispatch.disabled` (kill switch) or
`sudo systemctl stop seshat-dispatch-orchestrator`.
