# Development agent: network, proxy and images

ADR-0159 (D3.3, D4.1, D4.2, D4.5, D2.1, D7.1, D7.3, D8.8). Ticket FRE-1682.

The gateway starts one agent container for each job. It also starts a check container and a handoff
container. This directory holds the images and the egress proxy. The container boundary is data in
`config/dev_agent/container_profiles.yaml`.

## Images

| Image | Built from | Holds | Network |
|-------|-----------|-------|---------|
| `seshat-dev-agent:0.1` | `Dockerfile.browser`, target `agent` | OpenCode (pinned), git, Chromium, Playwright, CSP test server, probe | `dev-agent` |
| `seshat-dev-check:0.1` | `Dockerfile.browser`, target `check` | Chromium, Playwright, CSP test server, probe | none |
| `seshat-dev-handoff:0.1` | `Dockerfile.handoff` | git, `handoff-bundle.sh` | none |
| `seshat-dev-proxy:0.1` | `Dockerfile.proxy` | Squid, `squid.conf` | `dev-agent` and `dev-agent-egress` |

Build all four: `make dev-agent-build`. The proxy also builds with `docker compose up` because it is a compose service.

## The network

- `dev-agent` is `internal: true`. It has no route to the internet, the data stores, Caddy or the gateway.
- Only `dev-agent-proxy` joins it from compose. The gateway starts the job containers on it.
- `dev-agent-egress` is the proxy's way out. Only the proxy joins it.
- The proxy tunnels HTTPS (`CONNECT`) to `registry.npmjs.org` on port 443. It refuses plain HTTP, other ports, other hosts and any IP address.
- The agent uses it through `HTTPS_PROXY=http://dev-agent-proxy:3128`.

## Check the setup

```bash
make dev-agent-build
make dev-agent-verify ARGS="--compose-project <throwaway> --up --down --output report.json"
```

Use a throwaway project name. The script refuses `--up` and `--down` for the project `seshat`, because
`down` would stop production and `up` is a deploy. After a deploy, run it with no flag. It then uses the
running proxy of project `seshat`.

The script starts canary listeners on a separate network, on the production ports. It does not connect to
production containers unless you pass `--include-production-ips`. It runs the probe in a container built
from the spec, compares `docker inspect` with the spec, and runs the same probe on the canary network as a
control. A probe that never reaches anything proves nothing, so the control must see every canary.

## Notes for the next tickets

- **OpenCode pin.** `config/dev_agent/container_profiles.yaml` (`opencode_version`) and `ARG OPENCODE_VERSION` in
  `Dockerfile.browser` hold the same value. A test fails if they differ. OpenCode 2.x is the npm package
  `@opencode/cli`. The command prints `opencode v<version>`.
- **Permission file.** `/etc/opencode/opencode.json` is `opencode.json` of this directory (FRE-1684). See "The job supervisor" below.
- **Toolkit mount.** The read-only toolkit volume mounts at `/srv/artifact-lib`. Do not mount at `/lib`: on the base image `/lib` is a symlink to `usr/lib`, and a volume there replaces the system libraries. The CSP server serves one root. To serve the toolkit under `/lib/`, link it into the root: `ln -s /srv/artifact-lib <root>/lib`.
- **Console message `webrtc`.** The production CSP has a `webrtc` directive. Chromium does not know it. It logs
  `Unrecognized Content-Security-Policy directive 'webrtc'` as a console error on every page. The publish check
  (D7.4, "zero console errors") must ignore that one message.
- **CSP manifest.** `csp-manifest.json` holds the placeholder origins. A test pins it to `expected_csp_directives()`.
  On a production host the real `frame-ancestors` origin differs. The check does not frame the page, so this does not change its result.
- **Chromium.** Playwright starts Chromium with `--no-sandbox` by default. The container is the boundary. `/dev/shm` is 64 MB, so tests pass `--disable-dev-shm-usage`.
- **Handoff.** `handoff-bundle.sh` sets every git setting that can run a program on the command line. The container has no network and no secret. Both layers apply.
- **Residual risk.** The proxy decides by the host name of the `CONNECT` target. It cannot read the encrypted tunnel.
  The agent can send data to `registry.npmjs.org`. It cannot send it anywhere else.
- **Observed on 2026-11-06.** On `dev-agent`, external names do not resolve (no DNS path out). A connection to the network's `.1` address on port 22 times out.

## The broker (FRE-1683)

`seshat-dev-broker` (in `docker-compose.cloud.yml`) runs `python -m personal_agent.dev_broker` from the gateway
image. It has two listeners, each on the broker's own address on one network:

| Network | Port | Listener | Authenticated by |
|---|---|---|---|
| `dev-agent` | 8700 | `POST /v1/chat/completions`, OpenAI-compatible, `stream` supported | the job token (`Authorization: Bearer`) |
| `cloud-sim` | 8701 | `/{method} /relay/{job_id}/{path}` to the job's OpenCode server, and `/healthz` | `X-Dev-Broker-Secret` |

Limits and models are in `config/dev_agent/broker.yaml`. The ledger is migration `0038_dev_broker.sql`.

### First deploy (master)

1. Add the role `dev_agent` to the real `config/governance/budget.yaml`, as in `budget.yaml.example` (the role
   block and the `uncapped_roles` entry). Without it, the gateway refuses to start after its rebuild
   (`validate_role_totality`).
2. Apply `docker/postgres/migrations/0038_dev_broker.sql` with `AGENT_DATABASE_ADMIN_URL`. It creates the role
   `seshat_dev_broker` with no password, so nothing can log in as it before step 3.
3. Generate two values (`openssl rand -hex 32`). Put them in `/opt/seshat/.env` as `DEV_BROKER_DB_PASSWORD` and
   `AGENT_DEV_BROKER_CONTROL_SECRET`, and in `pass`. Then run
   `ALTER ROLE seshat_dev_broker PASSWORD '<DEV_BROKER_DB_PASSWORD>'`. The compose file defaults both to empty,
   so other services deploy without them, and the broker refuses to start (or to log in) until they are set.
4. Rebuild `seshat-gateway` (new `ModelRole`, the settings field, the `respond()` settle fix).
5. `docker compose -f docker-compose.cloud.yml up -d --build seshat-dev-broker`.
6. `make dev-broker-verify`. Every section of the report must be empty and `result` must be `PASS`.

### Notes for FRE-1684

- **Tokens.** `personal_agent.dev_broker.tokens.issue_job_token` (before the agent container starts) and
  `revoke_job_token` (first step of every end state) run on the gateway's pool. The broker's role cannot insert a
  token. The supervisor reads `dev_job_tokens.status` and `stop_reason` on each lease renewal.
- **Container name.** Name the agent container `agent_container_name(job_id)`
  (`personal_agent.dev_agent.container_spec`). The relay resolves that name and nothing else.
- **OpenCode provider.** Point OpenCode's OpenAI-compatible provider at `http://dev-broker:8700/v1`, with
  `SESHAT_JOB_TOKEN` as the API key and the job's catalog key (for example `deepseek-v4.1-flash-scaleway`) as the
  model id. Any other model id is refused.
- **Relay.** Send the OpenCode server password as `Authorization` through the relay. The relay forwards it and
  strips the control secret.
- **Spend.** `dev_job_spend` holds a job's committed and reserved spend. `dev_job_calls` is the per-call ledger.
  A call that stays `reserved` past the call limit plus 60 s (a failed settle, or a broker that died mid-call)
  is settled as `cancelled` by the broker's sweep, at start and every 60 s. Its worst case stays spent.
- **Errors.** A failure before the send (config, egress guard, a 10 s slot wait, the cost-gate database) or a
  4xx other than 408 from the provider settles at zero. A 5xx, a 408 or other timeout, or a connection lost after
  the send keeps the worst case (`provider_error`), because the provider may have billed. A settle that fails is
  logged, the answer still goes out, and the sweep settles the row at the worst case. Any other error after the answer keeps the
  worst case and stops the token with `cost_unknown`.
- **Refusals.** 403 `cap_reached` stops the token: not even the smallest call (an empty prompt and a full
  answer) fits in the committed spend. 403 `call_too_large` refuses only this call: it does not fit the room
  left, but a smaller one can. 429 `cap_busy` refuses for now: only the worst cases of calls in flight cause it,
  and a retry can pass once they settle. Only `cap_reached` stops the token.
- **Window boundary.** No call starts within 180 s of 00:00 UTC (the 150 s call, its reserve and its settle).
- **Input.** Message content must be text (a string or `text` parts). Any other part is refused, because the
  worst case bounds text only.
- **Stop.** One signal handler stops both listeners. In-flight model calls get up to 160 s to finish and
  settle (`stop_grace_period: 3m`). An open relayed stream re-checks its job every 5 s and ends after
  revocation.
- **Stored cap overrides.** The broker's role has no grant on `settings_overrides` (ADR-0159 D4.4). Its cost gate
  enforces the file caps, not a cap that the owner stores in the PWA. Its own job and month caps still apply.

## The job supervisor (FRE-1684)

The gateway runs the supervisor (`personal_agent.dev_agent.supervisor`) when
`AGENT_DEV_AGENT_SUPERVISOR_ENABLED=true`. It is off by default. The job record is migration
`0039_dev_jobs.sql`.

- **Lease.** 2 minutes, renewed every 30 s. All work stops after 60 s with no renewal. Postgres time
  (`clock_timestamp()`) is the authority for the lease and the 30-minute limit.
- **Fence.** Every external action runs in a transaction that holds the job row's lock until the
  action returns, and is logged in `dev_job_actions`. Recovery takes jobs with `SKIP LOCKED`, so the
  lease cannot change hands during an action.
- **End state.** Revoke the token, remove the job's containers and volume, verify, then write the end
  state, in one fenced action. A failed cleanup leaves the job active until recovery cleans it.
- **Labels.** `seshat.dev_job=<job id>` and `seshat.dev_job.scope=<AGENT_DEV_AGENT_LABEL_SCOPE>` on
  the container and the workspace volume `seshat-dev-ws-<job id>`. Every listing filters on the scope.
- **OpenCode.** `opencode.json` is the config and the D2.4 permission file. The provider `seshat` is
  the broker route. The rules start with `* * deny` and contain no `ask`, because OpenCode 2.2.5 puts
  the file's rules after its built-in rules (some of which ask) and the last match wins. Checked
  against `GET /api/agent` of a running server on 2026-11-07.

### Deploy (master)

1. `make dev-agent-build`. The agent image now holds `opencode.json`.
2. Apply `docker/postgres/migrations/0039_dev_jobs.sql` with `AGENT_DATABASE_ADMIN_URL`.
3. Rebuild `seshat-gateway` and `seshat-dev-broker`. Leave the supervisor off.
4. Check: `docker inspect --format '{{.State.Health.Status}}' seshat-seshat-dev-broker-1` prints
   `healthy` (the health check no longer imports the cost gate).

Before the supervisor can run a job, the toolkit volume (`AGENT_DEV_AGENT_TOOLKIT_VOLUME`) must exist
with the pinned toolkit. Run `make dev-agent-toolkit` (FRE-1751) after `make dev-agent-build`:

1. The command reads each asset of `config/artifact_lib_manifest.json` from R2 (key `lib/<path>`).
2. It checks every file against its SHA-384 pin in `config/artifact_lib_substitution_map.json`. One bad
   file stops the command before it creates anything, and an older volume stays as it was.
3. It replaces the volume, writes `toolkit-manifest.json` with the toolkit digest, reads the volume
   back, and checks it again. A failure removes the new volume.
4. It prints the digest. The T5 publish check records that digest (ADR-0159 D7.5, FRE-1686).

Run it again after any change of a pin, or when the launcher reports a wrong toolkit volume. The
command needs `AGENT_R2_*` in the environment and refuses a volume that a container uses.

The launcher copies the files out of the volume before every job and checks them against the pins in
the committed config. A missing, changed or extra file stops the job (`DevAgentToolkitError`). The
check reads bytes, not a label, so a half-copied volume never reaches an agent.

### Notes for FRE-1686

- `GitHubRepoPipeline` (`personal_agent.dev_agent.repo_pipeline`) holds `prepare`, `handoff` and
  `advance_main` (FRE-1685). Add `check` and `deliver` to it. Do every external action through
  `fence.run(action, operation)`, and bound it: `SupervisorPolicy.action_timeout_seconds` cancels it.
- `deliver` must be idempotent (D9.8). Recovery calls it again for a job that crashed in `delivering`,
  and `advance_main` runs after it on both paths.
- Read `dist/` and `tests/` at the result commit from a clean mirror (`GitMirror.open()`, then
  `fetch` of `job/<job id>` with `host.remote(repo)`). Never read the workspace.
- Start the check container with the job label and the scope label, so `remove` and the orphan
  sweep find it.
- The page template (`config/dev_agent/page_template/`) pins `@playwright/test` 1.60.0 and serves
  `dist/` through `playwright/serve.mjs`. The check container has no network, so its image must hold
  `@playwright/test`. Change the template if the check needs another setup.

## Repos and the handoff (FRE-1685)

ADR-0159 D8. The gateway holds the GitHub App key and is the only party that writes to GitHub.

- **Repo.** `prepare` creates a private repo `<slug>-<8 hex of the job id>` in the organization
  (`page-…` for a build) and pushes the template commit to `main`. A fix of a page that the agent
  did not build adds the page as `dist/index.html`. A fix of an agent-built page continues that
  page's repo from the source job's result commit. The fix source is looked up by artifact id **and**
  by the job's user. A refused source ends the job `failed`/`source_not_found` and creates no repo.
- **Workspace.** The gateway clones the base in a temp dir and copies the tar into the new agent
  container with `docker cp -a` before it starts. The clone is on `job/<job id>` and has no remote.
- **Handoff.** `handoff-bundle.sh` never opens the workspace as a git repo. It reads the branch head
  as text, then bundles it from a new repo in `/tmp` that uses only the workspace's object store.
  The gateway fetches the bundle into a clean mirror (fsck on), requires that the head descends from
  the base, and refuses a symlink or submodule anywhere in the tree, and a missing or oversized
  `dist/index.html`. Then it pushes `job/<job id>`, never by force.
- **main.** After the publish, `advance_main` moves `main` only by fast-forward. `dev_jobs.main_moved`
  is `true`, `false` (main was not an ancestor, so it stayed), or `NULL` (unknown).
- **Token scope.** Repo creation uses `administration: write`. Git uses `contents: write` on one
  named repo. The token reaches git only as an environment header, never on a command line.

### Deploy (master)

1. Rebuild the handoff image: `make dev-agent-build` (the new `handoff-bundle.sh`).
2. Apply `docker/postgres/migrations/0040_dev_jobs_main_moved.sql` with `AGENT_DATABASE_ADMIN_URL`.
3. Put the four GitHub values in the gateway's `.env`: `AGENT_DEV_AGENT_GITHUB_APP_ID`,
   `AGENT_DEV_AGENT_GITHUB_INSTALLATION_ID`, `AGENT_DEV_AGENT_GITHUB_ORG` (the values are on the
   ticket, FRE-1685), and `AGENT_DEV_AGENT_GITHUB_APP_PRIVATE_KEY_B64`, the output of
   `pass show seshat/DEV_AGENT_GITHUB_APP_PRIVATE_KEY | base64 -w0`. Without all four the supervisor
   does not start (it is off by default anyway).
4. Rebuild `seshat-gateway` (adds `git` to the image). Leave the supervisor off.
5. Check: `docker exec cloud-sim-seshat-gateway git --version` prints a version, and
   `uv run python scripts/verify_dev_agent_github.py` (with the same four variables) prints
   `"ok": true`. It creates one private `probe-…` repo, which stays.


## The publish check and delivery (FRE-1686)

ADR-0159 D7.3–D7.7 and D9.8–D9.10. `GitHubRepoPipeline.check` and `deliver`.

### The check

- The gateway reads `dist/index.html` and `tests/` at the result commit from a clean mirror, with
  `cat-file` (never `git archive`, because `.gitattributes` can change archive bytes). Only
  `dist/index.html` is served, because production publishes only that file.
- Two input volumes (`seshat-dev-check-dist-<job>`, `seshat-dev-check-tests-<job>`) are filled
  through a helper container that never starts. The check containers mount them read-only.
- The check container runs twice, from the image **id**, with the `check` profile:
  1. `page` (90 s): `publish-check.mjs page`. **Load**: zero console errors, page errors and CSP
     violations. **Generic interaction**: every visible, enabled button, input, select, textarea
     and `[role=button]`, operated with fixed values. An action that fails on such a control is a
     fail. No model-written Node code runs in this container.
  2. `tests` (200 s): the repo's Playwright tests under Seshat's `repo-tests.config.mjs`, with
     `PAGE_URL` set. No test is a fail. The repo's own Playwright config is not used.
  The repo tests are model code. In one container they could forge the result line of the first
  run, so they get a container of their own.
- The result (each step, the messages, the image id, the toolkit digest, the CSP manifest digest)
  is stored in `dev_jobs.check_result`. A fail ends the job `failed_check` and publishes nothing.
  A Docker or toolkit failure ends it `failed`/`check_unavailable`.
- The known benign console error `Unrecognized Content-Security-Policy directive 'webrtc'` is
  ignored.

### Delivery

- A fenced action preallocates `dev_jobs.result_artifact_id`, or reads the one that an earlier
  attempt stored.
- A fenced action publishes the page bytes of the result commit with `artifact_write_executor`,
  with `_artifact_id` set, the job's user and session, and the repo name as the slug. The R2 key
  derives from the id. The insert is `ON CONFLICT (id) DO NOTHING`. So a resumed delivery after a
  crash leaves one R2 object and one `artifacts` row.
- The session gets a `dev_job_artifact` state event (job id, artifact id, public URL, title). The
  job card of FRE-1688 renders it.

### Deploy (master)

1. `make dev-agent-build`. The check image gets `publish-check.mjs`, `repo-tests.config.mjs` and
   `@playwright/test` 1.60.0.
2. Rebuild `seshat-gateway`. Leave the supervisor off. No migration: `check_result` and
   `result_artifact_id` exist since `0039_dev_jobs.sql`.
3. Check: `docker run --rm --network none seshat-dev-check:0.1 ls /opt/check` lists
   `node_modules`, `publish-check.mjs` and `repo-tests.config.mjs`.
