# ADR-0155: Seat-Side Mods — the Daemons Keep Deciding, a Per-Seat Mod Reports State and Accepts Triggers, and Three Rules Protect the Owner's Remote Control

**Status:** Proposed
**Date:** 2026-10-09
**Deciders:** Owner (the question, the Remote Control constraint, the direction, the draft policy, the pilot seat, 2026-10-09), `master` (the commission and the verified API starting facts), `adr` seat at Opus 5.5 (author)
**Tags:** dispatch, sessions, claude-code, mods, remote-control, governance

**Umbrella:** FRE-1553.

---

## Context

### The question

The owner asked whether the session process (cc-master and the seats cc-1build, cc-2build, cc-adrs and cc-explore) can move to Claude Code mods. A mod is a plugin of function hooks that loads inside one Claude Code session. The API is in build 2.1.289, and it is early access: it changes between releases.

The owner set one hard constraint, in their words: "the separate sessions are accessible to me via remote-control. this is very useful as I may personally enter into the session and arbitrate or redirect, answer questions, and approve/deny."

### How the seats are driven today

The facts below are from `/opt/seshat` main on 2026-10-09.

- **Two system daemons decide everything.** Neither uses a model.
  - The gating watcher (`seshat-gating-watcher.service`) polls every 60 s. A green, mergeable PR sends `/master <PR#>` to cc-master. A red PR sends a correction prompt to the owning seat.
  - The dispatch orchestrator (`seshat-dispatch-orchestrator.service`) polls every 300 s. It starts an Approved ticket on a free stream.
- **Worker triggers already avoid typing.** The watcher first POSTs to the seat's `seshat-dispatch` channel. The channel is an MCP plugin, and `cc-seat-exec` gives it a port and a secret: ports 8790, 8791 and 8792, from column 4 of `~/.claude/cc-sessions.conf`. Only when that POST fails does the watcher type the prompt with `tmux send-keys` (`gating_watcher.py:939-1064`).
- **Three paths still type into a pane:**
  1. Master's triggers always use `send-keys`. cc-master has no channel port.
  2. The orchestrator's reuse path types `/clear`, `/model <tier>` and `/build <FRE>` (`launcher.py:1034-1149`).
  3. Each daemon's alert to master uses `send-keys`.
- **Busy state is read from the pane.** A seat counts as idle only when a bare `❯` line shows (`pane_state.py:27-110`). A held draft, such as `❯ /master 1218`, therefore reads as busy forever. Triggers queue behind it. One such draft stalled cc-1build for 2.5 h on 2026-10-03 and cc-master for about 5 h on 2026-10-04.

### What the mod API offers, and what it allows that must never happen

The API declaration of build 2.1.289 establishes these facts:

- **Exact seat state.** `turn.start`, `turn.complete`, `session.start` and `session.end` (which also fires on `/clear`) are events a mod can hook. `$.prompt.read()` returns the owner's current draft.
- **A trigger that does not type.** `$.prompt.submit` queues a prompt that starts its own turn once the session is idle. It never folds into a running turn. The API's box operation is a separate call, `$.prompt.fill`. So, by the contract, a submit leaves the draft alone. This is not verified on a live seat.
- **What a validator sees.** `claude plugin validate` scans a module before it loads. It lists the events the module hooks (`uses.events`) and every `$` call (`uses.calls`).
- **A mod can skip a permission prompt.** A `tool.check` hook can return `allow`. A `tool.call` hook can answer a tool itself, so the tool never asks.
- **A mod can take the owner's Remote Control input.** A `session.receive` hook can rewrite or consume a delivery whose origin is `bridge`, which is the owner's Remote Control input.
- **A mod fails open.** A hook that throws is skipped, and "a broken plugin never blocks a prompt".
- **A mod's timers end with it.** Timers from `$.clock.every` end at every reload, at `/clear` and at session end.
- **Loading.** A mod loads from `--plugin-dir` or from `CLAUDE_CODE_PLUGIN_DIRS`, taken from the process environment or from `~/.claude/settings.json`. An interactive session watches that folder, so every save reloads the module.

### What mods cannot fix

- **The backwards-move wedge** is the orchestrator's own state: a `launched` record releases only on a terminal ticket state (`orchestrator.py:674-705`).
- **The Bash-text guards** (`deploy-approval-gate.sh`, `block-direct-main-push.sh`) must fail closed. A mod fails open, so it cannot replace them.

---

## Decision

### D1 — The daemons stay the deciders

The gating watcher and the dispatch orchestrator keep every decision: what to trigger, when, and for which seat. No dispatch logic moves into a mod, and no mod runs in cc-master to replace them.

The reason is lifetime. A daemon is a supervised system service. A mod's timers end at every reload, `/clear` and session end, and master's own session is cleared often.

### D2 — Two tracks run in parallel

- **Track A, no mod.** cc-master receives a `seshat-dispatch` channel of its own, and the watcher sends master's triggers through it first. Master's channel needs its own server instructions. The worker channel's instructions forbid merging or approving any PR (`webhook.mjs:33-38`), and merging is master's job. The watcher's busy check reads Remote Control status (`claude agents --json`) first, and the pane only as a fallback. Remote Control status does not count a held draft as busy.
- **Track B, the seat agent.** One mod, `seat-agent`, loads in a seat. It reports the seat's state (phase 1) and, after the owner approves phase 2, delivers triggers. It never decides what to trigger.

### D3 — Three rules bind every mod in a seat

1. **A mod never decides a permission.** It never answers `tool.check`, and never answers `tool.call` with a result or a deny.
2. **A mod never rewrites or consumes the owner's input.** It never hooks `session.receive` or `prompt.submit`.
3. **A mod never writes the owner's input box.** It never calls `$.prompt.fill` or `$.prompt.suggest`.

**The check is an allow-list, not a deny-list.** `scripts/dispatch/check_mod_rules.py` runs `claude plugin validate` on the mod and compares the scanned lists with these sets.

| List | Allowed for `seat-agent` |
|---|---|
| `uses.events` | `session.start`, `session.end`, `turn.start`, `turn.complete`, `ui.render` |
| `uses.calls` | `fs.read`, `fs.write`, `fs.list`, `fs.stat`, `clock.every`, `clock.after`, `prompt.read`, `prompt.submit` (phase 2 only), `env.get`, `session.version`, `ui.status`, `ui.toast`, `ui.resolve`, `ui.invalidate` |

Anything else fails the check, including a new event that a future release adds. The module must also not reach `$` by a computed property (`$[name]`), because the scan cannot see such a call. A unit test asserts that the source has no computed access on `$`.

The check runs in pre-commit and in the promotion script (D5). The promotion check is the binding one: no version reaches a seat without it.

### D4 — A mod loads only in a seat, and only where its seat says so

- **Load.** `cc-seat-exec` exports `CLAUDE_CODE_PLUGIN_DIRS` and `SESHAT_SEAT=<seat name>` for a seat that `cc-sessions.conf` marks for the mod. `~/.claude/settings.json` never names the mod, so the owner's other sessions never load it.
- **Scope.** A session where `SESHAT_SEAT` is unset does nothing. Every hook passes straight to `next`.
- **Phase 1 scope:** only cc-2build.

### D5 — A mod change is a deploy, gated by promotion

- **Source.** The mod lives in the repo under `tooling/claude-mods/seat-agent/`. It changes by PR, with `claude plugin test` and the D3 check.
- **Runtime folder.** Seats load `~/.claude/seshat-mods/seat-agent/current/`, never the live `/opt/seshat` checkout. Hot reload watches the folder, so loading from the checkout would make every `git pull` an ungated deploy to every seat.
- **Promotion.** `scripts/dispatch/promote_mod.py <git-sha>` exports the mod at that commit and runs the D3 check and the tests. It replaces `current/` atomically and records the SHA in `current/VERSION`. Hot reload then loads the new version in every seat that loads the mod. Promotion is master's action, in the stricter deploy class.
- **Rollback.** Promote the previous SHA.
- **Kill switch.** While the file `~/.claude/seshat-mods/seat-agent.disabled` exists, every hook passes through, and the heartbeat records `disabled: true`.

### D6 — Phase 1 reports state and changes no decision

On cc-2build, `seat-agent` writes `/opt/seshat/telemetry/seat_state/<seat>.json` on every hooked event, and every 30 s as a heartbeat. The fields:

| Field | Meaning |
|---|---|
| `seat`, `session_id` | `SESHAT_SEAT` and the engine's session id |
| `state` | `idle`, `turn`, or `ended` |
| `turn_started_at`, `turn_completed_at` | from `turn.start` and `turn.complete` |
| `draft_present`, `draft_changed_at` | from polling `$.prompt.read()` every 5 s. The draft text is never written. |
| `engine_version`, `mod_version`, `disabled` | `$.session.version()`, `current/VERSION`, the kill switch |
| `heartbeat_at` | the last write |

The watcher logs its own reading for the seat on every tick: the pane reading and the Remote Control status. A report script compares the three readings. **No daemon acts on the mod's file in phase 1.**

### D7 — The daemons fall back when the mod is silent

A daemon treats a seat as having no mod when any of these holds:
- the heartbeat is older than 90 s;
- `disabled` is true;
- `engine_version` is not on the list the promoted version was tested against.

It then uses Remote Control status, and the pane reading after that. A broken mod, a disabled mod or an engine update can therefore never stop dispatch. It returns dispatch to today's behaviour.

### D8 — Phase 2 delivers triggers, after the owner approves it on the phase-1 evidence

Phase 2 starts only when the owner records approval on FRE-1553, with the AC-4 report beside it. Then:

- **Inbox.** The daemon writes a trigger as `/opt/seshat/telemetry/seat_inbox/<seat>/<id>.json`. The mod reads the inbox on each heartbeat.
- **Draft hold (owner, 2026-10-09).** The mod holds a trigger only while the owner's draft changed in the last 2 minutes, for at most 10 minutes. While it holds, a band above the prompt names the waiting trigger. A draft unchanged for 2 minutes never holds a trigger.
- **Submit.** The mod calls `$.prompt.submit` with the trigger's text, framed as the plugin's message, not as the owner's words. It writes `<id>.ack.json` with the submit time.
- **Fallback.** A trigger with no acknowledgement after 15 minutes falls back to the daemon's track A path, and the daemon logs it.
- **Two facts to verify before phase 2 ships:**
  - that a submit leaves a draft byte-identical;
  - whether a submitted `/clear` or `/build <FRE>` runs as a command.

  If commands do not run, the orchestrator's reuse path launches a fresh seat in place of `/clear`, and it stays on `send-keys` until that change lands.

### D9 — What this ADR does not decide

- The orchestrator's backwards-move wedge. It needs a fix in the orchestrator.
- The Bash-text guards. They stay as settings hooks. Their false positives are a separate fix.
- A board pane in cc-master. It is optional, and later.
- A phone push. FRE-1550 is on hold by the owner and is not part of this ADR.

---

## Alternatives Considered

### Option 1: Keep today's process

**Description:** `send-keys` delivery and busy detection by pane text.

**Pros:**
- Nothing to build.

**Cons:**
- A held draft stalls a seat with no signal: 2.5 h on cc-1build (2026-10-03), about 5 h on cc-master (2026-10-04).

**Why Rejected:** It keeps the measured stalls.

### Option 2: Track A alone, with no mod

**Description:** The channel for cc-master and Remote Control status for busy detection, and nothing else.

**Pros:**
- Small, and no early-access API.
- It removes master's `send-keys` and the draft-as-busy error.

**Cons:**
- No exact turn state when Remote Control status is unavailable.
- No draft awareness.
- No path for the orchestrator's reuse commands.

**Why Rejected as the whole answer:** It is adopted as track A. It leaves the reuse path and the state signal where they are, and the pilot measures what track B adds.

### Option 3: Replace the daemons with a mod in cc-master

**Description:** Dispatch logic runs on `$.clock.every` inside master's session.

**Pros:**
- One place for dispatch, inside the session the owner watches.

**Cons:**
- Every `/clear`, reload or restart of master stops dispatch.
- The logic shares master's context and turn loop.

**Why Rejected:** A supervised daemon outlives a session, and dispatch must outlive master's resets.

### Option 4: Move the guards into mods

**Description:** `tool.call` hooks replace `deploy-approval-gate.sh` and `block-direct-main-push.sh`.

**Pros:**
- Hooks see parsed tool input, and a mod can draw a reason in the session.

**Cons:**
- A hook that throws is skipped, so the guard fails open.
- The hook still reads the same command text, so text matching stays.

**Why Rejected:** A guard must fail closed.

### Option 5: Load the mod from `~/.claude/settings.json`

**Description:** Name the mod folder in the global settings' `env` block.

**Pros:**
- No change to the seat launcher.

**Cons:**
- Every session the owner opens on the VPS loads it.

**Why Rejected:** The mod belongs to seats only (D4).

### Option 6: Seats load the mod from the live checkout

**Description:** `CLAUDE_CODE_PLUGIN_DIRS` names `/opt/seshat/tooling/claude-mods/seat-agent`.

**Pros:**
- No promotion step.

**Cons:**
- Hot reload applies every `git pull` and every merge to every seat at once, with no gate and no rollback point.

**Why Rejected:** A change to every seat's behaviour is a deploy (D5).

---

## Consequences

### Positive Consequences

- A held draft no longer stalls cc-master (track A). In phase 2 it no longer stalls the seats either.
- Seat state comes from the engine's own events, not from reading pane text.
- The owner's Remote Control path stays outside every mod by a check that can fail, not by convention.
- A broken or disabled mod returns dispatch to today's behaviour (D7).

### Negative Consequences

- **The API is early access.** A Claude Code update can change or remove an event the mod uses. D7's version list turns that into a fallback, not an outage, but each update needs a test run before its version joins the list.
- **Two delivery paths exist during the transition:** the channel and the inbox. Trigger ids and the existing trigger ledger must deduplicate them.
- **One more deploy surface.** A promotion changes every loading seat's behaviour at once, so it sits in the stricter class.
- **The draft hold can delay a trigger by up to 10 minutes** while the owner types. The owner chose that delay.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A mod change skips a permission prompt or takes Remote Control input | High | D3's allow-list check in pre-commit and at promotion. AC-1 seeds the forbidden shapes. |
| A submit changes the owner's draft | Medium | Verified before phase 2 (D8). AC-7 checks it. |
| An engine update breaks the mod silently | Medium | D7: an unknown engine version means "no mod". The heartbeat records the version. |
| The mod loads in a non-seat session | Medium | D4: per-seat environment only. AC-2 checks that a session without `SESHAT_SEAT` writes nothing. |
| A promotion reaches all seats at once | Medium | D5: master's action, stricter class. AC-9 proves rollback. |

---

## Implementation Notes

**Files:**
- `~/cc-env/cc-seat-exec` and `cc-sessions.conf`: a per-seat mod column, `CLAUDE_CODE_PLUGIN_DIRS` and `SESHAT_SEAT`. cc-master's channel port.
- `scripts/dispatch/channel/plugins/seshat-dispatch/`: a master variant of the server instructions.
- `scripts/dispatch/gating_watcher.py`: master's channel-first delivery, the Remote Control busy check, per-tick logging of the readings, and the D7 fallback.
- `tooling/claude-mods/seat-agent/`: the mod, its contract and its tests.
- `scripts/dispatch/check_mod_rules.py`, `scripts/dispatch/promote_mod.py`, and the pre-commit entry.
- `scripts/dispatch/seat_state_report.py`: the phase-1 comparison.

**Order:**
1. Track A: master's channel. In parallel, the Remote Control busy check.
2. The gate: the rule check, promotion, the kill switch and the folder layout.
3. Phase 1: `seat-agent` state-only on cc-2build, with the report script.
4. Phase 2, after the owner's approval: the inbox, the draft hold and the submit.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1553. AC-1 to AC-5, AC-8 and AC-9 are adjudicated after phase 1 and track A, and AC-6 and AC-7 after phase 2.

- **AC-1 — The rule check refuses every forbidden shape.** · **Check:** fixture mods, each with one forbidden use: a `tool.check` hook, a `tool.call` hook, a `session.receive` hook, a `prompt.submit` hook, a `$.prompt.fill` call, an event outside the allow-list, and a computed `$[name]` access. Each goes through `promote_mod.py`. · *Fails if* any fixture is promoted, or the real `seat-agent` is refused.

- **AC-2 — The mod acts only in a seat.** · **Check:** a unit test loads the mod with `SESHAT_SEAT` unset and drives every hooked event. Live: during phase 1, `/proc/<pid>/environ` shows `CLAUDE_CODE_PLUGIN_DIRS` for cc-2build's process only, and `telemetry/seat_state/` holds a file for cc-2build only. · *Fails if* the test finds a write or a submit, or any other session's environment names the mod.

- **AC-3 — The owner's Remote Control path is intact.** · **Check:** during phase 1, the owner sends at least 3 prompts to cc-2build by Remote Control, and answers at least 1 permission prompt there. The transcript shows each prompt verbatim, and the permission prompt appears and takes the owner's answer. The owner confirms on FRE-1553. · *Fails if* any prompt is changed, missing or delayed past its turn, or a permission prompt does not appear.

- **AC-4 — The mod's state agrees with the engine, and the pane errors are explained.** · **Check:** `seat_state_report.py` over at least 3 days and at least 50 state changes on cc-2build. It compares the mod's `state` with Remote Control status and with the watcher's pane reading. · *Fails if* the mod and Remote Control status disagree on any change for longer than one heartbeat, or a disagreement with the pane reading has no cause in the report.

- **AC-5 — The fallback works.** · **Check:** on cc-2build, create the kill-switch file, then stop the mod by promoting a version that does not load. · *Fails if*, within 2 minutes of each, the watcher log does not show the D7 fallback for the seat, or dispatch to the seat stops.

- **AC-6 — No trigger is submitted before the owner approves phase 2.** · **Check:** the `uses.calls` list of every promoted version, against the date of the owner's phase-2 approval on FRE-1553. · *Fails if* any version promoted before that approval lists `prompt.submit`.

- **AC-7 — A trigger respects the draft (phase 2).** · **Check:** three live cases on the pilot seat:
  1. a draft that changed in the last minute;
  2. a draft unchanged for 5 minutes;
  3. no draft.

  Record `$.prompt.read()` before and after each submit, the hold time, and whether the band showed. · *Fails if* any draft changes, case 1 is not held or is held past 10 minutes, case 1 shows no band, or case 2 is held at all.

- **AC-8 — Master's triggers no longer stall behind a draft (track A).** · **Check:** over 14 days after track A deploys, the watcher's trigger ledger for cc-master: delivery path and time from queue to delivery. · *Fails if* any master trigger used `send-keys` while the channel was up, or any trigger waited more than 10 minutes while cc-master's Remote Control status was idle.

- **AC-9 — Rollback restores the previous version.** · **Check:** promote a new version on cc-2build, then promote the previous SHA. · *Fails if* the heartbeat does not show the previous `mod_version` within 2 minutes of the rollback.

---

## References

- FRE-1553 (umbrella)
- FRE-1550 — on hold by the owner, not part of this ADR
- The plugin API declaration of Claude Code build 2.1.289: `prompt.submit`, `$.prompt.submit`, `$.prompt.read`, `session.receive` and `SessionReceiveOrigin`, `tool.call`, `tool.check`, `plugin.register` and `PluginRegisterUses`, `turn.complete`, `session.end`; and its `reference.md` on `CLAUDE_CODE_PLUGIN_DIRS`, hot reload and `/reload-plugins`
- `scripts/dispatch/gating_watcher.py:939-1064` (delivery), `scripts/dispatch/pane_state.py:27-110` (busy reading), `scripts/dispatch/launcher.py:1034-1149` (reuse path), `scripts/dispatch/orchestrator.py:674-705` (the wedge)
- `scripts/dispatch/channel/plugins/seshat-dispatch/webhook.mjs:33-38` (the worker channel's instructions)
- `~/cc-env/cc-seat-exec`, `~/.claude/cc-sessions.conf`
- `.claude/settings.json:88-106` (the guard hooks)
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — event-driven dispatch actuation, the `seshat-dispatch` channel

---

## Status Updates

### 2026-10-09 - Proposed
**Changed By:** `adr` seat (FRE-1553)
**Reason:** Drafted on the owner's "craft it" after the exploration and the owner's answers on the direction, the draft policy and the pilot seat.
