# FRE-1579 — Convert the 3 authoring skills (ADR-0156 T7)

**Ticket:** FRE-1579 · **ADR:** ADR-0156 D5.1–D5.5 · **Blocked by:** FRE-1577 (merged)
**Tier:** Standard. No `src/` change. The diff adds one script that starts a subprocess, and it edits
tests that read the skill files.

## Scope

Convert `artifact-design`, `mermaid-diagrams` and `sequential-thinking` to the S1 folder format
of ADR-0156. Each skill gets `evals.yaml` (written first), a `## Success` section and a
`## Recovery` section. `artifact-design` is split into `SKILL.md` and `references/`.
The `mmdc` check of `mermaid-diagrams` moves into a tested script.

## Findings that change the plan

| # | Finding | Evidence | Effect |
|---|---|---|---|
| F1 | The skill says "`mmdc` exits 0 even on parse failure". This is false for `@mermaid-js/mermaid-cli` 11.17.0. | Run on a broken diagram: exit 1, no SVG, error on stderr. | The new text states the observed behavior. The script still checks the exit code AND the output file. |
| F2 | The old recipe writes to the fixed path `/tmp/m.svg` and tests it with `test -s`. A stale file from an earlier good diagram makes a broken diagram read VALID. | Reproduced: stale SVG + broken diagram → the old recipe printed VALID. | The script writes to a fresh temporary directory on every run. A seeded-negative test covers it. |
| F3 | `python3` asks (owner decision 2026-10-10, ADR-0156 D8.2). `write` can change files under `/app/**`, so an allow rule for a script path would let the model change the script and run it. | `config/governance/permissions.yaml`, `config/governance/tools.yaml` (`write.allowed_paths`). | The script call is a documented ask (`# ask:` line, D5.5). No allow rule is added. This keeps the diff out of governance config. |
| F4 | `test_artifact_design_doc_matches_manifest` reads only `SKILL.md`. The URLs, the gotchas and the typography recipes move to `references/`. | `tests/personal_agent/tools/test_artifact_tools.py:2298`. | The test reads the whole skill folder. |
| F5 | The D8.7 test reads `docs/skills/*/SKILL.md` only. | `tests/personal_agent/governance/test_skill_commands_permissions.py:53`. | No `bash` fence goes into `references/`. The command that the script builds is checked in the script test. |
| F6 | The bash tool runs in the process cwd, which is `/app` in the image. | `Dockerfile.gateway` `WORKDIR /app`; `bash.py` sets no `cwd`. | The body uses the repo-relative path `docs/skills/mermaid-diagrams/scripts/validate_mermaid.py`. |
| F7 | A broken `pie` diagram (`"a" : abc`) makes `mmdc` exit 0 and write an SVG. The SVG is mermaid's error diagram: the root `<svg>` has `aria-roledescription="error"`. The exit code and the file check alone call it VALID. | Run on the real `mmdc` 11.17.0; 1 of 9 broken diagrams. | The script also refuses an SVG whose root tag carries `aria-roledescription="error"`. A seeded-negative test covers it. |
| F8 | A browser failure looks like a diagram failure: exit 1, no SVG. Its first line is `Error: Tried to find the browser at …` or `Configuration file … doesn't exist`. | Run on the real `mmdc` with a bad browser path and a bad `-p` file. | The script reads the first line of the error. A start-anchored match on those phrases gives `ERROR` (exit 2). Every other failure gives `INVALID`. Diagram text can echo later in the message, never at the start of the first line. |

## Steps

Each step ends with a command and its expected result. Run one pytest process at a time.

### 1. `evals.yaml` first, and the validator marks out (D5.2, AC-1)

Create `docs/skills/{artifact-design,mermaid-diagrams,sequential-thinking}/evals.yaml`, 3 or more
scenarios each, in the template format (`skill`, `scenarios[].prompt`, `scenarios[].expect`).
Every prompt is a request that needs only this skill.

In `tests/docs/test_skill_validator.py`, remove at once every T7 mark: `_T7`, the three
`_CONVERTED_IN` entries, and `("artifact-design", "body_tokens")`. The marks are strict, so a
check that passes while marked fails CI. Without the marks the 3 skills show the real red state.

Verify (red, as expected): `uv run python -m pytest tests/docs/test_skill_validator.py -k "artifact-design or mermaid-diagrams or sequential-thinking" -q` →
`evals` passes; `sections` fails for the 3 skills (`mermaid-diagrams` has `Failure Recovery`
but no `Success`); `body_tokens` fails for `artifact-design`. Steps 2, 4 and 6 turn them green.

### 2. `sequential-thinking` (D5.1)

Add `## Success` and `## Recovery`. Replace the stale phrase "and for the keyword path" (the
keyword path was deleted in FRE-1577). No other change to the body.

Verify: `uv run python -m pytest tests/docs/test_skill_validator.py -k "sequential-thinking" -q`.

### 3. `mermaid-diagrams` script, tests first (D5.4)

1. Write `tests/docs/test_validate_mermaid_script.py`. A fake `mmdc` on `PATH` repeats the
   behavior seen on the real binary (F1). Cases:
   - good diagram → `VALID`, exit 0;
   - parse error (exit 1, no SVG, stack frames on stderr) → `INVALID`, exit 1, the error text
     cut before the first stack frame;
   - fake that exits 0 and writes no file → `INVALID` (the output file is the proof);
   - fake that exits 0 and writes an empty file → `INVALID`;
   - fake that exits 0 and writes mermaid's error SVG (root `aria-roledescription="error"`) →
     `INVALID` (F7), and a good SVG whose label text says "Syntax error in text" → `VALID`;
   - fake that exits 1 with the first line `Error: Tried to find the browser at …`, and another
     with `Configuration file "…" doesn't exist` → `ERROR`, exit 2 (F8);
   - fake that exits 1 with a parse error whose echoed diagram text holds the word "browser" →
     `INVALID`, exit 1 (the start-anchored match must not fire);
   - stale SVG next to the input, fake exits 1 → `INVALID` (F2);
   - `mmdc` not on `PATH` → `ERROR`, exit 2;
   - input file missing → `ERROR`, exit 2;
   - fake that sleeps, `--timeout 1` → `ERROR`, exit 2;
   - `--help` → usage text, exit 0;
   - the command the script builds is `allow` under `config/governance/permissions.yaml` (D8.7).
2. A test with the real `mmdc` (`skipif` when `mmdc` is not on `PATH`): one good diagram
   `VALID`, one broken diagram `INVALID`. This test is the AC-2 proof on a machine that has `mmdc`.
3. Run the tests, confirm all fail (no script yet).
4. Write `docs/skills/mermaid-diagrams/scripts/validate_mermaid.py` (standard library only).
   Output: first line `VALID`, `INVALID` or `ERROR`; then plain text with the next action.
   Exit codes: 0 valid, 1 invalid, 2 the check could not run.
5. Run the tests, confirm they pass. Run the real-`mmdc` test with the scratchpad install.

Verify: `uv run python -m pytest tests/docs/test_validate_mermaid_script.py -q`.

### 4. `mermaid-diagrams` body (D5.1, D5.4)

- Validation section: write the diagram to `/tmp/m.mmd` with the `write` tool (allowed), then
  run the script (documented ask). State the real behavior (F1) and the fresh-directory rule (F2).
- Add `## Success`. Rename `## Failure Recovery` to `## Recovery` and merge the repair loop into it
  as one table with a stop rule. The table has a row for `ERROR` (the check could not run: do not
  repair the diagram). Keep every other section.
- The `# ask:` line and the command stay in one fence with no blank line between them: the D8.7
  extractor cuts a fence at blank lines. Skill files are written as inline code, never as local
  Markdown links (the validator reads every relative link as a skill file).

Verify: `uv run python -m pytest tests/docs/test_skill_validator.py -k "mermaid" -q` and
`uv run python -m pytest tests/personal_agent/governance/test_skill_commands_permissions.py -q`.

### 5. `artifact-design` split, tests first (D5.3, AC-3)

1. Widen the reader of `test_artifact_design_doc_matches_manifest` to `SKILL.md` plus
   `references/*.md`, and change its docstring to name the skill folder. It stays green before the
   split.
2. Write `tests/docs/test_artifact_design_split.py` and see it fail:
   - for each non-eval-gated asset of the manifest, the URL is in that library's own reference
     file (`katex`, `chartjs`, `three`, `highlightjs`, and the three fonts);
   - those 5 reference files each hold a `Gotchas` list (only the libraries whose section has one
     today; the paged.js section has none);
   - `references/pagedjs.md` holds the "experimental" and "eval_gated" prose and does NOT hold
     its URL (the existing drift guard requires it to stay absent);
   - `SKILL.md` names every file of `references/`.
3. Do the split below. Move each library section verbatim into `references/` (one file per
   library). Keep the rest in `SKILL.md`. The map (AC-3):

| Section of today's file | New home |
|---|---|
| Frontmatter, "What This Skill Does", "When to Use" | `SKILL.md` |
| The Curated `/lib/` Shelf — intro (absolute pinned URL rule) | `SKILL.md`, with a table of the 6 reference files |
| KaTeX (boot recipe + gotchas) | `references/katex.md` |
| Chart.js (boot recipe + gotchas) | `references/chartjs.md` |
| three.js (boot recipe + gotchas) | `references/threejs.md` |
| highlight.js (boot recipe + gotchas) | `references/highlightjs.md` |
| OFL fonts (boot recipe + gotchas) | `references/ofl-fonts.md` |
| paged.js (experimental, gated) | `references/pagedjs.md` |
| Native Typography | `SKILL.md` (default path, small) |
| Design-System Conventions | `SKILL.md` |
| Sealed-Box Constraints | `SKILL.md` |
| Portability (3 lanes) | `SKILL.md` |
| Standing rule: never bake secrets | `SKILL.md` |
| Distillation Map, E2E verification, References | `references/maintenance.md` |
| New: Success, Recovery | `SKILL.md` |

Verify: every fenced block and every gotcha bullet of the old file is found verbatim in the new
folder (a one-time script in the scratchpad against `git show origin/main:…`); the body is under
5,000 tokens and 500 lines.

### 6. Green run of every reader (F4)

Verify: `uv run python -m pytest tests/docs tests/personal_agent/tools/test_artifact_tools.py tests/personal_agent/governance/test_skill_commands_permissions.py -q` →
no failure, no XPASS.

### 7. Generated documents

`make render-prompt-corpus` (as FRE-1577 did). Review the diff: only the 3 skills change.

### 8. Gates and review

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Commit. Then `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 conformance | `tests/docs/test_skill_validator.py` with no xfail for the 3 skills; `artifact-design` body under 5,000 tokens (measured value in the handoff) |
| AC-2 scripts run | `tests/docs/test_validate_mermaid_script.py` (good → VALID, broken → INVALID, with the real `mmdc` and with the fake); `test_skill_commands_permissions.py` passes |
| AC-3 nothing lost | The map above in the PR; `test_artifact_design_split.py`; `test_artifact_design_doc_matches_manifest`; the one-time verbatim check |

## Out of scope

- An allow rule for the script (F3). It needs an owner decision. The handoff names it.
- Skill selection quality. T9 (FRE-1581) measures it.
- The other 8 skills (T6, FRE-1578).
