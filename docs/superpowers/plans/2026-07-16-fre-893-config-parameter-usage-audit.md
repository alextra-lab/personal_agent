# FRE-893 — Config-parameter usage audit (ADR-0099 hygiene)

**Ticket:** [FRE-893](https://linear.app/frenchforest/issue/FRE-893) · **Backing ADR:** [ADR-0099](../../architecture_decisions/ADR-0099-configuration-management-and-validation.md) · **Reuses:** [CONFIG_INVENTORY.md](../../reference/CONFIG_INVENTORY.md) (FRE-648 stage-0 inventory)

**Revision note:** this plan went through a codex plan-review pass (below) that found real
gaps in v1 — those are folded in here, not left as open findings.

## Scope (from the ticket's acceptance criteria)

`AppConfig` has **311** typed fields (verified: `len(AppConfig.model_fields) == 311` on this
branch; the CONFIG_INVENTORY.md §1 count is current, the ticket's own "~301" and the doc's
stale §0 "277" are both off — v1 of this plan mistakenly propagated "301", now corrected).
The AC text, read literally, scopes the *categorization deliverable* to **every `AppConfig`
field** — the other ~10 params (model-role headers, profile keys, governance policy) are
already covered by `CONFIG_INVENTORY.md` §2–§6 (role-assignment matrix, model-definition
drift, profile table, governance table) with their own divergence/drift analysis. This plan
does not redo that — it adds the piece those sections don't have: a read-count +
override-evidence categorization for the 311 `AppConfig` scalars.

**Explicit limitation to state in the report (measure-don't-assert):** the real deployed
`.env` (gitignored, not in the repo) is the one place a field could be overridden that this
audit cannot see. A field with zero in-repo override evidence is not proof the field is
*never* overridden in production — it is proof there is **no repo-visible override**. The
report states this per the `UNVERIFIABLE` convention (lifecycle-rules § Evidence contract),
not as silent "definitely never overridden."

## Codex plan-review findings and how this revision addresses each

1. **Field count wrong (301 vs actual 311).** Fixed throughout — see above.
2. **Dynamic resolution path missed: `config/substrate.yaml` → `_resolve_setting()` in
   `src/personal_agent/config/substrate.py` does `getattr(settings, field)` where `field` is
   a *variable* sourced from the manifest's `"setting:<field>"` strings — a literal-string
   grep can't see this.** Fixed: a third read-evidence source, `manifest_reads(name)`, parses
   `config/substrate.yaml` directly with `re.findall(r'source:\s*"setting:(\w+)"', text)` and
   checks membership — no need to trace the dynamic `getattr` generically, the manifest text
   itself names every field literally. Confirmed by inspection: `database_url`, `neo4j_uri`,
   `elasticsearch_url`, `llm_base_url`, `managed_database_url`, `managed_neo4j_uri`,
   `managed_elasticsearch_url`, `managed_embedding_endpoint`, `managed_reranker_endpoint`,
   `managed_slm_endpoint` all resolve this way.
3. **Test/script reads collapsed into the same bucket as production `src/` reads, which can
   make dead production config look load-bearing.** Fixed: `external_reads` now tags each hit
   by root (`src/` vs `scripts/` vs `tests/`). Categorization's "has a production read" =
   `src/` hits **or** `internal_self_read` **or** `manifest_reads` (not test/script hits
   alone). A field found only under `tests/`/`scripts/` gets an explicit `"read only in
   tests/scripts, not production src/"` annotation in its evidence row instead of silently
   passing as load-bearing.
4. **Override evidence too narrow (docker-compose only; missed `docker-compose.study.yml`
   and `tests/conftest.py`'s `os.environ.setdefault("AGENT_...", ...)` test-substrate
   overrides).** Fixed: `COMPOSE_FILES` now lists all 5 files present in the repo
   (`docker-compose.yml`, `.cloud.yml`, `.eval.yml`, `.test.yml`, `.study.yml`) — verified via
   `ls docker-compose*.yml`. Added `conftest_overrides(name, field)` —
   `re.findall(r'os\.environ\.setdefault\(\s*["\'](AGENT_\w+)["\']', conftest_text)` — tagged
   as `"test-substrate (conftest.py)"` in the evidence, distinct from `"compose"` so a reader
   can tell a real deployment override from a test-isolation default (FRE-375).
5. **Raw-text compose grepping is fragile (YAML anchors, list vs mapping `environment:`
   forms, comments).** Fixed: `override_locations` now `yaml.safe_load`s each compose file and
   walks `services.*.environment` for real key membership (list form `"KEY=value"` split on
   first `=`; mapping form used as-is) instead of a text regex over the raw file. `pyyaml` is
   already a project dependency (`pyproject.toml`).
6. **`_is_secret()` (imported from `config_inventory.py`) misses fields carrying the
   authoritative `json_schema_extra={"secret": True}` marker — verified 7 mismatches, all
   `managed_*` fields (`managed_database_url`, `managed_neo4j_uri`,
   `managed_elasticsearch_url`, `managed_embedding_endpoint`, `managed_embedding_token`,
   `managed_reranker_endpoint`, `managed_slm_endpoint`): schema says secret, the regex
   heuristic doesn't match (`_url`/`_endpoint`/`_token` suffixes aren't in the pattern).**
   Fixed: guardrail detection here is `_is_secret(name) OR (field.json_schema_extra is a dict
   and .get("secret") is True)` — the schema flag is authoritative and this audit does not
   rely on the regex alone. **This mismatch is itself a finding** — reported in the dated
   report as a discovered drift in `config_inventory.py`'s own secret heuristic, flagged as a
   candidate follow-up (fixing that heuristic is a `src`-adjacent change to an existing tool,
   out of scope for this audit-only ticket).
7. **Scoping AppConfig-only, deferring model-YAML/profile/governance to CONFIG_INVENTORY
   §2–§6, is defensible** per codex (measured against the plan's own AC table; codex could not
   reach the live Linear ticket text directly, so this is confirmed against the AC as
   transcribed here, which matches what was pulled via `get_issue` at scoping time).

## Design

New module `scripts/audit/config_usage_audit.py` (separate concern from
`config_inventory.py`, which owns table generation/verification — reused via import for
`_is_secret` / `_accepted_env`, not duplicated):

- `external_reads(name) -> dict[str, list[str]]` — `git grep -n -P` for `settings\.<name>\b`
  **or** `getattr\(settings,\s*["']<name>["']` across `src/`, `scripts/`, `tests/`, excluding
  `config/settings.py`, keyed by root (`"src"`, `"scripts"`, `"tests"`). Prototyped against
  `debug`, `reranker_input_cap`, `location_precision`, `event_bus_dead_letter_stream`,
  `searxng_default_categories`, `gateway_mount_local` (all found — including through a
  locally-aliased `current_settings` variable, since the unanchored substring match catches
  that for free) and `url_guard_allowlist` (only found via the `getattr` form — confirms both
  patterns are needed; a `settings.`-only grep would have false-flagged it as dead).
- `internal_self_read(name) -> bool` — whether `settings.py` itself consults the field via
  `self.<name>` (the 5 cross-field `@model_validator`s, e.g. `owner_storage_allowlist`
  consumed only as `self.owner_storage_allowlist` inside `_validate_owner_storage_allowlist`
  — confirmed by prototype).
- `manifest_reads(name) -> bool` — whether `config/substrate.yaml` names this field via
  `source: "setting:<name>"` (confirmed 10 fields resolve this way — see finding #2 above).
- `override_locations(name, field) -> list[tuple[str, str]]` — `(source, kind)` pairs:
  `kind="compose"` for any of the 5 `docker-compose*.yml` files whose parsed
  `services.*.environment` sets `AGENT_<FIELD>` or its declared alias (via `_accepted_env`
  from `config_inventory.py`); `kind="test-substrate"` for `tests/conftest.py`
  `os.environ.setdefault("AGENT_...")` matches.
- `is_guardrail(name, field) -> bool` — `_is_secret(name)` OR
  `isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("secret") is
  True` OR `name == "owner_storage_allowlist"`.
- `categorize(name, field) -> FieldUsage` (frozen dataclass: name, reads-by-root,
  internal_self_read, manifest_read, overrides, category, notes):
  - **writer-pinned-guardrail** — `is_guardrail()` is true. Secrets structurally can never
    show an in-repo override (real values live only in gitignored `.env`) and would otherwise
    false-land in "hardcode-candidate"; `owner_storage_allowlist` is the one non-secret field
    whose only consumption is the self-referential validator guarding substrate-host safety.
    Neither is ever a removal/hardcode candidate regardless of read/override evidence.
  - **never-read** — no `src/` read, no `internal_self_read`, no `manifest_read` (test/script-
    only hits, if any, are recorded in `notes`, not treated as production evidence).
  - **read-but-never-overridden** — has a production read (`src`/self/manifest), zero
    override locations of either kind.
  - **load-bearing** — has a production read AND at least one override location.
- `audit_all() -> list[FieldUsage]` over `sorted(AppConfig.model_fields.items())`.
- `main()` CLI (`generate` mode) writes two outputs — no stdout secret/DSN leakage risk since
  neither output touches secret values (same discipline as `config_inventory.py`; only field
  names, categories, and file:line evidence are emitted, never field values):
  1. The dated deliverable: `docs/research/2026-07-16-fre-893-config-parameter-usage-audit.md`
     — methodology (incl. all limitations above), full categorized table (name, category,
     read evidence by root, override evidence by kind), two ranked candidate lists
     (never-read, read-but-never-overridden), the `config_inventory.py` secret-heuristic-drift
     finding, and the `.env`/deployed-environment limitation note.
  2. A short new `§10 — Parameter usage audit (FRE-893)` appended to `CONFIG_INVENTORY.md` —
     category counts + a link to the dated report (extends the existing doc, does not
     duplicate its content).

## Steps

1. **Test first** — `tests/scripts/test_config_usage_audit.py`:
   - `test_every_appconfig_field_categorized` — `audit_all()` covers all 311 fields, each
     category ∈ the 4 allowed values.
   - `test_secrets_and_owner_allowlist_are_guardrail_pinned` — every `_is_secret` field, every
     `json_schema_extra={"secret": True}` field (including the 7 `managed_*` fields the regex
     heuristic misses), and `owner_storage_allowlist` categorize as `writer-pinned-guardrail`.
   - `test_known_load_bearing_field_detected` — `debug` (read in `security.py`) has a `src`
     read.
   - `test_validator_only_field_is_not_never_read` — `owner_storage_allowlist` has
     `internal_self_read is True`.
   - `test_getattr_pattern_catches_indirect_read` — `url_guard_allowlist` has a non-empty
     `src` read (only reachable via the `getattr` form).
   - `test_manifest_read_detected` — `llm_base_url` (and `database_url`) show
     `manifest_read is True` via `config/substrate.yaml`.
   - `test_compose_override_parsed_yaml_aware` — a field known to be set in
     `docker-compose.cloud.yml`'s `environment:` block shows an override location of kind
     `"compose"`.
   - `test_conftest_override_detected` — `neo4j_uri` (set via
     `os.environ.setdefault("AGENT_NEO4J_URI", ...)` in `tests/conftest.py`) shows an override
     location of kind `"test-substrate"`.
   - `test_generate_writes_dated_report_and_extends_inventory_doc` — after running the
     generator, the dated file exists with the categorized table, and `CONFIG_INVENTORY.md`
     contains the new `§10` marker section.
   Run — confirm they fail (module doesn't exist yet).
2. Implement `scripts/audit/config_usage_audit.py` per the design above.
3. Run the generator once to produce the committed deliverable + inventory doc update.
4. Confirm tests pass; `make mypy`; `make ruff-check` + `make ruff-format`; `pre-commit run --all-files`.
5. Self-review: `code-review` skill at `low` effort (new script has zero runtime/production
   src/ behavior change — a one-shot audit tool with subprocess + regex/YAML-parsing logic,
   not a hot path) — but since it uses `subprocess` + reads YAML/text files, also run
   `security-review` per Step 8's own trigger rule. Fix confirmed findings on-branch.
6. PR + Linear comment per Step 9 (acceptance-criteria proof, self-review summary, context
   disposition).

## Acceptance criteria mapping

| AC (from ticket) | How this plan proves it |
|---|---|
| Every AppConfig field categorized with evidence | `audit_all()` (311 fields) + the dated report's full table; `test_every_appconfig_field_categorized` |
| Ranked candidate list of dead / always-default params | Report's two candidate-list sections, generated from `categorize()` output |
| Existing config-inventory doc reused/extended, not duplicated | New `§10` in `CONFIG_INVENTORY.md` links out to the dated report instead of re-deriving §1–§9 |
| Zero configuration removed or changed | Diff touches only `scripts/audit/`, `tests/scripts/`, `docs/research/`, `docs/reference/CONFIG_INVENTORY.md` — no `settings.py` / `.env*` / compose / model-YAML / `substrate.py` edits |

## Out of scope (explicitly, per the ticket's scope guard)

Actual pruning, hardcoding, or removal of any parameter, and fixing `config_inventory.py`'s
secret-heuristic-regex drift (finding #6) — both are separate, owner-gated follow-up work
the report's candidate lists / findings feed into.
