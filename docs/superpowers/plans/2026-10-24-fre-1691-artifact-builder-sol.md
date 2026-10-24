# FRE-1691 — Artifact builder binds gpt-6.1-sol, and the builder prompt carries the owner's five questions

Backing evidence: the FRE-1668 pilot report (owner decision 2026-10-24). Design intent: ADR-0122
(build-time artifact builder selection) keeps `artifact_builder` an `open: true` role. Only the default changes.

## Scope (from the ticket)

1. `config/model_roles.yaml`: `artifact_builder` deployment `claude_sonnet` → `gpt-6.1-sol`, `open: true` kept.
2. Fix the comments that go stale: the `artifact_builder` pin comment (model_roles.yaml) and the
   `gpt-6.1-sol` "binds captains_log only" comment (models.yaml).
3. Add a QUALITY item after INTERACTIVITY in `_HTML_GENERATION_SYSTEM_PROMPT`
   (`src/personal_agent/tools/artifact_tools.py`) naming the five questions.
4. No change to the output ceiling (`settings.artifact_draft_max_tokens`), the timeout, any other binding,
   or `config/governance/budget.yaml`.

## Acceptance criteria

| ID | Criterion | Proof |
|----|-----------|-------|
| AC-1 | Only the `artifact_builder` line of `model_roles.yaml` changes, to `gpt-6.1-sol` with `open: true`. Default resolution is Sol. | `test_fre1691_artifact_builder_sol.py`: binding equals `{deployment: gpt-6.1-sol, open: true}`; `resolve_role_model_key` and `get_llm_client(role_name="artifact_builder").model_id` give Sol; every other binding resolves as before; Sol binds exactly `{captains_log, artifact_builder}`. `git diff -- config/model_roles.yaml` shows one binding line plus comments. |
| AC-2 | The builder system prompt contains each of the five questions. | Parametrized test over five distinctive phrases, one per question. A missing phrase fails that case. |
| AC-3 | Live, master. | Out of scope for the build. The handoff gives the log command. |

## Steps

1. **RED.** Create `tests/personal_agent/config/test_fre1691_artifact_builder_sol.py` (AC-1) and add the AC-2 test to
   `tests/personal_agent/tools/test_artifact_tools.py`.
   Run: `make test-file FILE=tests/personal_agent/config/test_fre1691_artifact_builder_sol.py` and `make test-k K=quality_questions`. Expect failures.
2. **Config.** Edit the binding line and the two stale comments.
3. **Prompt.** Add the QUALITY item after INTERACTIVITY. Keep the `\` line-continuation style of the prompt. Do not drop a question.
4. **Update every test that pins `claude_sonnet` as the builder default** (from grep):
   - `tests/personal_agent/config/test_model_loader_roles.py:105`
   - `tests/personal_agent/config/test_resolve_cli.py:42-44`
   - `tests/personal_agent/gateway/test_session_api.py:978`
   - `tests/test_llm_client/test_factory_artifact_builder.py` (class docstring, test name, expected key)
   - `tests/personal_agent/config/test_fre1646_captains_log_binding.py::test_only_captains_log_binds_sol`
   - `tests/personal_agent/llm_client/test_fre1625_new_cloud_entries.py` (AC-3 "pickable, not bound" test)
   - `tests/personal_agent/orchestrator/test_artifact_builder_turn_start.py:124` (docstring names the default)
   Tests that pass `ConstraintDecision("claude_sonnet", "user_choice")` model an owner pick, not the default. They stay.
5. **Golden snapshot.** Hand-edit the `binding|artifact_builder` cell of `tests/personal_agent/config/catalog_snapshot_golden.json`
   to the Sol definition (copy the `binding|captains_log` cell, keep the `key` field `artifact_builder`). Add a "seventeenth rebaseline"
   paragraph to the module docstring of `test_catalog_snapshot.py`, same shape as the fifteenth (FRE-1646).
   Do not regenerate (the regenerator re-sorts cells and copies `.env` endpoints).
6. **Prompt corpus.** Run `make render-prompt-corpus`. Check that the diff only adds the QUALITY text.
7. **Gates.** `make test` (affected dirs first, then the full suite once), `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
8. Commit. Self-review (`feature-dev:code-reviewer`; `security-review` only if the diff touches inputs). Rebase, push, open PR, post handoff.

## Risks and checks

- **Cost lane.** The `artifact_builder` budget lane is unchanged. Sol costs about half of Sonnet per build (pilot). No cap change.
- **Egress.** The plan text and the owner's request go to OpenAI for builds. `captains_log` already sends conversation text to OpenAI (FRE-1646).
  `OPENAI_API_KEY` is already in use. If the key is missing, the builder pick fails closed to the existing availability check. Verify with the constraint-option tests.
- **Golden drift.** The golden test is the guard that no other role moved. Compare the golden diff to the `captains_log` cell before commit.
- **Prompt budget.** Adding about 120 tokens to the system prompt is small against the 32768 output ceiling. No ceiling change.
