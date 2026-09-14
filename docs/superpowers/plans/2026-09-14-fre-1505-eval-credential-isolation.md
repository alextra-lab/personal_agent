# FRE-1505 — Eval gateway credential isolation

Ticket: FRE-1505 (Urgent, Approved 2026-09-14). Related: FRE-1372, FRE-375.

## Problem (verified in code and in the running image)

- `seshat-gateway-treatment` runs `AGENT_PRIMITIVE_TOOLS_ENABLED=true` and
  `AGENT_APPROVAL_UI_ENABLED=false`. `cat`, `env` and `curl` are on the NORMAL auto-approve list.
- `bash_executor` spawns `/bin/bash` with no `env=`, so the child inherits every secret.
- The gateway image runs as root. PID 1 is `uv run uvicorn …` and holds the full environment.
  The Python app is a child process (PID 27 in the live cloud container).
- `/proc/<pid>/environ` access is a ptrace check. A same-uid reader passes unless the target is
  non-dumpable and the reader lacks `CAP_SYS_PTRACE`. The container does not hold
  `CAP_SYS_PTRACE` (`CapEff 00000000a80425fb`, bit 19 clear).
- `create_linear_issue` checks `ctx.eval_mode` (a per-request flag). `create_linear_project`,
  `find_linear_issues` and `list_linear_projects` have no eval guard. Nothing keys on
  `deployment_profile`.

Experiment (gateway image, root, PID 1 python, seeded `AGENT_FAKE_API_KEY`):
before `prctl(PR_SET_DUMPABLE, 0)` a bash child finds the seeded value in `/proc/1/environ`.
After it, the read fails with `Permission denied`. `/proc/1/mem` also fails.

## Mechanism

All changes are keyed on `settings.deployment_profile == "eval"`. Production (`cloud`, `local`)
behaviour does not change.

1. **Child environment allowlist.** In eval, `bash_executor` passes `env=` built from an
   allowlist of non-secret names: `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TERM`,
   `TMPDIR`, `VIRTUAL_ENV`. An allowlist, not a denylist, because `AGENT_DATABASE_URL` carries a
   password under a name no suffix pattern matches.
2. **Non-dumpable gateway process.** In eval, when primitive tools register, the process calls
   `prctl(PR_SET_DUMPABLE, 0)` through `ctypes`. A bash child then cannot read the gateway's
   `/proc/<pid>/environ` or `/proc/<pid>/mem`.
3. **Python as PID 1 in eval.** `uv` as PID 1 holds the environment and we cannot make it
   non-dumpable. `docker-compose.eval.yml` sets `command:` on the eval base to run
   `/app/.venv/bin/uvicorn` directly, and sets the treatment `PATH` to the same value `uv run`
   produces (`/app/.venv/bin:` prefix), so bash children see the same binaries.
4. **Fail-closed startup probe.** In eval, after step 2, registration runs the real child path
   once: `cat /proc/[0-9]*/environ; printenv`. If any value of an environment variable named
   `*_KEY`, `*_TOKEN`, `*_SECRET` or `*_PASSWORD` (value length ≥ 8) appears in the output, the
   `bash` tool is not registered and an error is logged. This catches a reverted `command:`,
   an added `CAP_SYS_PTRACE`, or a failed `prctl`.
5. **Linear eval guard.** `_gql` raises `ToolExecutionError` before any HTTP client is built when
   `deployment_profile == "eval"`. All four Linear tools route through `_gql`, so none sends a
   request to `api.linear.app` from eval.

### Rejected alternatives

- **Drop the bash child to an unprivileged uid.** Blocks `/proc` for every root process,
  including short-lived siblings. Rejected: the eval arm then cannot write
  `/app/agent_workspace`, which breaks parity with production bash (root) for the arm under
  measurement.
- **Delete secrets from `os.environ` after settings load.** Does not change the kernel's initial
  environ block (`/proc/self/environ`), and any later `AppConfig()` re-read loses values.
- **Eval-scoped credentials.** Needs owner-side key provisioning. It does not close AC-1 for the
  Anthropic and OpenAI keys the eval stack needs.

### Residual risk (stated, not closed)

- A gateway-spawned child that inherits the full environment (`mmdc` from `artifact_tools`, git
  from `captains_log`, the Claude Code delegation adapter) is dumpable while it runs. A
  concurrent bash call can read it during that window. The startup probe does not see it.
- The `read` primitive runs in-process and can read `/proc/self/environ` if governance allows it.
  `tools.yaml` forbids `/proc/**` for `read` and `write`, and `read` resolves the path first.

## Steps

1. Write failing tests → verify: each fails for the expected reason.
   - `tests/test_tools/test_eval_credential_isolation.py`
     - AC-1: a subprocess (`python -c`) with seeded `AGENT_SEEDED_API_KEY`, `SEEDED_TOKEN`,
       `SEEDED_SECRET`, `SEEDED_PASSWORD` and profile `eval` calls the harden helper, then
       `bash_executor("printenv; cat /proc/$PPID/environ")`. Assert no seeded value in stdout.
       (`$PPID` is the gateway-equivalent process. In CI it is not PID 1.)
     - AC-3: same subprocess, `bash_executor("echo ok")` → `stdout == "ok\n"`, `success`.
     - AC-4: profile `cloud` → child `printenv` still shows the seeded value; process stays
       dumpable; `bash` registers.
     - Probe: a seeded leak (dumpable not set, parent env readable) → `bash` not registered.
   - `tests/test_tools/test_linear.py`
     - AC-2: profile `eval`, key set → `create_linear_issue` and `create_linear_project` raise
       and the HTTP client factory is never called.
     - AC-4: profile `cloud` → the existing success tests still pass (unchanged).
   - `tests/personal_agent/config/test_docker_compose_eval_yaml.py`: eval base `command` starts
     with `/app/.venv/bin/uvicorn`; treatment `PATH` starts with `/app/.venv/bin:`.
2. Add `src/personal_agent/tools/primitives/credential_isolation.py`: `eval_child_env()`,
   `make_process_non_dumpable()`, `credential_leak_probe()`.
3. Wire `bash_executor` (`env=`), `register_mvp_tools` (harden + probe in eval), `_gql` (guard).
4. Edit `docker-compose.eval.yml`.
5. Container probe (evidence, not committed): build `seshat-gateway:fre1505` (never `:latest`),
   run with seeded fake keys as PID 1, call `bash_executor("printenv; cat /proc/1/environ")`.
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | unit test (unprivileged, `$PPID`) + container probe (root, `/proc/1`) |
| AC-2 | `test_linear.py` eval-profile tests: no client built |
| AC-3 | unit test `echo ok` under eval hardening |
| AC-4 | no production change: every change is keyed on `deployment_profile == "eval"`; cloud-profile tests show bash env pass-through and Linear requests unchanged |

## Post-deploy

Rebuild of the eval image only (`make eval-infra-up` path). The production gateway code path does
not change behaviour, but the image is shared, so a gateway rebuild ships inert code to production.
