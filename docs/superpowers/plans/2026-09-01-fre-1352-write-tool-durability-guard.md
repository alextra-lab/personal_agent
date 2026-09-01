# FRE-1352 — write tool durability guard

## Problem

`write`'s governance in `config/governance/tools.yaml` allows `/app/**`. Only
`/app/agent_workspace/**` and `/app/telemetry/**` (+ subdirs) are bind-mounted and survive a
container restart. Everything else under `/app` is the writable image layer. A write to
`/app/nfl-predictor/...` passed governance, looked successful, and was destroyed on the next
rebuild. Root cause and file references: FRE-1352 ticket body.

## Design

Derive durability from the live mount table instead of a second hand-maintained list of
compose volume targets (AC-4). **Revised after codex plan-review**: use `/proc/self/mountinfo`
directly rather than `os.path.ismount` — `ismount` only compares parent/child device+inode and
can misdetect a same-filesystem bind mount; parsing mountinfo reads the kernel's own mount
table and is what "derived, not transcribed" should mean. Walking a resolved path's ancestors
up to `/app` and checking membership against the parsed mount-point set answers "does this
survive a rebuild" from the OS itself. A new volume added to `docker-compose.cloud.yml` needs
no matching edit here. Note: `/` is always present as a mount entry in real mountinfo, so the
ancestor walk must stop at `/app` and never test `/` itself, or every path would read durable.

Scope: `write` only. `bash` heredocs have the same exposure (noted in the ticket) but are a
separate tool/governance surface — file a follow-up ticket rather than fold in, per the ticket's
own "decide deliberately" instruction.

**Known residual limitations (documented, not fixed, in this ticket):** a symlink under a
durable path pointing outside `/app` would resolve before the check and escape it (the tool
cannot itself create symlinks; would need `bash` cooperation — same bash gap already deferred);
general TOCTOU between path-check and write already exists for every governance check in this
file, not introduced here; only `/app` gets the durability distinction — `$HOME/**` and
`/opt/seshat/**` are out of the ticket's scope. Noted in the handoff comment.

## Steps

1. `src/personal_agent/tools/primitives/_governance.py`
   - `_mount_points(mountinfo_path: str = "/proc/self/mountinfo") -> frozenset[str]`: parse the
     kernel mount table (field index 4 of each `/proc/self/mountinfo` line is the mount point).
     Returns an empty set if unreadable (non-Linux, permission) — durability then fails open via
     the ancestor walk below. `mountinfo_path` is a parameter so the parser has a hermetic unit
     test against fixture text, independent of the real filesystem.
   - `_is_durable_mount(resolved: Path) -> bool`: if `resolved` is not under `/app`, return
     `True` (durability question doesn't apply outside the ticket's scope — other allowed
     roots, e.g. `$HOME/**`, `/opt/seshat/**`, are not the image-layer trap this ticket is
     about). Otherwise walk `(resolved, *resolved.parents)`, returning `True` on the first
     ancestor found in `_mount_points()`, stopping (and returning `False` if none matched) once
     the ancestor is `/app` itself — never testing `/`, which is always a mount entry in real
     mountinfo and would otherwise make every path read as durable.
   - `_check_durability(resolved, tool_name, *, trace_id) -> dict | None`: `None` when durable;
     otherwise an error dict `{"success": False, "error": "not_durable", "path": ..., "detail":
     "<names /app/agent_workspace/ and /app/telemetry/ as the fix>"}`. Mirrors
     `_check_path_governance`'s return shape so `write.py` handles it the same way.

2. `src/personal_agent/tools/primitives/write.py`
   - Import `_check_durability`; call it immediately after `_check_path_governance` returns
     `None`, before `mkdir`/write. Same early-return + `log.warning` pattern as the existing
     `write_path_rejected` branch (AC-1: refuse, don't silently succeed).
   - Update `write_tool.description` to name `/app/agent_workspace/` and `/app/telemetry/` as
     the durable locations and state that other `/app/` paths do not survive a restart (AC-3).

3. Tests:
   - `tests/test_tools/test_primitives_write.py` — two tests run together, satisfying AC-5:
     - `test_write_rejects_nonmounted_app_path`: monkeypatch `_mount_points` to
       `frozenset({"/app/agent_workspace", "/app/telemetry", "/"})` (root included deliberately,
       to prove the walk doesn't stop there); write to `/app/nfl-predictor/x.py` (the ticket's
       real incident path) → `success is False`, `error == "not_durable"` (AC-1).
     - `test_write_allows_mounted_app_path`: same mount set; monkeypatch `Path.mkdir`/
       `Path.write_text` to no-ops (this sandbox doesn't own `/app`, mirroring how the existing
       suite avoids real prod paths) — write to `/app/agent_workspace/nfl-predictor/x.py` →
       `success is True` (AC-2).
   - New `tests/test_tools/test_primitives__governance.py` for the pure logic, per codex review:
     - `test_mount_points_parses_mountinfo_fixture`: real parsing against crafted mountinfo-format
       text in a tmp file (hermetic, no `/proc` dependency).
     - `test_is_durable_mount` cases (monkeypatching `_mount_points`): non-durable `/app/...`;
       durable via direct mount; durable via a nested mount one level down
       (`/app/telemetry/graph_quality/...`); a path outside `/app` (e.g. `/tmp/x`) always durable;
       the boundary case proving `/` in the mount set does not make a plain `/app/x` durable.

4. No `config/governance/tools.yaml` change — `allowed_paths` for `write` is unchanged; the new
   check is an independent second gate, not a narrowing of path admissibility.

5. File a follow-up ticket (`Needs Approval`, label `PersonalAgent`) for the same guard on
   `bash` heredoc writes; note the split in the PR body and handoff comment.

## Test plan

`make test-file FILE=tests/test_tools/test_primitives_write.py` — new tests pass, existing ones
unaffected (they don't touch `/app`, so `_is_durable_mount` short-circuits `True` for them).
`make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
