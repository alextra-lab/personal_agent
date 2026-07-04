# FRE-652 — ADR-0099 stage 4: consolidate/retire model-def YAMLs + assembled-seam gate

**Linear**: [FRE-652](https://linear.app/frenchforest/issue/FRE-652) (Approved · High · Tier-2:Sonnet · stream:build2)
**Branch**: `fre-652-adr-0099-stage-4-consolidate-model-def-yamls` (off `origin/main`)
**Backing**: [ADR-0099](../../architecture_decisions/ADR-0099-configuration-management-and-validation.md) — D1 staged delivery, stage 4 (final). Depends on FRE-650 (stage 2, generative loader) and FRE-651 (stage 3, provenance manifest) — both merged to `main` (`9e93d14`, `#361`/`#363`).

---

## Scope reconciliation (current reality vs. the ADR's 2026-06-28 text)

The ADR's stage-4 line item names three files to retire: `models.eval.yaml`, `models-baseline.yaml`, `models.medium.yaml`. Per `docs/reference/CONFIG_INVENTORY.md` §0 (FRE-648 audit) and confirmed by direct inspection this session:

- **`config/models.eval.yaml` no longer exists** — already retired by FRE-735 (`docker-compose.eval.yml` now points at `models.cloud.yaml`). Nothing to do here.
- **`config/models-baseline.yaml`** — exists. No production or test code loads it (`grep` across `src/`, `tests/`, `scripts/`, `docker-compose*.yml`, `Makefile` returns nothing). Manual-only, invoked via `AGENT_MODEL_CONFIG_PATH=config/models-baseline.yaml uv run uvicorn ...` for a one-time, already-completed foundation-model-baseline eval (`docs/plans/completed/2026-03-24-foundation-model-baseline.md`). Carries **drifted** `claude_sonnet` (`claude-sonnet-4-6` vs. live `claude-sonnet-5` — CONFIG_INVENTORY §3, finding F2).
- **`config/models.medium.yaml`** — exists. No production or test code loads it either; `tools/TOOLS_OVERVIEW.md`'s one reference is inside a section already marked `## Legacy Documentation (for reference)` / `**Status**: moved to slm_server` — the tool it invoked (`tools/benchmark_models.py`) no longer exists in this repo at all. Declares **no role headers**, so if ever loaded live it would silently route extraction/log/insights to `primary` (CONFIG_INVENTORY §2, finding F4).
- **`config/models.benchmark-{8b,4b,4b-f16}.yaml`** — exist, added 2026-06-30 (after the ADR was written). **Not named in the ADR or in FRE-652's ticket body.** Out of scope for this ticket; `config/model_roles.yaml`'s own comment already documents them as intentionally excluded from `active_profiles`. Leaving them untouched — no drift they cause is part of this ticket's acceptance criteria.

**Net scope**: delete `config/models-baseline.yaml` and `config/models.medium.yaml`. This leaves exactly two model-definition files in play for the matrix: `config/models.yaml` (local) and `config/models.cloud.yaml` (cloud) — matching `config/model_roles.yaml`'s `active_profiles`. That *is* "subsumed by the matrix + profiles."

## Assembled-seam acceptance (the four checks master runs at the integration gate)

Per the ADR and the ticket body, verbatim:

1. `uv run python scripts/check_config.py` green against the real repo.
2. AC-2(a) grep (`grep -rEl '^(entity_extraction|captains_log|insights|compressor|embedding|reranker)_role:' config/*.yaml` → empty) **and** AC-2(b) deterministic-failure test pass.
3. `config-resolve --profile cloud --role entity_extraction` → `gpt-5.4-mini`, from committed files only.
4. AC-1 green — one resolved `ModelDefinition` per forbidden role across profiles.

Checks 2(b), 3, and 4 already have passing pytest coverage from stages 2/3 (`test_role_resolution_golden.py`, `test_model_loader_roles.py`, `test_resolve_cli.py`). Check 1 (`scripts/check_config.py`) already passes today (`test_check_config.py::test_passes_on_real_repo`). **The one gap**: AC-2(a) — the grep — is described in the ADR/ticket as a command master runs by hand; nothing in the codebase enforces it continuously in CI/pre-commit. That's exactly the kind of regression this "assembled-seam gate" ticket exists to close permanently, not just prove once. This ticket adds it as a first-class guard check (mirroring the existing `config_guard.py` check pattern) so a role header reintroduced in any future PR fails CI the same way the other seam checks do.

## Implementation steps

1. **Delete the two files**: `git rm config/models-baseline.yaml config/models.medium.yaml`.
2. **Add `check_no_role_headers` to `src/personal_agent/config/config_guard.py`** (new function, same pattern as the other checks): scans `config/*.yaml` for a line matching `^\s*(entity_extraction|captains_log|insights|compressor|embedding|reranker)_role\s*:` and returns a `policy`-severity `Finding` per hit (message names the file:line and the offending role). Wire into `run_all_checks`.
   - *Why policy, not safety*: a stale header is a maintainability regression (the loader already ignores it since stage 2 — D1 made the header a no-op) — it can't wedge a boot; ADR-0099 D4's default-to-policy rule applies. Mirrors the guard's existing severity discipline.
3. **Test fixture + unit tests** (`tests/personal_agent/config/fixtures/role_header_reintroduced/config/some_model.yaml` — a minimal YAML with one planted `entity_extraction_role: foo` line) in `tests/personal_agent/config/test_check_config.py`:
   - `test_fails_on_reintroduced_role_header_fixture` — `run_all_checks` on the fixture root flags `role_header_reintroduced`, names the role, **and asserts `severity == "policy"`** (locks in the classification per codex plan-review — ADR D4 defaults new/unclassified findings to policy, not safety).
   - `test_no_false_positive_on_real_repo` — `run_all_checks(_REPO_ROOT)` still returns `[]` after the deletion (this is the actual AC-2(a) proof, now permanent).
4. **Regression test that the two files are gone** (`tests/personal_agent/config/test_check_config.py` or a small new test): assert `not (repo_root / "config/models-baseline.yaml").exists()` and same for `models.medium.yaml` — cheap, direct proof of the ticket's literal scope line, survives independent of the grep guard.
5. **Doc updates** (no behavior, but these are stale the moment the files are gone):
   - `config/model_roles.yaml` lines 17-18: drop the `models-baseline.yaml` mention, keep the `models.benchmark-*.yaml` exclusion note.
   - `docs/reference/CONFIG_INVENTORY.md`: mark §2/§3 rows for `models-baseline.yaml`/`models.medium.yaml` retired; update §9 finding F4 (header-less fallback — now closed, the file is gone) to "Resolved (FRE-652)". For F2 (definition drift), do **not** claim it globally closed — per codex plan-review, the same `claude_sonnet` `claude-sonnet-4-6` drift the finding names also lives in the three untouched `models.benchmark-*.yaml` files (`claude-sonnet-5` live vs. `claude-sonnet-4-6` benchmark). Word it as "Resolved for the retired files (FRE-652); the same drift persists, out of this ticket's scope, in `models.benchmark-*.yaml`" so the doc doesn't overclaim.
   - `docs/architecture_decisions/ADR-0099-configuration-management-and-validation.md`: append a **Status Updates** entry — stage 4 delivered, all four assembled-seam checks pass, ADR → **Implemented**. (Per the ADR's own text: "The ADR does not move to Implemented until all four pass together" — this ticket is what makes that true, so the status flip belongs here, subject to master's gate confirming it live on the assembled branch.)
6. **Quality gates**: `make test-file FILE=tests/personal_agent/config/test_check_config.py`, then `make test` (full — config changes are widely imported), `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria this ticket proves

- [ ] `config/models-baseline.yaml` and `config/models.medium.yaml` no longer exist (test: file-absence assertions).
- [ ] AC-2(a) is CI-enforced going forward, not just manually checkable (test: `check_no_role_headers` fixture + real-repo clean).
- [ ] `scripts/check_config.py` still exits 0 on the real repo post-deletion (existing test, re-run to confirm no regression).
- [ ] AC-1 (`test_role_resolution_golden.py`), AC-2(b), AC-5 (`test_resolve_cli.py`) all still pass post-deletion (existing tests, re-run to confirm no regression — they don't touch the deleted files, but full-suite proof is part of the seam).
- [ ] `config-resolve --profile cloud --role entity_extraction` → `gpt-5.4-mini` (manual verification command for the PR/ticket comment, in addition to the existing test).

## Follow-up (out of scope, to file as Needs Approval if not already tracked)

- The three `models.benchmark-*.yaml` files are undocumented in the ADR and excluded from the guard by convention-only (a comment, not an enforced allow-list). Whether they should get an explicit allow-list check (so a *new* stray model-def YAML can't silently reappear) is a real open question CONFIG_INVENTORY.md's F6 already flagged — but it's not named in this ticket's scope or the ADR's four seam checks, so it stays out of this PR.
