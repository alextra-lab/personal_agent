# FRE-871 — Prove the approved channels-allowlist launch path (headless, no consent) + package the seshat-dispatch channel

**Backing ADR:** ADR-0116 (event-driven dispatch actuation), Phase 1 · **AC:** ADR-0116 **AC-2**
**Ticket:** FRE-871 (Approved, Tier-1:Opus, stream:build2) · **Branch:** `fre-871-channels-allowlist-launch`

---

## The decisive recon finding (shapes everything)

The production `--channels` allowlist path is **not** a per-project/user setting. Per the Claude Code
channels docs (fetched 2026-07-13):

- `--channels <ref>` accepts **only** plugins on an *effective allowlist*. For an account with **no org**,
  that is the **Anthropic-curated list** (telegram/discord/imessage/fakechat). The spike confirmed this box
  runs **individual Claude Max** (no org).
- The override that would let a **custom** `seshat-dispatch` plugin register — **`allowedChannelPlugins`** —
  is a **managed-settings-only** key (`/etc/claude-code/managed-settings.json`, root-owned, highest
  non-overridable precedence, **read by every seat on the box**). It also requires `channelsEnabled: true`
  (managed-only). There is **no** user- or project-level tier for these keys.
- **Genuine open question (the ticket's crux):** the docs say individual Pro/Max users "skip these checks
  entirely." It is **unverified** whether writing managed `allowedChannelPlugins` on an individual-Max box
  actually causes a custom plugin to register, or whether the account ignores the allowlist override and
  only ever accepts the Anthropic-curated list. **If the latter, the allowlist path is impossible for this
  account → the ticket's documented dev-flag fallback is the outcome** (surface to master, do not silently
  switch — per ticket + ADR-0116 Risk table row 1).

**Consequence:** proving AC-2 requires a **sudo, global, all-seats** write to
`/etc/claude-code/managed-settings.json` + a live headless launch. That is a system-config change on the
shared production box — master/owner deploy territory (lifecycle-rules § Deploy: "everything else — ask
first"). So the live proof is gated on an owner decision (see § Owner decision).

Other recon facts: **no Bun** on the box → channel server is **Node** (spike already proved Node works,
CC 2.1.207). Marketplace/plugin packaging mirrors the installed `openai-codex` custom plugin
(`.claude-plugin/marketplace.json` + `plugins/<name>/.claude-plugin/plugin.json`).

---

## Scope (5 bullets)

1. Harden the spike's `webhook.mjs` into the production **`seshat-dispatch`** Node channel server:
   per-seat port (`SESHAT_CHANNEL_PORT`), **localhost-only** bind, **shared-secret `X-Sender` header**
   gate (ungated inbound = prompt-injection vector), one-way delivery (no reply tool for Phase 1).
2. Package it as a **plugin** in a **local marketplace** so it is referenceable as
   `plugin:seshat-dispatch@seshat-dispatch`, versioned in-repo under `scripts/dispatch/channel/`.
3. Wire `launcher.py` `_build_tmux_command` to add `--channels plugin:seshat-dispatch@seshat-dispatch`
   to the RC seat argv, **behind an opt-in per-seat flag** (default OFF — not-yet-cutover seats are
   unchanged; satisfies ADR-0116 §5 "one seat at a time", precludes double-fire until FRE-872 lands the
   mode flag / exactly-once).
4. Provide the **managed-settings config** (`channelsEnabled` + `allowedChannelPlugins`) as a
   committed template + a recorded exact-key runbook; the live write is master's deploy step.
5. **Prove AC-2** empirically: an isolated headless launch shows the channel injecting + `/mcp` reports it
   connected, **zero blocking consent** — OR record the fallback finding and surface to master.

## Acceptance criterion (definition of done)

**ADR-0116 AC-2** — a seat launches non-interactively (via `launcher.py`, no TTY) on the `--channels`
allowlist path with the channel **registered and live**; startup notice shows the channel injecting, `/mcp`
reports it connected, **no** interactive consent/trust prompt blocked startup. *Fails if* any consent prompt
appears or the channel is not live.

---

## Plan — atomic steps

**Order is measure-first: run the crux probe (step 1) before building the full artifact, so we don't build
packaging + launcher wiring for a path that may not work for this account.**

### Step 1 — Crux probe (isolated, reversible) — the sudo write is OWNER/MASTER-executed (codex #1)
**The `/etc/claude-code/managed-settings.json` write is a shared-box system-config change = master/owner
deploy territory (lifecycle-rules § Deploy; codex finding #1). The build session does NOT do it
unilaterally.** I prepare the throwaway artifacts + the exact one-line runbook; the owner (or master) runs
the sudo write, or explicitly authorizes me to run the isolated reversible probe in-session (§ Owner
decision). Then:
- Build a **throwaway** minimal channel plugin + local marketplace in `/tmp` (Node, `claude/channel` cap).
- Back up (none exists) then write minimal `/etc/claude-code/managed-settings.json`:
  `{"channelsEnabled": true, "allowedChannelPlugins": [{"marketplace":"<tmp>","plugin":"<tmp>"}]}`.
  **NOTE (codex #2): no user/project/`CLAUDE_CONFIG_DIR` override for these keys is known or repo-proven —
  `/etc` is the only verified path. If the probe reveals an isolated override, prefer it.**
- Register the tmp marketplace; launch an **isolated `cc-test`** seat (outside `gating_watcher`'s seat map)
  with `--channels plugin:<tmp>@<tmp>`, cwd `/tmp`, **non-interactively**.
- **Verify:** startup notice shows `Channels (experimental) messages from … inject directly` for the
  custom plugin AND `/mcp` reports it connected, **no consent prompt**. Push a `curl` with the shared
  secret → file-write side-effect in `/tmp`.
- **Record, at this probe (codex #5/#6), before concluding anything:** (a) does registration also need an
  `enabledPlugins`/plugin-install step beyond `allowedChannelPlugins`? (b) does a **filesystem** marketplace
  path work, or is a **git-repo** marketplace required? Test the git form before declaring failure.
- Teardown: kill `cc-test`, restore/remove managed-settings.
- **Fork:** works → steps 2–6 (allowlist path). Doesn't (after checking #5/#6) → step 7 (fallback finding),
  do **not** wire the dev flag; surface to master.

### Step 2 — `seshat-dispatch` channel server (TDD)
- `scripts/dispatch/channel/plugins/seshat-dispatch/webhook.mjs` (Node, `@modelcontextprotocol/sdk`):
  `SESHAT_CHANNEL_PORT` (required, no insecure default), bind `127.0.0.1`, `claude/channel` capability,
  one-way `notifications/claude/channel`. **Sender gate:** reject any POST whose `X-Sender` header ≠
  `SESHAT_CHANNEL_SECRET` (from env; 403 + drop, no notification). Instructions string documents the
  one-way contract.
- **Tests** (`tests/dispatch/channel/` — Node test runner, `node --test`): (a) POST with correct secret
  emits one notification; (b) POST with wrong/absent secret → 403, **zero** notifications; (c) bind is
  `127.0.0.1` only; (d) missing `SESHAT_CHANNEL_PORT` → refuse to start. Confirm each fails first.

### Step 3 — Plugin + local marketplace packaging
- `scripts/dispatch/channel/.claude-plugin/marketplace.json` (name `seshat-dispatch`, one plugin).
- `.../plugins/seshat-dispatch/.claude-plugin/plugin.json` + `.mcp.json` (server `seshat-dispatch` →
  `node webhook.mjs`) + `package.json` (dep on the MCP SDK).
- `npm install` the SDK into the plugin dir (vendored or lockfile-pinned).

### Step 4 — Launcher wiring (TDD)
- `_build_tmux_command`: when the seat is channel-mode (new opt-in signal — a `LauncherCapabilities`
  field `channels: bool = False`; default OFF), append `--channels plugin:seshat-dispatch@seshat-dispatch`
  to `inner_argv`.
- **Env-injection mechanism (codex #4 — current `_build_tmux_command` has NO env seam):** the per-seat
  `SESHAT_CHANNEL_PORT` + `SESHAT_CHANNEL_SECRET` are prefixed onto the **inner** command via an explicit
  `env KEY=VAL … claude …` wrapper inside the `shlex.join`'d inner argv (testable in the argv, no reliance
  on tmux `-e`). Per-seat port is derived from topology (a fixed base + stream offset); secret is read from
  the launcher's env at plan time.
- **Tests:** channel-mode ON → inner argv contains `env SESHAT_CHANNEL_PORT=<port> SESHAT_CHANNEL_SECRET=…`
  **and** the `--channels` ref; OFF (default) → argv **byte-for-byte unchanged** vs today (proves
  not-yet-cutover seats are untouched — ADR-0116 §5).

### Step 5 — Managed-settings template + runbook
- Commit `scripts/dispatch/channel/managed-settings.template.json` (the exact `channelsEnabled` +
  `allowedChannelPlugins` shape) + a `README.md` recording the **exact key** proven in step 1 and the
  deploy sequence (register marketplace → write managed-settings → launch).

### Step 6 — Live AC-2 proof (codex #7 — must exercise the real launcher path)
- Drive the **actual `launcher.py`** against an isolated test seat (a `cc-test` topology entry, or a
  documented test-only seam that reuses `execute_plan`'s runner) with channel-mode ON — not a hand-mirrored
  argv. Assert channel live + `/mcp` connected + **no consent**. Capture raw evidence (startup notice line,
  `/mcp` output, push→file-write) for the ticket comment.

### Step 7 — Fallback path (only if step 1 says the allowlist path is impossible)
- Do **not** wire the dev flag. Record the finding (raw evidence) + the ADR-0116 Risk-row-1 conclusion in
  the research doc + ticket comment; surface to master for the architectural call.

### Step 8 — Docs + quality gates + PR
- Append results to `docs/research/2026-07-11-event-driven-dispatch-actuation-spike.md` (or a new dated
  research doc). `make test` (channel + launcher) · `make mypy` · `make ruff-check`/`ruff-format` ·
  `pre-commit` · `code-review` (high — src/launch + security-sensitive) · `security-review` (inbound
  network + secret). PR + ticket comment with AC-2 proof (or fallback finding) + the managed-settings
  deploy runbook for master.

---

## Owner decision (RESOLVED 2026-07-13) — Build-then-master-verifies

Owner chose: **I build ALL artifacts speculatively (channel server + plugin/marketplace + launcher wiring
behind default-OFF flag + managed-settings template + tests) and open the PR; master runs the sudo
`/etc/claude-code/managed-settings.json` write + the live AC-2 verification via the runbook at deploy.**
Clean session boundary; no sudo write / live launch from the build seat. Consequence I must honor: since I
cannot run the crux probe, the PR **cannot** claim AC-2 proven — it delivers the artifacts + a
verification harness I *can* run locally (the channel server's HTTP sender-gate, exercised without Claude
Code / without managed settings), and hands master a runbook that (a) does the `/etc` write, (b) launches
headless, (c) checks the crux (does individual-Max honor managed `allowedChannelPlugins`?), (d) resolves
codex #5 (`enabledPlugins`?) and #6 (filesystem vs git marketplace?) live, and (e) falls back to the
dev-flag finding if the allowlist path is not honored — surfaced to master, never silently switched.

## Risks
- Malformed managed-settings (highest precedence) could break **all** seats' next launch → minimal file,
  backup first, validate JSON, remove after probe.
- Allowlist path may not honor custom plugins for individual Max → step-1 fork to documented fallback.
- Two live transports until cutover — mitigated: launcher flag default OFF; no seat cuts over in this ticket.
