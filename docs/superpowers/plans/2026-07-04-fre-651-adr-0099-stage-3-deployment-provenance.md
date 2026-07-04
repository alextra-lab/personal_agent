# FRE-651 — ADR-0099 stage 3: deployment-provenance manifest + config-resolve CLI

**ADR:** ADR-0099 §D2.2, staged-delivery stage 3. **Blocked by:** FRE-649 (merged, guard+matrix live). **Blocks:** FRE-652 (stage 4, assembled seam).

## Scope (from ticket + PR #266 handoff comment)

1. `config/deployment.yaml` — provenance manifest mapping `profile → compose file → active model-definition file → env overrides`.
2. `src/personal_agent/config/resolve.py` — `config-resolve` CLI: `uv run python -m personal_agent.config.resolve --profile <p> --role <r>`, resolving from committed files only (no container access).
3. Guard cross-check (extend `scripts/check_config.py` / `config_guard.py`): manifest's declared `AGENT_MODEL_CONFIG_PATH` per profile must match what the profile's actual compose file sets.

**Explicitly out of scope:** `required_secrets` already lives in `config/model_roles.yaml` (added in stage 1/FRE-649, consumed by `enforce_required_secrets` in `settings.py`) — not duplicated into `deployment.yaml`. Duplicating it would recreate the exact drift-surface ADR-0099 fights. **Codex plan-review note:** ADR §D4 prose does say the provenance manifest declares required secrets, so this is a real (acknowledged) divergence between the ADR text and the stage-1 implementation, not an absence of a textual claim — but relocating already-merged, working stage-1 code is out of this ticket's brief; flag it as an ADR/impl erratum in the PR description rather than re-plumbing it here.

## Acceptance criterion this ticket carries

**AC-5** — `uv run python -m personal_agent.config.resolve --profile cloud --role entity_extraction` reads only committed files and prints `gpt-5.4-mini`; the guard exits non-zero if any compose `AGENT_MODEL_CONFIG_PATH` disagrees with the manifest. Fails if answering needs container introspection, or a planted manifest/compose mismatch passes the guard.

## Design (grounded in existing stage-1/2 code, read this session)

- `config/model_roles.yaml` already declares `active_profiles: {local: config/models.yaml, cloud: config/models.cloud.yaml}` — this governs *role-divergence checking*. `config/deployment.yaml` is a distinct, deployment-provenance concern (per ADR Implementation Notes, a new top-level file) even though `cloud`'s and `eval`'s `model_config_path` values will match `active_profiles.cloud` today (eval shares cloud's model-def file, per the existing comment in `model_roles.yaml`).
- Confirmed via `grep`: `docker-compose.yml` (local) sets no `AGENT_MODEL_CONFIG_PATH` (uses `AppConfig.model_config_path` default `config/models.yaml`). `docker-compose.cloud.yml` sets it once (`seshat-gateway` service) to `/app/config/models.cloud.yaml`. `docker-compose.eval.yml` sets it twice (`seshat-gateway-control`, `seshat-gateway-treatment`), both `/app/config/models.cloud.yaml`.
- Confirmed via `yaml.safe_load` smoke test: all four compose files parse cleanly as plain dicts (merge keys `<<: *anchor` resolve transparently), so the cross-check parses YAML properly rather than regex-scanning compose text (unlike the committed-secrets check, which regex-scans because it must also catch commented-vs-live distinctions across non-compose YAML).
- `resolve_role_model_key()` (stage 2, `model_loader.py`) already does the real resolution work — for a `forbidden` role it needs no profile lookup at all (`all:` value + existence-check against the resolved config's `models:` mapping); for an `allowed` role it calls `resolve_active_profile()` which matches by resolved absolute path against `model_roles.yaml`'s `active_profiles`. Since `deployment.yaml`'s `cloud.model_config_path` and `model_roles.yaml`'s `active_profiles.cloud` point at the same file, `--profile cloud` and `--profile eval` both correctly resolve through the existing matrix mechanism — no eval-special-casing needed in `resolve.py`.

## Files to create

1. **`config/deployment.yaml`** (new, repo root):
   ```yaml
   profiles:
     local:
       compose_file: docker-compose.yml
       model_config_path: config/models.yaml
       env_overrides: {}
     cloud:
       compose_file: docker-compose.cloud.yml
       model_config_path: config/models.cloud.yaml
       env_overrides:
         AGENT_MODEL_CONFIG_PATH: /app/config/models.cloud.yaml
     eval:
       compose_file: docker-compose.eval.yml
       model_config_path: config/models.cloud.yaml
       env_overrides:
         AGENT_MODEL_CONFIG_PATH: /app/config/models.cloud.yaml
   ```
   Plus a header comment explaining the eval/cloud file-sharing note and the "why a separate file from model_roles.yaml" rationale (mirrors the existing comment style in `model_roles.yaml`).

2. **`src/personal_agent/config/resolve.py`** (new): `resolve(profile, role) -> str` function + `main(argv) -> int` CLI entry point (argparse `--profile`/`--role`, prints resolved model key to stdout, non-zero exit + stderr message on `DeploymentProfileError`/`ModelRoleError`). Mirrors `scripts/check_config.py`'s CLI shape.

## Files to modify

**`src/personal_agent/config/config_guard.py`** — add:
- `class DeploymentProfileError(Exception)` — raised when a profile is undeclared or missing `model_config_path` in the manifest.
- `load_deployment_manifest(root: Path) -> JSONDict` — mirrors `load_matrix`.
- `model_config_path_for_profile(profile: str, manifest: JSONDict, root: Path) -> Path` — resolves the profile's model-def file path; raises `DeploymentProfileError` on an unknown profile or missing field.
- `_normalize_container_model_config_path(value: str) -> str` — strips a leading `/app/` container-mount prefix so a compose/env-override value compares equal to a repo-relative `model_config_path`.
- `_compose_model_config_paths(compose_yaml: JSONDict) -> set[str]` — walks `services.*.environment` (dict or list form) collecting every `AGENT_MODEL_CONFIG_PATH` value set in that compose file.
- `check_deployment_manifest_internal_consistency(manifest: JSONDict) -> list[Finding]` — **(added per codex BLOCKING finding)** for each profile row, asserts `env_overrides.AGENT_MODEL_CONFIG_PATH` (normalized) names the same file as `model_config_path`. Without this, `config-resolve` (which reads `model_config_path`) could silently answer from a different file than the one `env_overrides` documents as deployed, even when the compose-cross-check below passes — a manifest row can be internally self-contradictory. `policy` severity.
- `check_deployment_manifest_matches_compose(root: Path, manifest: JSONDict) -> list[Finding]` — for each declared profile, loads its `compose_file`, compares actual `AGENT_MODEL_CONFIG_PATH` value(s) (normalized) against the manifest's declared `env_overrides` value (or "none declared" for local): mismatch, or "declared but compose doesn't set it", or "compose sets it but manifest doesn't declare an override" are each a `policy`-severity finding (ADR-0099 D4 table: "provenance-manifest ≠ actual compose" is explicitly policy-class).
- Together, the two checks close the AC-5 gap codex flagged: `model_config_path == env_overrides` (internal-consistency) **and** `env_overrides == compose actual` (manifest-vs-compose) transitively prove `model_config_path == compose actual` — i.e. `config-resolve`'s answer really is what's deployed.
- Wire both into `run_all_checks()`.

**`scripts/check_config.py`** — no change needed; it already calls `run_all_checks()`, which will now include the new check.

## Tests (TDD — write first, confirm failing, then implement)

**New fixture:** `tests/personal_agent/config/fixtures/deployment_manifest_mismatch/`
- `config/deployment.yaml` — cloud profile declares `env_overrides.AGENT_MODEL_CONFIG_PATH: /app/config/models.WRONG.yaml`, `compose_file: docker-compose.fixture.yml`.
- `docker-compose.fixture.yml` — one service setting `AGENT_MODEL_CONFIG_PATH: /app/config/models.cloud.yaml` (the real deployed value) — deliberately disagreeing with the manifest.

**`tests/personal_agent/config/test_check_config.py`** — add `TestDeploymentManifestMatchesCompose`:
- `test_fails_on_manifest_compose_mismatch_fixture` — `run_all_checks` (or `check_deployment_manifest_matches_compose` directly against the loaded manifest) on the fixture root produces a `deployment_manifest_mismatch` finding naming both values.
- `test_no_false_positive_on_real_repo` — `run_all_checks(_REPO_ROOT)` still returns `[]` once the real `config/deployment.yaml` is added (extends the existing `test_passes_on_real_repo` guarantee).
- **New fixture** `fixtures/deployment_manifest_internal_mismatch/config/deployment.yaml` — a profile row whose `model_config_path` and `env_overrides.AGENT_MODEL_CONFIG_PATH` name different files (no compose file needed). `test_fails_on_internal_mismatch_fixture` asserts `check_deployment_manifest_internal_consistency` flags it; `test_no_false_positive_on_real_repo` extends to this check too.
- Direct unit tests of `_compose_model_config_paths` (not full fixture round-trips, per codex's parsing-edge-case note) covering: dict-form single service, list-form (`- AGENT_MODEL_CONFIG_PATH=/app/...`), two services agreeing (one value in the returned set), two services disagreeing (two values — proves the guard would flag the non-matching one), and a merge-key (`<<: *anchor`) service inheriting `environment` from a base (already implicitly covered since `yaml.safe_load` resolves merge keys before this function ever sees the dict — test asserts this explicitly against a small in-memory fixture dict, not a new compose file).
- Direct unit test of `_normalize_container_model_config_path`: `/app/config/models.cloud.yaml` → `config/models.cloud.yaml`; a bare relative path passes through unchanged.

**New file `tests/personal_agent/config/test_resolve_cli.py`**:
- `test_resolve_cloud_entity_extraction_returns_gpt_5_4_mini` — calls `resolve("cloud", "entity_extraction")` directly, asserts `== "gpt-5.4-mini"` (AC-5's exact value).
- `test_cli_prints_resolved_key_to_stdout` — `main(["--profile", "cloud", "--role", "entity_extraction"])` via `capsys`, asserts stdout is exactly `gpt-5.4-mini\n` and return code `0`.
- `test_cli_exits_nonzero_on_unknown_profile` — `main(["--profile", "nonexistent", "--role", "entity_extraction"])` returns non-zero, stderr mentions the profile.
- `test_cli_exits_nonzero_on_unknown_role` — same shape for an undeclared role.
- `test_eval_profile_resolves_same_as_cloud` — `resolve("eval", "entity_extraction") == resolve("cloud", "entity_extraction")` (proves the shared-model-def-file design note holds in practice).

## Verification (this ticket's AC-5 proof)

```
uv run python -m personal_agent.config.resolve --profile cloud --role entity_extraction
# expect: gpt-5.4-mini

uv run python scripts/check_config.py
# expect: check_config: clean (real repo passes with deployment.yaml added)
```

Plus full quality gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Risk classification

**Standard** — touches `src/` (`config_guard.py`, new `resolve.py`), extends the shared guard used at CI/pre-commit/startup. Codex plan-review required per `/build` Step 3.
