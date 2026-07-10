# Event-Driven Dispatch Actuation — Claude Code Capability Assessment

**Date:** 2026-07-10 · **Author:** master (guardian session) · **Status:** evidence base for a forthcoming ADR (Build/ADR Dispatch Automation project)

## 1. Context — a deferral, not a rejection

The dispatch automation delivers turns into worker/master Claude Code sessions on gating events. Today this is **poll-based**: the `gating_watcher` (FRE-823) runs outside every session, polls `gh`/Linear/`tmux`, and injects commands via `tmux send-keys`:

- **Master ← ready PR** (CI-green) → `/master <PR#>` into `cc-master`.
- **Worker ← red CI** → a plain `PR #N failed CI checks - correct them` into the owning `cc-<stream>` seat (`gating_watcher.py:533`).

The **event-driven push** version — a GitHub webhook that pushes the CI result straight into the right session instead of polling — was **deferred on 2026-07-04** (owner decision: stabilize the poll-based system first). It was always the intended next step and is technically the correct end-state. The `docs/superpowers/specs/2026-07-04-dev-deploy-process-v2-design.md` Alternatives table originally recorded this as "Rejected (dissolved)" — a mischaracterization, corrected 2026-07-10.

The fragile part of the current approach is the `tmux capture-pane` **idle-detection scrape** (repeatedly buggy: FRE-825, FRE-845). Retiring it is the prize.

## 2. Capability findings (current Claude Code)

Assessed against current CC docs. Verdict: the push mechanism **now exists** but is at the frontier.

| Capability | Supported today? | Maturity | Notes |
|---|---|---|---|
| **MCP Channels** (`claude/channel`) — push into a running session | **Yes** | **Research preview** (v2.1.80+) | Docs name "CI results" as the use case. Opt-in per session via `--channels plugin:X` at startup. Delivers only while the session is running. |
| Programmatic turn-injection API (non-tmux) | No | — | No first-class "send message to session X" API outside Channels. |
| Wake a **stopped** session on a push | No | — | Channels push into *running* sessions only. |
| Target a session **by name/id** | No (single opted-in session) | — | Worked around by one channel **per seat**. |
| Remote Control as an external control API | No | Stable | Human-facing steering (web/mobile) only; no IPC/programmatic endpoint. |
| Agent SDK to inject into an existing session | No | Stable | SDK spawns *new* agent runs (`query()`), can't inject into a named live session. |

**Two apparent Channels limitations do not bite our topology:**
- *"Can't target by name"* → run **one channel per seat**; the gateway routes the event to the right seat's channel.
- *"Can't wake a stopped session"* → our seats are **persistent RC sessions that stay running** (idle at the prompt, not stopped).

**The one claim to spike, not assume:** does a channel message delivered to a **running-but-idle** seat actually trigger a turn? If yes, `send-keys` + the pane-scrape can be retired. This is a ~1-hour prototype, not a design assumption.

## 3. Architecture framing — platform (OpenClaw model), not protocol

OpenClaw's platform-vs-protocol distinction resolves the apparent "MCP server vs CLI" choice into a **hybrid** where both are needed:

- **The capability gateway is the architecture** — a long-running daemon owns routing, per-seat targeting, permissioning, the trigger ledger, dedup, and logging. We already have its **embryo**: `gating_watcher` + `next_resolver` + `launcher` + `trigger_ledger` + `send_keys_whitelist`. MCP is a *reach surface* this gateway uses, not the organizing principle (avoids pushing orchestration/security into every session — the "pure MCP" failure mode).
- **MCP Channels is one delivery surface** for the last hop (event → warm seat), alongside `send-keys` (the fallback).

So: **openclaw-style CLI/daemon gateway (ingress + routing + policy) + per-seat MCP Channel (delivery).** Both owner instincts ("it needs an MCP server" / "I'm more in favor of CLI style openclaw") compose rather than conflict.

## 4. Verdict

- The mechanism the deferral waited for **has arrived** (MCP Channels) and is purpose-fit for CI-result push.
- It is a **research preview** — buildable now, but accept API-churn risk, or gate on GA.
- The **poll-based watcher stays as the fallback** until the channel path is proven — no regression risk to live dispatch.
- **Boundary (ADR-0113 lesson):** actuation only. Master's gate and the human-approval gates are unchanged; this must not creep toward autonomous merge.

## 5. Open decisions for the ADR

1. Adopt the openclaw capability-gateway model for dispatch actuation (MCP = reach surface, not org principle)?
2. Delivery: MCP Channels (per-seat) for push, `send-keys` as fallback — accept research-preview churn, or gate on GA?
3. Trigger: GitHub `check_suite`/PR webhook → gateway ingress, replacing the `gh` poll.
4. De-risk spike: running-but-idle channel delivery (retire the pane-scrape iff it fires).
5. Reconciliation: relationship to ADR-0110; whether FRE-846 (resolver-wiring) and FRE-844 (watcher dry-run bug) fold in.

## References

- Claude Code docs: Channels (`code.claude.com/docs/en/channels.md`), MCP (`…/mcp.md`), Remote Control (`…/remote-control.md`), Sessions (`…/sessions.md`), Agent SDK (`…/agent-sdk/overview.md`).
- OpenClaw platform-vs-protocol model: `docs.openclaw.ai/cli/mcp`; AgentSkills-vs-MCP (clawforall.app blog, 2026-03-23).
- Internal: ADR-0110 (external dispatch orchestrator) · `scripts/dispatch/gating_watcher.py` · `docs/superpowers/specs/2026-07-04-dev-deploy-process-v2-design.md` (§ Alternatives) · FRE-823/825/845 (watcher + idle-detection history).
