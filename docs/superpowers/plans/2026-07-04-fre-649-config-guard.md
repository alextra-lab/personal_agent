# FRE-649 — ADR-0099 stage 1: cross-config guard + divergence policy + drift correction

**Ticket:** [FRE-649](https://linear.app/frenchforest/issue/FRE-649) · **ADR:** [ADR-0099](../../architecture_decisions/ADR-0099-configuration-management-and-validation.md) §D1 (intent layer) + §D4 (tiered guard) · **Feeds from:** [FRE-648](https://linear.app/frenchforest/issue/FRE-648) (`docs/reference/CONFIG_INVENTORY.md`) · **Blocks:** FRE-650 (stage 2, generative loader)

## Scope decisions (surfaced before coding — not silently assumed)

1. **Which roles go `forbidden` at this stage.** ADR-0099's own example matrix marks `entity_extraction`, `captains_log`, `insights`, `compressor`, `embedding`, `reranker` all `forbidden`. The ticket's "Correct the drift" bullet names only three: `entity_extraction → mini`, `captains_log/insights → claude_sonnet`. Reading `config/models.yaml` vs `config/models.cloud.yaml` directly (not just the ADR's stale table) turned up **two more real divergences the ticket does not ask us to correct**:
   - `compressor`: local `gpt-5.4-nano` vs cloud `gpt-5.4-mini` — same drift *class* as extraction, uncorrected.
   - `reranker`: local `Qwen3-Reranker-0.6B` vs cloud `Qwen3-Reranker-4B-mxfp8` — looks **deliberate** (FRE-696 comments explain an MLX-vs-llama.cpp reliability tradeoff), not a bug.
   - `embedding` is the one cognitive-pipeline role that is **already consistent** across both active profiles (`Qwen/Qwen3-Embedding-0.6B` in both) — marking it `forbidden` requires no correction and cannot go red.

   Marking `compressor`/`reranker` `forbidden` today without correcting them would make the guard **red on day one** — exactly what ADR-0099's staged-delivery note forbids ("never a red-on-day-one or vacuous-no-op guard"). **Decision (revised after codex plan-review):** `config/model_roles.yaml` marks `forbidden` the three named roles (`entity_extraction`, `captains_log`, `insights`) **plus `embedding`** (free — already consistent, moves stage 1 closer to the ADR's full target policy without any drift correction needed); `primary`, `sub_agent`, `compressor`, `reranker` stay `allowed` for now. This is an **explicit, ticket-scoped exception to ADR-0099's stated end-state policy** (which wants `compressor`/`reranker` forbidden too) — call this out by name in `config/model_roles.yaml`'s comments and in the master handoff comment, not just in this plan. File a follow-up ticket (Step 5) for the owner to decide `compressor`/`reranker` policy deliberately — reranker in particular may be an intentional permanent `allowed` exception to the ADR's own example, not a bug.

2. **"Active profiles" = the two files actually deployed**, not all 6 role-bearing YAMLs on disk. Per `CONFIG_INVENTORY.md` §6, only `docker-compose.cloud.yml` and `docker-compose.eval.yml` run the agent service, and both pin `AGENT_MODEL_CONFIG_PATH=.../models.cloud.yaml`; local dev (`make dev`) uses the `model_config_path` default, `config/models.yaml`. `models-baseline.yaml` and the three `models.benchmark-*.yaml` files are eval/benchmark artifacts, never deployed. **Decision:** `config/model_roles.yaml` declares `active_profiles: {local: config/models.yaml, cloud: config/models.cloud.yaml}`; the guard's forbidden-role and dangling-reference checks operate over exactly these two files. This also matches the ticket's own drift-correction check ("local extraction == cloud == gpt-5.4-mini").

3. **AC-9 dangling-reference check, without per-profile model names in the matrix.** Ticket scope item 1 says stage 1's matrix carries *intent only* (`divergence: allowed|forbidden`), not per-profile model values (those are stage 2/D1's generative form). AC-9 talks about "a matrix entry whose model name is absent from the active profile's model-definition file" — read literally this needs a model name in the matrix, which contradicts item 1. **Resolution:** for every role in the matrix, for every active profile, resolve that profile's **actual YAML role header** (`<role>_role:`, falling back to the `ModelConfig` field default `"primary"` if the header is absent — mirroring `src/personal_agent/llm_client/models.py`'s own fallback), then check that resolved model name is a key under that YAML's `models:` mapping. A role header pointing at an undefined model name fails (safety). This is self-consistent, requires no matrix schema change beyond `divergence`, and is exactly the check that would have caught `models.medium.yaml`'s previously-silent `primary` fallback (F4) had it been role-bearing.

4. **Required-secret-per-profile (AC-6c)** needs a notion of "active profile" at `AppConfig` construction time, which the existing `environment` (dev/test/staging/prod) enum doesn't carry (`ExecutionProfile` — local/cloud — is a per-request contextvar, not known at boot). **Decision:** derive it structurally — the active profile is whichever `active_profiles` entry's file matches `settings.model_config_path`. `config/model_roles.yaml` additionally declares `required_secrets: {cloud: [anthropic_api_key, openai_api_key], local: []}`.

   **Codex plan-review flagged a landmine here:** `docker-compose.eval.yml` also sets `AGENT_MODEL_CONFIG_PATH=/app/config/models.cloud.yaml` (with `APP_ENV: eval`, which isn't a real `Environment` enum value and falls through to `development` per `env_loader.py`). Path-matching against `active_profiles` means **eval resolves to the `cloud` bucket**, since it shares the same file. **This is deliberate, not a bug, for stage 1:** eval also passes through `AGENT_ANTHROPIC_API_KEY`/`AGENT_OPENAI_API_KEY` (`docker-compose.eval.yml:163-164,195-196`), so requiring the same secrets for eval-as-cloud is correct, and ADR AC-7 (eval fidelity — eval's cognitive-pipeline roles must match `cloud`) *wants* eval treated as cloud for the forbidden-role check too. What stage 1 does **not** implement is AC-7's opt-in-divergence mechanism (a `divergence_opt_in` marker letting eval deliberately diverge from cloud) — that AC is not in this ticket's own AC list (only AC-3/4/6/8/9 + drift-correction are) and is left for whichever later stage owns it. Note this explicitly in the matrix comments and the master handoff comment so it isn't mistaken for an oversight.

5. **Secret markers are additive metadata, not a behavior change.** Add `json_schema_extra={"secret": True}` to the 8 fields `CONFIG_INVENTORY.md` §8 already identified (`anthropic_api_key`, `openai_api_key`, `perplexity_api_key`, `linear_api_key`, `neo4j_password`, `cf_access_client_secret`, `r2_secret_access_key`, `artifact_resolve_internal_token`). The guard derives its secret-field list by introspecting this marker — adding a 9th secret field later auto-extends coverage with no guard edit (ADR-0099 D2).

6. **Startup-validator test seam (codex: blocking finding, now fixed).** The original draft had the new `model_validator` read `config/model_roles.yaml` / `.env.example` directly off a hardcoded repo-root path, which (a) cannot be exercised with a planted-orphan fixture since the real `.env.example` has zero orphans today, and (b) risks breaking `test_environment_substrate_validator.py`'s `make_config()` (which builds `AppConfig` via `model_validate()` with no file-loading) if the file is ever unreadable in some test context. **Fix:** all guard logic lives in plain, `root: Path`-parameterized functions (in `scripts/check_config.py`, importable); `settings.py` adds a module-level `_repo_root() -> Path` resolver (same pattern as `env_loader`) that tests can `monkeypatch.setattr` to point at a fixture directory, and the new validator calls the check functions with `_repo_root()`. If `config/model_roles.yaml` is missing at the resolved root (degenerate case), the validator logs a warning and returns without raising — a missing intent file is a policy gap, never a safety failure (matches D4's "unclassified defaults to policy" rule), and it must never make unrelated `AppConfig()` construction in tests raise.

7. **Committed-secret heuristic (codex: blocking finding, now fixed).** The original draft's "flag anything that isn't `${...}`/`<...>`" heuristic would false-positive on `.env.example`'s own documented examples (`AGENT_ANTHROPIC_API_KEY=sk-ant-...`, `AGENT_NEO4J_PASSWORD=neo4j_dev_password`, etc.) if it scanned them at all. **Checked directly:** every one of those lines in `.env.example` is `#`-commented (verified via grep — all 8 secret fields' documented lines start with `#`; CONFIG_INVENTORY §7's "all bar one are commented examples" holds). **Fix:** the committed-secret check skips any line whose stripped form starts with `#` before applying the key=value match — comments are documentation, not committed config. A live (uncommented) assignment is then flagged unless its value is empty, a `${...}` shell interpolation, or contains `<...>` angle-bracket placeholder syntax. This makes the AC-8 fixture need a genuinely *uncommented* planted line to trigger (already the plan's fixture design) and requires no ellipsis/prefix special-casing.

## Files touched

**New:**
- `config/model_roles.yaml` — the D1 intent-layer matrix (`active_profiles`, `roles: {<name>: {divergence}}`, `required_secrets`).
- `scripts/check_config.py` — the guard (mirrors `scripts/check_no_direct_substrate_in_tests.py`'s structure: patterns/checks, `# fre-649-allow: <reason>` exemption, `sys.exit` codes).
- `tests/personal_agent/config/test_check_config.py` — guard unit tests (AC-3, AC-4, AC-8, AC-9) against fixtures.
- `tests/personal_agent/config/test_config_guard_startup.py` — tiered startup hook tests (AC-6, three cases).
- `tests/personal_agent/config/fixtures/divergent_forbidden_role/{model_roles.yaml,models.yaml,models.cloud.yaml}` — AC-3 known-bad fixture.
- `tests/personal_agent/config/fixtures/orphan_env/.env.example` (+ minimal matrix/models) — AC-4 fixture.
- `tests/personal_agent/config/fixtures/committed_secret/` — AC-8 fixture (a YAML committing `AGENT_OPENAI_API_KEY: sk-live-...`).
- `tests/personal_agent/config/fixtures/dangling_reference/` — AC-9 fixture (role header pointing at a model key absent from `models:`).

**Modified:**
- `config/models.yaml` — drift correction: `entity_extraction_role: gpt-5.4-mini`, `captains_log_role: claude_sonnet`, `insights_role: claude_sonnet` (replacing the `nano` assignments + stale commented-out lines).
- `src/personal_agent/config/settings.py` — add `secret: true` `json_schema_extra` to the 8 fields; add one new `model_validator(mode="after")` implementing the tiered policy/safety startup hook (orphan-env warn, required-secret-per-profile raise). The existing `_validate_substrate_isolation` (FRE-375) stays as-is — it already is the AC-6(a) safety case.
- `.pre-commit-config.yaml` — new `check-config-guard` hook.
- `.github/workflows/ci.yml` — new `config-guard` job (mirrors the `telemetry-surface` job: standalone script invocation, gated on `needs.changes.outputs.backend`).

## Steps

1. **`config/model_roles.yaml`** (no tests yet — pure data). Write the matrix per decisions 1–4 above.
   - Verify: `uv run python -c "import yaml; yaml.safe_load(open('config/model_roles.yaml'))"` parses clean.

2. **Fixtures first (TDD).** Create the 4 fixture directories under `tests/personal_agent/config/fixtures/` with deliberately-broken content:
   - `divergent_forbidden_role/`: a `model_roles.yaml` marking `entity_extraction: forbidden`, plus a `models.yaml` (`entity_extraction_role: gpt-5.4-nano`) and `models.cloud.yaml` (`entity_extraction_role: gpt-5.4-mini`) — the pre-correction real-world state.
   - `orphan_env/`: minimal `model_roles.yaml` + a `.env.example` with one planted `AGENT_TOTALLY_MADE_UP_KEY=foo` alongside legitimate keys.
   - `committed_secret/`: minimal `model_roles.yaml` + a YAML file with `AGENT_OPENAI_API_KEY: "sk-live-abcdef123456"` committed.
   - `dangling_reference/`: `model_roles.yaml` + `models.yaml`/`models.cloud.yaml` where `entity_extraction_role: gpt-9-ghost` and `gpt-9-ghost` is not a key under `models:`.

3. **`scripts/check_config.py`.** Implement `main(root: Path) -> int` plus four check functions (`check_forbidden_role_divergence`, `check_orphan_env_keys`, `check_committed_secrets`, `check_dangling_model_refs`), each returning a list of violation strings; classify each into `safety`/`policy` per the ADR-0099 D4 table (dangling ref + committed secret + forbidden-role definition mismatch = safety; orphan env = policy). CLI: `--root <path>` (defaults to repo root) so fixtures can be pointed at directly, matching the ticket's literal invocation (`uv run python scripts/check_config.py --root tests/personal_agent/config/fixtures/divergent_forbidden_role/`). Exit non-zero if any **safety** finding, or (in default/CI mode) any finding at all — policy findings print but don't fail *startup*; they do fail the **guard's own exit code** (CI/pre-commit is the universal gate per D4: "CI/pre-commit is the universal gate; startup is the last-line safety net").
   - Test: `uv run pytest tests/personal_agent/config/test_check_config.py -v` — write these tests first, confirm they fail against a stub, then implement until green.

4. **Drift correction in `config/models.yaml`.** Change lines 36-40 to `entity_extraction_role: gpt-5.4-mini`, `captains_log_role: claude_sonnet`, `insights_role: claude_sonnet`; drop the stale commented override lines.
   - Verify: `uv run python scripts/check_config.py` exits 0 against the real repo.

5. **Startup hook in `settings.py`.** Add `secret: true` json_schema_extra markers to the 8 fields. Add a module-level `_repo_root() -> Path` resolver (monkeypatchable test seam) and `_validate_config_guard_policy` (`model_validator(mode="after")`): calls `_repo_root()`, loads `config/model_roles.yaml` if present (silently skips the whole check with a debug log if absent — never raises on a missing intent file), determines active profile by matching `self.model_config_path` against the matrix's `active_profiles`, and:
   - orphan-env check (via the same `scripts/check_config.py` function used by the CLI guard) → `log.warning(...)`, never raises;
   - required-secret-per-profile check → `raise ValueError(...)` only if the active profile is `cloud` and a required secret field is `None`.
   - Test: `uv run pytest tests/personal_agent/config/test_config_guard_startup.py -v` (3 cases per AC-6; reuse `make_config`-style helper from `test_environment_substrate_validator.py`; monkeypatch `_repo_root` to a fixture dir for the orphan-warns case since the real `.env.example` has no orphans to plant).

6. **Pre-commit + CI wiring.**
   - `.pre-commit-config.yaml`: add hook block (`id: check-config-guard`, `entry: uv run python scripts/check_config.py`, `always_run: true`, `pass_filenames: false`).
   - `.github/workflows/ci.yml`: add a `config-guard` job mirroring `telemetry-surface` (needs: `changes`, `if: push || backend changed`, single step running the guard).
   - Verify: `pre-commit run check-config-guard --all-files` exits 0.

7. **Full quality gates** (Step 8 of the build skill): `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance-criteria proof map (for the master handoff comment)

| AC | Proof |
|---|---|
| AC-3 | `test_check_config.py::test_fails_on_divergent_forbidden_role_fixture` + `test_passes_on_real_repo` |
| AC-4 | `test_check_config.py::test_flags_planted_orphan_env_key` + `test_no_false_positive_on_real_env_example` |
| AC-6 | `test_config_guard_startup.py` — 3 cases (safety-raises / policy-warns-boots / secret-per-profile) |
| AC-8 | `test_check_config.py::test_fails_on_committed_secret_fixture` + `test_no_false_positive_on_real_repo` |
| AC-9 | `test_check_config.py::test_fails_on_dangling_model_reference` |
| Drift corrected | `test_check_config.py::test_real_repo_resolves_extraction_mini_both_profiles` (or equivalent assertion inside `test_passes_on_real_repo`) |

**Deliberately out of scope for FRE-649 (per the ticket's own AC list, which enumerates only AC-3/4/6/8/9 + drift-correction):** ADR-0099's AC-1, AC-2, AC-5, AC-7 belong to later stages (AC-1/2 to the stage-2 generative loader FRE-650; AC-5 to the stage-3 provenance manifest; AC-7's eval-fidelity opt-in mechanism to whichever stage implements `divergence_opt_in`). Flagged during codex plan-review as worth stating explicitly rather than leaving silently absent from the proof map.

## Follow-ups to file (Step 5 of build skill)

- Owner decision needed: promote `compressor` and `reranker` to `divergence: forbidden` — requires either correcting `compressor`'s local/cloud nano-vs-mini split (same class as this ticket's correction) or explicitly documenting `reranker`'s 0.6B/4B split as an intentional, permanent `allowed` exception to the ADR's stated policy.
- (Lower priority, already noted in `CONFIG_INVENTORY.md` §8, check it isn't already tracked before filing): move `neo4j_password`/`database_url` dev-placeholder defaults out of `settings.py` so the source itself carries no credential-shaped string.
