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

**Two triggers.** For each open PR (read once via `gh pr view` so a tick is
internally consistent):
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
(`{"sent": {"<kind>:<pr>:<sha>": epoch}}`, atomic write, pruned each tick). Because
`send-keys` is at-least-once, dedup is timestamped, not permanent: master entries
carry a long re-arm TTL (a genuinely stuck send re-nudges once instead of being
suppressed forever); worker entries carry a short in-flight lease over the
send→pre-ack window (the ack markers remain the primary key).

Enable it alongside the orchestrator (single instance — one serial `--loop`):
```
sudo install -m 644 infrastructure/systemd/seshat-gating-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seshat-gating-watcher
```
Preflight by hand: `/opt/seshat/.venv/bin/python -m scripts.dispatch.gating_watcher --preflight`
(checks the Linear key + gh; exit 0 when both are present).

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
