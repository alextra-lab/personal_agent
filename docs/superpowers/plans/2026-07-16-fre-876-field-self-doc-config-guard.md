# FRE-876 — ADR-0099 D4 field self-documentation config-guard check

Backing ADR-0099 (config management and validation), Decision D4. No new ADR needed.

## Scope

1. **Required.** A new `config_guard` check: every `AppConfig` field must carry a
   non-empty `description`. Wired into `run_all_checks` at `policy` severity (same
   class as the existing orphan/roles/secrets checks). Currently 311/311 fields
   already carry a description — this lands green immediately; it's a regression
   ratchet, not a cleanup.
2. **Optional add-on (recommended by the ticket) — included.** A second check:
   no secret-classified field (`json_schema_extra={"secret": True}`) may declare a
   plaintext default value in `settings.py`. The existing `check_committed_secrets`
   only scans YAML/`.env` text for a *committed* value; it never looks at a secret
   field's own Python default.

## Investigation finding (changes the plan)

Live-checked all 16 secret-marked fields via `AppConfig.model_fields`. 15 of 16
default to `None`. One — `neo4j_password` — defaults to the literal string
`"neo4j_dev_password"`. This is **not a leaked credential**: it's the documented
local-dev convenience default, and it exactly matches `docker-compose.yml`'s own
hardcoded fallback (`NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-neo4j_dev_password}`,
also present verbatim in `docker-compose.test.yml` and commented in `.env.example`).

This matters because `scripts/check_config.py` (the CLI both pre-commit and CI
invoke) already exits non-zero on **any** finding, safety or policy — so naively
adding the secret-default check would immediately turn pre-commit/CI red on the
real repo, for a value that isn't actually sensitive. Two fixes considered:

- Drop the default (`None`) — rejected: `docker-compose.yml`'s own fallback means
  the container would still come up with `neo4j_dev_password` while the agent's
  `AppConfig` would now have no matching default, breaking `make up` for anyone
  without a `.env` — a bigger behavior change than this ticket should carry.
- **Add a narrow, declarative exemption** on the field itself — chosen. Mirrors the
  existing `# fre-649-allow: <reason>` convention `check_committed_secrets` already
  uses ("a considered exception, not a rubber stamp"), but as Python-native
  metadata since this check reads `json_schema_extra`, not raw text: add
  `"secret_default_allow": "<reason>"` alongside `"secret": True` on `neo4j_password`
  only. The check treats an unexempted plaintext default as a finding; an exempted
  one is skipped. No other secret field needs it (all 15 others already default
  to `None`).

## Codex plan-review verdict: approve with changes

Two tightenings folded in before implementation:
- `secret_default_allow` must be a **non-empty string reason**, not merely truthy —
  and a field carrying `secret_default_allow` while NOT `secret`-marked is itself a
  policy finding (dead/contradictory metadata).
- Description and default-value checks treat whitespace-only strings as invalid
  (`.strip()`), not just falsy/`None`.

## Implementation

### `src/personal_agent/config/config_guard.py`

- Add `from collections.abc import Mapping` and, under `TYPE_CHECKING`, `from
  pydantic.fields import FieldInfo`.
- `check_field_descriptions(fields: Mapping[str, FieldInfo] | None = None) ->
  list[Finding]` — `fields=None` lazily imports the real `AppConfig.model_fields`
  (mirrors the existing lazy-import convention used throughout this module to
  avoid the `model_loader.py` cycle). Flags any field whose `.description` is
  falsy or whitespace-only. `check="undocumented_field"`, `severity="policy"`.
  Accepting an injected `fields` mapping (rather than hardcoding `AppConfig`)
  keeps the check unit-testable with a throwaway fixture model, without needing
  a filesystem fixture root — this check has no YAML/root surface, unlike the
  matrix/manifest checks.
- `check_secret_field_plaintext_defaults(fields: Mapping[str, FieldInfo] | None =
  None) -> list[Finding]` — same injection pattern. For every field whose
  `json_schema_extra` carries `secret: True`, skip it if `json_schema_extra` also
  carries a truthy `secret_default_allow`; otherwise flag if `field.default` is a
  non-empty `str`. `check="secret_field_plaintext_default"`, `severity="policy"`.
- Wire both into `run_all_checks` (no `root` arg needed, matching
  `check_embedding_fallback_identity`'s call style).
- Extend the module docstring's check-list with these two bullets.

### `src/personal_agent/config/settings.py`

- `neo4j_password`'s `json_schema_extra` gains `"secret_default_allow": "local-only
  dev convenience password; matches docker-compose.yml's own hardcoded fallback
  (NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-neo4j_dev_password}) — not a real credential"`.

### Tests — `tests/personal_agent/config/test_check_config.py`

New classes, following the file's existing style (no new fixture directory needed;
`check_embedding_fallback_identity(AppConfig())`-style direct injection is the
established precedent for a model-attribute check that isn't YAML-root-based):

- `TestFieldDescriptions`
  - a throwaway `pydantic.BaseModel` subclass with one field missing a
    description → `check_field_descriptions(Model.model_fields)` flags exactly it.
  - a throwaway model with an empty-string / whitespace-only description → flagged.
  - `check_field_descriptions()` (real `AppConfig`) == `[]`.
- `TestSecretFieldPlaintextDefaults`
  - throwaway model: secret field with a plaintext default, no exemption → flagged.
  - throwaway model: secret field with a plaintext default **and**
    `secret_default_allow` set → not flagged.
  - throwaway model: secret field defaulting to `None` → not flagged.
  - `check_secret_field_plaintext_defaults()` (real `AppConfig`) == `[]` (proves the
    `neo4j_password` exemption works and no other secret field regressed).

`run_all_checks(_REPO_ROOT) == []` (existing test in `TestForbiddenRoleDivergence`)
continues to cover both new checks end-to-end on the real repo.

## Out of scope

- The branch-protection ruleset change making config-guard a required CI check —
  explicitly called out in the ticket as owner-executed separately.

## Verification

- `make test-file FILE=tests/personal_agent/config/test_check_config.py`
- `make mypy` / `make ruff-check` / `make ruff-format`
- `pre-commit run --all-files` (exercises the real `check_config.py` CLI path —
  must stay green, proving the exemption actually works end to end)
