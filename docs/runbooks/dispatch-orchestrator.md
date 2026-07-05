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

## Relationship to the prime-worker monitor

The `prime-worker` in-session monitor and this orchestrator read the **same**
Linear-native dispatch contract (`.claude/skills/lifecycle-rules.md` § Dispatch).
The monitor **advises** (surfaces a dispatch card; the owner actuates); the
orchestrator **actuates** (resolves NEXT, sets the model tier, launches). They
are two front-ends on one contract — the orchestrator does not replace the
monitor's role of surfacing state, it removes the manual switch→clear→type step.
