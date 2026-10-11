# FRE-1580 — ADR-0156 T8: worker skill catalogs, `read_skill` for every type, per-call asks, read-only file grant

Ticket: FRE-1580 (ADR-0156 D9). Tier: Standard/Complex (src logic, a file-read security boundary).

## What exists already

- **D9.3 (per-call asks) shipped with FRE-1574.** `sub_agent.py:2263-2267` passes `worker=` to
  `dispatch_tool_call`, `_check_permissions` asks per call with the worker named, and
  `tests/test_tools/test_permission_ask_flow.py::TestTheCard::test_each_call_asks_again[general]`
  is labelled "T8 AC-4". T8 adds no code for D9.3. The handoff cites that test.
- `read_skill` is granted to workers by governance (`sub_agent_tools.read_skill`), but only
  `general` lists it. The researcher does not.
- Workers get no skill catalog: `SubAgentSpec.skill_index_block` is never set.
- Every skill except `artifact-design` and `sequential-thinking` has `requires-tools: bash`.

## Design

### D9.1 — filtered catalog per worker

- `skills.py`: `skills_for_tools(held: Collection[str]) -> dict[str, SkillDoc]` returns the skills
  whose `requires_tools` is a subset of `held`. `assemble_skill_catalog(held: Collection[str] |
  None = None)`: `None` keeps the primary's full catalog; a set filters it.
- `sub_agent._build_sub_agent_system_prompt`: when `read_skill` is in `spec.tools`, append
  `assemble_skill_catalog(spec.tools)`. `spec.tools` is the granted list (governance- and
  mode-filtered), so "holds" means "was granted". The text depends only on the type and the
  mode, so the cached prefix per type stays byte-identical.
- Remove the dead `SubAgentSpec.skill_index_block` field. The capture field
  `skill_index_block_chars` stays (it is an ES template field) and now records the catalog length.
- `sub_agent._build_tool_defs`: the worker's `read_skill` definition gets `name.enum` = the
  visible skill names (S2: hide filtered skills).
- The worker loop refuses a `read_skill` call whose `name` is not visible, before dispatch, with
  the D6 error `unknown_skill` that lists the visible names. A hidden skill is never readable.

### D9.2 — `read_skill` for every type

- `worker_types.py`: the researcher's tools gain `read_skill`. Governance already grants it;
  its `reason` text is updated to name both types.
- `read_skill` stays NEUTRAL in the FRE-1564 split test: it reads only bundled skill files.
  `read_skill_file` already refuses any path that is not a listed file of that skill.

### D9.4 — the read-only file grant for `general`

- New module `governance/worker_read_grant.py`:
  - `GrantedFile(real_path: str, device: int, inode: int)` (frozen).
  - `ReadGrant`: `for_task(paths, *, trace_id) -> ReadGrant`, `files`, `closed`, `close()`,
    `check_path(real_path) -> GrantedFile`, `check_opened(entry, st) -> None`. Raises
    `ReadGrantRefused(code, message)` with code in `not_granted | file_replaced | grant_ended`.
  - `for_task`: each entry must be a `str` and absolute. It is resolved with
    `Path(p).resolve(strict=True)`, then `os.lstat`; only `S_ISREG` survives. The device and
    inode are recorded. Every dropped entry logs `worker_read_grant_path_dropped` with a reason.
    At most 10 entries are considered.
- `PlanTask.read_paths: tuple[str, ...] = ()`. The planner schema and prompt gain an optional
  `read_paths` (array of strings). `_validate_plan_json` keeps only absolute string paths, and
  only for a type whose registry tools include `read`; it never invalidates a plan for this field.
- `SubAgentSpec.read_paths: tuple[str, ...] = ()`.
- `worker_types.py`: `general` tools gain `read`; its description adds "or a file the task
  names". Governance `sub_agent_tools.read: granted: true` with the owner decision as reason.
  The split test classifies `read` as PRIVATE, so the researcher can never hold it.
- `general` holds `read` on every task, so every general worker keeps the same tool bytes
  (ADR-0150's per-type prefix). With no file named, a read is refused (`not_granted`).
  `_maybe_redispatch_on_gap`: a redispatch to a type without `read` drops `read_paths`. Fold-in:
  a redispatch to a type that holds an outbound tool also drops the conversation `context`
  (FRE-1564 decision A; before, `replace(spec, ...)` carried a general worker's context to a
  researcher).
- `run_sub_agent`: grant time is the task start. `grant = ReadGrant.for_task(spec.read_paths)`.
  The task message lists the granted real paths ("Files you may read with the read tool: …").
  `finally: grant.close()` — the grant ends with the task.
- `dispatch_tool_call(..., read_grant: ReadGrant | None = None)`: a `sub_agent` call to `read`
  with no grant is refused (`not_available_to_worker`), so a future worker path cannot read
  unrestricted. The grant is passed to `execute_tool(..., read_grant=)`.
- `ToolExecutionLayer.execute_tool(..., read_grant=None)`: injects `read_grant` into an executor
  that declares it (the reserved-name pattern of `approve`, ADR-0153 D7). A model-supplied
  `read_grant` argument is dropped.
- `read_executor(..., read_grant: ReadGrant | None = None)`:
  1. resolve and run `_check_path_governance` as today (the grant never widens it);
  2. when a grant is present: `check_path(resolved)` — refuses an unnamed file, a `../` or
     symlink path to another file, a hard link at another path, and a closed grant;
  3. after `open`: `check_opened(entry, os.fstat(fh.fileno()))` — refuses a rename-over and a
     symlink swapped in after the grant (device or inode differs).
  `ReadCode` gains `not_granted`, `file_replaced`, `grant_ended`.

## Acceptance criteria → proof

| AC | Test |
|---|---|
| AC-1 (ADR AC-10) | `tests/personal_agent/orchestrator/test_worker_skill_catalog.py`: per type, the first-call system prompt and tools (via `run_sub_agent`, real registry and governance) over a seeded skills dir with skills needing `{}`, `bash`, `web_search`, `query_telemetry`, `web_search,query_telemetry`. Catalog lines and the `read_skill` enum equal the subset rule; every type holds `read_skill`. Plus the real skills dir. |
| AC-2 | Same file: as the researcher, `read_skill(name, path=<listed file>)` returns the file; `path="../../..."`, an absolute path, and a hidden skill name are refused; `read` is refused (not held). |
| AC-3 (ADR AC-9) | `tests/personal_agent/orchestrator/test_worker_read_grant.py`: a `general` worker run (scripted LLM, real `read` tool, real path governance on `tmp_path`) and direct dispatch rows: named file read; unnamed file; `write` and `edit` of the named file (refused, file unchanged); `../` to another file; symlink to another file; named file replaced by a symlink; directory in `read_paths`; hard link at another path; after the task ends (closed grant, and a new task without `read_paths` does not hold `read`); rename-over. Seeded negative: with `check_opened` skipped, the rename-over row fails. |
| AC-4 | Existing `test_each_call_asks_again[general]` (FRE-1574). |
| AC-5 | Live, post-deploy: master's owner PWA turn. Runbook in the handoff. |

Also: planner tests (schema has `read_paths`; validator keeps absolute paths only and only for
`general`); split test (`read` is PRIVATE; researcher holds `read_skill`); redispatch drops
`read_paths` and context for the researcher.

## Commands

- `make test-file FILE=tests/personal_agent/orchestrator/test_worker_skill_catalog.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_worker_read_grant.py`
- `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`
- `make render-prompt-corpus` (the planner prompt and worker type text change).

## Codex plan review (2026-10-11) and what changed

1. *Hidden skills readable through `read_paths`* — not adopted. D9.1 hides a skill to keep an
   unusable skill out of the worker's catalog (S2), not to keep its text secret. Skill files are
   checked-in docs that the primary can read, and a review of a skill file is a valid task.
2. *Symlinked skill folder or `SKILL.md`* — adopted. The loader skips both (fold-in in
   `skills.py`), with a test.
3. *Fail closed only at dispatch* — adopted. `execute_tool` also refuses a worker call to an
   executor that declares `read_grant` when no grant is passed.
4. *Cache claim* — adopted. `general` always holds `read` (above).
5. *Race between the two checks* — adopted. Tests swap the file (rename-over, symlink) between
   `check_path` and `open`.

## Owner decision (2026-10-11): the planner pin

The planner prompt changes (D9.2 and D9.4). ADR-0154 D7 requires a planner probe before such a
change ships, and model testing is stopped (FRE-1573). The owner chose: re-pin the planner hash
now, and run the D7 probe in the FRE-1581 (T9) gate run.
