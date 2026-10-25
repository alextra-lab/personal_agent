# FRE-1678 — a worker reads the artifact its task names

## Finding

A `general` worker called `read` on `/workspace/artifacts/entropy_one_equation.html`. The path
does not exist anywhere in the repo. An artifact has no file path: its bytes live in R2 and its row
in Postgres (`artifact_read_executor`, `tools/artifact_tools.py`). The worker holds `read` (files
named in `read_paths` only) and holds no `artifact_read`. `executor.py` refuses the path with
`Path ... is not in allowed paths` and the next action `Use a path that the path rules allow`. The
worker then searched `/app` and `/sandbox`.

## AC-1 decision

The worker reads an artifact with `artifact_read`, by id. Reasons:

- `artifact_read` is already scoped to `ctx.user_id` (`WHERE user_id = :user_id`). A worker
  carries the turn `user_id` since FRE-1467, so no new identity path is needed.
- A `read_paths` grant needs a real file. An artifact has none. A path grant would need a new
  directory mapping that exports artifact bytes to disk. That adds a second read path for one
  data class.
- `artifact_read` is private and outbound-free. It goes to the `general` type only, like
  `notes_search`. The FRE-1564 split test keeps this true.

## Steps

1. `worker_types.py`: add `artifact_read` to the `general` tool tuple. The `general` description
   stays as it is, because the planner prompt byte-identity tests pin it. The planner sees the
   tool in its `Tools granted now` line.
2. `config/governance/tools.yaml`: add a `sub_agent_tools.artifact_read` entry (`granted: true`,
   with a reason).
3. Refusal text (AC-3): for the `read` tool, a path outside the rules names `artifact_read`.
   Change `executor.py` `_validate_tool_arguments` (generic path refusal) and `read.py`
   `_grant_error` (worker grant refusal).
4. Tests:
   - `tests/personal_agent/orchestrator/test_worker_artifact_read.py` (new): AC-2 and AC-3.
   - `tests/_helpers/worker_run.py`: `run_worker` accepts and forwards `user_id`.
   - Update the lists that pin the `general` tool tuple and the private set:
     `test_worker_types.py`, `test_worker_tool_split.py`, `test_sub_agent_tools.py`,
     `test_governance_loader.py`.
   - `test_seshat_effort_prompt_pin.py`: the granted surface and the planner prompt change, so
     both pins are marked as a strict `xfail` until a passing D7 run (ADR-0157 D7) sets a new
     hash. Owner decision, 2026-10-25: ship first, run D7 later. Master holds the merge until
     the run passes. The hash is not edited here.

## Verify

- `uv run pytest tests/personal_agent/orchestrator/test_worker_artifact_read.py -q` fails before
  steps 1 to 3 and passes after.
- `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
