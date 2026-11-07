# FRE-1685 — private GitHub repos, workspace clone, sandboxed handoff, gateway-only push

ADR-0159 D8 (all items). Ticket FRE-1685. Blocked-by FRE-1684 (merged). Blocks FRE-1686.

## Scope

- The gateway creates one private repo per page in the private dev-agent organization, with the GitHub App.
- Before the agent container starts, the gateway puts a git clone of the base commit into the workspace, on `job/<job-id>`.
- After the agent, the handoff container writes a bundle of `job/<job-id>`. The gateway verifies it in its own clean mirror and pushes `job/<job-id>` with its own token.
- After publish, the gateway fast-forwards `main` only when the result descends from it. The job row records whether `main` moved.
- A fix of a page that the agent did not build imports the page through an owner-scoped lookup.

Out of scope: the publish check and delivery (FRE-1686), the native tool (FRE-1687), the toolkit volume (FRE-1751).

## Facts checked before the plan (2026-11-07)

| Fact | How it was checked |
|---|---|
| `docker cp -a - <created container>:/workspace` writes into the job's named volume, with the tar's uid/gid, and the agent can write there with a read-only root | Live, `seshat-dev-agent:0.1` |
| `docker cp <stopped container>:/out/job.bundle -` returns a tar with one regular file. `docker start -a` returns the container's exit status | Live, `seshat-dev-handoff:0.1` |
| `git bundle create - <ref>` writes to stdout (git 2.47) | Live |
| `docker volume create` of an existing name exits 0 | Live |
| The gateway image has no `git` | `Dockerfile.gateway` |
| `pyjwt` 2.15 and `cryptography` 50 are dependencies (RS256 signing) | `uv run python -c` |

## GitHub App permissions against each call

The App has `administration: write`, `contents: write`, `metadata: read` (master's check on the ticket, 2026-11-07).

| Call | Needs | Source |
|---|---|---|
| `POST /app/installations/{id}/access_tokens` with `repositories` and `permissions` | App JWT only. The token can be down-scoped. It lasts 1 hour. | GitHub REST docs, "Create an installation access token" |
| `POST /orgs/{org}/repos` (`private: true`, `auto_init: false`) | Administration write | "Permissions required for GitHub Apps", Administration table |
| `GET /repos/{org}/{repo}` | Metadata read | same page, Metadata table |
| `git fetch` / `git push` over HTTPS (`job/<id>`, `main` without force) | Contents write | Not stated on the docs page. The live probe (step 9) proves it. |

So the App's permissions cover every call. No call uses a GitHub template repo, so no extra permission is needed. Each token is scoped down: repo creation gets `administration: write, metadata: read`; git work gets `contents: write, metadata: read` on one named repo.

## Design decisions

1. **The template lives in this repo**, at `config/dev_agent/page_template/` (the gateway image copies `config/`). The gateway creates an empty private repo and pushes the template commit to `main`. Reason: the template is reviewed and tested with the code, and no second GitHub repo needs care. The template commit is the base commit (D8.5).
2. **Repo name.** `<slug>-<first 8 hex of the job id>`. Fix of a non-agent page: the source artifact's slug. Build: `page`. The slug is lower-cased, reduced to `[a-z0-9-]`, and cut to 40 characters. Empty → `page`.
3. **The workspace clone needs no container of its own.** The gateway builds the clone in a local temp dir from its own mirror (trusted data), tars it with uid/gid 10001, and the launcher runs `docker cp -a` into the created agent container before `docker start`. The clone has no remote and a fixed local git identity.
4. **The handoff script never reads the workspace's git config.** It resolves `refs/heads/job/<id>` by reading the loose ref file or `packed-refs` (40 hex only, `.git` must be a real directory), then runs git with `GIT_DIR` set to a new bare repo in `/tmp` and `GIT_OBJECT_DIRECTORY=/workspace/.git/objects`. The `-c` overrides stay as a second layer. The bundle goes to `/out/job.bundle` as before. The gateway reads it with `docker cp ... -` and accepts one regular-file tar member of at most 64 MB.
5. **The gateway's clean mirror** is a new bare repo in a 0700 temp dir for each operation. Git runs with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, `protocol.allow=never` plus `protocol.https.allow=always` and `protocol.file.allow=always`, and `transfer.fsckObjects=true`. The token reaches git only as an `http.extraHeader` through `GIT_CONFIG_COUNT/KEY/VALUE` environment variables, never on a command line or on disk.
6. **The handoff checks** run in the mirror on the bundle's objects only: `merge-base --is-ancestor <base> <head>` (a missing base object fails), and `ls-tree <head> dist/index.html` must show mode `100644` or `100755`, type `blob`, size ≤ 5 MB. Then `git push <head>:refs/heads/job/<id>` without force. The result commit is stored in the same fenced transaction.
7. **`main` fast-forward** (D8.9): fetch `main` and the result, require `is_ancestor(main, result)`, then push `<result>:refs/heads/main` without force, so GitHub also refuses a non-fast-forward. `main == result` counts as moved. Any refusal or error leaves `main`, and the job row gets `main_moved = false`. A new nullable column `dev_jobs.main_moved` (migration 0040) holds it.
8. **Fix source lookup** (D8.6, D8.7), before any GitHub call:
   - agent-built: `dev_jobs` row with `result_artifact_id = source_artifact_id`, the same `user_id`, `state = 'succeeded'`, non-null `repo` and `result_commit`. Base = that result commit, in that repo.
   - otherwise: `artifacts` row with `id`, `user_id`, `type = 'artifact'`, `upload_pending = false`, content type `text/html` (with or without parameters), `size_bytes ≤ 5 MB`. Then `R2ArtifactStore.get`, and the bytes must be ≤ 5 MB.
   - nothing found → `DevJobRefusedError("source_not_found")` → the job ends `failed` with that reason. No repo is created.
9. **Supervisor hooks.** `JobResultPipeline` gets `prepare(job, fence) -> PreparedJob` (called after `running`, before `start_container`) and `advance_main(job, fence) -> None` (called after a successful `deliver`, before `succeeded`). An error in `advance_main` is logged and does not fail the job (the page is published). `NoResultPipeline` gets no-op versions. `DevJob` gains `repo` and `base_commit`.
10. **Wiring.** The gateway builds `GitHubRepoPipeline` when the four GitHub settings are set. Without them, `start_supervisor` logs and returns `None` (as for the control secret). The supervisor stays off by default. `check` and `deliver` still raise `NotImplementedError` until FRE-1686.

## Steps

Each step: failing test first, then code.

1. **Settings** — `src/personal_agent/config/settings.py`: `dev_agent_github_app_id: int | None`, `dev_agent_github_installation_id: int | None`, `dev_agent_github_org: str | None`, `dev_agent_github_app_private_key_b64: str | None` (secret), `dev_agent_github_api_url = "https://api.github.com"`. `.env.example` entries.
2. **GitHub App client** — `src/personal_agent/dev_agent/github_app.py`: `GitHubAppConfig`, `app_jwt()`, `GitHubAppHost` (implements `RepoHost`: `create_private_repo(name)`, `remote(name) -> GitRemote`, `is_private(name)`). Repo name pattern enforced. A 422 "name already exists" reads the repo and requires `private: true` and the org owner. Tests: `tests/personal_agent/dev_agent/test_github_app.py` with `httpx.MockTransport` (JWT claims verified with the public key, token body scopes, create body has `private: true`, existing public repo refused, the token never appears in `repr`).
3. **Git mirror** — `src/personal_agent/dev_agent/git_mirror.py`: `GitMirror` (async context manager) with `commit_template`, `commit_page`, `fetch`, `fetch_bundle`, `is_ancestor`, `page_entry`, `push`, `workspace_tar`. Bytes subprocess runner with a timeout and an output cap. Tests: `test_git_mirror.py`, real git on local bare repos.
4. **Schema** — `docker/postgres/migrations/0040_dev_jobs_main_moved.sql` and `init.sql`. `JobStore`: `repo`, `base_commit` on `DevJob`; static fenced writers `record_repo`, `record_base`, `record_result`, `record_main`.
5. **Launcher** — `start(..., workspace: bytes | None = None)` copies the tar in between create and start. `bundle_workspace(job_id, branch) -> bytes` runs the handoff container (job + scope labels, kind `handoff`, a labelled out volume), bounded by the profile's 300 s, then `docker cp` and tar parse. Tests: `test_launcher.py` with the fake runner.
6. **Handoff script** — `docker/dev-agent/handoff-bundle.sh` per decision 4. `make dev-agent-build`.
7. **Pipeline** — `src/personal_agent/dev_agent/repo_pipeline.py`: `GitHubRepoPipeline(pool, launcher, host, page_reader, template_dir)` with `prepare`, `handoff`, `advance_main`. Every GitHub write and every container run is one `fence.run`. Supervisor hooks per decision 9. Gateway wiring. Tests: `test_repo_pipeline_integration.py` (test stack DB, local bare repos as the host, the real launcher calls faked) and supervisor tests for the hooks.
8. **Live tests** — `test_repo_live.py` (`integration` marker, real Docker): hostile workspace (fsmonitor, a clean/smudge filter with `.gitattributes`, a `pre-commit` and a `post-checkout` hook, each writing a marker into a host directory mounted at `/marker`). Control: plain git in the same container runs them (markers appear). Handoff script: no marker. A workspace whose `main` was rewritten: GitHub-side (local bare) `main` unchanged. Full path: `prepare` → agent container sees the clone on `job/<id>` → a scripted commit → `handoff` → pushed.
9. **Live GitHub probe** — `scripts/verify_dev_agent_github.py`: against the real App and org, create `probe-<8hex>`, push the template, read `private`, push a job branch from a bundle whose workspace rewrote `main`, check `main`, fast-forward, then an older-version result that must leave `main`. JSON report. Run once. The repo stays (v0 deletes none).
10. **Docs** — `docker/dev-agent/README.md` (FRE-1685 section, deploy steps), `Dockerfile.gateway` (`git`).

## Acceptance criteria → proof

| Criterion | Proof |
|---|---|
| A workspace commit that rewrites `main` leaves `main` on GitHub unchanged | `test_repo_live.py::test_main_rewrite_in_workspace_does_not_reach_remote` + probe |
| Malicious `.git/config` (fsmonitor, filter, hook) runs nothing in the handoff, proven by a marker outside the container | `test_repo_live.py::test_hostile_config_runs_nothing` (with the control) |
| A head that does not descend from the base is rejected and not pushed | `test_repo_pipeline_integration.py::test_non_descendant_head_is_not_pushed` |
| A >256 KB non-agent page imports with equal SHA-256 at the base commit | `test_repo_pipeline_integration.py::test_large_page_import_hash` |
| Another user's artifact id creates no repo | `test_repo_pipeline_integration.py::test_other_users_artifact_creates_no_repo` |
| An older-version fix leaves `main` unchanged | `test_repo_pipeline_integration.py::test_older_version_fix_leaves_main` + probe |
| Every created repo is private | `test_github_app.py` (request body and the 422 path) + probe (`GET` reports `private: true`) |

## Test commands

```bash
make test-file FILE=tests/personal_agent/dev_agent/test_github_app.py
make test-file FILE=tests/personal_agent/dev_agent/test_git_mirror.py
uv run python -m pytest -m integration tests/personal_agent/dev_agent/test_repo_pipeline_integration.py -q
uv run python -m pytest -m integration tests/personal_agent/dev_agent/test_repo_live.py -q
uv run python scripts/verify_dev_agent_github.py --output <scratch>/github-probe.json
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
```

## Codex plan review (2026-11-07) and dispositions

| Finding | Disposition |
|---|---|
| High: the new `GIT_DIR` of the handoff has no named ref, so `git bundle create` fails | Fixed in decision 4: after the object id is checked, the script creates `refs/heads/<branch>` in its own temp repo with `update-ref`. The branch name comes from the gateway's argument (checked by the script), never from a workspace file name. |
| High: `main_moved` is wrong after a push with an unclear result, or `NULL` after a failed record | Fixed in decision 7: after a push error, fetch `main` again. `main == result` → `true`. `main` not an ancestor of the result → `false`. Otherwise raise. The supervisor tries `advance_main` twice. If both fail, the job still ends `succeeded` (the page is published) and `main_moved` stays `NULL`, which means "unknown". The job card must show `NULL` as unknown. |
| Medium: a `prepare` refusal would end as `supervisor_error: DevJobRefusedError` | Fixed in decision 9: `_work` catches `DevJobRefusedError` around `prepare` and `handoff`, and ends the job `failed` with its `reason`. Tests cover a missing artifact id and another user's artifact id. |
| Low: no named tests for the D8 edge cases | Added to `test_git_mirror.py`: a symlink anywhere, a symlinked `dist`, a submodule anywhere and at `dist/index.html`, a folder at `dist/index.html`, a head that is a tree, a missing base object, a page over 5 MB. |
| Checked, not defects: Q1 (no agent-controlled config reaches git once the handoff no longer uses `-C /workspace`), Q2 (mode and type checks), Q3 (fetch, check, non-force push is race-safe), Q4 (hook points), Q5 (the owner-scoped lookup) | No change. |

Also changed after the review: the handoff refuses a symlink or a submodule anywhere in the result tree, not only at `dist/index.html`. Reason: a later fix job checks out that tree in the gateway (`workspace_tar`), so every stored result tree must hold plain files only.

## Risks

- The template's Playwright setup must match what FRE-1686's check runs. FRE-1686 owns the check and can change the template.
- `git` in the gateway image also reaches Seshat's `bash` tool. `permissions.yaml` already has `git log/status/diff` rules, and every other git command asks. The mirror dirs are 0700 and hold no token.
- Diff class: escalated (GitHub writes, a schema change, untrusted input).
