# FRE-1684 — dev_jobs, the lease-fenced supervisor, the launcher and OpenCodeAdapter

ADR-0159 D2.2–D2.4, D3.1, D3.2, D3.4, D3.5, D9.1–D9.7. Tier: Complex (schema, `src/` logic, a
security boundary). One PR.

## Scope

- T3 owns the job table, the supervisor (lease, fencing, recovery), the launcher, the adapter and the
  30-minute limit. It deletes the three old adapters.
- T4 (FRE-1685) owns the clone into the workspace and the bundle handoff. T5 (FRE-1686) owns the
  publish check and the delivery. T6 (FRE-1687) creates jobs from a tool. T3 calls them through one
  protocol, `JobResultPipeline`, and ships a default that has no result.
- Fold-in from master (first): the broker health check takes 30 s.

## Facts measured before the plan (2026-11-07)

1. **Broker health.** `python -m personal_agent.dev_broker --health` imports
   `dev_broker.server` → `dev_broker.config` → `cost_gate` → `config` → `llm_client` →
   `telemetry.metrics` → `litellm`. In the live container the import costs 11.4 s of CPU and
   25.5 s of wall time (`cpus: 0.5`). The health route itself answers at once. No exporter flush
   is involved.
2. **OpenCode 2.2.5 API** (read from a no-network container of `seshat-dev-agent:0.1`):
   - Basic auth, user `opencode`, password `OPENCODE_SERVER_PASSWORD`. No password → 401.
   - `POST /api/session` `{title, model: {id, providerID}}` → `{data: {id: "ses_…"}}`.
   - `POST /api/session/{id}/prompt` `{text}`. `POST /api/session/{id}/interrupt`.
   - `GET /api/session/active` → `{data: {"ses_…": {type: "running"}}}`.
   - `GET /api/event` (SSE, `data: {id, type, data}`). Events of a session carry
     `data.sessionID`. End events: `session.execution.succeeded`, `.failed`, `.interrupted`.
     Progress: `session.step.started/ended`, `session.tool.called/success/failed`,
     `session.text.ended`.
3. **Config file.** `OPENCODE_CONFIG` takes `provider.<id>` with `npm: @ai-sdk/openai-compatible`,
   `options.baseURL`, `options.apiKey: "{env:SESHAT_JOB_TOKEN}"` and `models`. The provider
   package is bundled, so no npm download happens. `permissions: [{action, resource, effect}]`
   rules go after the built-in rules of every agent, and the last match wins. The built-in rules
   contain `read *.env → ask`, so the file must override every `ask`.
4. **Full cycle**, with a fake model server inside the container: the session ran one `shell`
   tool call, wrote a file to `/workspace`, sent `Authorization: Bearer <token>` and
   `model: deepseek-v4.1-flash-scaleway`, and ended with `session.execution.succeeded`.
   OpenCode state lives in `$HOME/.local/share/opencode`, which is a tmpfs.
5. Actions in the binary: `read edit shell grep glob execute external_directory webfetch
   websearch question subagent skill browser doom_loop` and two MCP actions.

## Design

### Schema — `docker/postgres/migrations/0039_dev_jobs.sql` + `init.sql`

`dev_jobs` (D9.1, D9.2): `id UUID PK`, `user_id UUID`, `session_id UUID`, `trace_id UUID`,
`approval_request_id TEXT UNIQUE NOT NULL`, `mode` (`build`|`fix`), `task_text`,
`source_artifact_id UUID` (required for `fix`), `repo`, `base_commit`, `result_commit`,
`result_artifact_id UUID`, `model_id TEXT NOT NULL`, `state` (the 11 states), `spend_usd`,
`check_result JSONB`, `lease_owner TEXT`, `lease_epoch BIGINT NOT NULL DEFAULT 0`,
`lease_expires_at TIMESTAMPTZ`, `created_at`, `started_at` (agent container start, D9.7),
`ended_at`, `failure_reason`.

- D9.3: `CREATE UNIQUE INDEX dev_jobs_one_active ON dev_jobs ((true)) WHERE state IN
  ('queued','running','checking','delivering')`.
- `dev_job_actions` — the fenced-action log: `id BIGSERIAL`, `job_id`, `lease_epoch`,
  `lease_owner`, `action`, `created_at`. A row is written in the same transaction as the fenced
  renewal, so a refused action writes no row.
- `seshat_app` gets its grants from the existing `ALTER DEFAULT PRIVILEGES FOR ROLE agent`.
  `seshat_dev_broker` gets none (D4.4). `test_role_integration.py` already asserts the refusal on
  `dev_jobs` once the table exists.

### `personal_agent/dev_agent/jobs.py` — the record and the lease (D9.4, D9.5)

All time values come from an injected `Clock` (`dev_broker.clock`), passed to SQL as `$now`, so a
fake clock drives the real Postgres in tests.

- `JobState` (`StrEnum`), `END_STATES`, `ACTIVE_STATES`. `DevJob` (frozen). `Lease` (frozen:
  `job_id`, `owner`, `epoch`).
- `JobStore(pool)`:
  - `create_job(...)` → `DevJob`. A second active job raises `UniqueViolationError` from the
    index (T6 calls this).
  - `claim_new(owner, now, lease_seconds)` → `(DevJob, Lease) | None`: a `queued` job with
    `lease_epoch = 0`, `FOR UPDATE SKIP LOCKED`, sets owner, `epoch + 1`, expiry.
  - `take_expired(owner, now, lease_seconds)` → the same, for a job that is not at an end state,
    has `lease_epoch > 0` and `lease_expires_at < now`.
  - `renew(lease, now, lease_seconds)` → `RenewResult(token_status, stop_reason) | None`. One
    `UPDATE … WHERE id AND lease_epoch AND lease_owner AND state NOT IN END_STATES`, joined to
    `dev_job_tokens` for the stop signal of D5.8.
  - `fenced(lease, action, now, lease_seconds)` → `bool`: renew and insert the action row in one
    transaction. `False` means the lease is lost. The caller then does not act.
  - `transition(lease, to_state, now, **fields)` → `bool`: requires the epoch. An end state sets
    `ended_at`, clears `lease_expires_at` and copies `committed_usd` from `dev_job_spend` into
    `spend_usd`.
  - `job_state(job_id)`, `end_state_job_ids(job_ids)` for the orphan sweep.

### `personal_agent/dev_agent/launcher.py` — one interface, one Docker implementation (D3)

- `AgentLauncher` protocol: `start(job_id, *, job_token, server_password) -> None`,
  `remove(job_id) -> None` (every container and volume with the job's label, then verify that none
  remains), `labelled_job_ids() -> set[UUID]`.
- `DockerLauncher(profiles, network_name, toolkit_volume)`:
  - refuses to start unless `docker volume inspect <toolkit_volume>` succeeds. It never creates the
    toolkit volume. An empty volume would hide a missing toolkit.
  - `docker volume create --label seshat.dev_job=<id> seshat-dev-ws-<id>` (fresh per job).
  - `build_create_args(profiles.agent, name=agent_container_name(id), …)` plus the command
    `opencode serve --hostname 0.0.0.0 --port 4096`. Labels: `seshat.dev_job=<id>`,
    `seshat.dev_job.kind=agent`.
  - The two secrets reach `docker create` through the subprocess environment only
    (`-e NAME` without a value), never on the command line.
  - `asyncio.create_subprocess_exec`, never a shell (the `run_python` pattern).

### `personal_agent/delegation/adapters/opencode.py` — `OpenCodeAdapter` (D2.2, D2.3)

- Implements `DelegationExecutorProtocol`. Built per job with an `httpx.AsyncClient` whose base URL
  is `<dev_broker_control_url>/relay/<job_id>` and whose headers are the control secret and the
  Basic auth of the job's server password; the model ref `{id: <catalog key>, providerID:
  "seshat"}`; and an `on_progress(step: str)` callback.
- `available()`: the control secret is set (no I/O).
- `delegate(package, timeout, trace_ctx)`: wait for the server (poll `GET /api/session/active`,
  bounded); open `GET /api/event` first; create the session; send the prompt; read the events of
  that session. Return on an end event. On timeout, send `interrupt`. On a dropped stream,
  reconnect (bounded) and read `/api/session/active`: a session that is not active ended while the
  stream was down. Never raises: every error becomes a failed `DelegationOutcome`.

### The permission file — `docker/dev-agent/opencode.json` (D2.4)

Copied into the agent image at `/etc/opencode/opencode.json`, on the read-only root. Provider
`seshat`: `baseURL http://dev-broker:8700/v1`, `apiKey {env:SESHAT_JOB_TOKEN}`, the models of
`broker.yaml`. Rules, in order: `* * deny` (no action is unmatched, so nothing asks), then allow
`read edit shell grep glob`, then deny `external_directory webfetch websearch question subagent`.
A test pins: no `ask` effect, the catch-all first, the denies present, the models equal to
`broker.yaml`, the port equal to `model_port`.

### `personal_agent/dev_agent/supervisor.py` — `JobSupervisor` (D9.4–D9.7)

Policy (ADR numbers): lease 120 s, renew every 30 s, stop after 60 s without renewal, scan every
30 s, time limit = `profiles.agent.wall_clock_seconds` (1800 s) from `started_at`.

- `run_forever()`: each scan → `recover()` → orphan sweep → if this supervisor runs no job,
  `claim_new()` and start the job task.
- **Job task**, with a lease keeper beside it. The keeper renews every 30 s. A renewal that
  returns a stopped token sets the stop reason. No successful renewal for 60 s → cancel the work.
  A lost lease → cancel the work, and the stale task does nothing more.
  1. fenced `issue_token` → `issue_job_token` (expiry: limit + 5 min).
  2. `transition(running)`, fenced `start_container` → `launcher.start`, store `started_at`.
  3. `adapter.delegate(timeout = time left)`.
  4. fenced `revoke_token` + fenced `stop_agent` (D8.8.1), then `pipeline.handoff()` → result
     commit or `None`.
  5. A commit → `checking` → `pipeline.check()` → pass → `delivering` → `pipeline.deliver()` →
     `succeeded`. A fail → `failed_check`.
  6. No commit → `timed_out` when the limit passed, `cap_reached`/`cost_unknown` from the token,
     else `failed` (`no_result`). An exception → `failed` (`supervisor_error`).
  7. At every end state, first the fenced cleanup (revoke the token, `launcher.remove`), then the
     end transition. So the job leaves the one-job slot only after the cleanup.
- **Recovery** (D9.6): `take_expired()` in a loop. `delivering` → a resume task that calls
  `pipeline.deliver()`. Any other state → fenced cleanup, then `failed`/`supervisor_restart`.
- **Orphan sweep**: a container or volume labelled with a job that is at an end state, or that has
  no row, is removed. That undoes a late keyed action of a stale supervisor (D9.5).
- `DefaultResultPipeline`: `handoff()` returns `None` (T4 replaces it), `check()` and `deliver()`
  raise. So in T3 a job ends `timed_out` or `failed`/`no_result`.

### Gateway wiring

- Settings: `dev_agent_supervisor_enabled` (default `False`), `dev_agent_network`
  (`seshat_dev-agent`), `dev_agent_toolkit_volume` (`seshat_dev_agent_toolkit`),
  `dev_broker_control_url` (`http://dev-broker:8701`).
- Lifespan: when enabled, build the supervisor on its own small asyncpg pool and start
  `run_forever()` with `context=contextvars.Context()`. Cancel it at shutdown. Default off, so the
  gateway deploys before migration 0039 without errors.

### Broker health fold-in

- New `dev_broker/addresses.py`: `ListenAddresses`, `interface_addresses`,
  `select_listen_addresses` move here unchanged. `server.py` imports them from it.
- New `dev_broker/health.py`: reads `control_port` and `control_subnet` from `broker.yaml` with
  a small model, then calls `/healthz`. It imports no cost gate and no `litellm`.
- `__main__.py` sends `--health` to `health.main` before it imports `server`.

### Deletion (D2.2)

`delegation/adapters/claude_code.py`, `codex.py`, `generic_mcp.py`,
`tests/personal_agent/delegation/test_claude_code.py`, `test_adapters.py`. Update the
`delegation_enabled` description and the protocol docstring.

## Steps

1. Health fold-in. Test first: `python -X importtime -m personal_agent.dev_broker --health` loads
   no `litellm` and no `personal_agent.cost_gate` module. → `make test-file
   FILE=tests/personal_agent/dev_broker/test_health.py`.
2. Migration 0039 + `init.sql`. Apply to the test stack. Integration test: a second active job is
   refused by the index (AC-3).
3. `jobs.py` + integration tests on the test stack with a fake clock: claim, renew, epoch fencing,
   expiry, the action log.
4. `launcher.py`. Unit tests of the arguments with a fake `docker` runner (no secret on the command
   line, labels, toolkit check).
5. `opencode.json` + `Dockerfile.browser` copy + its pin test.
6. `opencode.py` adapter. Unit tests with `httpx.MockTransport` that replays the events recorded in
   fact 4.
7. `supervisor.py`. Unit tests with a fake clock, a fake launcher and the real store (test stack):
   - restart just after a renewal: recovery ends the job `failed`/`supervisor_restart` after expiry,
     cleanup done, token `revoked` (AC-1);
   - a supervisor paused past its lease while recovery takes the job: after resume, no launcher
     call and no new row in `dev_jobs` or `dev_job_actions` (AC-2);
   - a job past 1800 s reaches `handoff()` and ends `timed_out`, launcher `remove` called (AC-4);
   - `delivering` resumes; token stop ends `cap_reached`; 60 s without renewal stops the work.
8. Live tests (`integration`, skipped without Docker or the image), with the real `DockerLauncher`
   on a throwaway internal network and toolkit volume, and short policy values:
   - a supervisor in a subprocess is killed with SIGKILL just after a renewal. A new supervisor
     recovers: `failed`/`supervisor_restart`, `docker ps -a` and `docker volume ls` show nothing for
     the job, token `revoked` (AC-1);
   - the paused-supervisor case with `docker ps` (AC-2);
   - a job past its limit ends `timed_out` with no container (AC-4);
   - two consecutive jobs: job 1 writes markers in every writable path and creates an OpenCode
     session. Job 2's probe finds no marker and no session (AC-5).
9. Delete the adapters. `ast-grep` finds no import (AC-6).
10. Settings + lifespan wiring + README notes + runbook.
11. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
    `pre-commit run --all-files`, the integration tests above.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| 1 Restart after a renewal → `failed`/`supervisor_restart`, no container or volume, token `revoked` | supervisor unit test (fake clock, 120/30/60) + live SIGKILL test |
| 2 Paused supervisor does nothing after resume | unit test (action log + launcher calls) + live test (`docker ps`) |
| 3 Second active job refused by the index | integration test on the test stack |
| 4 Past 30 min → handoff → `timed_out`, no container | unit test (fake clock, 1800 s) + live test (short limit) |
| 5 Second job finds nothing of the first | live test (files + OpenCode sessions) |
| 6 No import of the deleted adapters | `ast-grep` output |
| Fold-in: health check inside its timeout | importtime test + `docker inspect` healthy after deploy |

## Revisions after the codex plan review (2026-11-07)

These replace the matching parts of the design above.

1. **The fence holds the job row's lock through the action.** `fenced(lease, action, operation)`
   opens a transaction, locks the row (`FOR UPDATE`) only if the epoch, the owner and an unexpired
   lease match, extends the lease, inserts the action row, runs the action, marks the row `done` or
   `error`, and commits. `take_expired` uses `SKIP LOCKED`, so recovery cannot take a job while one
   of its actions runs. A stale supervisor either finds the lease gone (no action, no row), or it
   holds the lock and its action ends before the takeover, so the cleanup after the takeover
   removes what it did.
2. **The token insert runs in the fenced transaction** (`issue_job_token` takes a connection).
3. **Renew, fence and every transition require `lease_expires_at > clock_timestamp()`.** After
   expiry, only `take_expired` can make a new epoch.
4. **Postgres time is the authority.** Lease expiry, `started_at` and the 30-minute limit use
   `clock_timestamp()`. SQL takes durations, not instants. The 60 s watchdog uses an injected
   monotonic clock. Tests seed past instants in the row ("a seeded job that runs past 30 minutes",
   "after its lease expires") and drive the watchdog with a manual clock at the real 30/60 values.
5. **`started_at` is set in the fenced transaction of `start_container`**, before Docker starts.
6. **The end state and its cleanup are one fenced action**: revoke the token, remove and verify,
   then the end transition, in one transaction. A failed cleanup leaves the job active. Its lease
   expires and recovery tries again. So the slot frees only after a verified cleanup.
7. **`JobResultPipeline` receives a `Fence`**: `fence.run(action, operation)` is the only way for
   T4 and T5 to do an external action.
8. **Delivery recovery**: the resumed job gets a lease keeper, calls `pipeline.deliver(job, fence)`
   (idempotent, D9.8), then the fenced cleanup and `succeeded`. An exception ends it `failed`
   (`delivery_failed`). A crash leaves it `delivering`, and the next recovery resumes it again.
9. **Tests add barriers** after a fence and before the action, and before a fence. They assert the
   launcher call history, the token row and the action log, and inject cleanup failures.

## Known gaps (stated, not hidden)

- The live tests drive the job through a scripted adapter, not through the broker relay, because a
  test broker needs a production-shaped stack. The adapter's HTTP side is tested against events
  recorded from OpenCode 2.2.5.
- The toolkit volume is not filled by this ticket. A follow-up ticket fills it from the hosted
  `/lib/` with a digest check (D3.3). T5 needs it as well.

## Deploy (master)

1. `make dev-agent-build` (the permission file is in the agent image).
2. Apply `0039_dev_jobs.sql` as `agent` (`AGENT_DATABASE_ADMIN_URL`).
3. Rebuild `seshat-gateway` and `seshat-dev-broker`. Supervisor stays off
   (`AGENT_DEV_AGENT_SUPERVISOR_ENABLED` unset).
4. Verify: `docker inspect --format '{{.State.Health.Status}}' seshat-seshat-dev-broker-1` →
   `healthy`.
