# FRE-1505 — Eval gateway credential isolation

Ticket: FRE-1505 (Urgent, Approved 2026-09-14). Related: FRE-1372, FRE-375.

## Problem (verified on `main` before this branch, and in the running image)

- `seshat-gateway-treatment` runs `AGENT_PRIMITIVE_TOOLS_ENABLED=true` and
  `AGENT_APPROVAL_UI_ENABLED=false`. `cat`, `env` and `curl` are on the NORMAL auto-approve list.
- `bash_executor` spawns `/bin/bash` with no `env=`, so the child inherits every secret.
- The gateway image runs as root. PID 1 is `uv run uvicorn …` and holds the full environment.
  The Python app is a child process (PID 27 in the live cloud container).
- `/proc/<pid>/environ` access is a ptrace check. A same-uid reader passes unless the target is
  non-dumpable and the reader lacks `CAP_SYS_PTRACE`. A different-uid reader without
  `CAP_SYS_PTRACE` fails.
- `create_linear_issue` checks `ctx.eval_mode` (a per-request flag). `create_linear_project`,
  `find_linear_issues` and `list_linear_projects` have no eval guard.

## Mechanism (revision 2, after codex plan review)

All changes are keyed on `settings.deployment_profile == "eval"`. Production (`cloud`, `local`)
behaviour does not change.

1. **Bash child as `nobody`.** On eval, `bash_executor` spawns the child with `user=65534`,
   `group=65534`, `extra_groups=[]`. A non-root process cannot read `/proc/<pid>/environ` or
   `/proc/<pid>/mem` of any root process: the gateway, PID 1 (`uv`), Docker health checks, and
   gateway-spawned children. The kernel clears the child's capabilities on the uid change, so
   an added `CAP_SYS_PTRACE` on the container does not reach it.
2. **Allowlisted child environment.** On eval the child gets only `PATH`, `LANG`, `LC_ALL`,
   `LC_CTYPE`, `TZ`, `TERM`, `VIRTUAL_ENV`, plus `HOME=/tmp` and `TMPDIR=/tmp`. An allowlist,
   because `AGENT_DATABASE_URL` carries a password under a name no suffix pattern matches.
3. **Fail closed.** On eval, if the gateway is not root it cannot drop privileges. Bash returns
   `credential_isolation_unavailable` and spawns nothing.
4. **Linear eval guard.** `_gql` raises `ToolExecutionError` before any HTTP client is built when
   `deployment_profile == "eval"`. All four Linear tools route through `_gql`.

### Revision 1 (rejected by codex review)

Revision 1 used `PR_SET_DUMPABLE=0` on the gateway, an env allowlist, and uvicorn as PID 1,
with a startup leak probe. Codex findings that rejected it:

- Critical: gateway-spawned children (`mmdc`, git, delegation) inherit the full environment and
  are dumpable again after `execve`. A root bash child can read them while they run.
- Critical: the Docker health check runs every 10 s with the full environment. It is a
  recurring, dumpable target that a root bash loop can catch.
- High: the startup probe ran async work from sync registration, used a suffix-name detector,
  and degraded (dropped bash) instead of failing.

The uid drop closes all three, because none of those processes is readable by `nobody`.

### Codex findings not adopted, with reason

- **Profile key fails open when `AGENT_DEPLOYMENT_PROFILE` is missing** (finding 8). Both eval
  services declare `AGENT_DEPLOYMENT_PROFILE: eval` as a compose literal, never interpolated.
  `AppConfig._validate_eval_deployment_isolation` already relies on the same literal. A new
  `linear_tools_enabled` flag defaulting to false changes production configuration (AC-4) for a
  misconfiguration that the eval stack cannot produce.
- **Linear no-network test is a unit seam** (finding 10). `_gql` is the only network path in
  `linear.py`, and the test patches its only client factory for all four tools. A sentinel
  endpoint adds a container test for the same boundary.

### Parity cost (stated)

On eval, bash runs as `nobody`. It can read `/app` and write `/tmp`. It cannot write root-owned
paths such as `/app/agent_workspace`. The `write` primitive still runs in-process as root, so
file writes through `write` are unchanged.

### Residual risk (stated, not closed)

- The `read` primitive runs in-process as root and can read `/proc/self/environ` if governance
  allows it. `tools.yaml` forbids `/proc/**` for `read` and `write`, and `read` resolves the path
  before the check.
- `nobody` can still reach the network (`curl`). It holds no credential to send.

## Steps

1. Failing tests → verify each fails for the expected reason.
   - `tests/test_tools/test_linear.py`: AC-2 eval-profile tests (all four tools, no client built)
     and AC-4 cloud/local tests (request sent).
   - `tests/test_tools/test_eval_credential_isolation.py`: real-subprocess AC-4 (cloud bash still
     sees seeded values — also the instrument check), real-subprocess fail-closed refusal on a
     non-root eval gateway, mocked-spawn wiring for eval and production.
2. `src/personal_agent/tools/linear.py`: `_gql` guard.
3. `src/personal_agent/tools/primitives/bash.py`: uid drop, env allowlist, fail closed.
4. `scripts/eval/probe_bash_credential_isolation.py`: root outcome probe for AC-1 and AC-3. It
   starts a full-environment sibling, confirms a root scan finds credentials (instrument), then
   scans `/proc/[0-9]*/environ` and `/proc/[0-9]*/task/*/environ` through `bash_executor` for
   25 s (crosses health-check ticks). Prints names only.
5. Build `seshat-gateway:fre1505` (never `:latest`) and run the probe as root with seeded fake
   keys. Record the output in the handoff.
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | probe in the gateway image as root: `eval_scan_leaked == []`, child uid 65534, instrument finds credentials; covers `/proc/1/environ`, all processes, and threads |
| AC-2 | `test_eval_profile_sends_no_request_to_linear` (5 cases) |
| AC-3 | probe: `echo_ok == true` as `nobody` |
| AC-4 | no production change: every change is keyed on `deployment_profile == "eval"`; `test_cloud_bash_behaviour_is_unchanged`, `test_production_spawn_keeps_identity_and_environment`, `test_non_eval_profile_still_sends_linear_request` |

## Post-deploy

The eval stack must be rebuilt (`make eval-infra-up`) before it runs again. Then run the probe
against `cloud-sim-seshat-gateway-treatment` with `docker exec -i`. The production gateway image
also contains the code, but no production path changes behaviour.
