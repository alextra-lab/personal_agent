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
- **Worker triggers already avoid typing.** The watcher first POSTs to the seat's `seshat-dispatch` channel. The channel is an MCP plugin, and `cc-seat-exec` gives it a port and a secret: ports 8790, 8791 and 8792, from column 4 of `~/.claude/cc-sessions.conf`. Only when that POST fails does the watcher type the prompt with `tmux send-keys` (`gating_watcher.py:939-1064`). The channel payload carries the PR facts but no trigger id (`gating_watcher.py:478-507`).
- **Three paths still type into a pane:**
  1. Master's triggers always use `send-keys`. cc-master has no channel port. After 30 minutes the watcher force-delivers into the pane, whatever it holds.
  2. The orchestrator's reuse path types `/clear`, `/model <tier>` and `/build <FRE>` (`launcher.py:1034-1149`).
  3. Each daemon's alert to master uses `send-keys`.
- **Busy state is read from the pane.** A seat counts as idle only when a bare `❯` line shows (`pane_state.py:27-110`). A held draft, such as `❯ /master 1218`, therefore reads as busy forever. Triggers queue behind it. One such draft stalled cc-1build for 2.5 h on 2026-10-03 and cc-master for about 5 h on 2026-10-04.

### What the mod API offers, and what it allows that must never happen

The facts below are from the API declaration of build 2.1.289 and its `reference.md`.

- **Exact seat state.**
  - `turn.start`, `turn.complete`, `session.start` and `session.end` are events a mod can hook.
  - `/clear` fires `session.end` with reason `clear`. The process then goes on under a new session id, and no `session.start` fires. `$.session.id()` returns the current id.
  - `$.prompt.read()` returns the owner's current draft.
- **A trigger that does not type.**
  - `$.prompt.submit` queues a prompt that starts its own turn once the session is idle. It never folds into a running turn.
  - The call resolves when that turn **starts**, not when the prompt is queued.
  - The box operation is a separate call, `$.prompt.fill`. The declaration does not state that a submit leaves the draft byte-identical, so that is a fact to verify on a live seat.
  - A submit may carry `asUser: true`. The model then reads the text as the person's own words.
- **What a validator sees.** `claude plugin validate` scans a module before it loads. It lists:
  - the event patterns the module registers (`uses.events`);
  - each `$` call by method name only (`uses.calls`);
  - the environment variable names it reads (`uses.env`).
  
  The scan does not see call arguments. A method name such as `fs.write` says nothing about the path written.
- **A mod can skip a permission prompt.** A `tool.check` hook can return `allow`. A `tool.call` hook can answer a tool itself, so the tool never asks.
- **A mod can take the owner's Remote Control input.** A `session.receive` hook can rewrite or consume a delivery whose origin is `bridge`, which is the owner's Remote Control input.
- **A mod can rewrite what the owner sees.** `ui.render` can rewrite components such as `UserMessage`, `AskUserQuestion`, `PromptHint` and `AbovePrompt`. The engine keeps the permission dialog itself.
- **A hook that throws, times out or returns an invalid answer is skipped.** A mod guard therefore fails open. A hook that is logically wrong can still drop a prompt on purpose.
- **Lifetime.** Timers from `$.clock.every` end when the module reloads and when the process ends. A `/clear` does not reload the module.
- **Loading.**
  - A mod loads from `--plugin-dir` or from `CLAUDE_CODE_PLUGIN_DIRS`, taken from the process environment or from `~/.claude/settings.json`.
  - An interactive session watches that folder. Changes reload the module, coalesced, and deferred to the end of a turn when the session's own turn made them.
  - A plugin folder given as a link is watched at the folder that the link names.

### What mods cannot fix

- **The backwards-move wedge** is the orchestrator's own state: a `launched` record releases only on a terminal ticket state (`orchestrator.py:674-705`).
- **The Bash-text guards** (`deploy-approval-gate.sh`, `block-direct-main-push.sh`) must fail closed. A mod fails open, so it cannot replace them.

---

## Decision

### D1 — The daemons stay the deciders

The gating watcher and the dispatch orchestrator keep every decision: what to trigger, when, and for which seat. No dispatch logic moves into a mod, and no mod runs in cc-master to replace them.

The reason is lifetime. A daemon is a supervised system service. A mod lives inside one session process: its timers end at every reload and with the process. Master's process restarts and reloads often.

### D2 — Two tracks run in parallel

- **Track A, no mod.** cc-master receives a `seshat-dispatch` channel of its own, and the watcher sends master's triggers through it first.
  - Master's channel needs its own server instructions. The worker channel's instructions forbid merging or approving any PR (`webhook.mjs:33-38`), and merging is master's job.
  - The watcher's busy check reads Remote Control status (`claude agents --json`) first, and the pane only as a fallback. Remote Control status does not count a held draft as busy, and it reports a session that waits on a prompt as `waiting`.
- **Track B, the seat agent.** One mod, `seat-agent`, loads in a seat. Phase 1 reports the seat's state. Phase 2, after the owner approves it, delivers triggers. The mod never decides what to trigger.

### D3 — Three rules bind every mod in a seat, and the check is argument-sensitive

**The rules.**
1. **A mod never decides a permission.** It never hooks `tool.check` or `tool.call`, and it never writes a settings, permission, hook or plugin file.
2. **A mod never rewrites or consumes the owner's input.** It never hooks `session.receive` or `prompt.submit`, and it never submits text as the owner's words (`asUser`).
3. **A mod never writes or redraws what the owner types or reads.** It never calls `$.prompt.fill` or `$.prompt.suggest`. It never hooks `ui.render` for any component except `AbovePrompt`.

**The threat model.** The check guards against an **accidental** change, for example a seat's model that edits the mod in a later task. It does not guard against a hostile author: anyone who can change the mod and its check can also change the review. PR review and master's promotion (D5) cover intent. The check covers drift, mechanically, on every version.

**The check.** `scripts/dispatch/check_mod_rules.py` has three layers. Every layer must pass.

1. **Scan allow-lists**, from `claude plugin validate`. Each phase has a fixed set, held in `tooling/claude-mods/seat-agent/rules.yaml`:

   | List | Phase 1 | Phase 2 adds |
   |---|---|---|
   | `uses.events` | `session.start`, `session.end`, `turn.start`, `turn.complete` | `ui.render` |
   | `uses.calls` | `fs.write`, `fs.stat`, `clock.every`, `prompt.read`, `env.get`, `session.id`, `session.version` | `fs.read`, `fs.list`, `prompt.submit`, `ui.invalidate`, `ui.resolve` |
   | `uses.env.reads` | `SESHAT_SEAT` | none |

   Anything outside the set fails, including an event or call that a future release adds.
2. **Source rules**, from `ast-grep` rules over the module:
   - there is no computed access on `$` (`$[x]`) and no alias of `$` or of a noun (`const p = $.prompt`);
   - every `$.fs.write` call has, as its path, the module's single path function `statePath()` or `inboxPath()`;
   - every `$.prompt.submit` call passes an object literal without an `asUser` key;
   - every `ui.render` registration names the matcher `{ component: 'AbovePrompt' }` literally.
3. **Runtime guard.** `statePath()` and `inboxPath()` accept only a seat name matching `^cc-[a-z0-9]+$` and an id matching `^[A-Za-z0-9_-]{1,64}$`. Each resolves the **parent folder** with `$.fs.stat(dir, { resolve: true })`, refuses any `realPath` outside `/opt/seshat/telemetry/seat_state/` or `/opt/seshat/telemetry/seat_inbox/<seat>/`, and then appends the validated file name. If the file already exists, it is resolved too, so a link planted at the file name is refused. The seat setup creates both folders, so the parent always exists. A test drives both functions with traversal, link and missing-file inputs.

The check runs in pre-commit and in the promotion script (D5). The promotion run is the binding one: no version reaches a seat without it.

### D4 — A mod loads only in a seat, and only where its seat says so

- **Load.** `cc-seat-exec` exports `CLAUDE_CODE_PLUGIN_DIRS` and `SESHAT_SEAT=<seat name>` for a seat that `cc-sessions.conf` marks for the mod. `~/.claude/settings.json` never names the mod, so the owner's other sessions never load it.
- **Scope.** If `SESHAT_SEAT` is unset, or does not match a seat marked in `cc-sessions.conf` for the mod, the mod registers its hooks but does nothing. Every hook passes straight to `next`.
- **Phase 1 scope:** only cc-2build.

### D5 — A mod change is a deploy, gated by promotion

- **Source.** The mod lives in the repo under `tooling/claude-mods/seat-agent/`. It changes by PR, with `claude plugin test` and the D3 check.
- **Runtime layout.** Seats load the stable folder `~/.claude/seshat-mods/seat-agent/`, never the live `/opt/seshat` checkout. Hot reload watches that folder, so loading from the checkout would make every `git pull` an ungated deploy to every seat. The folder holds:
  - `.claude-plugin/plugin.json` and `hooks/hooks.json`, which never change;
  - `releases/<sha>/`, one immutable directory per promoted version;
  - `hooks/register.ts`, a one-line entry module that re-exports `register` from one release and exports `MOD_VERSION = '<sha>'`.
- **Promotion.** `scripts/dispatch/promote_mod.py <git-sha>`:
  1. exports the mod at that commit into a new `releases/<sha>/`;
  2. runs the D3 check and the tests against that directory;
  3. writes the new entry module to a temporary file, and renames it over `hooks/register.ts`. A file rename is atomic, so a seat loads either the old release or the new one, never a mix.
  
  Promotion completes only when every loading seat's heartbeat reports the new `MOD_VERSION` within 3 minutes. "Every loading seat" means every seat that `cc-sessions.conf` marks for the mod and that Remote Control lists as present. An offline seat loads the current entry module when it starts, and is not waited for. `promote_mod.py --dest <folder>` promotes into another folder, for tests. `MOD_VERSION` comes from the loaded module itself, not from a file. If a seat does not report it in time, the script renames the previous entry module back, which is an automatic rollback.
- **Who.** Promotion is master's action, in the stricter deploy class.
- **Rollback.** Promote the previous SHA. Its release directory is still present, so rollback is a single entry rename. Releases are deleted only when they are two versions old.
- **Kill switch.** While the file `~/.claude/seshat-mods/seat-agent.disabled` exists, every hook passes through, and the heartbeat records `disabled: true`.

### D6 — Phase 1 reports state, and no daemon acts on it

On cc-2build, `seat-agent` writes `/opt/seshat/telemetry/seat_state/cc-2build.json` on every hooked event, and every 30 s as a heartbeat. The fields:

| Field | Meaning |
|---|---|
| `seat`, `session_id` | `SESHAT_SEAT`, and `$.session.id()` at the write |
| `state` | `idle`, `turn` or `ended` |
| `engine_version` | `$.session.version().version` |
| `turn_started_at`, `turn_completed_at` | from `turn.start` and `turn.complete` |
| `draft_present`, `draft_changed_at` | from polling `$.prompt.read()` every 5 s. The draft text is never written. |
| `mod_version`, `disabled` | the loaded `MOD_VERSION`, the kill switch |
| `heartbeat_at` | the last write |

**After a `/clear`.** `session.end` with reason `clear` sets `state` to `ended`. No `session.start` follows. On the next heartbeat, the mod reads `$.session.id()`. If the id differs from the one recorded, it sets `state` to `idle` under the new id.

**The state mapping.** `claude agents --json` reports a session's status as one of the values of the API's `AgentStatus`. For comparison, the report maps them to the mod's states:

| Remote Control status | Mod state |
|---|---|
| `idle` | `idle` |
| `running`, `waiting` (a permission prompt or a question inside a turn), `pending` | `turn` |
| `completed`, `failed`, `killed`, or the session absent from the list | `ended` |

**Observation only.** The watcher logs on every tick, for the seat: its pane reading, the Remote Control status, and the mod file's `state`. `scripts/dispatch/seat_state_report.py` compares the three. No daemon makes a decision from the mod's file in phase 1.

### D7 — The delivery order, and the last resort is an alert

D7 applies to track A as soon as track A deploys. Its inbox branch (point 1 below) is observation-only in phase 1: the watcher logs that it would have used the inbox, and uses the channel. The inbox branch becomes active in phase 2.

A daemon treats a seat as having no mod when any of these holds:
- the heartbeat is older than 90 s;
- `disabled` is true;
- `engine_version` is not on the list that the promoted version was tested against.

The delivery order for a trigger is then:
1. the seat's inbox (D8), while the mod is present;
2. the seat's channel (track A), with no typing;
3. `send-keys`, but only while Remote Control status reads `idle` **and** the draft is known to be empty. A fresh mod report (heartbeat inside 90 s, engine version on the list) decides whether a draft exists. Without one, the pane must show a bare empty prompt. A disagreement between the readings, or an unknown reading, counts as "not known empty";
4. otherwise, **an alert to master and the owner**, never a typed injection into a busy seat, a held draft or a permission wait.

This replaces the 30-minute force-delivery for any seat on track A or track B.

### D8 — Phase 2 delivers triggers by one protocol, after the owner approves it on the phase-1 evidence

Phase 2 starts only when the owner records approval on FRE-1553, with the AC-4 report beside it. The PR that changes `rules.yaml` to phase 2 links that approval, and master's gate checks the link.

**One trigger id across transports.** Every trigger carries a stable `trigger_id`, created once by the daemon and recorded in its trigger ledger. The channel payload gains the same field. A daemon sends a trigger on one transport at a time, in D7's order.

**The receiver-side record.** The inbox holds one file per trigger, `/opt/seshat/telemetry/seat_inbox/<seat>/<trigger_id>.json`. The mod moves it through these states by rewriting its `state` field:

| State | Written by | When |
|---|---|---|
| `pending` | the daemon | the trigger is placed |
| `claimed` | the mod | before it calls `$.prompt.submit` |
| `started` | the mod | when the submit resolves, which is when its turn starts |

- **No re-send from the inbox.** A trigger placed in the inbox is never sent on another transport, because a re-send could race the mod's claim. If it is still `pending` 5 minutes after it was placed, the daemon alerts. The daemon places a trigger in the inbox only while the mod is present (D7), so a trigger left `pending` means the mod failed after the placement.
- **Claimed but not started.** This is normal while the seat waits on a permission prompt or a running turn. The daemon never re-sends a claimed trigger. If it is not `started` within 30 minutes, the daemon alerts.
- **The ambiguous case.** A crash between `claimed` and `started` cannot be resolved: the submit may or may not have queued. The daemon reports such a trigger as ambiguous and alerts. It never re-sends it.
- **Draft hold (owner, 2026-10-09).** Before it claims a trigger, the mod holds it while the owner's draft changed in the last 2 minutes, for at most 10 minutes. While it holds, a band above the prompt names the waiting trigger. A draft unchanged for 2 minutes never holds a trigger.
- **Submit.** The mod calls `$.prompt.submit` with the trigger's text, framed as the plugin's message (no `asUser`).

**Two facts to verify before phase 2 ships:**
- that a submit leaves the owner's draft byte-identical;
- whether a submitted `/clear` or `/build <FRE>` runs as a command.

If commands do not run, the orchestrator's reuse path launches a fresh seat in place of `/clear`. Until that change lands, the reuse path stays on `send-keys`, under D7 point 3.

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
- Remote Control status gives a session state, but not a turn's start and end, or whether the owner has a draft.
- A channel event becomes a model turn, and a model cannot run `/clear` or `/model`. So the reuse path still types, or needs a relaunch.

**Why Rejected as the whole answer:** It is adopted as track A. The pilot measures what track B adds on top of it.

### Option 3: Extend the channel into a seat sidecar

**Description:** A per-seat, model-free process, or the existing channel server, gains trigger ids, acknowledgements and a state report. No mod is used.

**Pros:**
- No early-access API, and no code inside the session's hook chain.
- A separate process can enforce file paths with ordinary file permissions.
- Durable acknowledgements are easy.

**Cons:**
- Neither a channel server nor a sidecar can see a turn start, a turn end, the owner's draft or a permission wait. Those are inside the session. The sidecar would infer them, which is today's problem in a new place.

**Why Rejected as the whole answer:** Its useful part, the shared `trigger_id`, is adopted in D8. The state signal needs the engine's own events.

### Option 4: Replace the daemons with a mod in cc-master

**Description:** Dispatch logic runs on `$.clock.every` inside master's session.

**Pros:**
- One place for dispatch, inside the session the owner watches.

**Cons:**
- Every reload or restart of master's process stops dispatch.
- The logic shares master's context and turn loop.

**Why Rejected:** A supervised daemon outlives a session process. Dispatch must outlive master's restarts.

### Option 5: Move the guards into mods

**Description:** `tool.call` hooks replace `deploy-approval-gate.sh` and `block-direct-main-push.sh`.

**Pros:**
- Hooks see parsed tool input, and a mod can show a reason in the session.

**Cons:**
- A hook that throws is skipped, so the guard fails open.
- The hook still reads the same command text, so text matching stays.

**Why Rejected:** A guard must fail closed.

### Option 6: Load the mod from `~/.claude/settings.json`

**Description:** Name the mod folder in the global settings' `env` block.

**Pros:**
- No change to the seat launcher.

**Cons:**
- Every session the owner opens on the VPS loads it.

**Why Rejected:** The mod belongs to seats only (D4).

### Option 7: Seats load the mod from the live checkout

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
- No trigger is ever typed into a busy seat, a held draft or a permission wait (D7). The last resort is an alert.
- Seat state comes from the engine's own events, not from reading pane text.
- The owner's Remote Control path stays outside every mod by a check that can fail. Within the stated threat model, an accidental change cannot reach it.
- A broken or disabled mod returns dispatch to track A (D7).

### Negative Consequences

- **The API is early access.** A Claude Code update can change or remove an event the mod uses. D7's version list turns that into a fallback, not an outage, but each update needs a test run before its version joins the list.
- **The check guards against accident, not malice** (D3). Intent stays a matter of review.
- **The ambiguous case exists** (D8). A crash between claim and start leaves a trigger that the system cannot classify, so it alerts.
- **One more deploy surface.** A promotion changes every loading seat's behaviour at once, so it sits in the stricter class.
- **The draft hold can delay a trigger by up to 10 minutes** while the owner types. The owner chose that delay.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A mod change skips a permission prompt, takes Remote Control input or misleads the owner's view | High | D3's three-layer check in pre-commit and at promotion. AC-1 seeds every forbidden shape. |
| A trigger is delivered twice | Medium | D8's one `trigger_id`, the receiver-side record, and no re-send after a claim. AC-7 tests it. |
| A submit changes the owner's draft | Medium | Verified before phase 2 (D8). AC-7 checks it. |
| An engine update breaks the mod silently | Medium | D7: an unknown engine version means "no mod". The heartbeat records the version. |
| The mod loads in a non-seat session | Medium | D4: per-seat environment only. AC-2 checks every Claude process. |
| A promotion reaches all seats at once | Medium | D5: master's action, stricter class, automatic rollback without a heartbeat. AC-9 tests it. |

---

## Implementation Notes

**Files:**
- `~/cc-env/cc-seat-exec` and `cc-sessions.conf`: a per-seat mod column, `CLAUDE_CODE_PLUGIN_DIRS` and `SESHAT_SEAT`. cc-master's channel port.
- `scripts/dispatch/channel/plugins/seshat-dispatch/`: a master variant of the server instructions, and `trigger_id` in the payload.
- `scripts/dispatch/gating_watcher.py`: master's channel-first delivery, the Remote Control busy check, per-tick logging of the three readings, the D7 order, and the end of force-delivery.
- `tooling/claude-mods/seat-agent/`: the mod, `rules.yaml`, its contract and its tests.
- `scripts/dispatch/check_mod_rules.py` with its `ast-grep` rules, `scripts/dispatch/promote_mod.py`, and the pre-commit entry.
- `scripts/dispatch/seat_state_report.py`: the phase-1 comparison.

**Order:**
1. Track A: master's channel with `trigger_id`. In parallel, the Remote Control busy check and the D7 order.
2. The gate: the rule check, promotion, the kill switch and the folder layout.
3. Phase 1: `seat-agent` state-only on cc-2build, with the report script.
4. Phase 2, after the owner's approval: the inbox protocol, the draft hold and the submit.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1553. AC-1 to AC-5 and AC-8 to AC-9 are adjudicated after phase 1 and track A. AC-6 and AC-7 are adjudicated after phase 2.

- **AC-1 — The rule check refuses every forbidden shape.** · **Check:** fixture mods, each with one forbidden use, go through `promote_mod.py --dest <temporary folder>`, never the live runtime folder:
  - a `tool.check` hook, a `tool.call` hook, a `session.receive` hook, a `prompt.submit` hook;
  - a `$.prompt.fill` call, an event or call outside the phase's allow-list, an environment read other than `SESHAT_SEAT`;
  - a `ui.render` matcher other than `AbovePrompt`;
  - an `fs.write` whose path does not come from `statePath()` or `inboxPath()`, and a path function given `../` or a link that resolves outside the two roots;
  - a `prompt.submit` with `asUser`, a computed `$[x]` access, an alias of `$.prompt`.

  · *Fails if* any fixture is promoted, or the real `seat-agent` is refused.

- **AC-2 — The mod acts only in a seat.** · **Check:**
  1. A unit test loads the mod three ways: `SESHAT_SEAT` unset, set to an unknown name, and set to a seat not marked for the mod. In each, it drives every hooked event.
  2. Live, during phase 1: empty `telemetry/seat_state/` first. Then, for every running `claude` process on the VPS, read `/proc/<pid>/environ`.
  
  · *Fails if* the test finds any write, or any process other than cc-2build's names the mod, or a file other than `cc-2build.json` appears.

- **AC-3 — The owner's Remote Control path is intact.** · **Check:** during phase 1, the owner sends at least 3 prompts to cc-2build by Remote Control, and answers at least 1 permission prompt there. For each prompt, the transcript records the `bridge`-origin row and the turn it starts. For the permission prompt, it records the request and the owner's answer. · *Fails if*:
  - any prompt's stored text differs from what the owner sent;
  - any prompt starts no turn within 60 s of the seat being idle;
  - the permission prompt does not appear, or, when the owner answers it with a deny, the tool runs.

- **AC-4 — The mod's state agrees with the engine across every transition.** · **Check:** `seat_state_report.py` over phase 1, timestamped from the mod's file, the Remote Control status and the pane reading. The window must contain at least these transitions:
  - turn start and turn end, at least 20 of each;
  - a `/clear`;
  - a promotion reload;
  - a seat restart;
  - a held draft for more than 5 minutes;
  - a permission wait.
  
  · *Fails if* any listed transition is absent, or the mod's `state` differs from the mapped Remote Control status (D6's table) for longer than 35 s (one heartbeat plus one poll) at any transition. It also fails if, after a `/clear`, the mod does not report `idle` under the new session id within 35 s. Disagreements with the pane reading are listed by type, and a held draft that the pane reads as busy is the expected type.

- **AC-5 — In phase 1, the fallback decision is logged correctly.** · **Check:** on cc-2build, create the kill-switch file; later, promote a version that does not load. Then place a synthetic trigger with a known `trigger_id`. · *Fails if*, within 3 minutes of each change (90 s stale threshold plus a 60 s poll, with margin), the watcher's log does not record the D7 path for the seat, or the synthetic trigger is delivered on anything but exactly one transport.

- **AC-6 — No trigger is submitted before the owner approves phase 2.** · **Check:**
  1. `promote_mod.py` refuses any version that calls `prompt.submit` while `rules.yaml` is at phase 1. A fixture proves the refusal.
  2. A test of the phase-1 build places a trigger in the inbox and captures every `$.prompt.submit` call.
  3. The PR that moves `rules.yaml` to phase 2 links the owner's approval on FRE-1553.
  
  · *Fails if* the fixture is promoted, the test captures any submit, or the phase-2 PR has no linked approval.

- **AC-7 — A trigger respects the draft, the permission wait and exactly-once delivery (phase 2).** · **Check:** live cases on the pilot seat, each with a synthetic `trigger_id`. Record `$.prompt.read()` before and after, the hold time, the band, the captured submit arguments, and the inbox states:
  1. a draft that changes every 30 s throughout;
  2. a draft unchanged for 5 minutes;
  3. no draft;
  4. a pending permission prompt when the trigger arrives;
  5. the mod reloaded between `claimed` and `started`;
  6. the mod stopped (kill switch) after the trigger is placed and before it is claimed, so it stays `pending` past 5 minutes.
  
  · *Fails if*:
  - any draft changes;
  - case 1 is not held, or held less than 9.5 minutes or more than 10.5 minutes, or shows no band;
  - case 2 is held at all;
  - case 4 is re-sent on another transport;
  - case 5 is re-sent instead of alerted as ambiguous;
  - case 6 is sent on another transport instead of alerted;
  - any submit carries `asUser`;
  - any trigger starts more than one turn.

- **AC-8 — Master's triggers no longer stall behind a draft (track A).** · **Check:** after track A deploys, the owner leaves a draft in cc-master's box, and a synthetic trigger with a known `trigger_id` is placed. Then, over 14 days of normal use, the watcher's trigger ledger for cc-master records each trigger's transport and delivery time. · *Fails if*:
  - the synthetic trigger does not start a master turn within 5 minutes while the draft stays;
  - any `send-keys` was used for it;
  - in the 14 days, any trigger used `send-keys` while the channel was up, or was force-delivered into a held draft.

- **AC-9 — Promotion and rollback load the version they name.** · **Check:**
  1. Promote version B over version A on cc-2build.
  2. Promote A again.
  3. Promote a version that fails to load.
  4. Interrupt a promotion between the release export and the entry rename.

  · *Fails if*:
  - after steps 1 and 2, the heartbeat's `mod_version` (the loaded module's own constant) is not the promoted SHA within 3 minutes;
  - after step 3, the script does not roll back to the previous entry automatically;
  - after step 4, the seat does not still run the previous version.

---

## References

- FRE-1553 (umbrella)
- FRE-1550 — on hold by the owner, not part of this ADR
- The plugin API declaration of Claude Code build 2.1.289:
  - `prompt.submit`, `$.prompt.submit`, `PromptSubmitArgs.asUser` and `$.prompt.read`;
  - `session.receive` and `SessionReceiveOrigin`, `tool.call`, `tool.check`, `ui.render`;
  - `plugin.register` and `PluginRegisterUses`, `turn.complete`, `session.end` and `SessionEndInput`, `$.session.id`;
  - its `reference.md` on `CLAUDE_CODE_PLUGIN_DIRS`, hot reload, timers and `/reload-plugins`
- `scripts/dispatch/gating_watcher.py:478-507` (the channel payload), `:939-1064` (delivery); `scripts/dispatch/pane_state.py:27-110` (busy reading); `scripts/dispatch/launcher.py:1034-1149` (reuse path); `scripts/dispatch/orchestrator.py:674-705` (the wedge)
- `scripts/dispatch/channel/plugins/seshat-dispatch/webhook.mjs:33-38` (the worker channel's instructions)
- `~/cc-env/cc-seat-exec`, `~/.claude/cc-sessions.conf`
- `.claude/settings.json:88-106` (the guard hooks)
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — event-driven dispatch actuation, the `seshat-dispatch` channel

---

## Status Updates

### 2026-10-09 - Proposed
**Changed By:** `adr` seat (FRE-1553)
**Reason:** Drafted on the owner's "craft it" after the exploration and the owner's answers on the direction, the draft policy and the pilot seat. Codex round 1 (6 blocking): the rule check became argument-sensitive, with a stated threat model; a single `trigger_id` protocol with a receiver-side record replaced the acknowledgement timeout; promotion became an atomic entry-module rename with heartbeat confirmation and automatic rollback; the `/clear` timer claim was corrected and the heartbeat now detects a new session id; D7 became phase-2 only, with an alert as the last resort; Option 3 (a channel sidecar) was added. Codex round 2 (5 blocking): the path guard resolves the parent folder, so a first write succeeds; a pending inbox trigger is never re-sent, so no claim race exists; D7 applies to track A at once, with its inbox branch observation-only in phase 1; a defined precedence makes conflicting draft readings alert; and a mapping from Remote Control status to the mod's states makes AC-4 decidable.
