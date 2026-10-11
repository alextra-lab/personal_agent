# FRE-1577 — ADR-0156 T5: skill loader and layout

**Ticket:** FRE-1577 (Approved, stream:build2, Tier-1). **ADR:** ADR-0156 D1–D4, D5.7, D5.8.
**Blocked by:** FRE-1575 (T3), merged in PR #1249. **Risk tier:** Complex (src/ logic, prompt
assembly, a tool rewrite, deleted code paths). Codex plan review is required.

## Scope

1. Move the 11 agent skills to `docs/skills/<name>/SKILL.md` with S1 frontmatter (`name`,
   `description`, `metadata`). Delete the 8 folded skills and `seshat-delegate`. Move the 3
   `seshat-*` skills to `docs/external-skills/<name>/SKILL.md`.
2. Add the S1 validator as a CI test, with the inventory and a seeded negative.
3. Replace keyword routing, the hint loop, the Haiku router and the always-on `bash` body with
   one catalog line per skill in the cached system prompt.
4. Rewrite `read_skill` as `read_skill(name, path?)`: `name` is an enum, `path` names one listed
   file in the skill folder, anything else is refused.
5. Keep a loaded skill body in the context for the whole turn.
6. Update every reader of `docs/skills/`.

## Acceptance criteria (from the ticket)

| AC | Proof |
|---|---|
| AC-1 inventory | `tests/docs/test_skill_validator.py`: 11 agent folders, 3 external folders, every S1 rule, seeded negative (1,025-char description) |
| AC-2 message selects nothing | `tests/personal_agent/orchestrator/test_skill_catalog_prompt.py`: drives `step_llm_call` twice (plain message, every saved keyword); system prompts equal; no skill body in system prompt or messages |
| AC-3 read_skill bounded | `tests/personal_agent/tools/test_read_skill.py`: unknown name, `../bash/SKILL.md`, absolute path, symlink out, symlink in, directory, unlisted file refused; listed file returned |
| AC-4 scripts ship | Static test: `Dockerfile.gateway` copies `docs/skills/` whole and `.dockerignore` excludes nothing under it. Live: master runs `docker exec seshat-gateway find /app/docs/skills -type d -name scripts` after the build (no skill has `scripts/` until T6/T7, so today the expected output is empty and the folder layout is present) |
| AC-5 live | Master: one owner PWA turn after deploy, then check that no skill body is in the first-call prompt without a `read_skill` call |

## Decisions and assumptions (stated for review)

- **D1 checks that fail today are strict xfail, never skipped.** The ticket allows this for the
  success/recovery sections and `evals.yaml` (T6, T7). Two more checks fail today for content
  that T6/T7 own (D5.3, D5.4), and they are also strict xfail with the ticket named:
  - body size (> 5,000 tokens at 4 chars/token): `artifact-design` (~5,300), `query-elasticsearch` (~5,400);
  - a named file that is not in the skill folder: `artifact-design` names
    `scripts/build_e2e_artifact_fixtures.py`, `query-elasticsearch` names
    `scripts/es_index_granularity.py` (both repo paths, not skill files).
  `strict=True` makes each one fail CI when T6/T7 fix the content, so the mark is removed then.
- **Token estimate** is 4 characters per token, the repo's existing estimate.
- **Third person** (S3) is checked as: no `I`, `me`, `my`, `you`, `your` word in `description`.
- **External skills**: the 3 `seshat-*` files get S1 frontmatter. Their 2 mentions of
  `./scripts/init-services.sh` are reworded, because S1 reads `scripts/…` as a skill file.
- **Catalog** is deterministic (sorted by name), has no token cap, and stays in the cached
  prefix, gated by `prefer_primitives_enabled` as today. The header follows S2's activation
  text. The "propose a new skill" gap-signal line goes: `name` is now an enum.
- **`read_skill` result:** no `path` → `{skill_name, base_dir, body, files}`; with `path` →
  `{skill_name, path, content}`. `files` lists regular files under the folder, not following
  symlinks, without `SKILL.md`. A `path` must equal one listed entry exactly. The file is
  opened with `O_NOFOLLOW` and its real path must stay inside the folder. Non-UTF-8 is refused.
- **Dedup** keys on `name` or `name/path`, per turn (`ctx.loaded_skills` is per request). The
  marker text says "earlier in this turn", not "this conversation".
- **Body never pruned within its turn** (D2.4). Four paths can drop a tool result mid-turn:
  1. `_trim_messages_for_context_retry` (executor) — skip `read_skill` results;
  2. `_trim_for_landing` (sub_agent) — skip `read_skill` results;
  3. ADR-0085 digest (off by default) — add `read_skill` to `_NEVER_DIGEST_TOOLS`;
  4. ADR-0061 hard compression (user-consented, ADR-0076) — keep `read_skill` call/result pairs
     out of the summarised middle band and re-emit them, verbatim, after the summary.
- **Removed as orphans of this change:** `route_skills`, `get_skill_block`, `get_skill_bodies`,
  `fit_skill_bodies`, `find_skills_for_tool`, `assemble_skill_index_directive`,
  `assemble_skill_usage_directives`; settings `skill_routing_mode`, `skill_routing_model_key`,
  `skill_index_max_tokens`, `skill_bodies_max_tokens`, `skill_nudge_enabled`; the per-request
  `set_skill_routing_mode` override and the `/chat` `skill_routing_mode` parameter;
  `ctx.skill_routing_done` and `ctx.skill_routing_model_id`; the ADR-0066 D2 threshold monitor
  (it recommends a mode that no longer exists) with its 3 settings and scheduler hook; eval
  pass-through of `AGENT_SKILL_ROUTING_MODE` (compose, `fre1537/gateway.py`,
  `recovery_harness.py`). `settings` uses `extra="ignore"`, so a stale `.env` line does not
  break boot.
- **Kept on purpose:** `ModelRole.SKILL_ROUTING`, the `skill_routing` budget role and its caps.
  Removing a budget role is cost/governance code and old cost rows carry that role. It is
  noted in the handoff, not changed here. `scripts/eval/skill_routing_analysis.py` and its
  Makefile targets read historical run directories, so they stay.
- **Turn evidence** keeps `skill_body_names` (always empty now) and the `skill_bodies`
  component id is no longer stamped. `route_trace.skills_loaded` reads `ctx.loaded_skills`.
- **EMPIRICAL_TEST_RESULTS.md** moves to `docs/research/2026-04-28-skill-docs-empirical-test-results.md`
  (its own date). **SKILL_TEMPLATE.md** stays at `docs/skills/SKILL_TEMPLATE.md` (the only
  non-folder entry the validator allows) with the S1 template, the success and recovery
  sections and an `evals.yaml` example.

## Codex plan review (2026-10-11) — revisions

Each item replaces the decision above where they differ.

1. **No flag gates the catalog or the loader (D2, D3.3).** `read_skill` registers outside the
   primitive-tools block, and the catalog is always in the cached prefix.
   `prefer_primitives_enabled` has no other reader, so it goes.
2. **Path boundary without TOCTOU.** The listing records `(st_dev, st_ino)` per file. A read
   opens the skill folder, then each path part with `O_NOFOLLOW` relative to the previous
   descriptor (`dir_fd`), so no part can be a symlink. `fstat` on the opened file must be a
   regular file with `st_nlink == 1` and the recorded `(st_dev, st_ino)`.
3. **Compression keeps the exchange before the summary.** Complete, id-matched `read_skill`
   exchanges leave the middle band before the summariser sees it. A mixed assistant message
   keeps only its `read_skill` calls. The result is
   `head → kept exchanges → assistant summary → tail`, checked through
   `_validate_and_fix_conversation_roles` and the history sanitiser.
4. **Dedup only for a body read.** `loaded_skills` stays a set of names (it is
   `route_trace.skills_loaded`). A `path` read is not deduplicated.
5. **The xfails stay narrow, minus one.** `artifact-design` no longer names
   `scripts/build_e2e_artifact_fixtures.py` (a wording fix, the E2E section is about the repo
   harness). Left as strict xfail: `artifact-design` body tokens (T7), `query-elasticsearch`
   body tokens and its `scripts/es_index_granularity.py` recipe (T6). The ADR gives the split
   (D5.3) and script moves (D5.4) to T6 and T7. The 4 chars/token estimate is the repo's
   existing one; a model tokenizer is not in the gateway.
6. **Make targets.** `eval-skill-routing` and `eval-skill-routing-cloud` drive routing modes and
   go. `eval-skill-routing-analyse` reads stored runs and stays.
   `TelemetryQueries.get_skill_index_p95_chars` goes with the monitor.
7. **AC-2** builds two fresh contexts with the same fixed state and intercepts each first
   `respond()`. It compares the system prompt and every non-user message, and checks that no
   skill body text is anywhere in the request.

## Steps

1. **Test data (AC-7 of the ADR).** Write `tests/data/skill_keywords_2026-10-11.yaml`: every
   `keywords` entry of all 23 skill files today, by skill. Verify: count equals a fresh scan.
2. **Validator test first (red).** `tests/docs/test_skill_validator.py` with a pure
   `validate_skill(folder, kind)` helper in `personal_agent.orchestrator.skills`. Run →
   fails (no folders).
3. **Layout.** `git mv` each agent skill to `<name>/SKILL.md`; rewrite frontmatter per D4
   (description = merged `when_to_use`, third person, ≤ 1,024; `metadata.requires-tools`;
   `nudge` → a "## Rules" section at the top of the body; `canonical_patterns` → a short
   "## Index patterns" line). Delete the 9 files. Move the 3 external skills. Move
   `EMPIRICAL_TEST_RESULTS.md`; replace `SKILL_TEMPLATE.md`. Run the validator → green with
   the strict xfails.
4. **Loader.** `skills.py`: glob `*/SKILL.md`, `SkillDoc(name, description, requires_tools,
   body, folder)`, mtime cache over every file in the folders, `assemble_skill_catalog()`.
5. **AC-2 test (red), then executor.** Replace the routing block in `step_llm_call` with the
   catalog; drop `_skill_bodies_tail` from the volatile fence. Delete the router, the
   settings, the selection override, the `/chat` parameter.
6. **Dispatch.** Delete the hint loop; dedup by `name`/`name/path`.
7. **read_skill (AC-3 tests first).** Enum, `path`, refusals as error codes
   (`missing_name`, `unknown_skill`, `invalid_path`, `not_text`).
8. **Never pruned.** Tests for the four paths, then the four exemptions.
9. **Readers.** `prompts.py`, `reflection_dspy.py`, `telemetry_query.py`,
   `render_prompt_corpus.py` (+ regenerate `docs/reference/PROMPT_CORPUS.md`),
   FRE-453 dataset/README/test, `test_skill_commands_permissions.py`,
   `test_skill_contract.py`, `test_artifact_tools.py`, `test_bash_shell_contract.py`
   docstring, `.env.example`, `docs/reference/CONFIG_INVENTORY.md`,
   `docs/reference/TOOL_INTEGRATION_GUIDE.md` (Tier 2 = S1 folder).
10. **Delete obsolete tests** (`test_route_skills.py`, the threshold monitor tests) and rewrite
    the skill tests that assert keyword routing or bodies (`test_skills.py` ×2,
    `test_skill_injection.py`, `test_skill_index_split.py`, `test_fre1529…`, `test_fre1549…`,
    `test_content_widening.py`, `test_prompt_identity_wiring.py`, `test_scheduler.py`).
11. **AC-4 static test** in the validator file.
12. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
    `pre-commit run --all-files`. Self-review, PR, handoff.

## Test commands

```bash
make test-file FILE=tests/docs/test_skill_validator.py
make test-file FILE=tests/personal_agent/orchestrator/test_skill_catalog_prompt.py
make test-file FILE=tests/personal_agent/tools/test_read_skill.py
make test-k K=skill
make test
```

## Diff class

Escalated: prompt assembly on the production path, deleted code paths (router, monitor),
a tool rewrite with a path boundary. Flag for owner `/code-review ultra` before merge.
