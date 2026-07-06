# FRE-819 — ADR-0112 Storage owner-host allowlist guard [AC-1]

**Backing:** [ADR-0112 — Configurable Substrate Backends](../../architecture_decisions/ADR-0112-configurable-substrate-backends.md) §D1 + AC-1.
**Builds on:** FRE-816 (AC-2 backend-selection seam, merged `5eb1d58`) and the FRE-375 `AppConfig` guard precedent.

## Acceptance criteria (ADR-0112 AC-1, verbatim)

> **AC-1 — Storage resolves to owner-controlled hosts by default.** **Check:** in the
> `private` profile, each resolved Postgres / Neo4j / Elasticsearch target matches an
> **allowlist of owner-controlled hosts** (loopback or the owner's declared host /
> private-IP range in config); validation fails a target outside the allowlist. *Fails
> if* a default-profile store resolves to any host not on the owner allowlist (e.g. a
> provider/managed hostname passed off as owned).

## Scope

- New `AppConfig` field `owner_storage_allowlist: list[str]`, defaulting to
  `["postgres", "neo4j", "elasticsearch"]` — the exact Docker Compose service names
  `docker-compose.cloud.yml` sets `AGENT_DATABASE_URL`/`AGENT_NEO4J_URI`/
  `AGENT_ELASTICSEARCH_URL` to at lines 308/312/314 (`postgres:5432`, `neo4j:7687`,
  `elasticsearch:9200`) — these are just container DNS names for services on the same
  owned VPS, so they belong on the allowlist by default with zero required config.
- New pure-function module `src/personal_agent/config/_owner_host_allowlist.py`
  (sibling to the existing `_substrate_fingerprint.py`, same placement rationale —
  breaks a `memory.service` → `config.settings` import cycle):
  `is_owner_controlled_host(uri: str, allowlist: Sequence[str]) -> bool`. Loopback
  (`localhost` / `127.0.0.1` / `::1`) is always allowed. Each allowlist entry is either
  an exact hostname (case-insensitive) or a CIDR range (e.g. `10.0.0.0/8`) checked via
  `ipaddress`. No implicit "any private IP passes" — a range must be **declared** in
  config, matching the AC-1 text precisely.
- New `@model_validator(mode="after")` on `AppConfig`,
  `_validate_owner_storage_allowlist`, placed immediately after the existing
  `_validate_substrate_isolation` (FRE-375) in `settings.py` (~line 2257). Fires only
  when `substrate_profile == "private"` (no environment gate — see Design decision 2,
  revised after codex review), checking `database_url`, `neo4j_uri`,
  `elasticsearch_url` (the three `AppConfig` fields `config/substrate.yaml`'s `private`
  profile maps `postgres` / `neo4j` / `elasticsearch` to via `setting:<field>`), plus
  `database_admin_url` and `sysgraph_database_url` (the two additional real Postgres
  connections FRE-375's own guard already covers — `sysgraph_database_url` backs a
  live repository connection in `events/pipeline_handlers.py`). Raises a single
  `ValueError` naming every offending field, mirroring the FRE-375 error shape exactly.
- `tests/conftest.py`: add `os.environ.setdefault("AGENT_SUBSTRATE_PROFILE", "test")`
  to the existing FRE-375 redirection block (alongside the `AGENT_NEO4J_URI` /
  `AGENT_ELASTICSEARCH_URL` `setdefault` calls at lines 18-20). This is what makes
  "no environment gate" safe — see Design decision 2.

## Design decisions (revised after codex plan-review — see PR/ticket for the full review)

1. **Direct-field check, not `resolve_substrate()`.** `resolve_substrate("private")`
   also resolves `embedder`/`reranker` via `model_endpoint:<role>`, which loads a model
   config file. Reusing it inside a validator that runs on **every** `AppConfig()`
   construction (including hundreds of ad-hoc test configs) would add file I/O and a
   new failure surface unrelated to AC-1. Since the manifest's `private` profile always
   declares `kind: local` + `setting:database_url|neo4j_uri|elasticsearch_url` for the
   three stores (hardcoded in `config/substrate.yaml`, not user-configurable per-field
   while remaining in the `private` profile), checking the three `AppConfig` fields
   directly is equivalent for this AC and carries none of that risk. This mirrors the
   FRE-375 guard, which also checks fields directly rather than through a resolver.
   **Codex-flagged addition:** add a manifest-drift test asserting
   `config/substrate.yaml`'s `private.{postgres,neo4j,elasticsearch}` rows still
   resolve from exactly `setting:database_url` / `setting:neo4j_uri` /
   `setting:elasticsearch_url` — if that mapping ever drifts, this direct-field
   validator silently stops matching "resolved target" and the drift test must fail
   loudly to catch it.
2. **No environment gate — fires whenever `substrate_profile == "private"`, full stop.**
   Originally gated on `environment != Environment.TEST` (mirroring FRE-375's own
   gate). Codex correctly flagged this as a gap: AC-1 is a private-profile custody
   guard, not a test-only concern, and `APP_ENV` is just an env var — a
   misconfigured/real process could set `APP_ENV=test` and silently defeat the guard.
   Fix: `tests/conftest.py` gets one more `os.environ.setdefault("AGENT_SUBSTRATE_PROFILE",
   "test")` alongside its existing FRE-375 `setdefault` calls, so the automated test
   suite runs under `substrate_profile="test"` (not the default `"private"`) and never
   trips this guard — verified empirically that `AppConfig.model_validate()` (what
   every existing test fixture uses) *does* pick up env-var defaults for fields not in
   the passed dict, so this propagates correctly. The guard itself now depends only on
   `substrate_profile`, never on `environment`, closing the gap.
3. **Extend to `database_admin_url` and `sysgraph_database_url`.** Originally scoped to
   just the 3 D3-manifest components. Codex found `sysgraph_database_url` backs a real
   runtime Postgres connection (`src/personal_agent/events/pipeline_handlers.py:274`),
   so leaving it unchecked is a genuine custody gap, not just an implicit-coverage
   argument. Extended the validator to check all 5 fields FRE-375's own guard already
   checks: `database_url`, `database_admin_url`, `sysgraph_database_url`, `neo4j_uri`,
   `elasticsearch_url`.
4. **No new bypass flag.** FRE-375 added `allow_test_writes_to_prod_substrate` because
   there was no other escape hatch. Here, the escape hatch already exists: setting
   `AGENT_SUBSTRATE_PROFILE=managed` (or `dev`/`test`) takes a config out of scope for
   this guard entirely — adding a second bypass flag would be redundant surface area.

## Files

| File | Change |
|---|---|
| `src/personal_agent/config/_owner_host_allowlist.py` | New — `is_owner_controlled_host()` |
| `src/personal_agent/config/settings.py` | New `owner_storage_allowlist` field + `_validate_owner_storage_allowlist` model_validator + import |
| `tests/conftest.py` | Add `AGENT_SUBSTRATE_PROFILE=test` to the existing FRE-375 `setdefault` block |
| `.env.example` | New commented `AGENT_OWNER_STORAGE_ALLOWLIST` entry near the `AGENT_MANAGED_*` block |
| `docs/reference/CONFIG_INVENTORY.md` | Regenerate §1 (`uv run python scripts/audit/config_inventory.py generate`) |
| `tests/personal_agent/config/test_owner_host_allowlist.py` | New — pure-function unit tests |
| `tests/personal_agent/config/test_owner_storage_allowlist_validator.py` | New — `AppConfig` validator tests (mirrors `test_environment_substrate_validator.py`) |
| `tests/personal_agent/config/test_substrate_manifest_drift.py` | New — asserts the `private` profile's 3 store rows still map to the exact `setting:` fields this validator checks |

## Steps (atomic, TDD)

1. Write failing tests in `tests/personal_agent/config/test_owner_host_allowlist.py`
   for `is_owner_controlled_host`: loopback (`localhost`, `127.0.0.1`, `::1`, and
   bracketed IPv6 `[::1]`) always passes; exact hostname match (case-insensitive);
   CIDR-range match; a plain private IP with **no** matching allowlist entry fails
   (proves no implicit "any private IP" bypass); a provider-looking hostname
   (`db.managed-provider.example.com`) fails; a malformed/hostless URI (e.g. empty
   string, no netloc) fails closed (returns False, doesn't raise).
   Confirm failure: `make test-file FILE=tests/personal_agent/config/test_owner_host_allowlist.py` (import error).
2. Implement `src/personal_agent/config/_owner_host_allowlist.py`. Re-run — green.
3. Write a failing manifest-drift test in
   `tests/personal_agent/config/test_substrate_manifest_drift.py`: load
   `config/substrate.yaml` (via `config_guard.load_substrate_manifest`) and assert
   `profiles.private.postgres.source == "setting:database_url"` (and the neo4j/
   elasticsearch equivalents) — fails loudly if the manifest's private-profile store
   mapping ever diverges from what the new validator checks directly.
4. Add `os.environ.setdefault("AGENT_SUBSTRATE_PROFILE", "test")` to
   `tests/conftest.py`'s existing FRE-375 block. Confirm via the empirical check
   already run this session: `AppConfig.model_validate()` picks up this default for
   any test fixture that doesn't explicitly override `substrate_profile`.
5. Write failing tests in `tests/personal_agent/config/test_owner_storage_allowlist_validator.py`:
   - `TestValidatorRaises`: `substrate_profile="private"` + one of the 5 URIs
     (`database_url`, `database_admin_url`, `sysgraph_database_url`, `neo4j_uri`,
     `elasticsearch_url`) pointing off-allowlist → `ValidationError` naming the
     offending field. No `environment` override needed — the guard no longer depends
     on it.
   - `TestValidatorSilentForAllowlistedHosts`: default `owner_storage_allowlist`
     (`postgres`/`neo4j`/`elasticsearch`) + those exact hostnames → no raise (proves
     the cloud-deploy default boots clean).
   - `TestValidatorSilentForLoopback`: `localhost` URIs (the local-dev default) → no
     raise regardless of `owner_storage_allowlist` contents.
   - `TestValidatorSilentForNonPrivateProfile`: `substrate_profile="managed"` (or
     `"test"`) + an off-allowlist host → no raise.
   - `TestValidatorCustomAllowlist`: overriding `owner_storage_allowlist` to include an
     extra declared host/CIDR makes an otherwise-failing config pass.
   - `TestValidatorDefaultTestSuiteConfigPasses`: a bare `AppConfig()` construction
     under the real test-suite env (as set by conftest) does not raise — proves Step 4
     actually neutralizes the guard for the automated suite.
   Confirm failure: `make test-file FILE=tests/personal_agent/config/test_owner_storage_allowlist_validator.py`.
6. Implement the `owner_storage_allowlist` field + `_validate_owner_storage_allowlist`
   validator in `settings.py`. Re-run all three new test files — green.
7. Run the **full** suite once (`make test`) to confirm the conftest change doesn't
   regress anything relying on the previous implicit `substrate_profile="private"`
   default during tests (none found in this session's survey, but this is the final
   check).
8. Add the `.env.example` entry; regenerate `docs/reference/CONFIG_INVENTORY.md`
   (`uv run python scripts/audit/config_inventory.py generate`, then `... verify`).
9. Full quality gates (Step 8 below).

## Acceptance-criteria proof (what master's gate will read)

- AC-1 check 1 ("each resolved target matches the allowlist"): `TestValidatorSilentForAllowlistedHosts` + `TestValidatorSilentForLoopback`.
- AC-1 "Fails if" clause (off-allowlist target in the default profile): `TestValidatorRaises`.
- Both proven against a real `AppConfig.model_validate()` construction — not a mock — so the guard is proven to actually run at boot, not just wired.

## Test commands

```
make test-file FILE=tests/personal_agent/config/test_owner_host_allowlist.py
make test-file FILE=tests/personal_agent/config/test_owner_storage_allowlist_validator.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Risk tier: Standard

Touches `src/` config/security logic (a custody guard) — codex plan-review required
per the build skill.
