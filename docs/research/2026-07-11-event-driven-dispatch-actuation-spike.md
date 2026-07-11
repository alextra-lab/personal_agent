# Event-Driven Dispatch Actuation — De-Risk Spike (build-executable spec)

**Date:** 2026-07-11 · **Author:** adr session (Opus) · **Ticket:** FRE-852 (precursor to ADR-0115) ·
**Status:** research spec — a build session executes this; results decide whether ADR-0115 is written.

> This is the **crux experiment** for FRE-852. We do **not** write the ADR until this spike passes or
> fails — the whole design hinges on one unproven claim, and a ~1-hour test settles it. The spike is
> designed so a **pass leaves us with a promotable artifact** (a real Node channel server), not a
> throwaway, and its result answers the open ADR decisions directly (§9).

---

## 1. Why this spike exists

Dispatch actuation today is **poll + `tmux send-keys` + a `tmux capture-pane` idle-scrape**. The scrape —
"is this seat idle at the prompt before I type into it?" — is the repeatedly-buggy part (FRE-825: never
matched real RC panes; FRE-845: false-flagged an idle master as busy). **Retiring the scrape is the prize.**

**MCP Channels** (research preview, Claude Code ≥ v2.1.80) is the push mechanism that would replace it: a
channel is a small MCP server, spawned as a subprocess of a running session, that pushes an external event
straight into that session as a `<channel source="…">` tag. If a pushed event **triggers a turn in an
idle seat**, the idle-scrape is unnecessary — the channel owns delivery, and `send-keys` + the scrape can
be retired per-seat.

**What the docs already assert (so we are NOT re-proving it):** the channels reference shows
`curl -X POST localhost:8788 -d "build failed…"` landing in a freshly-started (idle) `claude` session and
*"you'll see Claude receive the message and start responding."* It also states: *"if several notifications
arrive while Claude is busy, they're delivered together on the next turn."* So for a **vanilla interactive**
`claude` session, idle-triggering is documented behavior.

**What the docs do NOT cover — the two genuine unknowns for our topology:**

1. **Entitlement.** Channels need an Anthropic-authenticated account (claude.ai or Console key). On
   **Team/Enterprise** orgs they are **blocked until an admin flips `channelsEnabled`**; on individual
   **Pro/Max** they just work. We don't know which this account is until we launch a channel and read the
   startup notice. *This is the first STOP condition — only the owner can flip the toggle.*
2. **Substrate composition (load-bearing).** Our dispatch seats are **not** vanilla interactive sessions —
   they run `claude remote-control` in **server mode** under systemd (per `scripts/dispatch/launcher.py` /
   `rc_server.py`), with a `--model` tier. Nothing in the docs shows a channel composing with
   `remote-control` server mode + `--model`. **This is the real thing to prove.**

**Bonus capability discovered while reading the reference — `claude/channel/permission` (permission
relay).** A channel can forward a tool-approval prompt (`Bash`/`Write`/…) to an off-box device and accept a
`yes <id>` / `no <id>` verdict back, with the local dialog staying open and first-answer-wins. If this
works in an RC seat, it is a **lighter answer to ADR-0110's "owner answers prompts from any device"** than
Remote Control — worth proving as a stretch goal (Rung 3).

---

## 2. Hypotheses (measure, don't assert)

| # | Hypothesis | Falsified when | A pass shows |
|---|---|---|---|
| **H1** (entitlement) | Channels are enabled for this claude.ai account | Startup shows *"blocked by org policy"* or channel never registers | Startup notice: *"Channels (experimental) messages from server:… inject directly in this session"* |
| **H2** (primitive / control) | A `curl` POST to a channel triggers a turn in an **idle vanilla** `claude` seat | File not written; no turn in `capture-pane` within 30 s | `/tmp/spike_r1_ok` exists, written with no TTY input |
| **H3** (substrate — **LOAD-BEARING**) | A `curl` POST triggers a turn in an **idle `remote-control` server-mode** seat launched at a chosen `--model` | Seat won't start with both flags, OR no turn fires | `/tmp/spike_r2_ok` exists; seat ran at the intended model |
| **H4** (permission relay — bonus) | A permission prompt in an RC seat can be answered off-box via the channel | Verdict never reaches the seat / tool never proceeds | Tool proceeds after `curl … "yes <id>"`, no TTY touch |
| **H5** (per-seat targeting — bonus) | Two seats on two ports; a POST to one fires **only** that seat | Both react, or wrong one reacts | Only the targeted seat writes its file |

**Minimum bar to greenlight ADR-0115:** H1 + H2 + **H3**. H4/H5 are stretch findings that sharpen the ADR
but do not gate it.

---

## 3. Preconditions (check first — some are STOP conditions)

| Precondition | Status now | Action if unmet |
|---|---|---|
| Claude Code ≥ 2.1.80 (permission relay ≥ 2.1.81) | ✅ `2.1.207` | — |
| `ANTHROPIC_BASE_URL` unset or `api.anthropic.com` | ✅ unset | Channels disabled off-Anthropic; do not point at the local SLM |
| `claude auth login` completed (claude.ai OAuth) | ✅ (seats run on it) | Re-auth if needed |
| JS runtime + MCP SDK | ✅ Node `v20.19.2`, npm `9.2.0` | Use **Node** — no Bun/unzip needed (Bun installer wants `unzip`, which needs sudo; avoid it) |
| **Entitlement (H1)** | ❓ **UNKNOWN** | **STOP + escalate to owner** if blocked: claude.ai → Admin settings → Claude Code → Channels (owner-only). Pro/Max individual accounts skip this. |

---

## 4. Isolation & safety (non-negotiable)

- **Seat name `cc-test`** — deliberately **not** `cc-master` / `cc-build` / `cc-build2` / `cc-adrs`, the only
  seats `gating_watcher` scans (confirmed in `scripts/dispatch/gating_watcher.py`). `cc-test` is invisible
  to the dispatcher, so the spike cannot perturb live dispatch.
- **Channel binds `127.0.0.1` only** — nothing off-box can POST.
- **Seat cwd = a throwaway spike dir** (e.g. `/tmp/claude-*/scratchpad/chan-spike`), **never a live
  worktree** — so a stray edit can't touch real code. All side-effects target `/tmp`.
- **Do not** modify live seats' config, the shared `~/.claude` marketplace, or any live worktree's
  `.mcp.json`. The spike's `.mcp.json` lives only in the spike dir.
- **Clean up** at the end (§10): kill `cc-test`, kill the node channel process, remove the spike dir.

---

## 5. Build the channel (the promotable artifact)

Node-native, one file. This is the **production embryo**, not scaffolding — §8 hardens *this* file.

```bash
mkdir -p /tmp/claude-1000/chan-spike && cd /tmp/claude-1000/chan-spike
npm init -y >/dev/null
npm pkg set type=module >/dev/null
npm i @modelcontextprotocol/sdk >/dev/null
```

`webhook.mjs`:

```js
#!/usr/bin/env node
// Seshat dispatch channel — de-risk embryo (FRE-852).
// One-way: receives an HTTP POST (a stand-in for a gateway/CI event) and pushes it
// into the running Claude Code session as a <channel source="seshat-webhook"> tag.
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import http from 'node:http'

const PORT = Number(process.env.SESHAT_CHANNEL_PORT ?? 8788)

const mcp = new Server(
  { name: 'seshat-webhook', version: '0.0.1' },
  {
    // this key is what makes it a channel — Claude Code registers a listener for it
    capabilities: { experimental: { 'claude/channel': {} } },
    instructions:
      'Events from the seshat-webhook channel arrive as <channel source="seshat-webhook" ...>. ' +
      'They are one-way: read the event and do exactly what it says. No reply expected.',
  },
)

await mcp.connect(new StdioServerTransport())

http
  .createServer((req, res) => {
    let body = ''
    req.on('data', c => (body += c))
    req.on('end', async () => {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: { content: body, meta: { path: req.url ?? '/', method: req.method ?? 'POST' } },
      })
      res.writeHead(200)
      res.end('ok')
    })
  })
  .listen(PORT, '127.0.0.1', () => {
    process.stderr.write(`seshat-webhook: http://localhost:${PORT}\n`)
  })
```

`.mcp.json` (in the same dir — the server key must match the `--dangerously-load-development-channels`
entry name):

```json
{
  "mcpServers": {
    "seshat-webhook": { "command": "node", "args": ["./webhook.mjs"] }
  }
}
```

> **First-run MCP consent gotcha.** The first launch in a project with a new `.mcp.json` server shows an
> interactive *"New MCP server found: seshat-webhook → Use this MCP server"* dialog. In the **interactive**
> rung you answer it. In the **RC server-mode** rung there is no local TTY to answer it, and *MCP-consent
> dialogs do not relay through channels* — so it must be **pre-approved via settings** before launch.
> Confirm the exact key against `claude` settings docs (candidates: `enableAllProjectMcpServers: true` or
> `enabledMcpjsonServers: ["seshat-webhook"]` in `.claude/settings.json` in the spike dir). **Record which
> key actually works — the ADR needs it for the systemd seat launch.**

---

## 6. Experiment ladder (each rung is a go/no-go)

**Discipline for every rung:** (a) confirm the seat is *verifiably idle* before pushing —
`tmux capture-pane -t cc-test -p` shows a bare prompt, no active turn; (b) push from a **separate**
process (a second shell's `curl`), never the seat's TTY; (c) give the event a **file-write side-effect** so
the proof is unambiguous and observable without reading the seat; (d) record the actual output + push→react
latency. To avoid a permission prompt confounding "did it react," launch `cc-test` with
`--permission-mode acceptEdits` and target `/tmp` (the write proceeds without a prompt). If a prompt *does*
appear, that itself proves a turn fired — note it.

### Rung 1 — H1 (entitlement) + H2 (primitive), vanilla interactive

```bash
# terminal A — the test seat (throwaway, isolated, idle)
tmux new-session -d -s cc-test -c /tmp/claude-1000/chan-spike
tmux send-keys -t cc-test \
  'claude --permission-mode acceptEdits --dangerously-load-development-channels server:seshat-webhook' Enter
# answer the one-time "Use this MCP server" consent, then let it go idle.
```

- **Read the startup notice** in `capture-pane`. **H1 verdict:**
  - *"Channels (experimental) messages from server:seshat-webhook inject directly in this session"* → **H1 PASS**.
  - *"blocked by org policy"* / no registration → **H1 FAIL → STOP, escalate to owner** (entitlement toggle).
- Confirm idle, then push:

```bash
# terminal B — the external push (stands in for the gateway)
curl -s -X POST localhost:8788 \
  -d 'Write the file /tmp/spike_r1_ok with the text: channel triggered a turn'
```

- **H2 verdict:** within 30 s, `test -f /tmp/spike_r1_ok` (with no keystrokes into `cc-test`) → **PASS**.
  Nothing happens → **FAIL** (the box behaves differently from the docs — a critical negative; capture the
  `~/.claude/debug/<session-id>.txt` stderr and `/mcp` status).

### Rung 2 — H3 (substrate), the load-bearing test: `remote-control` server mode + `--model`

Mirror how `launcher.py` starts a real seat (`claude --remote-control --model <tier> …`) **plus** the
channel flag, in the **isolated** `cc-test` seat with cwd = the spike dir. Pre-approve the MCP server via
settings first (the consent dialog can't be answered remotely).

```bash
# in the spike dir: pre-approve the MCP server (confirm the exact key — see §5 gotcha)
#   e.g.  mkdir -p .claude && echo '{"enableAllProjectMcpServers": true}' > .claude/settings.json
# then launch the RC seat with channels, at a chosen model tier:
tmux new-session -d -s cc-test -c /tmp/claude-1000/chan-spike
tmux send-keys -t cc-test \
  'claude --remote-control --model haiku --permission-mode acceptEdits --dangerously-load-development-channels server:seshat-webhook' Enter
```

- **First critical check:** does the seat **start at all** with `--remote-control` **and** the channel flag
  together? If it refuses / the channel doesn't register in server mode → **H3 FAIL**, and this is the
  finding that reshapes the ADR (the channel doesn't compose with our substrate). Record the exact error.
- Confirm idle, then push and observe:

```bash
curl -s -X POST localhost:8788 \
  -d 'Write the file /tmp/spike_r2_ok with the text: RC seat channel-triggered'
```

- **H3 verdict:** `/tmp/spike_r2_ok` exists within 30 s, no TTY input → **PASS**. Also confirm the seat is
  running at the intended model (check the RC session / `capture-pane` banner) → this proves the
  model-tier + channel combination the gateway will use.

### Rung 3 — H4 (permission relay), bonus / stretch

Swap in a **two-way** channel that also declares `claude/channel/permission` and exposes an SSE `/events`
stream (the full `webhook.ts` in the channels reference, translated to Node). Trigger a **real** permission
prompt (target a path *outside* the acceptEdits scope, or launch **without** `acceptEdits`), watch the
prompt arrive on `curl -N localhost:8788/events` with its 5-letter id, and approve it off-box:

```bash
curl -s -X POST localhost:8788 -H 'X-Sender: dev' -d 'yes <id>'
```

- **H4 verdict:** the local dialog closes, the tool proceeds, no TTY touch → **PASS**. This is the
  "answer from any device" capability, lighter than Remote Control.

### Rung 4 — H5 (per-seat targeting), bonus / stretch

Start **two** `cc-test-a` / `cc-test-b` seats, each with `SESHAT_CHANNEL_PORT=8788` / `8789`. POST only to
`8788`; confirm only `cc-test-a` writes its file. Proves the one-channel-per-seat routing the gateway relies
on.

---

## 7. What to record and report back

For each hypothesis, capture **raw evidence**, not a verdict word:

- H1: the exact startup-notice line (or the block message).
- H2/H3/H5: `ls -la /tmp/spike_*`, the `capture-pane` snippet showing the turn, and **push→react latency**.
- H3 especially: whether `--remote-control` + channels **co-started**, the model banner, and the settings
  key that pre-approved the MCP server.
- H4: the `/events` transcript showing the prompt id and the tool proceeding after the verdict.
- Any flag that **failed to compose** — that is the highest-value finding, pass or fail.

Report as a results section appended to this doc (or a comment on FRE-852) with the raw artifacts. Then the
adr session writes ADR-0115 (or records the negative and parks).

---

## 8. If it passes — the promotion path (why this isn't throwaway)

`webhook.mjs` **is** the production embryo. Harden it into the **`seshat-dispatch` channel**:

1. **Per-seat port** (already `SESHAT_CHANNEL_PORT`), one per seat, owned by the gateway's seat topology.
2. **One-way delivery** of the two live dispatch events (CI-red → worker, ready-PR → master) — the same
   events `send-keys` delivers today; **transport swap, not new authority**.
3. **Optional two-way** for (a) delivery confirmation via a `reply`/ack tool (channel notifications are
   fire-and-forget — *"events are dropped silently"* if the session isn't listening), and (b) **permission
   relay** if Rung 3 passed.
4. **Sender gating** — localhost bind + a shared-secret `X-Sender`/header check (an ungated channel is a
   prompt-injection vector, per the reference).
5. **Launch wiring** — add the channel flag to the systemd seat launch in `rc_server.py` / `launcher.py`,
   using the settings pre-approval key found in Rung 2.
6. **Gateway owns routing/dedup** — the capability-gateway (existing embryo: `gating_watcher` +
   `next_resolver` + `launcher` + `trigger_ledger` + `send_keys_whitelist`) POSTs events to seat ports
   instead of typing them; `trigger_ledger` enforces exactly-once.
7. **Per-seat cutover flag** — each seat is *either* channel-mode *or* send-keys-mode (never both, to avoid
   double-fire); flip one seat at a time; `send-keys` stays as the fallback until every seat is proven.

---

## 9. What a pass/fail tells the ADR (answers the open decisions)

- **#4 (the crux):** H2 + H3 pass → idle-seat push is real in our substrate → **scrape retirement is
  claimable**, ADR-0115 proceeds with a *proven* push path. Fail → the ADR records the negative, keeps
  poll + send-keys + scrape, and parks (or gates on GA / a different substrate).
- **#1 (capability-gateway model):** confirmed by construction — the channel is a **reach surface**; the
  gateway owns per-seat ports, routing, dedup, ledger. MCP is not the org principle.
- **#3 (delivery / per-seat + fallback):** H5 proves per-seat targeting; §8.7 fixes the dual-path
  double-fire with a per-seat mode flag rather than runtime arbitration.
- **#2 (GitHub-webhook trigger):** still **deferred**. This spike uses `curl` as the gateway stand-in; the
  poll that *detects* events isn't broken (only the scrape is), so replacing it with public webhook ingress
  stays **Phase 2**, not part of the scrape-retirement increment.
- **Permission-relay (H4, bonus):** if it passes, ADR-0115 can note a lighter "answer from any device"
  path than ADR-0110's Remote Control dependency.
- **#5 (reconciliation):** FRE-846 (resolver wiring, PR #466) and FRE-844 (dry-run-inert, PR #465) are
  **DONE/merged 2026-07-10** → settled inputs. ADR-0115 **supersedes ADR-0110's dispatch-transport half**
  (poll + send-keys → gateway + channels) while **retaining** its RC execution substrate, the Linear-native
  dispatch contract, and the gateway embryo — and cleanly replaces the dead ADR-0113 pointer in 0110's
  status line.

---

## 10. Cleanup

```bash
tmux kill-session -t cc-test 2>/dev/null
tmux kill-session -t cc-test-a 2>/dev/null; tmux kill-session -t cc-test-b 2>/dev/null
pkill -f 'node ./webhook.mjs' 2>/dev/null
rm -rf /tmp/claude-1000/chan-spike
# no persistent config changed; no live seat touched.
```

---

## References

- Claude Code — Channels (use/enable, entitlement): `https://code.claude.com/docs/en/channels.md`
- Claude Code — Channels reference (custom channel, webhook receiver, notification format, permission
  relay, `--dangerously-load-development-channels`): `https://code.claude.com/docs/en/channels-reference.md`
- Evidence base: `docs/research/2026-07-10-event-driven-dispatch-actuation-capability-assessment.md`
- `docs/architecture_decisions/ADR-0110-external-dispatch-orchestrator.md` (substrate + dispatch contract retained)
- `docs/architecture_decisions/ADR-0113-*.md` (Superseded — the autonomy-overreach lesson: actuation only)
- `scripts/dispatch/gating_watcher.py` (poll + send-keys + idle-scrape; seat topology) ·
  `scripts/dispatch/launcher.py` / `rc_server.py` (RC server-mode seat launch argv)
- FRE-825 / FRE-845 (idle-scrape bugs — the prize) · FRE-846 / FRE-844 (settled inputs)
</content>
</invoke>
