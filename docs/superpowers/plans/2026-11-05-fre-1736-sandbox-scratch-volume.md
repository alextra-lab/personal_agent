# FRE-1736 plan: run_python /sandbox is writable, and scratch_files returns the files

Ticket: FRE-1736. Related: FRE-1732 (the `bash` child owns the workspace), FRE-375 (no production write in a test).

## Cause (confirmed on the host, 2026-11-05)

- The gateway starts the sandbox with `docker run -v <scratch_dir>:/sandbox`. The Docker daemon runs on the host. It resolves `<scratch_dir>` on the host.
- `<scratch_dir>` is `/app/agent_workspace/sandbox/<trace id>`. On the host, `/app/agent_workspace/sandbox` holds 102 directories, `root:root`, mode 755. The daemon created them.
- In the gateway, the same path is in the `seshat_seshat_workspace_cloud` volume, and `nobody` (65534) owns it since FRE-1732.
- The sandbox runs as uid 1000. It cannot write a root-owned host directory. `_list_scratch_files` reads the gateway path, which is another directory.
- Second defect, same root: the gateway directory is owned by `nobody` (65534). Even with the right mount, uid 1000 cannot write it.

## Design

1. New setting `sandbox_scratch_volume` (`AGENT_SANDBOX_SCRATCH_VOLUME`), default empty.
   - Empty: the bind mount stays as it is (local and Mac, where the daemon sees the same path).
   - Set: the scratch directory lives in this named volume, and the gateway mounts the volume at `/app/agent_workspace`.
2. `run_in_sandbox`, when the volume is set, passes
   `--mount type=volume,src=<volume>,dst=/sandbox,volume-subpath=<scratch dir relative to /app/agent_workspace>`
   in place of `-v`. The daemon resolves the subpath inside the volume. The volume is the same one that the gateway reads. A subpath cannot escape the volume (the daemon checks this).
   - Docker on the host is 29.8.2. `volume-subpath` needs 26.0 or newer.
   - The value is checked before it reaches `--mount`: the volume name and the subpath must match `[A-Za-z0-9._/-]+`, so a comma or `=` cannot add an option. A scratch directory outside `/app/agent_workspace` with the volume set returns a `SandboxResult` with exit code 1 and a clear message.
3. Ownership. After the gateway creates the per-trace directory, and when the gateway runs as root, it gives that one directory to 1000:1000. It opens the directory with `open_no_symlinks` and calls `os.fchown` on the descriptor. This applies to the `cloud` and `eval` profiles alike. The `give_to_child` call is unchanged. The parent `sandbox` directory stays owned by `nobody`.
4. Compose: `docker-compose.cloud.yml` sets `AGENT_SANDBOX_SCRATCH_VOLUME: ${COMPOSE_PROJECT_NAME:-seshat}_seshat_workspace_cloud`. The comment beside the mount is corrected: the path is NOT the same on the host.
5. Docs: setting description, `run_python` module docstring ("bind-mount"), `.env.example`.
6. The 102 root-owned host directories: not deleted. They become unused, because the sandbox no longer mounts a host path. The handoff lists them for master. The removal command needs host root and master's OK.

Unchanged: `--read-only`, `--tmpfs /tmp`, `--network=none` by default, `-u 1000:1000`, `--cap-drop ALL`, `no-new-privileges`, one directory per trace.

## Steps (TDD)

1. Tests first, in `tests/test_tools/test_sandbox.py`:
   - AC-1 `test_the_mount_source_is_the_directory_the_listing_reads`: a small model of the daemon. A `-v SRC:` source is a host path. A `--mount type=volume,src=V,volume-subpath=S` source is `S` inside `V`. The gateway sees `V` at `/app/agent_workspace`. In the deployed configuration (volume set), the mount must resolve to the directory that `_list_scratch_files` reads. It fails on the old code, because the old code always passes a host path.
   - AC-3 `test_two_traces_get_two_subpaths`.
   - AC-4 `test_the_sandbox_flags_do_not_change`, parametrized over the volume form and the bind form.
   - `test_the_scratch_directory_goes_to_the_sandbox_user`: root, `fchown(fd, 1000, 1000)` on the trace directory only; not root: no call.
   - `test_an_unsafe_volume_or_subpath_is_refused`, `test_a_scratch_root_outside_the_volume_is_refused`.
   - Run: `uv run pytest tests/test_tools/test_sandbox.py tests/test_tools/test_child_workspace.py -q`.
2. Implement in `src/personal_agent/tools/primitives/sandbox.py` and `src/personal_agent/config/settings.py` (+ the settings registries that a test requires).
3. AC-2 probe `scripts/probes/probe_run_python_scratch_volume.py`: real Docker, no model call. It creates a throwaway volume, runs the gateway image as a throwaway root container (volume at `/app/agent_workspace`, docker socket, repo `src` read-only), and calls `run_python_executor` for two trace ids from inside. It checks: exit 0, the file is in `scratch_files`, the content reads from the gateway side, trace B does not list trace A's file. It removes the volume. It never touches the production volume or the compose stacks. The eval gateway has no docker socket and the test stack has no gateway, so a throwaway container is the way to run the real path.
4. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | `test_the_mount_source_is_the_directory_the_listing_reads`, failing on main, passing after |
| AC-2 | probe output pasted in the handoff |
| AC-3 | unit test + probe step |
| AC-4 | `test_the_sandbox_flags_do_not_change` |
| AC-5 | the Step 4 gates |

## Diff class

Escalate? It touches the sandbox mount (a security boundary), but not a production write path, deletion, schema, cost or governance code. Expected: self-serve, with a security review.

## Revisions after the codex plan review (2026-11-05)

1. Ownership race (finding 1, accepted). The `nobody` child must not own the parent `sandbox` directory, because the owner of a parent can rename or replace any entry in it.
   - `prepare_workspace` skips the `sandbox` subtree, and sets `sandbox` itself to root (0:0).
   - `run_in_sandbox` no longer calls `give_to_child` for the scratch parts. When the gateway is root, it chowns the trace directory to 1000:1000 on every call (also a directory that exists already), through `open_no_symlinks` and `os.fchown`.
   - Effect: the `bash` child can read the scratch files (mode 755) but cannot write or replace them. The `write` tool runs as root and keeps its access.
2. Tests (finding 2, accepted). Add: a trace directory that exists and belongs to 65534 becomes 1000; `prepare_workspace` reclaims `sandbox` and does not descend; `give_to_child` is not called for the scratch parts. The three swap tests are not needed once the parent is root-owned.
3. Probe (finding 3, accepted). Unique names, `try/finally`, the container is removed before the volume, a pre-created 65534-owned trace, the owner is read from the gateway side.
4. Scope (finding 4, accepted). The change targets the `cloud` gateway only. The eval gateways have no Docker socket, so `run_python` cannot run there. The ownership chown applies whenever the gateway is root, and the volume setting is wired in `docker-compose.cloud.yml` only.
5. Validation (finding 5, accepted). Check the volume name against Docker's rule `[A-Za-z0-9][A-Za-z0-9_.-]*`. Check the subpath by parts: no empty part, no `.`, no `..`, each part `[A-Za-z0-9_.-]+`. Update the `SandboxResult.scratch_files` docstring.
