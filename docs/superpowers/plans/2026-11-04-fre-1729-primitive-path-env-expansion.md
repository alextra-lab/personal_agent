# FRE-1729: stop the expansion of environment variables in a model's path

Ticket: FRE-1729. Class: Standard, because the change touches `src/` logic and the path gate.

## Change

`src/personal_agent/tools/primitives/_governance.py`:

- `_expand_path(path)` becomes `os.path.expanduser(path)`. `read`, `write` and `edit` use it for a model's path.
- New `_expand_pattern(pattern)` is `os.path.expanduser(os.path.expandvars(pattern))`.
- `_matches_any` uses `_expand_pattern` for each pattern. This covers `forbidden_paths`, `allowed_paths` and `unattended_paths`.

`src/personal_agent/tools/executor.py`:

- Make the same split in the local copy. `_validate_path_against_patterns` applies `_expand_path` to the path and `_expand_pattern` to each pattern.

`scripts/eval/fre1728/docker-compose.fre1728.yml` and `scripts/eval/fre1728/README.md`:

- Correct the reason that the overlay gives for primitive tools off. The value stays `"false"`. The ticket says that the pilot can turn them on, and that choice belongs to the next pilot run.

## Tests

New file `tests/test_tools/test_primitives_path_env.py`. It uses the real `ToolExecutionLayer`, the real `config/governance` rules and a new directory under `/tmp`, which the real rules allow.

| Criterion | Test |
|---|---|
| AC-1 | For `read`, `write`, `edit` and for `$NAME`, `${NAME}`: the call acts on a file with the literal name. A decoy file named with the value stays unchanged. The value is in no `ToolResult` and no captured log entry. |
| AC-1 | `read` and `edit` of a missing `$NAME` path: the error holds the literal text and not the value. |
| AC-1 | `$DIR/file`, with `DIR` set to an allowed root, is refused by the gate. |
| AC-1 seeded negative | With the old expansion patched in, the same detector finds the value in the result and in the logs. Without this, a test that captures no logs passes for the wrong reason. |
| AC-2 | A pattern `$ROOT/**` allows a path in `allowed_paths`, refuses it in `forbidden_paths`, and marks it in `unattended_paths`, in both gates. With `ROOT` unset, the pattern does not match. |
| Kept behaviour | `~/x/personal_agent/f` still reads under the real rules. `~/.ssh/id_rsa` is still refused. |

## Commands

- `make test-file FILE=tests/test_tools/test_primitives_path_env.py`, before the fix (fail) and after the fix (pass).
- `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Production check for AC-3

Read only, with no production turn. The results are on the ticket.
