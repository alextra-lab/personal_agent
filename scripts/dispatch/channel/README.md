# seshat-dispatch channel (ADR-0116 Phase 1, FRE-871)

The production **dispatch actuation channel**: a per-seat MCP channel that lets the dispatch gateway push
a PR/CI gating event straight into a *running* worker seat as a `<channel source="seshat-dispatch">` tag,
replacing the `tmux send-keys` + idle-scrape delivery hop for cut-over seats. This is a **transport swap,
not new authority** — the seat reasons over the event and pushes fixes to its **own** worker branch only
(boundary owned by `lifecycle-rules.md` § Session boundary).

## What's here

| Path | Role |
|------|------|
| `.claude-plugin/marketplace.json` | Local marketplace `seshat-dispatch` (one plugin). |
| `plugins/seshat-dispatch/webhook.mjs` | Channel entrypoint — wires the gate to the MCP `claude/channel` notification. |
| `plugins/seshat-dispatch/server.mjs` | SDK-free HTTP transport + **fail-closed** shared-secret sender gate + localhost bind. |
| `plugins/seshat-dispatch/server.test.mjs` | `node --test` gate suite (403 on bad/missing secret, localhost-only). |
| `plugins/seshat-dispatch/.mcp.json` | Registers `node webhook.mjs` as the `seshat-dispatch` MCP server. |
| `managed-settings.template.json` | The `channelsEnabled` + `allowedChannelPlugins` policy master writes at deploy. |

## Security model

An ungated inbound channel is a prompt-injection vector (ADR-0116). Two gates, both fail closed:

1. **Localhost-only bind** — the HTTP listener binds `127.0.0.1`; nothing off-box can POST.
2. **Shared-secret sender gate** — every POST must carry `X-Sender: <secret>`; a wrong/missing secret is
   dropped with `403` and never reaches Claude. The secret is `SESHAT_CHANNEL_SECRET`, read from the seat's
   environment (**never** placed on the launcher command line — it would leak via `ps`/plan JSON).
   `SESHAT_CHANNEL_PORT` is required too; the server refuses to start without either (no insecure default).

## Local verification (no Claude Code, no managed settings — runs in a build session)

```bash
# gate unit suite (no npm install needed — server.mjs is SDK-free)
cd plugins/seshat-dispatch && node --test        # 8 pass
# …also bridged into `make test` via tests/scripts/test_seshat_dispatch_channel.py
```

End-to-end boot of the real `webhook.mjs` (needs the MCP SDK: `npm ci` in the plugin dir) was verified on
2026-07-13: listens on `127.0.0.1:<port>`, authorized POST → 200, wrong/missing secret → 403, no secret →
process refuses to start. This proves the **channel server**; it does **not** prove AC-2 (the account
allowlist + headless launch), which needs the managed-settings write below.

---

## Deploy + live AC-2 verification (MASTER, at deploy — NOT the build session)

AC-2 (ADR-0116) needs a **sudo write to `/etc/claude-code/managed-settings.json`** (root-owned, highest
non-overridable precedence, **read by every seat on the box**) + a headless launch. That is a shared-box
system-config change = master/owner deploy territory. Sequence:

1. **Install the plugin's runtime dep:** `cd scripts/dispatch/channel/plugins/seshat-dispatch && npm ci`
   (brings in `@modelcontextprotocol/sdk`; `node_modules` is gitignored).
2. **Register the local marketplace:** `claude plugin marketplace add "$(pwd)/scripts/dispatch/channel"`
   (filesystem path). **Codex #6 open check:** if a filesystem marketplace is rejected and a git repo is
   required, point the marketplace at a repo path instead — record which worked.
3. **Provision the shared secret** into the seat environment (systemd unit `Environment=` or a root-only
   env file the tmux server inherits): `SESHAT_CHANNEL_SECRET=<generated-secret>`. The gateway (FRE-872+)
   must POST the same secret.
4. **Back up + write managed settings** (validate JSON first):
   `sudo cp /etc/claude-code/managed-settings.json{,.bak} 2>/dev/null; sudo install -D -m0644
   scripts/dispatch/channel/managed-settings.template.json /etc/claude-code/managed-settings.json`
   then strip the `//` comment key (JSON has no comments — the template's `//` is illustrative; write real
   JSON). `channelsEnabled: true` is additive/harmless to running seats.
5. **Headless launch** an isolated test seat (NOT a live `cc-*` seat) with channel-mode wired. Since
   FRE-875 there is no `--channels` flag: channel-wiring is derived from the seat's `StreamTopology.mode`
   (single source of truth). To wire a channel launch, set the target seat's `mode` to `"channel"` in
   `_TOPOLOGY` (against a throwaway topology/tmux name), then
   `python -m scripts.dispatch.launcher --stream <seat> --model haiku --ticket <t> --execute`, or mirror
   the emitted argv into an isolated `cc-test`.

### AC-2 pass criteria (the crux)
- Startup notice shows **`Channels (experimental) messages from … seshat-dispatch … inject directly`**.
- `/mcp` reports the `seshat-dispatch` server **connected**.
- **Zero** interactive consent/trust prompt blocked startup.
- A `curl -X POST http://127.0.0.1:<port>/ -H "X-Sender: <secret>" -d "…"` fires a turn in the idle seat.
- **Argv-ordering live-check (the derived `--channels <ref>` is variadic + undocumented):** the launcher
  places the seed positional (`/build FRE-…`) BEFORE `--channels <ref>` so the flag cannot swallow it. Confirm the
  channel-mode seat both (a) registers the channel AND (b) actually runs the seeded first turn. If the
  seed is dropped, the flag parse differs from the assumption — try `--channels=<ref>` or a `--`
  separator and record what the parser accepts.

### The genuine open question this proof settles (surface the result to the owner)
This box is **individual Claude Max** (no org). The channels docs say individual accounts "skip the
enterprise checks entirely." It is **unverified** whether managed `allowedChannelPlugins` is even consulted
for an individual account, or whether it only ever accepts the Anthropic-curated allowlist and ignores a
custom plugin. **Codex #5 check:** if the channel does not register, before concluding failure, check
whether an `enabledPlugins` / `/plugin install` enablement step is also required beyond the allowlist.

### Fallback (documented, never silently taken — ADR-0116 Risk row 1)
If the allowlist path cannot register the custom plugin headlessly on this account, the
`--dangerously-load-development-channels server:seshat-dispatch` dev flag is the **spike-proven**
contingency — but it carries an interactive per-launch consent a non-interactive launch cannot answer.
**Surface the finding to the owner; do not silently wire the dev flag.**
