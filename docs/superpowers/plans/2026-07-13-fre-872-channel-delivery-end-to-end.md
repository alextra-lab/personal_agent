# FRE-872 — ADR-0116 T2–T4 (consolidated): channel delivery end-to-end

**Ticket:** FRE-872 (Approved, Tier-2:Sonnet, `[codex: required]`)
**Backing ADR:** `docs/architecture_decisions/ADR-0116-event-driven-dispatch-actuation.md`
**Depends on (shipped):** FRE-871 (`d980f638`) — `seshat-dispatch` channel (server.mjs/webhook.mjs) +
launcher `--channels` allowlist opt-in. This ticket does **not** touch the Node channel process's
security-critical gate; it wires the **Python gateway side** (the watcher) to talk to it, and extends
the ledger + launcher topology.
**Explicitly out of scope (per ticket body):** the actual per-seat cutover (flipping a real seat's
mode to `channel` in production) and idle-scrape deletion — that is FRE-875 (the seam, ask-first
deploy). This ticket lands code + tests with the **default mode staying `send_keys` for every real
seat** — i.e. this PR must not change any live dispatch behavior when merged.

## Codex plan-review — findings and revisions (2026-07-13)

Codex reviewed the first draft of this plan (`codex:rescue`, adversarial pass). Two **High** findings
changed the ledger design; one **Medium** changed the dependabot/master-ready scoping; one **Medium**
added payload fields; one **Low** added channel-server hardening. All five are folded into the sections
below (not left as follow-up tickets — "fold in, don't over-ticket"). Summary of what changed from the
first draft:

1. **No more optimistic `transport="channel"` ledger write.** The original design wrote
   `transport="channel"` at `record_pending` time and "corrected" it to `send_keys` on fallback. Codex
   flagged two crash windows where this leaves the ledger auditing a transport that never actually
   happened (a crash before `mark_send_started` recovers via reconcile's unconditional tmux retry, but
   the entry still reads `transport=channel`; a crash between a same-tick fallback send and the
   correction call). **Fix:** `transport` defaults to `send_keys` at `record_pending` (unchanged
   default) and is flipped to `channel` via `mark_transport` **only once, exactly when a channel POST is
   confirmed `"delivered"`** — never optimistically, never as a "correction." A same-tick fallback simply
   never calls `mark_transport`, so the default stays accurate. A crash-recovered entry (via the
   existing, unmodified `reconcile()` → tmux retry) is *always* correctly audited as `send_keys`, because
   that is genuinely what happened — reconcile's retry path needs **zero changes** for this ticket
   (it already only knows how to retry via tmux with the entry's plain-string `command`, which is exactly
   the right universal fallback for a never-confirmed entry regardless of what transport was attempted).
2. **Dependabot guard now also suppresses master-ready classification**, not just the worker path.
   Codex noted the ADR's "green + dependabot-clean → ready" master condition was left completely
   unaddressed in the first draft. Rather than inventing a "clean" heuristic, this ticket takes the
   conservative reading: a dependabot-authored PR produces **no watcher trigger at all** (neither worker
   nor master) until a follow-up defines what "dependabot-clean" means quantitatively. Master's existing,
   separate dependabot review process (unchanged, pre-existing, outside the watcher) still applies.
3. **Channel payload gains `conclusion` and `details_url` per check** (both already present in the
   `statusCheckRollup` JSON the watcher already fetches — no extra API call) — codex flagged the
   original `{name, state}`-only payload as too thin for FRE-875's later live AC-3 proof to have a
   realistic chance of passing. Failing-log-excerpt enrichment (would need a *new* `gh api`/`gh run view`
   call per failing check) stays out of scope — noted as a real follow-up, not implemented here.
4. **`server.mjs` gains a body-size cap and method/path rejection**, folded into this PR even though
   the file was FRE-871's, because codex flagged it directly against the ADR's own named threat model
   ("ungated inbound channel = prompt-injection vector") and it is a small, contained, obviously-correct
   hardening this ticket's own delivery code newly starts exercising in anger.
5. **AC-1/AC-3/AC-6 framing sharpened**: the plan must not present FRE-872 as having proven the *live*
   halves of these ACs (a real seat reacting to a real push; a real git/GitHub audit). Kept as a firm
   "what this PR does NOT prove" callout for the Step-9 ticket comment (see the mapping table at the
   bottom), matching the FRE-871/AC-2 precedent exactly.

## Scope decisions surfaced for codex/owner review

These are judgment calls the ticket body doesn't pin down explicitly. Flagging them up front so
codex's plan-review (mandatory) evaluates them before code is written, not after.

1. **Per-seat mode flag location.** Adding `mode: Literal["channel", "send_keys"] = "send_keys"` to
   `StreamTopology` (`launcher.py`) — the one existing per-seat coordinate table both `launcher.py`
   and `gating_watcher.py` already share via `topology_for()`. `cc-master` has no `StreamTopology`
   entry (it isn't launched via `launcher.py`) and gets **no channel capability in this ticket** — it
   stays send-keys-always, unchanged. Master's own eventual channel cutover (if any) is not scoped by
   FRE-872's three consolidated items and would need its own ticket/topology entry.
2. **Master's `--channels` capability in `plan_launch` stays independent of `topology.mode`.**
   `LauncherCapabilities.channels` (FRE-871) remains a per-invocation flag, not auto-derived from
   `topology.mode`, so none of FRE-871's existing tests need to change. FRE-875 (the cutover ticket)
   is the natural place to wire "launch this stream with channels iff `topology.mode == 'channel'`".
3. **Dependabot boundary guard is structural, not just prompt-level, and covers both trigger kinds.**
   `classify_pr` gets an explicit `pr.is_dependabot` check that short-circuits **before** either the
   worker or the master branch — a dependabot-authored PR produces `None` (no trigger at all)
   unconditionally in this ticket (revised per codex finding — see "Codex plan-review" above). This is
   defense-in-depth on top of the existing incidental guard (a `dependabot/npm_and_yarn/...` branch never
   matches `_BRANCH_RE` today either, so the worker half was already accidentally protected; this makes
   it intentional and directly testable) **and** closes the master-ready gap codex identified: the ADR's
   "green + dependabot-clean → ready" is satisfied conservatively — "not yet defined as clean" reads as
   "never watcher-triggered ready" — rather than inventing an arbitrary "clean" heuristic. Master's
   existing, separate process for reviewing/merging dependabot PRs is unchanged and outside the watcher
   entirely.
4. **AC-1/AC-3's "seat literally reacts to a live push" is a live-verification, deferred to FRE-875** —
   same posture FRE-871 took for AC-2 (ships the artifact + tests; the live proof runs at cutover
   with a real running seat + channel process). This ticket proves the **code-provable half** of each
   AC with injected fakes (fake HTTP poster for the channel, fake tmux `CommandRunner`, fake ledger
   persistence) — no live seat, no live channel process, no `--execute` against a real tmux session.
5. **Gateway-side secret env var name:** `AGENT_SESHAT_CHANNEL_SECRET` (mirrors the existing
   `AGENT_LINEAR_API_KEY` load pattern in `scripts/reconcile_board.py`), distinct from the seat-side
   `SESHAT_CHANNEL_SECRET` the Node channel process reads (FRE-871, unprefixed since it isn't
   `personal_agent.config`-routed). **Both sides must be provisioned with the identical secret value**
   — called out explicitly in the PR handoff runbook for master.

## 1. Ledger transport + per-seat mode

**File: `scripts/dispatch/trigger_ledger.py`**

- Add `Transport = Literal["channel", "send_keys"]`.
- `LedgerEntry`: add frozen field `transport: Transport = "send_keys"`.
- `record_pending(...)`: **no new parameter.** Every entry is created with the default
  `transport="send_keys"` — always, regardless of which transport the caller intends to attempt. This
  is deliberate (see "Codex plan-review" above): there is no "optimistic" write to get wrong.
- New: `mark_transport(ledger: Ledger, event_id: str, transport: Transport) -> Ledger` — the *only* way
  an entry's transport ever becomes `"channel"`. Called exactly once, by the caller, **only after** a
  channel delivery is confirmed (`"delivered"`) — never before, never speculatively, never as a
  same-tick "correction" (there is nothing to correct: a fallback just never calls this, so the
  `send_keys` default silently stays accurate). Same shape as `mark_sent`/`mark_consumed`/`mark_surfaced`.
  A crash between a confirmed channel delivery and this call is the one narrow window where the ledger
  under-reports (`transport` reads `send_keys` for what was actually a channel delivery) — accepted as
  the safe direction to be wrong in (it never *overclaims* a channel delivery that didn't happen, and
  the entry is `sent`/`consumed` correctly either way — only the audit tag of *how* is briefly stale).
- `_entry_to_json`: include `"transport": entry.transport`.
- `load_ledger`: parse `transport` from the raw dict, defaulting to `"send_keys"` and coercing any
  unrecognized value to `"send_keys"` (never crash on an old on-disk ledger written before this field
  existed — matches the file's existing lenient-parse style for every other field).
- **`reconcile()`'s retry path (`scripts/dispatch/gating_watcher.py`'s `_retry_pending` closure) needs
  ZERO changes.** A never-started entry (any transport) is retried via the existing unconditional
  `send_to_session(entry.target_pane, entry.command, runner)` — tmux is the universal, always-safe
  recovery path since `entry.command` is always the plain-string form regardless of which transport was
  attempted live. This is the concrete fix for codex's two High findings: recovery never needs to know
  "was this meant to be a channel delivery," because a crash-recovered send is *definitionally* a
  send-keys send, and the ledger already defaults to (and never falsely overclaims) `transport=send_keys`.
- Docstring: one short addendum noting the transport tag, that it is set post-hoc on confirmed success
  only, and that `record_pending`'s existing per-`event_id` dedup is what actually delivers AC-4 (no
  double-fire) — a single ledger entry per event id, tagged with whichever transport delivered it, not a
  second parallel key space.

**File: `scripts/dispatch/launcher.py`**

- `StreamTopology`: add frozen field `mode: Literal["channel", "send_keys"] = "send_keys"`, with a
  docstring note (codex Medium finding) that flipping this to `"channel"` for a real seat is only
  correct **together with** launching that seat via `--channels` (today two independent per-invocation
  choices — `topology.mode` drives watcher delivery, `LauncherCapabilities.channels` drives the launch
  argv); FRE-875 must either derive one from the other or otherwise guarantee they can't drift, before
  any real seat flips. This ticket's own regression test (below) is the concrete guard available now:
  it fails loudly if `mode` defaults ever silently change to `"channel"` while nothing wires the launch
  side to match.
- `_TOPOLOGY`: no behavior change (all three entries explicitly `mode="send_keys"` — this ticket ships
  no live cutover).
- New pure helper: `stream_for_tmux_session(session: str) -> str | None` — reverse-maps a tmux session
  name back to its stream key by scanning `_TOPOLOGY` (`None` if no match). This lets
  `gating_watcher.decide()` recover a worker trigger's full topology (mode, channel_port) from the
  `session` string `session_resolver` already returns, without changing `session_resolver`'s existing
  `str | None` return type (keeps `send_keys_whitelist.py`'s and every existing test's call sites
  untouched).

**Tests: `tests/scripts/test_trigger_ledger.py`**
- `record_pending` always creates a fresh entry at `transport="send_keys"` (no way to override — this
  is intentional, see above).
- `mark_transport` flips an existing entry's transport, leaves every other field untouched.
- `load_ledger` on a JSON fixture with no `"transport"` key defaults to `"send_keys"`; on a garbage
  value (`"carrier-pigeon"`) also defaults to `"send_keys"` (never raises).
- `save_ledger` → `load_ledger` round-trips `transport` faithfully.

**Tests: `tests/scripts/test_launcher.py`** (existing file — add, don't restructure)
- `stream_for_tmux_session("cc-build2") == "build2"`; unknown session → `None`.
- `topology_for(stream).mode == "send_keys"` for all three streams (the "no live cutover" invariant,
  regression-guarded so a future accidental default flip is caught).

## 2. Gateway channel delivery

**File: `scripts/dispatch/gating_watcher.py`**

- Add `_DEPENDABOT_LOGIN = "dependabot[bot]"`.
- Add `CheckResult` frozen dataclass: `name: str`, `state: Literal["pass", "fail", "pending"]`,
  `conclusion: str`, `details_url: str` (both added per codex finding — already present in the
  `statusCheckRollup` JSON the watcher already fetches, no extra `gh` call; `""` when absent).
- Extend `PullRequest`: add `checks: tuple[CheckResult, ...] = ()`, `is_dependabot: bool = False`.
- `_fetch_pr_detail`: add `author` to the `gh pr view --json` field list; compute `is_dependabot` from
  `data["author"]["login"] == _DEPENDABOT_LOGIN` (missing/malformed author → `False`, never raises);
  build the `checks` tuple from the same rollup already fetched, reusing `_check_state` per entry
  (`name`/`conclusion`/`detailsUrl` read per the same `__typename` branch `_check_state` already
  switches on — `StatusContext` uses `context`/`state`/`targetUrl`, `CheckRun` uses
  `name`/`conclusion`/`detailsUrl`).
- Add `build_channel_payload(pr: PullRequest) -> dict[str, object]` — pure, JSON-serializable:
  `{"pr": pr.number, "head_sha": pr.head_sha, "head_ref": pr.head_ref, "mergeable": pr.mergeable,
  "checks": [{"name": c.name, "state": c.state, "conclusion": c.conclusion, "details_url":
  c.details_url} for c in pr.checks], "dependabot": pr.is_dependabot}`. This is the AC-3 proof
  surface — a test asserts two fixtures with different failing-check names/head SHAs/dependabot flags
  produce distinguishably different payload dicts, and a PR with an empty `checks` tuple and
  `ci="pending"` never reaches this function at all (no candidate → no trigger → no payload built,
  mirroring the "no-failure payload → no code change" AC-3 half at the code layer). Failing-log-excerpt
  enrichment (a further `gh api`/`gh run view` call per failing check) is a real follow-up, not built
  here — `details_url` gives the seat a concrete link instead.
- `classify_pr`: add `if pr.is_dependabot: return None` as the **first** check, before either the
  worker or master branch (item 3 above, revised scope) — a dependabot PR produces no trigger of either
  kind in this ticket.
- `Trigger`: add `mode: Literal["channel", "send_keys"]`, `channel_port: int | None`,
  `channel_payload: Mapping[str, object] | None`.
- `decide()`: for a worker candidate, after resolving `session`, resolve
  `stream = launcher.stream_for_tmux_session(session) if session else None`,
  `mode = launcher.topology_for(stream).mode if stream else "send_keys"`,
  `channel_port = launcher.topology_for(stream).channel_port if mode == "channel" else None`,
  `channel_payload = build_channel_payload(pr) if mode == "channel" else None`. For a master
  candidate: `mode="send_keys"`, `channel_port=None`, `channel_payload=None` (unchanged behavior).
- Add the HTTP delivery seam (mirrors `fetch_issue_labels`'s stdlib-`urllib` style, no new
  dependency):
  ```python
  ChannelOutcome = Literal["delivered", "unreachable"]

  def post_channel_event(
      port: int, secret: str, payload_json: str, *,
      opener: Callable[..., object] = urllib.request.urlopen, timeout_s: float = 5.0,
  ) -> ChannelOutcome: ...

  def load_channel_secret() -> str | None: ...  # AGENT_SESHAT_CHANNEL_SECRET env, else .env, mirrors load_linear_key
  ```
  `post_channel_event` POSTs to `http://127.0.0.1:{port}/` with header `X-Sender: {secret}`; any
  `URLError`/`TimeoutError`/`OSError`, or a non-2xx response, returns `"unreachable"` (never raises).
- `run_once`: add param `channel_poster: Callable[[int, str, str], ChannelOutcome] = post_channel_event`
  and `channel_secret: str | None = None` (resolved once by the caller/`main`, not per-trigger). For
  each worker trigger with `mode == "channel"`:
  - Ledger-write exactly as today (`record_pending` / `mark_send_started`) — creates the entry at the
    default `transport="send_keys"` (no new argument; see §1's revised design).
  - If `channel_secret is None`: log a warning (`channel_secret_missing`) once and fall straight to
    `send_to_session` (the pre-existing tmux path). No `mark_transport` call — the default is already
    correct.
  - Else call `channel_poster(trigger.channel_port, channel_secret, json.dumps(trigger.channel_payload))`.
    - `"delivered"` → `mark_transport(ledger, key, "channel")` (the *only* place this is ever called),
      then treat as `outcome = "sent"`. **`send_to_session`/`runner` is never called on this path** (the
      AC-1 proof: zero `tmux capture-pane`/`send-keys` calls for a channel-mode delivery that succeeds).
    - `"unreachable"` → log `channel_delivery_failed`, fall back to `send_to_session(trigger.session,
      trigger.command, runner, require_idle=True)` (the AC-5 fallback). No `mark_transport` call — the
      entry is already, correctly, `transport="send_keys"`.
  - `mark_sent`/`mark_consumed` bookkeeping unchanged from the existing pattern, keyed off whatever
    `outcome` resolved to (channel-delivered counts as `"sent"` for this bookkeeping).
  For a `mode == "send_keys"` worker trigger or the master trigger: **byte-for-byte the existing
  code path** — no branching change, no behavior change (this is the "not-yet-cutover seat still
  dispatches via send-keys" AC-5 first half, true by construction since nothing in this class of
  trigger touches the new branch at all).
- `main()`/`tick()`: resolve `channel_secret = load_channel_secret()` once per process start (mirrors
  `api_key = load_linear_key()`), thread it into `run_once`.

**Tests: `tests/scripts/test_gating_watcher.py`** (extend existing fixtures/fakes, don't restructure)
- `classify_pr`: a dependabot PR (`is_dependabot=True`) with `ci="failure"` → `None` (no worker
  candidate); the **same PR with `ci="success"` and `mergeable="MERGEABLE"` → also `None`** (no master
  candidate either — revised scope decision 3: dependabot PRs never produce a watcher trigger of any
  kind in this ticket, regression-guarded against a future accidental re-introduction of a dependabot
  master-ready path).
- `build_channel_payload`: two fixtures with different `head_sha`/failing-check-name/`is_dependabot`
  produce payload dicts that differ on exactly those fields, including `conclusion`/`details_url`
  (AC-3 code-layer proof).
- `decide()`: a worker trigger routed to a `mode="channel"` topology entry (monkeypatch
  `launcher._TOPOLOGY["build2"]` to `dataclasses.replace(..., mode="channel")` for the test) carries
  `mode="channel"`, the topology's `channel_port`, and a non-`None` `channel_payload`; the same trigger
  against the unmodified (`send_keys`) topology carries `mode="send_keys"`, `channel_port=None`,
  `channel_payload=None`.
- `run_once` — **AC-1 (scrape not consulted):** a `_RecordingRunner` + a fake `channel_poster` that
  always returns `"delivered"`; assert zero `capture-pane`/`send-keys` calls recorded against the
  channel-mode target, exactly one `channel_poster` call with the expected port/payload, and the
  ledger entry's `transport == "channel"` (set via the single `mark_transport` call on confirmed
  delivery).
- `run_once` — **AC-5 (fallback):** same setup but the fake `channel_poster` returns `"unreachable"`;
  assert `send_to_session`'s underlying `tmux send-keys` calls now appear in `_RecordingRunner.calls`
  for that target, and the ledger entry's `transport == "send_keys"` (the untouched default —
  `mark_transport` is never called on this path, proving the fallback can't leave a stale "channel"
  audit tag).
- `run_once` — **crash-recovery consistency (regression for codex's High findings):** an entry written
  by `record_pending` for a channel-mode trigger, left at `send_started_at=None` (simulating a crash
  before the send attempt), recovers via the **existing, unmodified** `reconcile()` → tmux retry path;
  assert the recovered entry's `transport` is `"send_keys"` (accurate — that is genuinely how it was
  delivered) and that `reconcile()` required no gating_watcher.py changes to do this correctly.
- `run_once` — **AC-5 (not-yet-cutover unaffected):** a `mode="send_keys"` topology (the real default)
  produces the exact same `_RecordingRunner.calls` sequence as before this ticket (a byte-for-byte
  regression check against the pre-FRE-872 test fixtures already in this file).
- `run_once` — **AC-4 (no double-fire):** for one event id, exactly one ledger entry, no separate
  `send_keys`-transport entry coexists with a `channel`-transport one for the same key (trivially true
  by construction since transport lives on the single per-event-id entry, but assert it directly so a
  future refactor that reintroduces a parallel key space is caught).
- `channel_secret is None` (missing config) → falls back to send-keys immediately, logs
  `channel_secret_missing`, never calls `channel_poster`.
- `post_channel_event`: fake `opener` returning a 200-like object → `"delivered"`; fake `opener`
  raising `URLError`/`TimeoutError`/returning a non-2xx status → `"unreachable"`, never raises out.

## 3. Fallback + boundary guard

Fallback is implemented as part of §2 above (`run_once`'s channel branch falls back to
`send_to_session` on `"unreachable"`; the ledger entry's `transport` stays at its untouched
`send_keys` default — there is nothing to correct). Nothing further to build for AC-5.

**Boundary guard (AC-6):**
- The runtime behavioral boundary ("commits/pushes to the session's own worker branch only") is
  governed by `webhook.mjs`'s existing MCP `instructions` string (FRE-871, already present — "act
  within THIS session only... Never push to, merge, approve, close, or deploy a branch/PR you do not
  own") plus the general worker-session boundary already owned by `.claude/skills/lifecycle-rules.md`
  § Session boundary (cited by the ADR as the normative source, not restated). **This ticket adds one
  doc line to `webhook.mjs`'s header comment** cross-referencing `lifecycle-rules.md` § Session
  boundary explicitly (currently it doesn't name the file), so the boundary language has one traceable
  source instead of being restated ad hoc.
- **Structural half (code-provable, this ticket):** the dependabot guard in `classify_pr` (item 3,
  §2, revised) — a dependabot-authored PR produces **no** `Trigger` of either kind, so the gateway
  structurally cannot hand any seat an instruction whose natural completion is "push to the dependabot
  branch." Proven by the `classify_pr` unit test above.
- **Live half (the actual git/GitHub/Linear audit AC-6's "Check" describes — session only pushes to
  its own branch, no merge/approve/close/deploy anywhere)** is a live-verification against a real
  channel-triggered turn, which requires a live cutover seat — **deferred to FRE-875**, exactly like
  AC-1/AC-3's live half (scope decision 4). Noted explicitly in the PR handoff so master doesn't read
  this ticket as claiming a false proof of AC-6's live audit.

**Channel-server hardening (folded in per codex Low finding):**

**File: `scripts/dispatch/channel/plugins/seshat-dispatch/server.mjs`**

The ADR names this exact system as a prompt-injection-adjacent ingress ("ungated inbound channel = a
prompt-injection vector"); this PR is what makes the gateway actually start POSTing to it, so it is the
right point to close the two gaps codex found in `createServer` while the threat model is already
front-of-mind:
- Reject any request whose `method !== 'POST'` or `url !== '/'` with `404`/`405` **before** the
  X-Sender check (cheap, unauthenticated rejection — no reason to even evaluate the secret for a
  malformed request shape).
- Cap the buffered body size (`64 * 1024` bytes — comfortably above the JSON payload this ticket ever
  sends, which is a handful of checks × a few hundred bytes each) — destroy the socket and respond
  `413` if exceeded, rather than buffering an unbounded `body += chunk` from an authorized-but-hostile
  sender.

**Tests: `scripts/dispatch/channel/plugins/seshat-dispatch/server.test.mjs`** (extend the existing
`node --test` file, don't restructure) — non-POST rejected before secret check; non-`/` path rejected;
an oversized authorized body is rejected with `413` and never reaches `onEvent`.

**Tests:** covered by §2's `classify_pr` dependabot tests, plus a small regression test asserting the
`lifecycle-rules.md` cross-reference string is present in `webhook.mjs` (grep-style, mirrors this
repo's existing `test_dispatch_skill_contracts.py` pattern of asserting doc cross-references exist).

## Step 6 — Documentation

- `webhook.mjs` header comment: add the `lifecycle-rules.md` § Session boundary cross-reference (see
  above).
- `ADR-0116`: no changes needed — this PR fulfills (part of) its Implementation Notes / AC list; the
  ADR itself is not amended (per its own note, only the seam ticket FRE-875 closes it out).
- Brief note at the top of `gating_watcher.py`'s module docstring: one or two sentences on the new
  channel-mode delivery branch + dependabot boundary guard, matching the file's existing
  documentation density (it already has substantial prose per trigger-kind).

## Test commands

```bash
make test-file FILE=tests/scripts/test_trigger_ledger.py
make test-file FILE=tests/scripts/test_launcher.py
make test-file FILE=tests/scripts/test_gating_watcher.py
(cd scripts/dispatch/channel/plugins/seshat-dispatch && node --test)
make test                 # full module + repo suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance-criteria mapping (for the Step-9 ticket comment)

**This PR does not claim ADR AC-1/AC-3/AC-6 are delivered end-to-end.** It delivers the code-provable
delivery/fallback/boundary mechanism plus unit-test proof of each; the live halves (a real seat reacting
to a real push; a real git/GitHub/Linear audit of that turn's side effects) require an actual cutover
seat, which no seat is in this PR (`topology.mode` stays `send_keys` for build1/build2/adr — no live
dispatch behavior changes). Those live halves are proven at FRE-875 (the seam, ask-first deploy) —
mirroring exactly the split FRE-871 took for AC-2 (ships the artifact + tests; master/the cutover ticket
runs the live proof).

| AC | Code-provable proof in this PR | Live proof (deferred to FRE-875) |
|----|--------------------------------|------------------------|
| AC-4 no double-fire | `run_once` test: zero send-keys calls to channel-mode target on success; ledger transport correct; crash-recovery regression test | — (fully provable here) |
| AC-1 scrape not consulted | `run_once` test: `_RecordingRunner` records zero `capture-pane` for the channel-mode target on a successful delivery | Real seat reacting to a real POST |
| AC-3 payload drives the action | `build_channel_payload` test: distinct fixtures → distinguishably different payload dicts (incl. `conclusion`/`details_url`); no-candidate PR → no payload built at all | Real seat's response content varying by payload |
| AC-5 fallback intact | `run_once` tests: not-yet-cutover seat unaffected; channel-down falls back to send-keys, ledger stays accurate | — (fully provable here) |
| AC-6 boundary | `classify_pr` dependabot guard test (structural, both trigger kinds); `server.mjs` hardening tests | git/GitHub/Linear audit of a real channel-triggered turn |
