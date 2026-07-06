# FRE-823 — Event-driven gating watcher (send-keys triggers)

Backing ticket: FRE-823 (Approved, Tier-1:Opus, stream:build2). Follow-up #2 to
FRE-822 (polling-cron cost incident). Relates to ADR-0110 (external dispatch
orchestrator — same `scripts/dispatch/` home, same systemd pattern).

## Goal

A watcher that runs **outside** every CC session (zero LLM context), polls
`gh`/Linear/`tmux` only, and pokes the **persistent** master/worker sessions via
`tmux send-keys` when a gating event needs a human-in-loop turn — replacing the
removed polling `/loop` crons without re-introducing any in-session polling.

## Design decisions (settled by the ticket; noted where I pin an approximation)

1. **Two triggers, natural mutual exclusion per PR per tick:**
   - *Master ← new PR:* `ci == success` AND `mergeable != CONFLICTING` AND no
     unacked bounce → `/master <PR#>` to `cc-master`.
   - *Worker ← bounce/red-CI:* unacked `## Master gate — BOUNCE` **or**
     (`ci == failure` AND no SHA-keyed ack) → `/prime-worker` to the owning
     `cc-<stream>` session. Bounce takes precedence over CI-red (mirrors
     prime-worker Step 3.2). A problem PR is never master-ready, so the two
     never fire together.

2. **Dedup — one timestamped suppress store, TTL by trigger kind** (unified after
   codex plan-review; `send-keys` is *at-least-once* delivery, so a permanent
   "sent" set both suppresses a needed retry after a byte-accepted-but-unprocessed
   send and leaves a pre-ack duplicate window). State is
   `{"sent": {"<kind>:<pr>:<headSha>": <epoch>}}`, written atomically
   (temp + `os.replace`, mirroring `orchestrator.save_state`), recorded **only on
   a successful send**, and pruned each tick (drop entries past the max TTL). A
   send is suppressed iff `now - last_sent < ttl(kind)`:
   - *Master* (`ttl ≈ 6 h`, a self-heal re-arm): a clean PR has no ack channel, so
     AC-1 needs durable state — the timestamp keyed on `master:<pr>:<sha>` is that
     ledger. A busy/absent skip records nothing → retries next tick. A bounce+push
     mints a new head SHA → new key → re-sent when it re-greens. The long TTL means
     a genuinely-stuck send (bytes accepted, master never processed, PR still
     green+open+unbounced 6 h later) self-heals with one re-nudge instead of being
     suppressed forever (codex top-risk 3). A bounced/merged PR is never
     master-ready / not open, so the re-arm never double-pokes a handled PR.
   - *Worker* (`ttl ≈ 15 min`, a transient in-flight lease): the **ack markers
     remain the primary idempotency key** (ticket: "dedup keys on the same ack
     markers prime-worker already uses") — prime-worker acks *first*
     (`Ack: addressing master bounce` / `Ack: addressing red CI at <short-sha>`)
     before touching code, so once the ack lands the live signal is off. The short
     TTL is only a **lease over the send→pre-ack window** (codex top-risk 4): it
     stops a second `/prime-worker` before the ack comment is visible even if the
     busy-skip is fooled, and re-arms if the ack never appears.

3. **`mergeable` gate:** block only on `CONFLICTING`. GitHub returns `UNKNOWN`
   until it lazily computes mergeability; treating `UNKNOWN` as sendable avoids a
   never-fires stall, and master assesses conflicts at its own gate anyway.

4. **CI status from `statusCheckRollup`** (approximation, documented): aggregate
   over the whole rollup — `failure` if any check has a failing conclusion/state,
   else `pending` if any is incomplete, else `success`; an empty rollup is
   `pending` (checks not yet registered — neither green nor red). "Required" is
   approximated by the full rollup: an over-eager CI-red nudge is harmless because
   prime-worker re-validates ("a required check FAILED, not merely pending") and
   stays silent if nothing is actionable; the dedup prevents thrash.

5. **Session mapping:** master → fixed `cc-master`. Worker → PR branch
   `fre-<id>-…` → ticket → its `stream:*` label → `topology_for(stream)
   .tmux_session` (`cc-build`/`cc-build2`/`cc-adrs`). No stream label resolvable
   → skip + log (`unroutable`), never guess.

6. **Injection safety (fail-safe = skip; advisory, not a proof of readiness):**
   send only when `tmux has-session` succeeds AND `session_is_idle(capture-pane)`.
   `session_is_idle` is a pure heuristic over the pane tail: idle iff a Claude
   input-prompt marker is present AND **no** busy marker — where busy markers
   include an active spinner / `esc to interrupt` **and a pending
   permission/decision/question prompt** (so master waiting on a deploy-auth
   decision is treated as busy and never interrupted — codex idle-example).
   Ambiguous → busy → skip. The consistent per-PR `gh pr view` snapshot (SHA +
   rollup + comments in one read) keeps a tick internally consistent; the loop
   re-fetches every interval, and head-SHA is in every dedup key, so a mid-tick
   SHA change simply re-evaluates next tick. Documented as best-effort: the
   watcher only *actuates* the trigger — master's and worker's own gates remain
   authoritative (they re-read live state). Residual risk (an unsent human draft
   in the input box) is inherent to the ticket's capture-pane approach and noted
   in the runbook.

7. **Kill-switch / liveness parity:** shares the orchestrator's kill switch
   (`telemetry/dispatch.disabled`) — one flag halts both. The watcher pokes local
   tmux, so it does not depend on RC reachability; the shared kill switch is the
   halt. Documented in the runbook.

## Files

- **`scripts/dispatch/gating_watcher.py`** (new) — the watcher.
  - Pure: `parse_ticket_from_branch`, `ci_status`, `latest_bounce_unacked`,
    `has_ci_red_ack`, `session_is_idle`, `session_for_labels`, `decide_pr`,
    `decide`.
  - Data: `PullRequest`, `Trigger` (frozen dataclasses; `Literal` kinds).
  - IO seam (injected `CommandRunner`): `fetch_open_prs` (`gh pr list` for the
    number set, then **one consistent `gh pr view <n> --json
    number,headRefName,headRefOid,mergeable,statusCheckRollup,comments` per PR** so
    SHA/CI/comments come from a single read), `fetch_issue_labels` (urllib
    GraphQL, mirrors `next_resolver.fetch_board`), `send_to_session`,
    `load_state`/`save_state` (atomic), `run_once`, `main` (`--once`/`--loop`/
    `--execute`/`--interval`/`--state-file`/`--kill-switch-file`/`--preflight`).
    Single-instance by construction — one systemd `Type=simple` unit, serial
    `--loop` (matches the orchestrator; run only one).
  - structlog with `trace_id` on every event. No Cypher / bus.publish (no
    ADR-0074 MERGE surface). Imports **no** LLM client (AC-4).
- **`infrastructure/systemd/seshat-gating-watcher.service`** (new) — mirrors the
  orchestrator unit: `WorkingDirectory=/opt/seshat`, `ExecStartPre … --preflight`,
  `ExecStart … --loop --execute`, `Restart=always`.
- **`docs/runbooks/dispatch-orchestrator.md`** (edit) — add a "Gating watcher"
  section: two triggers, session mapping, injection safety, shared kill switch,
  install/enable line.
- **`tests/scripts/test_gating_watcher.py`** (new) — AC-1…AC-6 + pure-fn unit
  tests.
- **`tests/scripts/test_dispatch_systemd_units.py`** (edit) — watcher unit runs
  `--loop` and `Restart=always`, preflights.
- **`tests/scripts/test_dispatch_runbook.py`** (edit) — runbook documents the
  watcher triggers + shared kill switch.

## TDD steps (each: write failing test → confirm red → implement → green)

1. Pure fns: `parse_ticket_from_branch`, `ci_status` (success/failure/pending/
   empty), `session_is_idle` (idle vs busy vs shell/blank), `session_for_labels`.
2. `latest_bounce_unacked` — bounce with no ack → True; ack after latest bounce →
   False; ack before a newer bounce → True; no bounce → False.
3. `has_ci_red_ack` — SHA-prefix match true/false.
4. `decide_pr` / `decide`:
   - **AC-1**: green+mergeable+no-bounce → master trigger `/master <n>` to
     `cc-master`; same `(pr,sha)` in ledger → None; new sha → trigger.
   - **AC-2**: unacked bounce → worker `/prime-worker`; acked → None.
   - **AC-3**: ci failure + no sha-ack → worker; sha-acked → None; conflicting/
     pending precedence checks.
5. `send_to_session` (mocked runner) — **AC-5**: absent → skip+log, no send-keys;
   busy pane → skip+log, no send-keys; idle → `tmux send-keys` issued.
6. `run_once` full tick (recording runner + injected board/label fetchers):
   - **AC-1 dedup** across two ticks (ledger persists, second tick no re-send).
   - **AC-6**: assert no argv invokes `claude` (never `claude -p`); the only
     actuation is `tmux send-keys`.
   - ledger recorded only on successful send; pruned to open PRs.
7. **AC-4**: import-purity — `ast`-parse the module source, assert no import of
   any LLM client (`personal_agent.llm_client`, `litellm`, `anthropic`, `openai`,
   `personal_agent.orchestrator`).
8. systemd unit + runbook structural tests.

## Quality gates

`make test-file FILE=tests/scripts/test_gating_watcher.py` → then `make test` →
`make mypy` → `make ruff-check` + `make ruff-format` → `pre-commit run
--all-files`.

## Acceptance-criteria proof map (for master's gate)

| AC | Proof |
|----|-------|
| AC-1 master trigger + dedup | `test_decide_master_ready_*`, `test_run_once_master_dedup_across_ticks` |
| AC-2 worker bounce + ack dedup | `test_decide_worker_bounce_*`, `test_latest_bounce_unacked_*` |
| AC-3 worker CI-red + SHA dedup | `test_decide_worker_ci_red_*`, `test_has_ci_red_ack_*` |
| AC-4 no LLM context | `test_module_imports_no_llm_client` |
| AC-5 injection safety | `test_send_to_session_absent/busy/idle` |
| AC-6 continuity (no `claude -p`) | `test_run_once_uses_only_tmux_and_gh` |

## Out of scope

RC programmatic completion detection (ADR-0110 defers it); precise
branch-protection required-check resolution (approximated by the rollup);
master's/worker's gate logic (unchanged — this only actuates the trigger).
