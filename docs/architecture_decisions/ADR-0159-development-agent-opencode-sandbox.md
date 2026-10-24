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
| Artifact write path | `artifact_write_executor(slug, content_type, content, title, summary, tags, ctx)`. It needs `ctx.user_id`. Each write is a new row. The `artifacts` table has no metadata, version or parent column. | `tools/artifact_tools.py:418-641`, `docker/postgres/init.sql:437-454` |
| Production artifact CSP | Lives in the Cloudflare Worker (`personal_agent_secrets`). The in-repo mirror is `expected_csp_directives()`. A Playwright server already serves pages with that exact CSP. | `observability/artifact_envelope/spec.py:24-56`, `e2e/artifact-lib/csp-server.ts:74-79` |
| Approval card | A tool on `ask` shows a PWA card. Deny, timeout and a lost connection all refuse. An explicit `ask` rule offers "allow once" only. The production flag `AGENT_APPROVAL_UI_ENABLED` is true. | ADR-0063 Amendment A, ADR-0156 D8, `tools/executor.py:366-580`, `config/governance/permissions.yaml` |
| Cost gate | `CostGate.reserve/commit/refund` by budget role. Windows are daily and weekly only. No per-job and no monthly cap exists. | `cost_gate/gate.py:57-354`, `cost_gate/types.py:24-28` |
| OpenAI-compatible endpoint for paid models | None. LiteLLM runs in-process only. | `service/app.py` |
| Work that outlives a turn | No job mechanism. `_push_event(event, session_id)` can push a persisted event to a session outside a turn. | `transport/agui/transport.py:107-160` |

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

This ADR gives Seshat a development agent. Seshat hands it a build or a fix. The agent works in its own git repo, tests the page in a browser, and Seshat publishes the result through the existing artifact path.

### D1 — Scope of v0

1. v0 does one kind of job: **build or fix an interactive artifact**. A job has one of two modes, `build` or `fix`.
2. v0 delivers one sealed HTML page per job through `artifact_write`, under the ADR-0089 sealed box.
3. The job contract keeps larger outputs possible (owner's choice). The agent writes its deliverable into a **build-output folder**, `dist/`, in the repo. v0 publishes `dist/index.html` only. A later ADR decides where multi-file outputs are hosted. It does not need a new contract.
4. Other development work (scripts, services, tools for Seshat) is out of scope for v0.

### D2 — The harness is OpenCode, behind the existing delegation contract

1. The harness is **OpenCode 2.x**, pinned to one version in the agent image. Owner's reason: it keeps Seshat's models separate from the owner's own coding agent (Claude Code), and it teaches us a second harness.
2. A new adapter, `OpenCodeAdapter`, implements `DelegationExecutorProtocol`. The protocol stays. The three unused adapters (`claude_code.py`, `codex.py`, `generic_mcp.py`) are deleted. A later adapter can still be added behind the same protocol.
3. The adapter drives OpenCode through its HTTP server (`opencode serve`, password-protected). It follows progress on the server's event stream.
4. Every job runs with an explicit OpenCode permission file. OpenCode asks a client when no rule matches, and a headless job has no client to answer. The file allows reads, edits and shell inside the job workspace, and denies everything outside it. It also denies web search and web fetch.
5. Each repo carries an `AGENTS.md` that states the test loop (D7) as a requirement.

### D3 — Placement: a container now, a separate VPS if the test shows value

1. v0 runs the agent as one long-lived container, `seshat-dev-agent`, on the current VPS. It runs one job at a time. Each job gets its own workspace folder and OpenCode session. The folder is deleted after the push.
2. Container limits: memory cap 3 GB, 2 CPUs, non-root user, `--cap-drop ALL`, `no-new-privileges`, read-only root with a writable workspace volume. The container never gets the Docker socket.
3. The container is restarted after a job that ends in a timeout, a cap stop or an error. This gives the next job a clean process.
4. Seshat talks to the agent only through HTTP endpoints: the OpenCode server, and the model route of D5. A move to a separate VPS changes base URLs and the link between hosts, not the code. The decision to move belongs to the owner, with the evaluation of D11 as input.

### D4 — Network isolation

1. The agent container is **not** on `cloud-sim`. It is on a new Docker network, `dev-agent`, with `internal: true`. That network has no route to the internet, to the data stores, or to Caddy.
2. Two other containers join `dev-agent`. No other container joins it:
   - **The broker**, `seshat-dev-broker`: a small Seshat service built from the gateway image. On `dev-agent` it exposes only the model route of D5. On `cloud-sim` it exposes a control API that the gateway calls to run a job. The gateway itself never joins `dev-agent`, so the agent cannot reach the gateway's API.
   - **The egress proxy**: a forward proxy that also joins an external network. It allows only `github.com`, `api.github.com`, `codeload.github.com` and `registry.npmjs.org`. OpenCode and git use it through `HTTPS_PROXY`.
3. The curated toolkit (`/lib/`, ADR-0089 Addendum A) is mounted read-only into the container. The agent does not need the internet to test a page.

### D5 — Model access through a metering route that Seshat owns

1. The agent holds **no model vendor key**. Its only model endpoint is the broker's OpenAI-compatible route. The route supports streaming.
2. The broker holds the Scaleway key. Each request carries a **job token**: a random token that the broker issues when a job starts and revokes when the job ends.
3. The broker reserves each call on the cost gate under a new budget role, `dev_agent`. Before each call it refuses the request if one of these holds:
   - the job's committed spend plus this call's worst-case reservation exceeds **€2**;
   - the `dev_agent` spend this calendar month plus this call's worst-case reservation exceeds **€30**.
4. These two limits are the owner's numbers (FRE-1679, 2026-10-24). They are configured as their equivalents in the ledger currency (USD), with the conversion factor stated in config.
5. The cost gate gains a `monthly` time window for the €30 cap. This amends ADR-0065 by one window value. The per-job limit uses the reservation's `task_id`, set to the job id.
6. Each call is written to `api_costs` with the job's `trace_id` and `session_id` and `purpose="dev_agent"`.
7. Vendor and models (owner's choice): **Scaleway Generative APIs**, Paris. The default model is `deepseek-v4.1-flash`, which has vision and a cached-input price. The alternatives are `glm-5.3` and `qwen3.8-27b`. The model is a configuration value, not a tool argument. Scaleway marks all three as preview models, so the job record stores the model id.

### D6 — Secrets in the agent container

The container holds exactly three credentials. Each one is scoped to one job:

| Credential | Scope | Lifetime |
|---|---|---|
| Job token for the model route | One job, capped by D5 | Revoked when the job ends |
| GitHub App installation token | One repository, `contents: write` only | One hour or the end of the job |
| OpenCode server password | Read by the broker only | Rotated when the container restarts |

The container holds no Scaleway key, no Anthropic key, no R2 credential, no database URL and no `.env`.

### D7 — The test loop and the publish check

1. The agent image contains headless Chromium, Playwright and the CSP test server from `e2e/artifact-lib/csp-server.ts`. That server sends the CSP from `expected_csp_directives()`, so the agent tests the page under the production policy.
2. `AGENTS.md` requires the loop: open the page, operate every widget, read console errors and CSP violations, fix, repeat. The agent commits its tests into the repo with the page.
3. Before Seshat publishes, the broker runs a **publish check** that the model does not control. It loads `dist/index.html` from the committed result in a fresh container with no network, under the same CSP. It requires zero console errors and zero CSP violations.
4. A page that fails the check is not published. The job ends as `failed_check`, and the job card shows the repo link and the check output.
5. A page that passes is published directly, with no owner approval step (owner's choice for v0).

### D8 — Private git projects

1. The owner creates a new private GitHub organization, separate from `alextra-lab`. A GitHub App is installed only on that organization.
2. The broker holds the App's private key. It creates each repo, and it mints the per-job token of D6. The agent cannot create repos, and it cannot reach any repo outside its job.
3. Repo name: `<artifact-slug>-<short-job-id>`. Repos are kept. v0 deletes none.
4. **Build mode** creates a new repo from a template that holds `AGENTS.md`, the test setup and an empty `dist/`.
5. **Fix mode, page built by the agent:** the job continues that page's repo from the recorded commit (owner's choice). The broker finds the repo and the commit through the job record of D9.
6. **Fix mode, page not built by the agent:** the broker reads the page from R2, creates a new repo from the template, and commits the page as `dist/index.html` before the agent starts.

### D9 — Job record and delivery

1. A new Postgres table, `dev_jobs`, holds one row per job. Columns: id, user id, session id, trace id, mode, task text, source artifact id, repo, base commit, result commit, result artifact id, model id, state, spend, start and end times, failure reason.
2. Job states: `queued`, `running`, `checking`, `succeeded`, `failed`, `failed_check`, `cap_reached`, `timed_out`, `cancelled`.
3. The `artifacts` table does not change. Provenance and lineage live in `dev_jobs`: a fix job's `source_artifact_id` points to the old page, and its `result_artifact_id` points to the new one.
4. The broker reads `dist/index.html` from GitHub **at the result commit**. It never reads from the container's working tree. It publishes through `artifact_write_executor` with the job's user id and session id.
5. The wall-clock limit for a job is 30 minutes.

### D10 — Consent, trigger and the user experience

1. A new native tool starts a job. The primary calls it with the task, the mode, and for a fix the artifact id.
2. `config/governance/permissions.yaml` puts the tool on an explicit `ask` rule. **Every job shows the approval card in v0**, including when the owner asked directly (owner's decision). The card offers "allow once" only (ADR-0156 D8.3). Deny, timeout and a lost connection refuse (ADR-0063 Amendment A).
3. The model never decides that the owner "asked directly". A later change can add a direct path that code recognizes, for example a command or a PWA button, and that path can skip the card. v0 has no such path.
4. After approval, the tool creates the job and returns at once. The turn ends with "build started". If a job is running already, or the month cap is reached, the tool refuses and says why.
5. The broker pushes job events to the session with `_push_event(event, session_id)`. The PWA shows a job card with the state, the latest step from the OpenCode event stream, the spend, and at the end the artifact card or the failure.
6. The gateway DELEGATE route stays off. This ADR does not use it.

### D11 — The first evaluation decides whether the work continues

1. The first paid run uses the trace 8e60be11 fix as its task. It runs each of the three models three times. The repeats show the noise. Today's path is the baseline, and it failed on this task.
2. The run follows the paid-run rule: a preflight on the ticket, a check of the Scaleway balance, and the owner's recorded go.
3. The owner rates each delivered page blind, as working or not working.
4. The result goes to the owner, who decides whether to continue and whether to move the agent to a separate VPS (D3.4). This ADR sets no automatic threshold for that decision.

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
**Why rejected:** Deferred, not rejected. D3 keeps the move to a configuration change, and D11 gives the owner the evidence to decide.

### Option D: Put the agent on `cloud-sim`, as `run_python(network=true)` does
**Description:** Reuse the existing network and sandbox call.
**Pros:**
- No new network or proxy.
**Cons:**
- The container can reach Postgres, Neo4j, Elasticsearch and Redis.
- It can reach `caddy:8601`, which reads every user's artifacts.
- It has open internet egress.
**Why rejected:** The blast radius is the whole production data plane.

### Option E: Give the agent the Scaleway key, and cap spend at the vendor
**Description:** Put the vendor key in the container and rely on Scaleway billing limits.
**Pros:**
- No metering route to build.
**Cons:**
- No hard spend cap at Scaleway was confirmed. Their billing is postpaid.
- The model can read the key through its shell.
- Spend does not reach `api_costs`.
**Why rejected:** The cap must sit outside the agent, and the key must not be in the container.

### Option F: Self-hosted Forgejo for the repos
**Description:** Run a git server on the VPS.
**Pros:**
- Everything stays on our host.
**Cons:**
- One more service to run and back up.
- Every repo uses the production disk.
- The owner cannot easily continue a project from anywhere.
**Why rejected:** The owner chose a private GitHub organization.

### Option G: Let the model decide that the owner asked directly, and skip the card
**Description:** The primary sets a flag such as `owner_requested=true`.
**Pros:**
- One tap less for a direct request.
**Cons:**
- The gate is then under the model's control.
**Why rejected:** A consent gate that the gated party can open is not a gate. D10.3 keeps the later direct path deterministic.

---

## Consequences

### Positive Consequences
- Seshat gets a write-test-fix loop for pages, which neither `artifact_draft` nor the primary has.
- The agent holds no production credential, and its network reaches only GitHub, npm and the broker.
- Every model call is metered on our side, with hard per-job and monthly caps, and lands in `api_costs`.
- Each delivered page has a repo and a commit, so a fix continues from history instead of from HTML alone.
- The `DelegationExecutorProtocol` gets its first real implementation, and three dead adapters leave the tree.

### Negative Consequences
- New moving parts: the broker, the egress proxy, the agent container, a `dev_jobs` table, a GitHub organization and App, a PWA job card.
- A new vendor (Scaleway) and a new budget role.
- The broker builds an OpenAI-compatible streaming route, which Seshat does not have today.
- The cost gate gains a monthly window, which amends ADR-0065.
- A job takes minutes, so the owner waits for a card instead of an answer in the turn.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Container escape on the production host | High | Non-root, `--cap-drop ALL`, `no-new-privileges`, no Docker socket, internal network. D3.4 keeps the move to a separate VPS cheap. |
| The model leaks the GitHub token or the job token | Medium | Each token is scoped to one repo or one job and expires at the end of the job (D6). |
| A job uses up memory with no swap | Medium | 3 GB memory cap, one job at a time (D3). |
| A preview model changes or disappears at Scaleway | Medium | The model is configuration (D5.7). The job record stores the model id. |
| The CSP mirror drifts from the Worker | Medium | The existing `verify-envelope` probe catches drift. The publish check uses the same mirror (D7.3). |
| Disk fills with repos and packages | Low | The workspace is deleted after the push (D3.1). The repos live at GitHub. |
| The approval card times out after 60 seconds | Low | A timeout refuses, and nothing starts (D10.2). |

---

## Implementation Notes

**Order of work (one phase per PR):**
1. The `dev-agent` network, the egress proxy and the agent image (OpenCode pinned, Chromium, Playwright, CSP server, toolkit mount).
2. The broker: the metering route (streaming, job tokens, cost gate role `dev_agent`, the monthly window and the per-job limit) and the control API.
3. The GitHub organization and App (the owner creates both), repo creation, per-job tokens, the template repo with `AGENTS.md`.
4. `dev_jobs` (in `docker/postgres/init.sql` and a migration), `OpenCodeAdapter`, the publish check, and delivery through `artifact_write_executor`.
5. The native tool, its `permissions.yaml` ask rule, its `tools.yaml` entry, and the deletion of the three old adapters.
6. The job events and the PWA job card.
7. The paid evaluation of D11, with a preflight.

**Prerequisites outside this repo:**
- The owner creates the Scaleway account and key, and puts the key in the secrets store.
- The owner creates the GitHub organization and installs the App.
- Master prunes the Docker build cache and unused images before the first job.

---

## Verification / Acceptance Criteria

These criteria are adjudicated on the umbrella ticket FRE-1679, after the implementation chain lands and deploys. They are not adjudicated at the merge of this ADR.

- **AC-1 — No job starts without a recorded approval.** Every `dev_jobs` row has a matching approval outcome `approve` for its tool call. **Check:** join `dev_jobs` to the approval events by `trace_id`, over the test phase. A seeded negative: deny one card and let one card time out. Neither creates a `dev_jobs` row, and the broker logs no model call for those traces. *Fails if* any job row exists without an `approve`, or a denied card leads to a model call.
- **AC-2 — Spend never passes a cap.** For every job, the `api_costs` sum for its trace is at most the €2 equivalent. For every calendar month, the `dev_agent` sum is at most the €30 equivalent. **Check:** SQL over `api_costs` grouped by trace and by month. A seeded negative: one job with its cap set to the equivalent of €0.05 ends as `cap_reached`, and the broker refuses its next call. *Fails if* any sum exceeds its cap, or the seeded job keeps running.
- **AC-3 — The agent container cannot reach production.** A probe run inside the container gets a connection failure for Postgres, Neo4j, Elasticsearch, Redis, `caddy:8600`, `caddy:8601`, the gateway, the Docker socket path, and one host that is not on the allowlist. It gets a success for `api.github.com` and `registry.npmjs.org` through the proxy. Its environment holds none of the production secret names from `.env.example`. The job's GitHub token gets a refusal when it pushes to a second repo in the organization. **Check:** a committed probe script, run once per image change. *Fails if* any forbidden target answers, any production secret name is present, or the second push succeeds.
- **AC-4 — The published page is the committed page.** For every `succeeded` job, the SHA-256 of the artifact bytes in R2 equals the SHA-256 of `dist/index.html` at the job's result commit. **Check:** a script that reads both for every succeeded job. *Fails if* one pair differs.
- **AC-5 — A fix continues the repo.** For every `fix` job on a page that the agent built, the result commit is a descendant of the source job's result commit, in the same repo. **Check:** GitHub compare API per fix job. *Fails if* a fix job creates a new repo for an agent-built page, or its history does not contain the earlier commit.
- **AC-6 — The publish check stops a broken page.** A seeded negative: a job whose page makes a `fetch()` call, or throws at load, ends as `failed_check`, and no artifact row is created. **Check:** the seeded job's row state and the `artifacts` table. *Fails if* the broken page is published.
- **AC-7 — The turn does not wait for the job.** The turn that starts a job ends within 10 seconds after approval, and the job's end state reaches the PWA session as a persisted event. **Check:** turn duration from the turn trace, and the `session_events` row for the job's end state. *Fails if* the turn waits for the job, or the end state never reaches the session.
- **AC-8 — The trace 8e60be11 fix works (the D11 evaluation).** The evaluation report states, for each model, how many of three runs passed the publish check, how many the owner rated as working, and the median spend and duration. **Check:** the report on FRE-1679 with the job ids, and the owner's blind ratings. *Fails if* the report lacks a model, a run, or the owner's ratings. The owner decides what the numbers mean for D3.4. This criterion does not set a pass mark for the agent's quality.
- **AC-9 — Production keeps its memory.** During the evaluation window, no production container is killed by the OOM killer, and the agent container's peak memory stays under its 3 GB cap. **Check:** `docker inspect` OOM flags and the kernel log for the window, and the container's peak memory. *Fails if* any production container shows an OOM kill in the window.

---

## References

- FRE-1679 — the owner's request and the decision log (comments of 2026-10-24)
- [ADR-0050](ADR-0050-remote-agent-harness-integration.md) — Remote Agent Harness Integration (Accepted). D4 and D6 superseded by this ADR.
- [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) — Primitive tools and action-boundary governance, Amendment A (approval fails closed)
- [ADR-0065](ADR-0065-cost-check-gate.md) — Cost check gate. Amended by D5.5 (monthly window).
- [ADR-0069](ADR-0069-r2-backed-artifact-substrate.md) — R2-backed artifact substrate
- [ADR-0077](ADR-0077-artifact-draft-subagent-generation.md) — Artifact draft (Implemented). Not changed. Its edit mode is not built.
- [ADR-0089](ADR-0089-artifact-execution-security-model.md) — Artifact execution security model (the sealed box, the curated toolkit)
- [ADR-0156](ADR-0156-skills-and-tools-follow-the-industry-standard.md) — Skills and tools follow the industry standard, D8 (deny/ask/allow)
- `docs/research/2026-10-24-fre-1667-opencode-context-and-prompts.md` — OpenCode study (compaction, prompts, skills)
- OpenCode docs at upstream commit `5e4aa55c` (2026-10-24): `cli/commands.mdx` (`serve`, `run`), `cli/web.mdx` (server password), `permissions.mdx` (unmatched rule asks), `providers.mdx` (OpenAI-compatible `baseURL`), `network.mdx` (`HTTPS_PROXY`), `instructions.mdx` (`AGENTS.md`), `build/client/effect.mdx` (event stream)
- [Scaleway Generative APIs pricing](https://www.scaleway.com/en/pricing/model-as-a-service/) and [data privacy](https://www.scaleway.com/en/docs/generative-apis/reference-content/data-privacy/)
- [OVHcloud VPS range](https://www.ovhcloud.com/fr/vps/) — prices for Option C

---

## Status Updates

### 2026-10-24 - Proposed
**Changed By:** adr seat (Opus), after the owner's go ("write it up") on FRE-1679
**Reason:** Discussion with the owner completed. Decisions recorded on FRE-1679.

---

**Template Version:** 1.1
