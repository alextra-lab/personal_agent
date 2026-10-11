# FRE-1576 — An exact-string `edit` tool, and `query_telemetry` for the primary

ADR-0156 T4 (D7.1, D7.2). Built on the T3 contract (FRE-1575, merged `7a5ff3f9`).
Tier: **Standard** (new `src/` tool, governance config). Codex plan review: required.

## Acceptance criteria (from the ticket)

| AC | Criterion | Proof |
|---|---|---|
| AC-1 | `edit` replaces exactly one match. One match: the file changes only at that span. Zero matches: an error with count 0, and the file is unchanged. Two matches: an error with count 2, and the file is unchanged. A path outside the write roots: refused. | `tests/test_tools/test_primitives_edit.py`, one test per case, each reading the file bytes after the call |
| AC-2 | `edit` is in the contract. The T3 contract test covers `edit`, and the registry is the 25 rows of the ADR-0156 tool table that are not removed. | `tests/personal_agent/tools/test_tool_contract.py`: `EXPECTED_TOOLS` gains `edit` (25 names); the existing registry-wide tests (code set, description lint, bound, typed failures) then cover it; the failure-line checker covers `edit.py` because it is a registered module |
| AC-3 | The primary's tool list on a turn includes `query_telemetry`. | `tests/personal_agent/orchestrator/test_worker_tool_split.py`: the test that asserted the opposite is replaced by one that builds the registry and reads `get_tool_definitions_for_llm(Mode.NORMAL)` (the call the primary's turn makes, `orchestrator/executor.py:6978`) |
| AC-4 | Live: one owner PWA turn, "How many model calls did you make in the last hour?" The primary calls `query_telemetry` and makes no `bash` call with `curl` to Elasticsearch. | Post-deploy, by master (runbook in the handoff) |

## Design

### `edit` (D7.1)

- **Module:** `src/personal_agent/tools/primitives/edit.py`, beside `write.py`.
- **Parameters:** `path`, `old_string`, `new_string`, all required strings. These are the names in Claude Code's `Edit` tool. S7 uses `old_str`/`new_str`. Either works, and the model knows both.
- **Codes:** `EditCode = Literal[PathCode, "empty_old_string", "file_not_found", "not_a_file", "not_text", "no_match", "multiple_matches", "permission_denied", "io_error"]` and `class EditError(ToolError[EditCode])`.
  - `no_match` and `multiple_matches` give the count in the message, for example `old_string matches 2 times in /tmp/x.md; it must match exactly once`. The `next_action` says how to fix it: read the file again and copy the text exactly, or add surrounding lines until the match is unique.
  - An empty `old_string` is refused. Otherwise `"".count` returns len+1, or 1 for an empty file, and the tool would insert text at offset 0.
- **Order of checks:**
  1. empty `old_string`
  2. resolve the path
  3. `_check_path_governance(tool_name="edit")`
  4. `_check_durability(tool_name="edit")`
  5. the file exists and is a regular file
  6. read UTF-8 with `newline=""`, so that `\r\n` is kept
  7. count the matches
  8. replace once, then write with `newline=""`
  - Steps 1–7 never write. A refusal leaves the file unchanged.
- **Success payload:** `{"path": str, "line": int, "bytes_written": int}`. `line` is the 1-based line where the replaced text starts, so the model can read that range back.
- **Bound:** `max_result_tokens=1_000`, like `write`.
- **Path governance = write's.** `config/governance/tools.yaml` gets an `edit:` row. Its `allowed_in_modes`, `forbidden_in_modes`, `unattended_paths`, `allowed_paths`, `forbidden_paths` and loop limits are YAML anchors of the `write:` row (`&write_allowed_paths` … `*write_allowed_paths`), so the two cannot drift. A test asserts that the loaded `edit` policy equals `write`'s for those fields.
- **Permission rules = write's** (`config/governance/permissions.yaml`): `allow: edit`, and `ask: edit` in `[ALERT, DEGRADED, RECOVERY]`, beside the `write` rules.
- **Workers:** no `sub_agent_tools` grant. The worker allowlist leaves it out, and that is the same as for `write`. ADR AC-9 (T8) needs a worker edit to fail, and it does.
- **Registration:** `tools/__init__.py`, inside `primitive_tools_enabled`, after `write`. `primitive_tools_registered count=6`.
- **Descriptions (S5):**
  - `edit`: what it does; to read the file first and copy the text exactly, including whitespace; that 0 or several matches is an error with the count, and to add context lines to make the match unique; when not to use it (a new file or a full rewrite: `write`); the durable-path rule; 2 examples.
  - `write`: add one sentence: "To change part of an existing file, use edit".

### `query_telemetry` for the primary (D7.2)

- Remove `worker_only=True` and its comment from `tools/telemetry_query.py`.
- Description: add one sentence near the top. "Use it instead of bash with curl to Elasticsearch or Tempo: it knows the fields, a refusal lists the allowed fields, and the result is bounded."
- The `worker_only` mechanism in `ToolDefinition`/`ToolRegistry` stays. No tool sets it after this change. Removing the mechanism is not in T4.

### Fold-ins

- `orchestrator/tool_result_digest.py:469`: a successful `edit` releases the read pins of its path, as `write` does. The pinned read is stale after the edit. Test in `tests/personal_agent/orchestrator/test_intra_turn_digest.py`.
- `src/personal_agent/tools/AGENTS.md`: the primitive list gains `edit`, if it lists them.
- No change to `docs/skills/` (owner/master: FRE-1577 runs on build2 there; T6 converts the telemetry skills).

## Steps (TDD)

1. **Failing tests for `edit`.** Write `tests/test_tools/test_primitives_edit.py`:
   - one match: the bytes equal the original with only that span replaced, and `line` is correct
   - zero matches: `no_match`, "0" in the message, bytes unchanged
   - two matches: `multiple_matches`, "2" in the message, bytes unchanged
   - a path outside the allowed paths (`/etc/hosts`): refused, file unchanged
   - a non-durable `/app/` path: `not_durable`
   - plus the other codes: empty, missing, directory, binary, `\r\n` kept, permission denied, IO error
   - Through the layer: `ToolExecutionLayer.execute_tool("edit", {"path": "/etc/x", ...})` refuses before the call (`path_refused`), and an `edit` in ALERT asks.

   Run: `uv run --no-sync python -m pytest tests/test_tools/test_primitives_edit.py -q`. Expect: collection error, because there is no module.
2. **Implement `edit.py`**, the `tools.yaml` row, the permission rules and the registration. Run the same tests again. Expect: pass.
3. **AC-2.** Add `edit` to `EXPECTED_TOOLS`. Run `tests/personal_agent/tools/test_tool_contract.py`. It fails first with "missing: ['edit']" before registration, if run in that order.
4. **AC-3.** Replace `test_the_primary_is_not_offered_the_worker_only_tool` with `test_the_primary_is_offered_query_telemetry`, and change `test_telemetry_query.py:98` to `worker_only is False`. Run them: they fail. Remove `worker_only=True` and update the description. Run them again: they pass.
5. **Digest fold-in:** a test first, then `tool_name in ("write", "edit")`.
6. **Gates:**
   - `uv run --no-sync python -m pytest -m "not integration" -q --cov=src/personal_agent/tools --cov=src/personal_agent/mcp --cov=src/personal_agent/orchestrator --cov-report=json:coverage-tools.json`
   - then `uv run --no-sync python scripts/check_tool_failure_coverage.py coverage-tools.json`. Expect: every line in `edit.py` executed, 0 conditional.
   - `make mypy`, `make ruff-check`, `make ruff-format` and `uv run --no-sync pre-commit run --all-files`.
7. Commit. Run the self-review (`feature-dev:code-reviewer` and `security-review`, because the change touches files and governance), fix the findings, rebase, push and open the PR. Post the handoff.

## Risks

- **Primary prompt size:** `query_telemetry`'s description (the field allowlists) joins every primary turn. ADR AC-4 (G4) measures the median first-call `prompt_tokens` at the D10 gate. D7.2 accepted this cost. The handoff names the added size in tokens.
- **Diff class:** escalated (a new production write path, and governance config).

## Codex plan review — dispositions

1. **High — symlink race between the path check and the write.** Not fixed in T4. The model can create a symlink only with `ln` or `mv` through `bash`. Neither has an allow rule, so each asks the user. An approved swap would also have to land in the few milliseconds between the check and the replace. `write` has the same pattern. The handoff records the residual risk.
2. **Medium — a concurrent change between the read and the write.** `edit_executor` has no `await`, so two `edit` calls in the gateway process cannot interleave. The docstring now says to keep it free of `await` between the read and the replace.
3. **Medium — atomic replacement.** Done: `_replace_file` writes a temporary file in the same directory, copies the permission bits and calls `os.replace`. A failed write leaves the original bytes, and a test covers it.
4. **Medium — overlapping matches.** Done: `_match_offsets` counts overlapping matches. `test_overlapping_matches_count_as_several` expects 2 for "aa" in "aaa".
5. **Medium — the canonical eval coverage test.** Fixed. `edit` is allowlisted with a reason in `scripts/eval/fre453_canonical_evalset/dataset.yaml`.
6. **Low — `SIDE_EFFECTING` in `test_worker_tool_split.py`.** Fixed: `edit` added.
7. **Low — the `query_telemetry` exemption names the worker-only premise.** Fixed. The exemption is removed, and `infra_triage_pedagogy` accepts `query_telemetry` beside `bash`. The eval set is the owner's pedagogy set, so no new case is invented.
8. **Low — the primitive registration tests.** Fixed: they assert `edit` with the flag on and off.

YAML aliases survive the loader. `test_the_edit_path_policy_is_write_s` reads the loaded policies and compares them field by field.
