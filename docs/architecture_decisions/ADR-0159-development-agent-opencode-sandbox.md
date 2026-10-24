# ADR-0159: A Development Agent for Seshat — OpenCode in a Sandbox, Private Git Projects, Delivery Through the Artifact Path

**Status:** Proposed
**Date:** 2026-10-24
**Deciders:** Project owner (design decisions recorded on FRE-1679, 2026-10-24), adr seat (Opus)
**Tags:** delegation, coding-agent, sandbox, artifacts, cost, governance
**Supersedes:** ADR-0050 D4 and D6 (outbound delegation and its adapters). ADR-0050 D1–D3 and D5 are not changed by this ADR.

---

## Context

### The case that started it

Trace 8e60be11 (session afe4d1cd, 2026-10-24) had three turns about one interactive page.

- Turn 1 asked for a page. `artifact_draft` sent the plan to the builder (Sonnet 5, 32,768-token ceiling), and the builder made the page.
- Turn 2 said that the widgets did not work. Turn 3 asked Seshat to test, fix and improve the page.
- `artifact_draft` takes only a plan and builds a new page (ADR-0077 D1). No path gives the builder an existing page to fix. So the local primary (12,288-token output limit) wrote the whole page itself.
- Turn 3 ran 23 minutes. Its last call stopped at the output limit inside one tool call. No answer reached the owner.

A development harness works in a loop that neither path has: write, open in a headless browser, operate the widgets, read the errors, fix. That loop is what turn 3 asked for.

### The owner's request

The owner asked for this discussion on 2026-10-24 (FRE-1679), in these words:

> "Is it time to build the agent to agent functionality. What if: a request to build gets delegated to claude code, or even Opencode running on the vps? It builds the artifact - full on dev performed by a harness built for development - which then places the artifact in our s3 storage. We could consider using private git repos for the remote developer agent."

> "The dev agent would be configured to create private git projects. This could open a world of possibilities"

### What exists today (verified on `main` 7b9bbd87)

| Item | State | Where |
|---|---|---|
| Outbound delegation design | ADR-0050 D4 and D6, Accepted 2026-04-13. Not wired. | `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md` |
| Delegation adapters | `ClaudeCodeAdapter` runs `claude --print <task>` with no workspace and no file return. It cuts the result to 500 characters. It passes `--mcp-server`, which is not a Claude Code flag. No code path calls any adapter. | `src/personal_agent/delegation/adapters/` |
| Delegation contract | `DelegationExecutorProtocol` (`delegate`, `available`) | `src/personal_agent/delegation/protocols.py` |
| Gateway DELEGATE route | Chosen by intent class, off by `delegation_enabled=False` | `request_gateway/decomposition.py:176`, `orchestrator/executor.py:5731` |
| Sandbox pattern | `run_python` starts a sibling container through the Docker socket: non-root, read-only root, `--cap-drop ALL`, `--network=none` by default | `tools/primitives/sandbox.py:178-217`, `docker-compose.cloud.yml:381` |
| The network that `run_python(network=true)` joins | `cloud-sim`, a plain bridge with internet egress. Every data store is on it. `caddy:8601` on it reads every user's artifact by UUID. | `docker-compose.cloud.yml:662-669`, `config/cloud-sim/Caddyfile:325-331` |
| Egress control for containers | None | — |
| Artifact write path | `artifact_write_executor(slug, content_type, content, title, summary, tags, ctx)`. It needs `ctx.user_id`. It generates a new UUID and uploads to R2 before it inserts the row. The `artifacts` table has no metadata, version or parent column. The size cap is 5 MB. | `tools/artifact_tools.py:418-641`, `docker/postgres/init.sql:437-454` |
| Artifact read path | `artifact_read_executor` returns content inline only up to 256 KB. A larger page needs an owner-scoped row lookup and `R2ArtifactStore.get`. | `tools/artifact_tools.py:727-888`, `storage/artifact_store.py:222` |
| Production artifact CSP | Lives in the Cloudflare Worker (`personal_agent_secrets`). The in-repo mirror is `expected_csp_directives()`. A Playwright server serves pages with the same directives from a generated manifest, with the artifact origin rebound to localhost. `make verify-envelope` catches drift between the mirror and the Worker. | `observability/artifact_envelope/spec.py:24-56`, `scripts/build_e2e_artifact_fixtures.py:106-123`, `e2e/artifact-lib/csp-server.ts` |
| Approval card | A tool on `ask` shows a PWA card. Deny, timeout and a lost connection all refuse. An explicit `ask` rule offers "allow once" only. The production flag `AGENT_APPROVAL_UI_ENABLED` is true. The request is persisted as a session event. The decision comes back through an in-memory waiter and is only logged. | ADR-0063 Amendment A, ADR-0156 D8, `tools/executor.py:266-580`, `transport/agui/transport.py:915-1000` |
| Cost gate | `CostGate.reserve/commit/refund` by budget role and window. Windows are daily and weekly only. A reservation expires after 180 seconds. `task_id` on a reservation is audit data and keys no counter. No per-job and no monthly cap exists. `api_costs` has no job column, and a row is written only for a positive cost. | `cost_gate/gate.py:54-354`, `cost_gate/types.py:24-28`, `llm_client/litellm_client.py:1781`, `docker/postgres/init.sql:108-122` |
| OpenAI-compatible endpoint for paid models | None. LiteLLM runs in-process only. | `service/app.py` |
| Work that outlives a turn | No job mechanism. `_push_event(event, session_id)` persists an event to `session_events` and puts it on the calling process's local queue. So it reaches a connected client only when the gateway process itself calls it. A reconnecting client replays persisted events. | `transport/agui/transport.py:107-160` |

ADR-0095 has "delegation boundary" in its title, but it is about the model that a sub-agent runs on. This ADR does not touch it.

### Measured headroom on the VPS (2026-10-24)

| Resource | State |
|---|---|
| CPU | 8 vCPU, load average 1.3 |
| RAM | 22 GiB, 6.8 GiB available, no swap. About 4.3 GB goes to idle test and eval stacks, two with no memory cap. |
| Disk | 197 GB, 84% used. Docker can reclaim about 88 GB (62.9 GB build cache, 25.6 GB unused images). |

The model runs at the vendor, so a job uses the VPS for OpenCode, a headless Chromium and package installs. A memory cap is required, because with no swap a job with no cap can wake the OOM killer on production containers.

---

## Decision

This ADR gives Seshat a development agent. Seshat hands it a build or a fix. The agent works in its own git repo and tests the page in a browser. Seshat checks the result and publishes it through the existing artifact path.

### The parts, and who does what

| Part | Where it runs | What it does | What it holds |
|---|---|---|---|
| **Gateway** (exists) | `cloud-sim` | Creates the job after approval. Runs the job supervisor. Starts and removes the agent, handoff and check containers through the Docker socket it already holds. Owns the GitHub App and is the only party that writes to GitHub. Imports, checks and publishes pages. Pushes job events to the session. | What it holds today, plus the GitHub App private key |
| **Broker** (new) | `cloud-sim` and `dev-agent` | Model route for the agent (D5). Control relay from the gateway to the job's OpenCode server (D4). Nothing else. | The Scaleway key, a restricted database role, the control secret. No Docker socket. |
| **Egress proxy** (new) | `dev-agent` and an external network | Forwards only to the allowlist of D4 | Nothing |
| **Agent container** (new, one per job) | `dev-agent` only | Runs OpenCode on the job | The job's two credentials (D6). No GitHub credential. |
| **Handoff container** (new, one per job) | No network | Bundles the agent's commits from the workspace (D8.8) | Nothing |
| **Check container** (new, one per check) | No network | Runs the publish check (D7) | Nothing |

### D1 — Scope of v0

1. v0 does one kind of job: **build or fix an interactive artifact**. A job has one of two modes, `build` or `fix`.
2. v0 delivers one sealed HTML page per job through the artifact path, under the ADR-0089 sealed box.
3. The job contract keeps larger outputs possible (owner's choice). The agent writes its deliverable into a **build-output folder**, `dist/`, in the repo. v0 publishes `dist/index.html` only. A later ADR decides where multi-file outputs are hosted. It does not need a new contract.
4. Other development work (scripts, services, tools for Seshat) is out of scope for v0.

### D2 — The harness is OpenCode, behind the existing delegation contract

1. The harness is **OpenCode 2.x**, pinned to one version in the agent image. Owner's reason: it keeps Seshat's models separate from the owner's own coding agent (Claude Code), and it teaches us a second harness.
2. A new adapter, `OpenCodeAdapter`, implements `DelegationExecutorProtocol`. The protocol stays. The three unused adapters (`claude_code.py`, `codex.py`, `generic_mcp.py`) are deleted. A later adapter can still be added behind the same protocol.
3. The adapter drives OpenCode through its HTTP server (`opencode serve`), through the broker's control relay. It follows progress on the server's event stream.
4. Every job runs with an explicit OpenCode permission file. OpenCode asks a client when no rule matches, and a headless job has no client to answer, so every action needs a rule. The file allows reads, edits and shell in the workspace. It denies external directories, web search and web fetch.
5. **The permission file is defense in depth, not the boundary.** The agent has a shell, so the permission file cannot stop a read outside the workspace. The container (D3) is the boundary.
6. Each repo carries an `AGENTS.md` that states the test loop and the test contract (D7).

### D3 — Placement: a fresh container for each job, a separate VPS if the evaluation shows value

1. The gateway starts **one agent container for each job** and removes it at the job's end state. No process, file, OpenCode session or cache survives from one job to the next.
2. The gateway starts it the same way `run_python` starts its sandbox, but on the `dev-agent` network. Only one job runs at a time (D9).
3. The container boundary:
   - non-root user, `--cap-drop ALL`, `no-new-privileges`, a process limit, memory cap 3 GB, 2 CPUs.
   - read-only root filesystem.
   - writable paths: a new workspace volume for this job, a `tmpfs` for `/tmp`, and a `tmpfs` for OpenCode's state.
   - read-only mount: the pinned toolkit (`/lib/`, ADR-0089 Addendum A), verified against its digest.
   - no other mount, no Docker socket, no host path.
   - environment: the two job credentials of D6, the proxy variables, and a fixed list of non-secret runtime variables (`PATH`, `HOME`, `LANG`, `TZ`, `NODE_ENV`, the OpenCode config path), declared in the launcher. Nothing is inherited from the gateway.
4. The gateway deletes the workspace volume at every end state.
5. Seshat talks to OpenCode only over HTTP, and it starts the container through one launcher interface. A move to a separate VPS replaces the launcher and the base URLs, not the job logic. The decision to move belongs to the owner, with the evaluation of D11 as input.

### D4 — Network isolation

1. The agent container is **not** on `cloud-sim`. It is on a new Docker network, `dev-agent`, with `internal: true`. That network has no route to the internet, to the data stores, to Caddy or to the gateway.
2. Two other containers join `dev-agent`. No other container joins it.
3. **The broker** runs two separate listeners, each bound to one interface address, never to `0.0.0.0`:
   - on `dev-agent`: the model route of D5 only, authenticated by the job token.
   - on `cloud-sim`: the control API, authenticated by a secret that only the gateway holds.
   The control API relays requests to the OpenCode server of the current job's container only. The broker forwards nothing else, and it has no general proxy function.
4. The broker runs with an explicit environment allowlist: the Scaleway key, a restricted database role, and the control secret. The role can read and write only the cost-gate tables, the job token table and the spend tables of D5, and `api_costs`. It has no access to `dev_jobs`, `artifacts` or any other table.
5. **The egress proxy** also joins an external network. It allows only `registry.npmjs.org`, by host name. It refuses IP literals, other ports and other hosts. npm uses it through `HTTPS_PROXY`. The agent needs no GitHub access, because the gateway places the repo in the workspace and takes the commits out (D8).

### D5 — Model access through a metering route that Seshat owns

1. The agent holds **no model vendor key**. Its only model endpoint is the broker's OpenAI-compatible route. The route supports streaming.
2. The broker dispatches each call through `LiteLLMClient` with the egress guard, as ADR-0141 D1 and D2 require for every LLM call. Scaleway gets a catalog entry and a guard allowlist entry like any other provider.
3. The broker holds the Scaleway key. It is an application credential under ADR-0132 D2, so it stays in a Seshat service and never moves to the environment layer or to the agent.
4. Each request carries the **job token** (D6). The broker checks it against a **job token table** that the gateway writes: token hash, job id, model, status, expiry, stop reason. The broker accepts a token only while its status is `active`, before its expiry, and only for its model. The gateway sets the status to `revoked` at every end state.
5. **The caps are atomic counters that Seshat owns, not the cost gate's windows.** Two counters exist:
   - one row per job, with committed and reserved spend.
   - one row per UTC calendar month for the `dev_agent` role, with committed and reserved spend.
6. Before each call, the broker reserves the call's worst-case cost on both rows in one database transaction, with row locks. Worst case means the counted prompt tokens and the requested output tokens at list price. The transaction refuses the call if the reservation takes either row past its cap: **€2 for the job, €30 for the month**. Parallel calls therefore cannot pass a cap together.
7. These reservations have no time limit. A reservation is released only when its call settles.
8. After each call, the broker settles the reservation with the cost from the provider's reported usage. If the usage is missing or the cost cannot be computed, the broker keeps the worst-case reservation as spent. In the same transaction it sets the job token's status to `stopped` with the stop reason `cost_unknown`. A refusal at a cap sets `stopped` with `cap_reached` the same way. The supervisor reads the token row on each lease renewal, and it ends the job with that reason.
9. The broker writes one row per call to a new table, `dev_job_calls`: job id, model, tokens, reservation, settled cost, status. This table is the per-job ledger. Each job also runs under its own trace id, so the `api_costs` rows that `LiteLLMClient` writes join to the job by trace id.
10. The broker also reserves each call on the cost gate under a new budget role, `dev_agent`, so the global daily and weekly caps of ADR-0065 also count this spend. A cost-gate reservation expires after 180 seconds. So the broker ends every model call at **150 seconds** and clamps the output tokens that a call can request. A call that reaches 150 seconds is settled at its worst-case reservation, on the cost gate and on the job and month counters, before the 180-second expiry. No cost-gate reservation of this role can expire during a live call.
11. The limits are the owner's numbers (FRE-1679, 2026-10-24). They are configured as their equivalents in the ledger currency (USD), with the conversion factor stated in config.
12. Vendor and models (owner's choice): **Scaleway Generative APIs**, Paris. The default model is `deepseek-v4.1-flash`, which has vision and a cached-input price. The alternatives are `glm-5.3` and `qwen3.8-27b`. The model is a configuration value, not a tool argument. The Scaleway console marked all three as PREVIEW on 2026-10-24, so the job record stores the model id.

### D6 — The credentials in the agent container

The container holds exactly two credentials. The model can read both through its shell, so each one grants only what this job may do anyway:

| Credential | What it grants | Lifetime |
|---|---|---|
| Job token | The broker's model route, for this job and its model, inside the caps of D5 | Revoked at the job's end state |
| OpenCode server password | Access to this job's own OpenCode server. It is endpoint authentication, not a secret from the agent. | New for each job |

The container holds no GitHub token, no Scaleway key, no Anthropic key, no R2 credential, no database URL and no `.env`.

### D7 — The test loop and the publish check

1. The agent image contains headless Chromium, Playwright and the CSP test server from `e2e/artifact-lib/`. That server sends the production CSP directives from the generated manifest, with the origin rebound to localhost.
2. `AGENTS.md` requires the loop: open the page, operate every widget, read console errors and CSP violations, fix, repeat. It also requires the **test contract**: the repo holds at least one Playwright test in `tests/` that operates the page's controls and asserts their effect.
3. Before Seshat publishes, the gateway runs the **publish check** in a check container. The model does not control the check. The repo's tests are code that the model wrote, so the check container has the same kind of boundary as the agent container:
   - no network, non-root user, `--cap-drop ALL`, `no-new-privileges`, read-only root.
   - memory cap 2 GB, 1 CPU, a process limit, and a wall-clock limit of 5 minutes.
   - read-only inputs: the `dist/` and `tests/` folders exported by the gateway from the result commit, and the pinned toolkit.
   - one bounded `tmpfs` for writable scratch.
4. The check has three steps. All three must pass:
   - **Load:** the page loads under the CSP with zero console errors, zero page errors and zero CSP violations.
   - **Generic interaction:** a script that Seshat owns clicks every button, changes every input, select, checkbox and range, and then requires zero new errors and zero CSP violations.
   - **Repo tests:** the repo's tests run and pass. A repo with no test fails this step.
5. The check stores its result on the job: pass or fail per step, the console and CSP messages, and the digests of the check image, the toolkit and the CSP manifest.
6. A page that fails the check is not published. The job ends as `failed_check`, and the job card shows the repo link and the check result.
7. A page that passes is published directly, with no owner approval step (owner's choice for v0).

### D8 — Private git projects and the handoff of the result

1. The owner creates a new private GitHub organization, separate from `alextra-lab`, and a GitHub App installed only on that organization.
2. The gateway holds the App's private key and is the only party that writes to GitHub. It creates each repo as private. **The agent never holds a GitHub credential and never reaches GitHub.** A repo-scoped token cannot be limited to one branch, and branch rules on private repos need a paid GitHub plan. So the agent gets no token at all.
3. Repo name: `<artifact-slug>-<short-job-id>`. Repos are kept. v0 deletes none.
4. Before the agent container starts, the gateway clones the repo at the base commit into the job's workspace volume, on a branch `job/<job-id>`. The agent commits there. Nothing that the agent does in the workspace reaches GitHub until the handoff.
5. **Build mode** creates the repo from a template that holds `AGENTS.md`, the test setup and an empty `dist/`. The template commit is the base commit.
6. **Fix mode, page built by the agent:** the job continues that page's repo (owner's choice). The base commit is the result commit of the job that built the page. The gateway finds both through `dev_jobs`.
7. **Fix mode, page not built by the agent:** the gateway looks up the artifact row by id **and** by the requesting user, and requires `type='artifact'`, an HTML content type and `upload_pending=false`. It refuses any other row. It reads the page with `R2ArtifactStore.get`, up to the 5 MB artifact cap. It creates a new repo from the template and commits the page as `dist/index.html`. That commit is the base commit.
8. **The handoff.** When OpenCode reports the session done, or at the time limit, the gateway does these steps in order:
   1. It revokes the job token and stops the agent container. From here the agent can change nothing.
   2. It starts a handoff container with no network and the same limits as the check container. The container mounts the workspace read-only, with git hooks and repo-local config disabled. It writes a `git bundle` of `job/<job-id>` to an output folder. The workspace is untrusted, so the gateway never runs git on it directly.
   3. The gateway fetches the bundle into its own clean mirror of the repo. It requires that the bundled head descends from the base commit, and that `dist/index.html` exists and is at most 5 MB.
   4. It pushes the head to `job/<job-id>` on GitHub with its own token, and stores the head as the job's result commit.
9. After a successful publish, the gateway moves the repo's `main` to the result commit by fast-forward. Only the gateway writes to GitHub and only one job runs at a time, so the fast-forward cannot conflict.

### D9 — The job record, the supervisor and delivery

1. A new Postgres table, `dev_jobs`, holds one row per job. Columns: id, user id, session id, job trace id, approval request id (unique), mode, task text, source artifact id, repo, base commit, result commit, preallocated result artifact id, model id, state, spend, check result, lease owner, lease expiry, start and end times, failure reason.
2. Job states: `queued`, `running`, `checking`, `delivering`, `succeeded`, `failed`, `failed_check`, `cap_reached`, `cost_unknown`, `timed_out`, `cancelled`.
3. A partial unique index allows at most one job in `queued`, `running`, `checking` or `delivering`. That index enforces "one job at a time".
4. **The supervisor** is a task in the gateway process. It takes a job by writing a lease to its row: an owner id, an expiry, and a lease number that grows by one on each take. It renews the lease while the job runs. Every state change is one transaction on the row that requires the current lease number. A supervisor that lost its lease therefore cannot change the job.
5. **Recovery.** The supervisor scans for expired leases on a fixed interval while the gateway runs, not only at start. For each job that is not at an end state and whose lease expired, it takes the lease, removes the job's containers by label, revokes the job token, deletes the workspace, and marks the job `failed` with the reason `supervisor_restart`. A restart before the old lease expires is covered by a later scan. v0 does not resume a job.
6. The time limit is 30 minutes from the start of the agent container. When the limit is reached, the job goes to the handoff of D8.8. It ends as `timed_out` if no result passes.
7. **Idempotent delivery.** The gateway preallocates the result artifact id on the job row before it publishes. The publish call takes that id instead of a new UUID, and the R2 key derives from it. A retry after a crash at any step finds the same id. It produces at most one R2 object and one `artifacts` row.
8. The gateway publishes the `dist/index.html` bytes that it read at the result commit, with the job's user id and session id. It never reads from the agent's working tree.
9. The `artifacts` table does not change apart from what idempotent delivery needs. Provenance and lineage live in `dev_jobs`.

### D10 — Consent, trigger and the user experience

1. A new native tool starts a job. The primary calls it with the task, the mode, and for a fix the artifact id.
2. `config/governance/permissions.yaml` puts the tool on an explicit `ask` rule. **Every job shows the approval card in v0**, including when the owner asked directly (owner's decision). The card offers "allow once" only (ADR-0156 D8.3). Deny, timeout and a lost connection refuse (ADR-0063 Amendment A).
3. **The approval decision becomes durable.** The permission layer persists each decision as a session event: request id, tool call id, decision, time. It passes the request id to the executor. The executor stores it on the job row, under the unique constraint of D9.1. A replayed or second use of one approval cannot create a second job.
4. The model never decides that the owner "asked directly". A later change can add a direct path that code recognizes, for example a command or a PWA button, and that path can skip the card. v0 has no such path.
5. After approval, the executor creates the job row and returns at once. The turn ends with "build started". If a job is active already, or the month cap is reached, the executor refuses and says why.
6. The supervisor runs in the gateway process, so its `_push_event` calls reach a connected client directly, and a reconnecting client replays them. The PWA shows a job card with the state, the latest step from the OpenCode event stream, the spend, and at the end the artifact card or the failure.
7. The gateway DELEGATE route and `delegation_enabled` stay off. This ADR does not use them.

### D11 — The first evaluation decides whether the work continues

1. The first paid run uses the trace 8e60be11 fix as its task. It runs each of the three models three times. The repeats show the noise. Today's path is the baseline, and it failed on this task. Nine jobs at most €2 each stay inside the €30 month.
2. The run follows the paid-run rule: a preflight on the ticket, a check of the Scaleway balance, and the owner's recorded go.
3. The owner rates each delivered page blind, as working or not working.
4. The result goes to the owner, who decides whether to continue and whether to move the agent to a separate VPS (D3.5). This ADR sets no automatic threshold for that decision.

---

## Alternatives Considered

### Option A: Give `artifact_draft` an edit mode and add a browser check (fix Goal A only)
**Description:** Pass the existing page into the builder, and run a headless check after it writes.
**Pros:**
- Small change in code that exists.
- Days of work, not weeks.
**Cons:**
- No loop: the builder writes once and cannot react to what the browser shows.
- The edit mode becomes dead code when the development agent ships.
**Why rejected:** The owner chose the development agent as the goal, with the artifact fix as its first use case. Option A is work that D1–D10 replace.

### Option B: Claude Code as the harness
**Description:** Run Claude Code headless with an Anthropic key, as ADR-0050 D4 sketched.
**Pros:**
- The strongest coding harness that the owner knows.
- The owner trusts its results.
**Cons:**
- Anthropic models only.
- It mixes Seshat's agent with the owner's own coding agent.
**Why rejected:** Owner's decision. Claude Code stays the owner's tool. OpenCode keeps the models open and teaches a second harness.

### Option C: A separate VPS from day one
**Description:** Run the agent on its own small VPS (OVH VPS-2 or VPS-3, €7.21 to €10.40 a month excluding VAT).
**Pros:**
- The agent never runs on the production host.
- Its RAM and disk do not compete with production.
**Cons:**
- One more machine to patch and monitor before the idea has shown value.
**Why rejected:** Deferred, not rejected. D3.5 keeps the move to a launcher and configuration change, and D11 gives the owner the evidence to decide.

### Option D: One long-lived agent container for all jobs
**Description:** Run `opencode serve` in one container and give each job a folder and a session.
**Pros:**
- No container start for each job.
**Cons:**
- OpenCode sessions, caches and processes survive from one job to the next.
- The server password lives for the life of the container.
**Why rejected:** A job must not see what an earlier job left. A fresh container for each job gives that by construction.

### Option E: Put the agent on `cloud-sim`, as `run_python(network=true)` does
**Description:** Reuse the existing network and sandbox call.
**Pros:**
- No new network or proxy.
**Cons:**
- The container can reach Postgres, Neo4j, Elasticsearch and Redis.
- It can reach `caddy:8601`, which reads every user's artifacts.
- It has open internet egress.
**Why rejected:** The blast radius is the whole production data plane.

### Option F: Give the agent the Scaleway key, or use the cost gate's windows for the caps
**Description:** Put the vendor key in the container and rely on Scaleway billing limits, or enforce the caps with `CostGate` alone.
**Pros:**
- No metering route and no new counters to build.
**Cons:**
- No hard spend cap at Scaleway was confirmed. Their billing is postpaid.
- The model can read the key through its shell.
- The cost gate has no monthly window, keys no counter by job, and expires reservations after 180 seconds.
**Why rejected:** The cap must sit outside the agent, hold under parallel calls, and survive long calls.

### Option G: Self-hosted Forgejo for the repos
**Description:** Run a git server on the VPS.
**Pros:**
- Everything stays on our host.
**Cons:**
- One more service to run and back up.
- Every repo uses the production disk.
**Why rejected:** The owner chose a private GitHub organization.

### Option I: Give the agent a repo-scoped GitHub token, and let it push
**Description:** The agent pushes its own branch with a short-lived installation token.
**Pros:**
- No handoff container.
**Cons:**
- An installation token cannot be limited to one branch. The agent can rewrite or delete `main` before the token ends.
- Branch rules that stop it on private repos need a paid GitHub plan, and a second App identity.
- The agent needs GitHub egress.
**Why rejected:** The handoff of D8.8 keeps every GitHub write with the gateway, at no plan cost, and removes a credential from the container.

### Option H: Let the model decide that the owner asked directly, and skip the card
**Description:** The primary sets a flag such as `owner_requested=true`.
**Pros:**
- One tap less for a direct request.
**Cons:**
- The gate is then under the model's control.
**Why rejected:** A consent gate that the gated party can open is not a gate. D10.4 keeps the later direct path deterministic.

---

## Consequences

### Positive Consequences
- Seshat gets a write-test-fix loop for pages, which neither `artifact_draft` nor the primary has.
- The agent holds no production credential and no GitHub credential. Its network reaches only npm and one broker route.
- Every model call is metered on our side, with hard per-job and monthly caps that hold under parallel calls.
- Each delivered page has a repo and a commit, so a fix continues from history instead of from HTML alone.
- A check that the model does not control operates the page before it is published.
- The `DelegationExecutorProtocol` gets its first real implementation, and three dead adapters leave the tree.

### Negative Consequences
- New moving parts: the broker, the egress proxy, the agent, handoff and check images, two new tables and a job-spend table, a supervisor, a GitHub organization and App, a PWA job card.
- A new vendor (Scaleway) and a new budget role.
- The broker builds an OpenAI-compatible streaming route, which Seshat does not have today.
- The permission layer and `artifact_write_executor` both change in small ways (D10.3, D9.7).
- A job takes minutes, so the owner waits for a card instead of an answer in the turn.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Container escape on the production host | High | The D3.3 boundary. D3.5 keeps the move to a separate VPS a launcher change. |
| A compromised broker reaches production | High | No Docker socket, a restricted database role, an environment allowlist, interface-bound listeners (D4.3, D4.4). |
| The model misuses a job credential | Medium | Each credential grants only what the job may do anyway, and ends with the job (D6). Only the gateway writes to GitHub (D8.2). |
| A job uses up memory with no swap | Medium | 3 GB memory cap, one job at a time (D3, D9.3). |
| A preview model changes or disappears at Scaleway | Medium | The model is configuration (D5.12). The job record stores the model id. |
| The CSP mirror drifts from the Worker | Medium | The existing `verify-envelope` probe catches drift. The check records the manifest digest (D7.5). |
| Disk fills with workspaces and images | Low | The workspace is deleted at every end state (D3.4). The repos live at GitHub. |

---

## Implementation Notes

**Order of work (one phase per PR):**
1. The `dev-agent` network, the egress proxy, the agent image (OpenCode pinned, Chromium, Playwright, CSP server), the handoff image and the check image.
2. The broker: the two listeners, the model route with the job token table, the job and month counters, `dev_job_calls`, the 150-second call limit, the cost-gate role `dev_agent`, the control relay, the restricted database role.
3. The GitHub organization and App (the owner creates both), repo creation, the workspace clone, the handoff container and bundle import, the template repo with `AGENTS.md` and the test setup.
4. `dev_jobs` (in `docker/postgres/init.sql` and a migration), the supervisor with lease and recovery, `OpenCodeAdapter`, the launcher, the handoff, the publish check, idempotent delivery.
5. The native tool, the durable approval decision, its `permissions.yaml` ask rule and `tools.yaml` entry, and the deletion of the three old adapters.
6. The job events and the PWA job card.
7. The paid evaluation of D11, with a preflight.

**Prerequisites outside this repo:**
- The owner creates the Scaleway account and key, and puts the key in the secrets store.
- The owner creates the GitHub organization and installs the App.
- Master prunes the Docker build cache and unused images before the first job.

---

## Verification / Acceptance Criteria

These criteria are adjudicated on the umbrella ticket FRE-1679, after the implementation chain lands and deploys. They are not adjudicated at the merge of this ADR. Each criterion that inspects successful jobs first requires that such jobs exist, so an empty run cannot pass.

- **AC-1 — No job starts without its own approval.** Every `dev_jobs` row carries an approval request id. That id matches one persisted decision `approve` for the same session and tool. **Check:** SQL join of `dev_jobs` to the persisted decision events on the request id. Seeded negatives: a denied card, a card left to time out, and a second job submitted with an approval id that was already used. None creates a job row, an agent container or a model call. *Fails if* any row lacks a matching `approve`, or a seeded negative creates a row, a container or a `dev_job_calls` row.
- **AC-2 — Spend never passes a cap, also under parallel calls.** At every point, a job's committed plus reserved spend is at most the €2 equivalent, and a UTC month's is at most the €30 equivalent. For each job, the count of broker-dispatched calls equals its `dev_job_calls` rows. **Check:** SQL over `dev_job_calls` and the counters. Seeded tests: (a) a job with its cap set to the €0.05 equivalent receives ten parallel calls, and the admitted reservations sum to at most the cap; (b) a call whose usage is missing ends the job as `cost_unknown` and keeps its worst-case reservation; (c) with a fake clock, spend reserved at 23:59 UTC on the last day of a month counts in that month, and the next month starts at zero; (d) a call held open past 150 seconds is ended and settled at its worst case before its cost-gate reservation expires. *Fails if* any sum passes its cap, a seeded test admits more than its cap, a month boundary is wrong, a cost-gate reservation of the role expires while active, or a call has no ledger row.
- **AC-3 — The agent container cannot reach production, GitHub or an earlier job.** A probe runs inside a live agent container and reports:
  - connection failure for Postgres, Neo4j, Elasticsearch, Redis, `caddy:8600`, `caddy:8601`, the gateway, the broker's control listener, the host, `169.254.169.254`, `github.com`, an IP literal, an IPv6 address, a host off the allowlist, and an allowlisted host on another port.
  - success for `registry.npmjs.org` through the proxy.
  - an environment with exactly the D6 credentials, the proxy variables and the declared runtime variables, and no file of an earlier job in any writable path or in OpenCode's state.
  A port scan of the broker from each network finds only the listener of D4.3 for that network. The control relay refuses a target other than the current job's container. The broker's database role is refused on `dev_jobs`, `artifacts` and `users`. `docker inspect` of the agent, handoff and check containers shows the D3.3, D7.3 and D8.8 flags and limits, and no socket or host mount. A seeded agent commit that rewrites `main` in the workspace does not change `main` on GitHub. **Check:** a committed probe script and inspect report, run on every image change and on two consecutive jobs. *Fails if* any forbidden target answers, any extra variable or earlier-job file is present, a refused probe succeeds, a flag, limit or mount differs, or `main` changes outside D8.9.
- **AC-4 — The published page is the committed page, once.** At least one build job succeeded. For every `succeeded` job, the SHA-256 of the artifact bytes in R2 equals that of `dist/index.html` at the result commit, and the result commit is the head that the gateway pushed to `job/<job-id>` from the handoff bundle. A seeded crash at each of four points (before upload, after upload, after the row insert, before the end state) followed by a retry leaves exactly one R2 object and one `artifacts` row. **Check:** a script over all succeeded jobs, and the seeded crash test. *Fails if* no build succeeded, a hash pair differs, or a crash leaves a duplicate or an orphan.
- **AC-5 — A fix continues the right history, and imports only the owner's page.** At least one fix of an agent-built page succeeded. For each such fix, the result commit descends from the source job's result commit in the same repo. A fix of a page that the agent did not build, larger than 256 KB, succeeds, and the SHA-256 of `dist/index.html` at its base commit equals that of the source artifact's R2 bytes. A fix request with another user's artifact id is refused, and no repo is created. **Check:** the GitHub compare API per fix job, and the two seeded requests. *Fails if* no such fix succeeded, a history check fails, the large page fails to import, or the cross-user request creates anything.
- **AC-6 — The publish check stops broken pages and passes good ones.** Seeded pages: one that throws only when a button is clicked, one that calls `fetch()`, and one whose repo has no test. Each ends as `failed_check`, with no artifact row. A known-good page that uses the Chart.js toolkit passes. A mutation test breaks the asserted interaction in a passing repo, and that repo's tests then fail. Every check result carries the three digests of D7.5. **Check:** the seeded jobs' rows and the `artifacts` table. *Fails if* a broken page is published, the good page fails, the mutated repo's tests still pass, or a digest is missing.
- **AC-7 — The job outlives the turn and the gateway.** The turn that starts a job ends within 10 seconds after approval. The job's end state reaches a client that stays connected, and a client that connects after the end. A gateway restart in the middle of a job, made just after a lease renewal, ends that job as `failed` with `supervisor_restart` once its lease expires. After that, its container is gone, its job token is revoked, and its end state reaches the session. **Check:** the turn trace, `session_events`, `docker ps`, the job token table, and the seeded restart. *Fails if* the turn waits for the job, a client misses the end state, or the restarted job is left running or without an end state.
- **AC-8 — One job at a time, and the limits end a job cleanly.** A second request while a job is active is refused, with no row and no container. A seeded job that passes 30 minutes ends as `timed_out`. A seeded job that reaches the month cap is refused at creation. Each seeded end leaves no container and no `active` job token. **Check:** the seeded requests, `docker ps` and the job token table. *Fails if* two jobs run at once, or a seeded end leaves a container or an active token.
- **AC-9 — The evaluation is complete and decided.** The D11 report on FRE-1679 lists, for each model and each of three runs: the job id, the check result, the owner's blind rating, the spend and the duration. It also records the owner's dated decision on continuing and on the move to a VPS. **Check:** the report and the decision comment. *Fails if* a model, a run, a rating or the decision is missing. This criterion sets no pass mark for the agent's quality. That judgement is the owner's.
- **AC-10 — Production keeps its memory.** During the evaluation window, Docker's retained events show no OOM kill for any container, including the agent and check containers. **Check:** `docker events --filter event=oom` for the window, and the kernel log. *Fails if* any OOM kill appears.

**Coverage of the decision by the criteria:**

| Decision | Criteria |
|---|---|
| D1 scope, `dist/` contract | AC-4, AC-6 |
| D2 OpenCode pinned, adapter, permission file | AC-3 (inspect report records the image digest and OpenCode version), AC-4 |
| D3 per-job container and its boundary | AC-3, AC-10 |
| D4 network, broker listeners, egress proxy | AC-3 |
| D5 metering, caps, ledger | AC-2, AC-8 |
| D6 credentials | AC-3, AC-7, AC-8 |
| D7 publish check | AC-6 |
| D8 repos, branch, handoff, import | AC-3, AC-4, AC-5 |
| D9 job record, supervisor, recovery, delivery | AC-4, AC-7, AC-8 |
| D10 consent, durable approval, async card | AC-1, AC-7 |
| D11 evaluation | AC-9 |

---

## References

- FRE-1679 — the owner's request and the decision log (comments of 2026-10-24)
- [ADR-0050](ADR-0050-remote-agent-harness-integration.md) — Remote Agent Harness Integration (Accepted). D4 and D6 superseded by this ADR.
- [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) — Primitive tools and action-boundary governance, Amendment A (approval fails closed)
- [ADR-0065](ADR-0065-cost-check-gate.md) — Cost check gate (the role `dev_agent` joins its daily and weekly caps)
- [ADR-0069](ADR-0069-r2-backed-artifact-substrate.md) — R2-backed artifact substrate
- [ADR-0077](ADR-0077-artifact-draft-subagent-generation.md) — Artifact draft (Implemented). Not changed. Its edit mode is not built.
- [ADR-0089](ADR-0089-artifact-execution-security-model.md) — Artifact execution security model (the sealed box, the curated toolkit)
- [ADR-0132](ADR-0132-outbound-authenticated-egress.md) — Outbound authenticated egress, D2 (credential custody: a provider key stays in the application)
- [ADR-0141](ADR-0141-one-llm-dispatch-path.md) — One LLM dispatch path, D1 and D2 (every call rides `LiteLLMClient` with the egress guard)
- [ADR-0156](ADR-0156-skills-and-tools-follow-the-industry-standard.md) — Skills and tools follow the industry standard, D8 (deny/ask/allow)
- `docs/research/2026-10-24-fre-1667-opencode-context-and-prompts.md` — OpenCode study (compaction, prompts, skills)
- OpenCode docs at upstream commit `5e4aa55c` (2026-10-24): `cli/commands.mdx` (`serve`, `run`), `cli/web.mdx` (server password), `permissions.mdx` (unmatched rule asks), `providers.mdx` (OpenAI-compatible `baseURL`), `network.mdx` (`HTTPS_PROXY`), `instructions.mdx` (`AGENTS.md`), `build/client/effect.mdx` (event stream)
- [GitHub: best practices for creating a GitHub App](https://docs.github.com/en/enterprise-cloud%40latest/apps/creating-github-apps/about-creating-a-github-app/best-practices-for-creating-a-github-app) — installation tokens are repository-scoped, not branch-scoped (Option I)
- [Scaleway Generative APIs pricing](https://www.scaleway.com/en/pricing/model-as-a-service/), [supported models](https://www.scaleway.com/en/docs/generative-apis/reference-content/supported-models/) and [data privacy](https://www.scaleway.com/en/docs/generative-apis/reference-content/data-privacy/)
- [OVHcloud VPS range](https://www.ovhcloud.com/fr/vps/) — prices for Option C

---

## Status Updates

### 2026-10-24 - Proposed
**Changed By:** adr seat (Opus), after the owner's go ("write it up") on FRE-1679
**Reason:** Discussion with the owner completed. Decisions recorded on FRE-1679.

---

**Template Version:** 1.1
