# FRE-1518 — Eval bash isolated spawn on the served event loop

Ticket: FRE-1518 (High, Approved 2026-09-14 by master under the Tier-3 bugfix grant).
Related: FRE-1505 (the isolation, Verify Failed), FRE-1517 (blocked).

## Problem (verified before this branch)

- The gateway runs `uv run uvicorn personal_agent.service.app:app`. `uvicorn[standard]` installs
  uvloop 0.22.1, and uvicorn selects it (`--loop auto`).
- FRE-1505 spawns the bash child with `asyncio.create_subprocess_exec(..., user=, group=,
  extra_groups=)`. uvloop rejects these kwargs **even when their value is `None`**. In the
  gateway image under `uvloop.run`, the call raises
  `ValueError: unexpected kwargs: user, group, extra_groups`.
- `ValueError` is not an `OSError`, so it escapes `bash_executor`.
- **Production is broken too.** The FRE-1505 spawn passes `None` for the three kwargs on a
  non-eval profile. In `cloud-sim-seshat-gateway` (profile `cloud`, started 2026-09-14 17:31 UTC),
  the `None` spawn under `uvloop.run` raises the same `ValueError`. Reported to master on the
  ticket at 18:19 UTC.
- `scripts/eval/probe_bash_credential_isolation.py` called `asyncio.run`, the stdlib loop. The
  probe passed, but it did not test the served loop.

## Mechanism — `setpriv` wrapper

On eval, the argv becomes:

```
/usr/bin/setpriv --reuid=65534 --regid=65534 --clear-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs -- /bin/bash -o pipefail -c <command>
```

with `env=eval_child_env()`. On every profile, the `user`, `group` and `extra_groups` kwargs are
removed. On production the argv stays `/bin/bash -o pipefail -c <command>` with `env=None`.

Checked in a throwaway `seshat-gateway:latest` container, as root, under `uvloop.run`:

| Property | Observed |
|----------|----------|
| Identity | `uid=65534(nobody) gid=65534(nogroup)` |
| Supplementary groups (`/proc/self/status` `Groups:`) | empty |
| `CapInh` / `CapPrm` / `CapEff` / `CapBnd` / `CapAmb` | all `0000000000000000` |
| `NoNewPrivs` | `1` |
| `cat /proc/1/environ` | `Permission denied` |
| `touch /app/x` | denied |
| Environment | only the variables passed in `env=` (bash adds `PWD`, `SHLVL`, `_`) |
| PID | the returned process PID is the bash PID (`setpriv` calls `execve`) |
| Processes running as uid 65534 in the eval treatment gateway | none |

### Why this candidate

- **`setpriv` wrapper (chosen).** The spawn stays on the running loop. `asyncio.wait_for`,
  `proc.kill()` and task cancellation behave the same on both profiles. `setpriv` replaces itself
  with bash, so `proc.kill()` still signals bash. `setpriv` comes from util-linux, which is
  Essential in Debian. It is present in the image (`util-linux 2.41.5`).
- **stdlib `subprocess` in a thread (rejected).** Timeout and kill move into `subprocess.run`. A
  cancelled turn does not stop the child, which runs until its timeout (max 120 s). Each call
  holds a default-executor thread for its full duration.
- **`preexec_fn` (rejected).** The gateway is multi-threaded. Python documents that `preexec_fn`
  is not safe in the presence of threads, because the child can deadlock after `fork`.

### Guarantee changes

- **Same:** uid and gid 65534, no supplementary groups, allowlisted environment, no read of any
  root process's `/proc/<pid>/environ`, no write to root-owned paths.
- **Added:** `--no-new-privs`, so a setuid or file-capability binary cannot raise the child's
  privileges. The inheritable, ambient and bounding capability sets are cleared explicitly.
- **Fail closed:** the non-root refusal stays. If `setpriv` is missing, `create_subprocess_exec`
  raises `FileNotFoundError`, an `OSError`. The executor returns `os_error` and runs no command.

## The instrument

`probe_bash_credential_isolation.py` runs `_probe()` under `uvloop.run`, the loop uvicorn uses.
The report records `event_loop`, and `passed` requires `uvloop`. The usage block now sets
`AGENT_SLM_BASE_URL`: without it, config load refuses (ADR-0132 D4) and the probe never starts.

## Codex plan review — dispositions

| Codex finding | Severity | Disposition |
|---------------|----------|-------------|
| Production kwargs change from `None` to omitted, so production is not identical | High | **Rejected, with evidence.** uvloop rejects the `None` kwargs, so the current production spawn fails. Omitted kwargs equal the asyncio defaults. The argv and `env=None` are unchanged. `test_production_bash_runs_on_served_event_loop` proves production bash runs on uvloop. |
| A secret under an allowlisted name (`PATH`, `TERM`) leaks | High | **Not adopted.** The allowlist names carry no credential in any compose file. A detector that flags allowlisted values reports `PATH` on every run. This is unchanged from FRE-1505. |
| Probe still calls `asyncio.run` | High | **Already in the plan (step 3).** Codex read the code before the change. |
| No `/chat`-driven credential probe | High | **Not adopted.** AC-2 allows the uvloop probe. AC-1 covers the served `/chat` path. |
| `CapInh` / `CapBnd` / `CapAmb` not cleared or checked | Medium | **Adopted.** Three flags added; all five sets observed as zero. |
| uid 65534 is a shared identity | Medium | **Checked.** No process in the eval treatment gateway runs as 65534. Concurrent bash children share it, and each holds only the allowlisted environment. |
| `/tmp` is writable | Medium | **No change.** `HOME` and `TMPDIR` are `/tmp` by design (FRE-1505 parity cost). |
| Background descendants survive timeout | Medium | **Out of scope.** Unchanged since FRE-283, on both profiles. |
| The uvloop test's non-root branch passes if `setpriv` is missing | Medium | **Adopted.** The test asserts `setpriv:` in stderr and a nonzero exit. |
| The seeded negative detects availability, not disclosure | Medium | **Not adopted.** AC-3 on the ticket specifies the uvloop kwargs error. |
| `setpriv` not pinned in `Dockerfile.gateway` | Low | **Not adopted.** util-linux is Essential. A missing binary fails closed (`os_error`). |
| stdin, cwd and umask inherited | Low | **Out of scope.** Unchanged on both profiles. |

## Steps

1. **Failing tests first** — `tests/test_tools/test_eval_credential_isolation.py`:
   - `test_eval_spawn_accepted_by_served_event_loop` (new): real spawn under `uvloop.run`, eval
     profile, `os.geteuid` patched to 0. As non-root, asserts `setpriv:` in stderr, a nonzero
     exit and empty stdout. On the old code it raised `ValueError`.
   - `test_production_bash_runs_on_served_event_loop` (new, cloud and local): `echo ok` under
     `uvloop.run` returns `ok`.
   - `test_eval_spawn_drops_to_nobody_with_allowlisted_env`: the exact setpriv argv, and no
     `user`/`group`/`extra_groups`/`preexec_fn` kwargs.
   - `test_production_spawn_keeps_identity_and_environment` (AC-4): argv exactly
     `("/bin/bash", "-o", "pipefail", "-c", "echo ok")`, `env is None`, no identity kwargs.
   - Run: `make test-file FILE=tests/test_tools/test_eval_credential_isolation.py`.
2. **Implement** — `src/personal_agent/tools/primitives/bash.py`: `EVAL_CHILD_SETPRIV_ARGV`,
   argv prefix per profile, identity kwargs removed, module docstring.
3. **Probe** — `uvloop.run`, `event_loop` in the report and in `passed`, usage block fixed.
4. **Compose comment** — `docker-compose.eval.yml` names `setpriv` (comment only).
5. **AC-3 (seeded negative)** — fixed probe against the unfixed image code:
   `docker run --rm -i -e AGENT_DEPLOYMENT_PROFILE=eval -e AGENT_SLM_BASE_URL=http://127.0.0.1:9/v1 -e AGENT_ANTHROPIC_API_KEY=fake-… -e AGENT_OPENAI_API_KEY=fake-… --entrypoint /app/.venv/bin/python seshat-gateway:latest - < scripts/eval/probe_bash_credential_isolation.py`.
6. **AC-2** — the same command, with the branch `bash.py` mounted read-only over
   `/app/src/personal_agent/tools/primitives/bash.py`.
7. **Gates** — `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`. Then `feature-dev:code-reviewer` and `security-review` on
   `git diff origin/main...HEAD`.

## Acceptance criteria

| AC | Proof | Where |
|----|-------|-------|
| AC-1 bash runs in a served eval turn (`id -u; echo ok` → 65534, ok) | Live turn through the eval treatment gateway `/chat` | Post-deploy runbook (master); needs a rebuilt eval image |
| AC-2 isolation holds on the served loop | Step 6 probe output | Handoff comment |
| AC-3 seeded negative | Step 5 probe output | Handoff comment |
| AC-4 production unchanged | `test_production_spawn_keeps_identity_and_environment`, `test_production_bash_runs_on_served_event_loop`, `test_cloud_bash_behaviour_is_unchanged` | Unit tests |

## Diff class

Escalated: security boundary on a tool execution path. Flag for owner `/code-review ultra`.
