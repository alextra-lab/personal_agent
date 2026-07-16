# FRE-896 — Config surface cleanup: alias-aware read re-derivation + provenance-gated removal

> **Ticket:** FRE-896 (Approved, Tier-1:Opus, High) · **Backing ADR:** ADR-0099 (config management & validation)
> **Source:** FRE-893 (config usage audit) · **Branch:** `fre-896-config-surface-cleanup`

## Problem

FRE-893's audit detects reads by *line-oriented* `git grep` for `settings.<field>` /
`getattr(settings, "<field>")`. Three real read patterns evade it, producing systematic
false-negatives (dead-flagging live config):

1. **Local alias** — `cfg = settings` / `cfg = get_settings()` then `cfg.<field>`
   (e.g. `proactive_memory_*` × 11 in `memory/proactive.py`).
2. **Factory chain** — `get_settings().<field>` (the regex wants `settings.`, sees `settings().`)
   (e.g. `insights_wiring_enabled` in `events/pipeline_handlers.py:203`).
3. **Multi-line `getattr`** — `getattr(\n settings, "<field>", …)` split across lines; `git grep`
   is per-line so `getattr(settings` never co-occur (e.g. `quality_monitor_daily_run_hour_utc`,
   `quality_monitor_anomaly_window_days` in `brainstem/scheduler.py:160-165`).

Result: the "never-read (43)" list cannot be trusted for deletion as-is.

## Acceptance criteria (from ticket)

- **AC1** Read-detection resolves alias reads; regenerating no longer false-flags the
  `proactive_memory_*` / `insights_wiring_enabled` / `quality_monitor_*` cluster as never-read.
- **AC2** Every deleted field carries an origin-ADR citation + a one-line "outgrown because …" in the PR table.
- **AC3** Zero forward-declaration params for active/planned streams (inference/routing) deleted.
- **AC4** Wiring-bug candidates and the cost-gov cap are carved out (follow-ups / deferral), not deleted here.
- **AC5** `make test` / `make mypy` / ruff clean; no behavioral change beyond removing confirmed-dead fields.

---

## Phase A — Alias-aware read detection (AST)

**A1. New pure-AST resolver module** `scripts/audit/settings_reads.py`:
- `settings_alias_names(tree) -> tuple[frozenset[str], frozenset[str]]` — `(name_aliases, attr_aliases)`
  bound to the AppConfig singleton in a module:
  - **name aliases**: `"settings"` seeded **unless the file rebinds it to a non-settings value**
    (`settings = StudySettings()` in `scripts/study/sweep.py:592` → shadowed → not seeded; fixes the
    codex-flagged evidence pollution); plus LHS of `Assign`/`AnnAssign` whose RHS is a settings value
    (`Name "settings"`, `get_settings()` call, or a `BoolOp` containing one — covers
    `settings or get_settings()`); plus function params / `AnnAssign` targets annotated `AppConfig`
    (handles `AppConfig`, `AppConfig | None`, `Optional[AppConfig]`, and the `"AppConfig"` string form).
  - **attr aliases** (codex fix — real at `brainstem/optimizer.py:98`): `self.<attr> = <settings-value>`
    → track `<attr>`, so `self.<attr>.<field>` reads count. Restricted to `self`/`cls` receivers.
  - File-level union (audit granularity is file→field, so per-scope precision is unnecessary; a stray
    reuse of an alias name is conservative — it can only *keep* a field, never wrongly delete one).
- `collect_field_reads(tree, field_names) -> list[tuple[str, int]]` — `(field, lineno)` for every
  read: `Attribute` whose `.value` is a settings value (name alias, `get_settings()` chain, or a
  `self.<attr-alias>` receiver) and whose `.attr` is a field name; plus
  `getattr(<settings-value>, "<field>")` with a string-literal key. AST parsing makes the multi-line
  `getattr` case fall out for free.

**A2. Rewire** `config_usage_audit.py`:
- Replace `_git_grep` + `_all_settings_usage_lines` (both become orphaned → remove) and the regex
  body of `external_reads` with a single cached AST pass: walk `src/`, `scripts/`, `tests/`
  `*.py` (excluding `settings.py` itself, as today), parse each, run `collect_field_reads`, bucket
  `field -> {root -> [file:line, …]}`. `external_reads(name)` returns that map's entry.
- Interface unchanged (`dict[str, list[str]]` keyed by root) → `categorize` / evidence rendering untouched.

**A3. TDD** — `tests/scripts/test_settings_reads.py` (new, unit-level, inline source strings):
- local alias (`cfg = settings; cfg.foo`), factory chain (`get_settings().foo`), multi-line
  `getattr`, `AppConfig`-typed param, `settings or get_settings()` BoolOp, **self-attribute alias**
  (`self._s = get_settings(); self._s.foo`), **shadow narrowing** (`settings = OtherSettings();
  settings.foo` → NOT a read), negative (unrelated `cfg.foo` where `cfg` never bound to settings).
- Extend `tests/scripts/test_config_usage_audit.py`: assert the three clusters
  (`proactive_memory_w_embedding`, `insights_wiring_enabled`, `quality_monitor_daily_run_hour_utc`)
  now have `src` read evidence and are **not** `never-read` (AC1 regression guard). Existing tests
  (`debug`, `url_guard_allowlist` getattr, manifest, test-only-exclusion) must still pass.

**A4. Regenerate** `docs/research/2026-07-16-fre-893-config-parameter-usage-audit.md` + CONFIG_INVENTORY §10
via `uv run python -m scripts.audit.config_usage_audit generate` (run on the VPS so the deployed `.env`
is read). Record the corrected `never-read` set — a strict subset of today's 43.

## Phase B — Provenance + curated removal

**B1. Provenance map.** For each field in the *corrected* never-read set: `git blame` its
`settings.py` line + read its section header (already ADR-tagged, e.g.
`# Proactive memory (ADR-0039, FRE-174–176)`) → `field → origin ADR/FRE → ADR current status`.

**B2. Classify** each into the ticket's four buckets:
- **Outgrown** (origin ADR retired/superseded/feature removed) → **delete** field.
- **Forward-declaration** (origin ADR active/planned — the inference/routing tree ADR-0082/0094/0095:
  `router_role`, `router_timeout_seconds`, `routing_policy`, `routing_heuristic_threshold`,
  `enable_reasoning_role`) → **keep**, note wiring gap. *(AC3)*
- **Wiring-bug** (set in deployed `.env` but no reader — `event_bus_ack_timeout_seconds`,
  `freshness_dormant_relationship_proposal_threshold`, `freshness_never_accessed_noise_days`,
  `second_brain_resource_gating_enabled`) → **keep**, file a follow-up investigation ticket. *(AC4)*
- **Cost-gov** (`cloud_weekly_budget_usd`) → **defer** to the cost-governance ADR (in flight); do not touch. *(AC4)*

**B3. Remove only confirmed-outgrown.** Before deleting any field, run the **non-Python-consumer
check** (codex fix): confirm it is absent from `config/**/*.yaml` (esp. `substrate.yaml`
`source: "setting:X"`), all 5 compose `environment:` blocks, the deployed `.env` keys, and env-bridge
scripts (`scripts/study/config.py`). Only a field dead across Python reads **and** every non-Python
surface is deletable. Delete each such `Field(...)` from `settings.py`, and remove any now-orphaned
validator/reference. **Deployed `.env` dead keys are noted in the master runbook** (the `.env` is
gitignored/VPS-only; the repo change is settings.py). Deletion table (field → origin ADR →
"outgrown because …") lives in the regenerated report **and** the PR body. *(AC2)*

**B4. Follow-ups.** File one Needs-Approval ticket for the wiring-bug set (reader-missing-vs-dead
investigation); deferral note for the cost-gov cap. No over-ticketing beyond these.

## Verification / halt

- Regenerate is idempotent (existing splice test); re-run twice, diff empty.
- `make test-file FILE=tests/scripts/test_settings_reads.py` then
  `make test-file FILE=tests/scripts/test_config_usage_audit.py`, then full `make test` · `make mypy` ·
  ruff. Self-review (`code-review` high — src+audit logic) + `security-review` (reads deployed `.env`).
- **Halt** if the corrected never-read set implies deleting a field whose origin ADR is not
  demonstrably retired — surface it, do not delete on a hunch (owner's binding caveat).

## Out of scope
The 186 read-but-never-overridden hardcode candidates (held vs ADR-0119 config-UI tunability).
