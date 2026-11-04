# FRE-1732: isolate the deployed `bash` child, and close three access gaps

Ticket: FRE-1732. Class: Complex, because the change touches subprocess identity, the permission guard, gateway auth and Caddy.

The repository is public. Measured results stay on the ticket.

## 1. `bash` child identity

`src/personal_agent/tools/primitives/bash.py`:

- Rename `EVAL_CHILD_*` to `CHILD_*` and `eval_child_env` to `child_env`, because the names no longer describe only eval.
- Isolate on every profile except `local`: the child runs as `nobody` through `setpriv`, with the allowlisted environment. A non-root gateway on a profile with isolation refuses and spawns nothing (`credential_isolation_unavailable`).
- Keep `local` as it is. A developer's machine has no root and no `setpriv`.
- The child's `HOME` is `/var/lib/seshat-bash-home`, which the image creates for `nobody` under a root-owned parent. `write` runs with no approval card and can reach `/tmp`, so `HOME` must not be there: curl, jq, git and psql read start-up files from it. mmdc's browser needs a writable config directory, so `HOME` must exist. `TMPDIR` stays `/tmp`.

### Two reviewed scripts need a secret

`cypher.py` needs `AGENT_NEO4J_PASSWORD`, and `probe_services.py postgres` needs `AGENT_DATABASE_URL`. The ticket says that each `bash(python3 skills/...)` allow rule must still work. Other options fail:

- `nobody` with the secret in its environment: a parallel `cat /proc/<pid>/environ` as `nobody` can read it.
- No secret: the two scripts stop working.

So on the `cloud` profile only, a command that is exactly one plain call of one of these two scripts runs as root, with no shell:

- The argv is `sys.executable -I /app/<script> <parsed words>`, with `cwd` set to `/app`. Bash does not read the command, and `PATH` does not choose the interpreter.
- The environment is a fixed `PATH`, the locale names, and the names that this script reads. It has no `HOME` or `TMPDIR`, because `nobody` can write `/tmp`.
- `probe_services.py` calls `psql -X`, so `psql` reads no start-up file.
- The check uses `parse_bash`. It needs no parser refusal, one top-level subcommand with no refusal, literal words, `python3` first, the script word exactly `skills/...` or `/app/skills/...`, and subcommand text equal to the whole command.
- A pipe, a list, an assignment, a redirection, an expansion or a trailing `&` fails the check, so that command runs as `nobody` with no secret.
- `eval` keeps `nobody` for every command (FRE-1505).

Reviewed scripts that need no secret run as `nobody`.

Docs: the module docstring, the `bash` tool description, `skills/graph-inspection/SKILL.md`, `skills/service-health/SKILL.md` and the `docker logs` recipes in `skills/turn-explanation/`.

## 1b. The child keeps its workspace (AC-6)

`/app/agent_workspace` is root-owned, and the `write` tool creates root-owned files, so a child as `nobody` cannot change project work. A new module `src/personal_agent/tools/primitives/_child_workspace.py` holds the account and these steps. They act only on the `cloud` profile, and only when the gateway is root. `eval` keeps its FRE-1505 behaviour.

- `prepare_workspace()` runs at gateway startup. It gives the whole tree to the child account and does not follow a symbolic link. No child runs at that time.
- `give_to_child(paths)` runs after `write`, the `bash` overflow write and `run_in_sandbox`. It gives only the paths that this call created, under the workspace or `/tmp`, from `missing_parts()`. An existing file, or the root directory itself, keeps its owner. It uses `open_no_symlinks` and calls `fchown` on that descriptor. It leaves a file with a second name, which can be a hard link.
- `edit` keeps the owner of the file that it replaces.

The child's account owns the workspace, so root tools now write into directories that the child controls. Section 2 covers that.

`run_python` is not affected: its `/sandbox` mount is a path on the Docker host, not this volume.

## 2. Root tools refuse a link that appears after the checks

A `nobody` child owns directories under `/tmp` and the workspace, and root `read`, `write` and `edit` check a path before they open it. A swap between the check and the open makes root follow a symbolic link.

`_governance.py`: `open_no_symlinks(path, flags)` opens from `/`, one part at a time, with `O_NOFOLLOW`. The caller passes a resolved path, which holds no link, so any link means that the path changed. `open_file_no_symlinks` adds `O_NONBLOCK` and refuses anything that is not a regular file, so a planted FIFO cannot block the event loop. `make_directory_no_symlinks` creates the sandbox directory.

- `read` and the read step of `edit` use `open_file_no_symlinks`. A changed path gives `file_replaced`.
- `write` opens an existing file with no `O_CREAT`, because `fs.protected_regular` refuses `O_CREAT` on another user's file in sticky `/tmp`, even for root. A missing file opens with `O_CREAT | O_EXCL`.
- The `edit` replace acts through one directory descriptor: the temporary file, its mode and the rename all use `dir_fd`, and the mode loses any setuid bit.
- The `bash` overflow write uses `O_CREAT | O_EXCL`, so a planted name fails.

## 3. Joined options

`src/personal_agent/tools/primitives/bash_allowlist.py`: a new `_mmdc_hazard` reads a short-option cluster as commander does. Boolean flags `f`, `q`, `h` and `V` continue the cluster. `p` is refused in any position. `o` and `a` take the rest of the word or the next word, and that value must be a scratch path. A listed value option takes the rest of the word as its value. Any other character asks, because commander reads `-q-name` as `-q` and then `--name`. `--artefacts` follows the same rule as `--output`. A word that commander reads as a separate option, such as `-o -q`, is read as a value here, so it asks. That is a false refusal, and it is safe.

## 4. Gateway tokens

- `config/gateway_access.yaml`: delete `dev-local`.
- `src/personal_agent/gateway/auth.py`: `load_token_config` skips an entry whose expanded secret is empty, only spaces, or still holds `${`. It logs the entry name and no value.

## 5. Caddy admin port

`config/cloud-sim/Caddyfile`: `admin localhost:2019`. The compose health check already uses `127.0.0.1:2019`.

## Tests

| Criterion | Test |
|---|---|
| AC-1 | `tests/test_tools/test_eval_credential_isolation.py`: the spawn on `cloud` and `eval` uses `setpriv` and the allowlisted environment. A real child, with `setpriv` patched out, shows that the seeded value is not in `cat /proc/self/environ` or `jq -n env`. Seeded negative: on `local`, the same commands return the value. |
| AC-1, with root | `scripts/eval/probe_bash_credential_isolation.py` runs the four commands and a wide scan in the gateway image as root, with fake values. CI runs tests with no root, so this result goes on the ticket. Its instrument check as root must find the value. |
| AC-1, scripts | Spawn test: the exact form gets root and the script's names. A pipe, `&&`, an assignment or a redirection gets `nobody` and no secret. `eval` gets `nobody`. |
| AC-2 | `tests/personal_agent/governance/test_bash_joined_options.py`: `decide_permission` asks for each joined, spaced and clustered form of a refused option, and a plain diagram call still runs. |
| AC-3 | `tests/personal_agent/gateway/test_auth_placeholder_tokens.py`: a placeholder, an empty value and the old `dev-local` text do not authenticate. A real variable still does. The real config file holds no literal secret. |
| AC-4 | A static test reads the Caddyfile. A throwaway Caddy container on a new Docker network refuses a call from a second container and answers on loopback. The live check is in the handoff runbook. |
| AC-6 | Unit tests: `give_to_child` gives the path and its directories, and leaves a file behind a planted link. In the gateway image as root, on a tmpfs workspace, a `bash` child creates, edits and runs files in an existing project and the sandbox directory, also after the `write` and `edit` tools change them. Seeded negative: before `prepare_workspace()`, the same child gets `Permission denied`. |
| Link after the checks | A unit test swaps the file at the end of the path check, for `read` and `write`, with a seeded negative on the old open. A real race in the image runs against a `nobody` child, with the same seeded negative. |

## Commands

- `make test-file FILE=...`, before the fix (fail) and after the fix (pass).
- `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `uv run pre-commit run --all-files`.

## Not in this ticket

- A route for commands that lose Docker access, `psql` passwords or `AGENT_` variables. The handoff lists the before and after result for each group.
- The observations routes that check a token and not a user.
