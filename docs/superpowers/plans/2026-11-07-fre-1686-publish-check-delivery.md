# FRE-1686 — the publish check and idempotent delivery (ADR-0159 D7.3–D7.7, D9.8–D9.10)

Ticket: FRE-1686. ADR: `docs/architecture_decisions/ADR-0159-development-agent-opencode-sandbox.md`.
Tier: Complex (security boundary, artifact write path). Codex plan review: required.

## Scope

- `GitHubRepoPipeline.check`: export `dist/index.html` and `tests/` at the result commit from a clean mirror, run the check container, store the result on the job, return pass or fail.
- `GitHubRepoPipeline.deliver`: preallocate the result artifact id, publish the page bytes from the result commit through `artifact_write_executor` with that id, then push a session event.
- `artifact_write_executor`: a new internal argument `_artifact_id`. The R2 key derives from it. The row insert has conflict handling on the id.
- The check image gets a Seshat-owned check script and `@playwright/test`.

## Design

### The check runs as two container runs

The check container runs twice, with the same profile (`check` in `container_profiles.yaml`) and the same inputs:

1. **`page` run** — Seshat's script only. Steps **load** and **generic interaction**. No model-written Node code runs in this container. Only the page's own JavaScript runs, inside Chromium.
2. **`tests` run** — the repo's Playwright tests. Step **repo tests**.

Reason: the repo tests are model code that runs as the same Unix user as the check script. In one container, a test can forge the result line of the load and interaction steps (through `/proc/<ppid>/fd/1`). Two runs keep the Seshat-owned steps out of reach of model code. The repo tests can still forge their own result, but that is equal to a trivial test, which the model can always write.

The two runs share one budget: `check.wall_clock_seconds` (300 s). The page run gets at most 120 s. The tests run gets the rest.

### Inputs

- The gateway builds one tar in Python from its mirror at the result commit: `dist/index.html` (the bytes of `GitMirror.read_page`, the same bytes that delivery publishes) and every blob under `tests/` (`ls-tree -r` + `cat-file blob`). It does not use `git archive`, because `.gitattributes` in the tree (`export-subst`, `export-ignore`) can change archive bytes. Caps: 500 files, 20 MB.
- Only `dist/index.html` is served in the check. Production publishes only that file, so another file in `dist/` must not make the check pass.
- The launcher creates two volumes (`seshat-dev-check-dist-<job>`, `seshat-dev-check-tests-<job>`) with the job and scope labels. A helper container that never starts (network `none`, both volumes read-write, job labels) receives the tar with `docker cp - helper:/input`. The helper is removed. The check container mounts both volumes read-only (the profile's `mounts`).
- The toolkit volume is checked against the pins (`check_toolkit_contents`) before the runs, as the agent start does.
- The check container is created from the **image id** (`docker image inspect --format {{.Id}}`), not the tag, so the recorded digest is the image that ran.

### The check script (`docker/dev-agent/publish-check.mjs`, in the check image at `/opt/check/`)

Common: copy `/input/dist/index.html` to a temp root under `/tmp`, link `/srv/artifact-lib` as `<root>/lib`, start the CSP server in-process (`startCspServer` from `/opt/csp/csp-server.ts`), with the production manifest `/opt/csp/csp-manifest.json`. Print one result line `SESHAT_CHECK_RESULT <json>` at the end. Messages: at most 50, each at most 500 characters.

`page` mode:

- One Chromium context. An init script registers a capture listener for `securitypolicyviolation` that calls a binding (`exposeBinding`), so page code cannot clear the record. Console errors (`console` type `error`) and page errors (`pageerror`) are collected. The one known benign console error `Unrecognized Content-Security-Policy directive 'webrtc'` is ignored (FRE-1682 README note). Dialogs are dismissed.
- **load**: `goto` with `waitUntil: 'load'` (30 s), then 1 s settle. Pass = zero console errors, zero page errors, zero CSP violations.
- **interaction**: the controls are the elements that match `button, input, select, textarea, [role=button]` at the start of the step, in document order, at most 200. Skip a control that is not visible or not enabled. Actions:
  - button, `[role=button]`, input `button|submit|reset|image`, checkbox, radio: `click` (2 s timeout);
  - select: `selectOption` of the first enabled option whose index differs from the current one;
  - range: set the value to `max` (or `min` when it is at `max`), dispatch `input` and `change`;
  - number: `fill('7')`; date: `fill('2026-01-15')`; color: `fill('#336699')`; file: skip; other text-like inputs and textarea: `fill('Seshat')`.
  - 100 ms after each action, 500 ms at the end.
  - An action that times out is a message, not a fail. A navigation away from the page is a fail.
  - Pass = zero new console errors, zero new page errors, zero new CSP violations after the load.
- Also prints `csp_manifest_sha256` (the SHA-256 of the manifest file that the server used).

`tests` mode:

- Copy `/input/tests` to `/tmp/work/tests`, write `/tmp/work/package.json` `{"type":"module"}` (as the template's `package.json`), link `/tmp/work/node_modules` to `/opt/check/node_modules`.
- Run `node /opt/check/node_modules/@playwright/test/cli.js test --config /opt/check/repo-tests.config.mjs` with `PAGE_URL` set. The config is Seshat's: `testDir` `/tmp/work/tests`, 1 worker, no retries, 30 s per test, a global timeout, JSON report to `/tmp/work/report.json`. The repo's own `playwright.config.mjs` is not exported and not used.
- Pass = exit 0, at least one passed test, zero failed, zero flaky. No test = fail ("the repo has no test").

### Gateway side (`src/personal_agent/dev_agent/publish_check.py`, new)

- `CheckStep` (name, passed, messages), `CheckResult` (passed, steps, image digest, toolkit digest, CSP manifest digest; `to_json()`).
- `parse_check_output(mode, run)`: reads the last `SESHAT_CHECK_RESULT` line. A missing or bad line, a nonzero exit or a timeout (124) is a failed step with a message, never an exception.
- `CheckLauncher` Protocol: `async run_check(job_id, inputs_tar) -> CheckRun` (image id, toolkit digest, two `BytesResult`s). `DockerLauncher` implements it with the `check` profile.
- `build_check_inputs(mirror, commit) -> bytes` (the tar).

### Pipeline (`repo_pipeline.py`)

- `check(job, result_commit, fence)`:
  1. Mirror: `fetch` `job/<job id>` with `host.remote(repo)`, require head == `result_commit`, read the page, require valid UTF-8 (else a failed **load** step with the message, no container), build the inputs tar.
  2. Fenced action `publish_check`: `run_check`. A launcher or toolkit error raises `DevJobRefusedError("check_unavailable")` (the job ends `failed`, not `failed_check`, because the page was not judged).
  3. Fenced action `record_check`: `JobStore.record(check_result=...)`.
  4. Return `result.passed`. The supervisor already ends a fail as `failed_check`, and publishes nothing.
- `deliver(job, fence)`:
  1. Fenced `preallocate_artifact`: `UPDATE ... SET result_artifact_id = COALESCE(result_artifact_id, $new) RETURNING result_artifact_id`. A retry gets the same id.
  2. Mirror: fetch `job/<job id>`, require head == `result_commit`, `read_page`.
  3. Fenced `publish_artifact`: `artifact_write_executor(slug=job.repo, content_type="text/html; charset=utf-8", content=page.decode("utf-8"), title=<page <title>, or None>, ctx=<user id, session id, job trace id>, _commit_path="dev_agent", _artifact_id=<id>)`.
  4. Best effort, after the fence: push `StateUpdateEvent(key="dev_job_artifact", value={job_id, artifact_id, public_url, title})` to the job's session through `_push_event` (durable in `session_events`, replayed on reconnect). T7 (FRE-1688) renders the job card from job events. A failed push is logged and never fails the job.
- The pipeline gets two new arguments: `checker: CheckLauncher | None` (no checker → `check_unavailable`) and `notify: ArtifactNotifier` (default: the transport push). `gateway.py` passes the `DockerLauncher` with the `check` profile.

### `artifact_write_executor`

- New internal keyword `_artifact_id: UUID | None = None` (not in the tool schema), like `_commit_path`.
- `None` → today's behaviour, unchanged.
- Given → that id is the artifact id, so the R2 key (`build_r2_key`) is the same on every retry. The R2 put overwrites the same key with the same bytes. The insert is `ON CONFLICT (id) DO NOTHING RETURNING id`. On a conflict it reads the existing row and requires the same `user_id` and `r2_key`, else it raises `ArtifactWriteError("id_conflict")`.

### `JobStore`

- `record(..., check_result: str | None)` (JSON text, cast to `jsonb`).
- `preallocate_artifact(conn, lease, candidate) -> UUID`.
- No schema change: `check_result` and `result_artifact_id` exist in `0039_dev_jobs.sql`. `artifacts` does not change (D9.10).

## Steps and tests

1. `artifact_write_executor` `_artifact_id` → test first: `tests/personal_agent/tools/test_artifact_write_preallocated_id.py` (unit, mocked store and session): the key derives from the given id, two calls put the same key, the SQL carries `ON CONFLICT`, a conflict with another user's row raises `id_conflict`, `None` keeps the old SQL.
   `make test-file FILE=tests/personal_agent/tools/test_artifact_write_preallocated_id.py`
2. `publish_check.py` parsing + inputs tar → `tests/personal_agent/dev_agent/test_publish_check.py` (unit): result line parsing, missing line, timeout, the three digests in `to_json`, the tar holds `dist/index.html` + `tests/*` only, `.gitattributes export-subst` does not change the page bytes (local git repo).
3. `JobStore.record(check_result)` + `preallocate_artifact` + pipeline `check`/`deliver` → `tests/personal_agent/dev_agent/test_delivery_integration.py` (test stack Postgres, local bare repos as in `test_repo_pipeline_integration.py`, a fake R2 store and a fake checker, embeddings monkeypatched):
   - a fail ends `failed_check`, no `artifacts` row, `check_result` stored with three digests;
   - a pass publishes one row, R2 bytes SHA-256 == `dist/index.html` at the result commit;
   - **AC-4 crash test**: the real `JobSupervisor` with the real pipeline. Seeded hang at each of four points (before upload, after upload, after the row insert, before the end state), then `stop()`, expire the lease, a second supervisor recovers. Each case: exactly one R2 object and one `artifacts` row for the job, the job ends `succeeded`.
4. Check image + script → `docker/dev-agent/publish-check.mjs`, `docker/dev-agent/repo-tests.config.mjs`, `Dockerfile.browser` (`check` target installs `@playwright/test@1.60.0` in `/opt/check`). Build with a test tag only: `docker build -t seshat-dev-check:fre1686-test --target check --build-context csp=e2e/artifact-lib -f docker/dev-agent/Dockerfile.browser docker/dev-agent/`. Production tags are not touched.
5. Live check → `tests/personal_agent/dev_agent/test_publish_check_live.py` (`integration`, real Docker, own scope label, synthetic toolkit volume as in `test_toolkit_live.py`, image `seshat-dev-check:fre1686-test`):
   - AC-1: page that throws only on a button click → interaction fails; page that calls `fetch()` → fails with a CSP violation; repo with no test → repo tests fail;
   - AC-2: a known-good page that loads Chart.js from `/lib/` with a real interaction test → all three pass;
   - AC-3: mutation of that page (the button no longer changes the asserted text) → the repo test fails;
   - AC-6: `docker inspect` of the check container (created, inspected before start) → `inspect_violations(profiles.check, ...)` is empty;
   - nothing of the job remains after the check (containers and volumes by label).
   The Chart.js file in the synthetic toolkit is the real pinned file from the repo's toolkit source, if it is in the repo; else the test reads it from the local toolkit volume. (Resolved at implementation: use the pinned `/lib/` asset bytes that `config/artifact_lib_manifest.json` names.)
6. Docs: `docker/dev-agent/README.md` "The publish check and delivery (FRE-1686)" section with the deploy steps; `GitHubRepoPipeline` docstrings.
7. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`, plus the integration files above with `make test-file`.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| Seeded pages (throw on click, `fetch()`, no test) end `failed_check`, no artifact row | live test (step outcome) + integration test (state + no row) |
| Known-good Chart.js page passes and is published | live test (passes) + integration test (published) |
| Mutation breaks the asserted interaction → repo test fails | live test |
| Seeded crash at four points + recovery → one R2 object, one row | integration crash test |
| SHA-256 of R2 bytes == `dist/index.html` at the result commit | integration test |
| `docker inspect` of the check container shows every flag and limit | live test with `inspect_violations` |

## Deploy (master, after merge)

1. `make dev-agent-build` (the check image gets the script and `@playwright/test`).
2. Rebuild `seshat-gateway`. The supervisor stays off (`AGENT_DEV_AGENT_SUPERVISOR_ENABLED` unset). No migration.
