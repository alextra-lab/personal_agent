# FRE-555 — Flip the reconciliation checker floor to a CI hard gate (baseline-snapshot)

**Ticket:** FRE-555 (Approved, Tier-2:Sonnet, Telemetry Surface Audit)
**Refs:** ADR-0090 D5/D6 · FRE-540 (the checker) · FRE-534/535 (baseline burn-down) ·
`docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md`

## Problem

`scripts/audit/telemetry_surface_check.py` ships in **report mode** (CI `telemetry-surface` job
runs with no `--gate`, never fails the build). The ticket asks to flip it to `--gate` so new/changed
`(field, family)` drift fails CI (ADR-0090 D5 + D6 done-bar).

**Blocker:** the floor is **not green**. `--gate` locally → **20 floor findings**:

- 2 mapping↔dashboard: `title.keyword`, `insight_type.keyword` (known broken `agent-insights` panels)
- 13 missing-meta: most templates still lack the ADR-0090 D2 `_meta` block
- 3 numeric-as-long: `metrics_summary.threshold_violations`, `latency_ms`, `probe_duration_ms`
- 2 long-text-ignore-above: `reason`, `decomposition_reason`

(2 mapping-dashboard + 18 trap-lint = **20**.)

FRE-534/535 are Done but did **not** burn the floor to zero (ADR-0090:83 — `_meta` backfill was
*part of FRE-534's pass*, only partially done). The ticket is explicit: **never gate on red.**

## Decision: refined gate — fix the surface, allowlist only reviewed-correct exceptions

Owner chose full-clean over grandfathering. On tracing all 20, only **6 are genuine** and **14 are
checker false-positives / deliberately-correct mappings**:

- **13 × missing-meta = checker bug.** Every flagged template *has* `_meta`, at the **template-root**
  level (`{index_patterns, priority, template, _meta}` — the valid ES composable-template metadata
  slot FRE-534 used, registered via `PUT /_index_template/...`). The checker only inspects
  `template.mappings._meta`. → **Fix the checker** to accept `_meta` at root *or* mappings.
- **2 × dashboard = genuine, cheap.** `insight_type.keyword` (bare keyword → ref should be
  `insight_type`, matching its 2 sibling panels) and `title.keyword` (`text` w/o subfield → add a
  `.keyword` subfield to the insights template; additive, no reindex).
- **3 × reviewed-correct** (`metrics_summary.threshold_violations` integer *count*; `reason`,
  `decomposition_reason` short keyword enums) — the name-heuristic lint false-fires on the words
  "threshold"/"reason". No static linter can disambiguate a *count* from a *value*; honest fix = a
  documented exception, not mutating a correct mapping.
- **2 × `*_ms` genuine but reindex-bound** (`latency_ms` long, `probe_duration_ms` integer → ADR wants
  float). Type change on the *hot* `agent-logs` index → ES rejects in place → needs reindex/rollover
  (deploy op). **Deferred to a follow-up ticket**; allowlisted here with a `pending-reindex` note.

Net: checker `_meta` fix (−13) + 2 dashboard fixes (−2) → **5 residual**, all deliberate/deferred,
captured in a small **documented allowlist** (not a grandfathered-defect baseline). Then plain-style
gate via `--gate --baseline <5-entry-file>`.

Allowlist key = 5-tuple `(check, klass, family, field, source)`, dropping only volatile `detail`
(Codex review: `source` keeps `mapping-dashboard` grandfathering panel-specific so a new broken panel
on an allowlisted field still fails; for `trap-lint`, `source == family`, redundant-but-harmless).

## Steps

1. **Checker** (`scripts/audit/telemetry_surface_check.py`):
   - `load_template_file`: `has_meta = "_meta" in mappings or "_meta" in data` (accept template-root).
   - `finding_key(f)` → `(check, klass, family, field, source)`.
   - `load_baseline(path)` → `set[tuple]`; error loudly if `--baseline` given but file absent.
     Ignores extra keys (`detail`/`note`) so the file is self-documenting.
   - `diff_baseline(floor, baseline)` → `(new, grandfathered, stale)` (pure, testable).
   - `--baseline PATH`: in `--gate`, fail only on `new`; print NEW / ALLOWLISTED(count) / STALE(info).
     Stale (fixed → no longer found) reported, **never gated** (Codex Q2).
   - `--write-baseline PATH`: regenerate from current floor (sorted, deterministic, incl. `detail`),
     exit 0. **Local-only helper; CI never invokes it.**
   - Update docstring phasing note.
2. **Surface fixes:** `docker/elasticsearch/insights-index-template.json` (add `title.keyword`
   subfield); `config/kibana/dashboards/insights_engine.ndjson` (`insight_type.keyword`→`insight_type`).
3. **`scripts/audit/telemetry_surface_baseline.json`** — generated via `--write-baseline`, then each
   of the 5 entries annotated with a `note` saying *why* it's an accepted exception.
4. **`.github/workflows/ci.yml`** — flip job to `--gate --baseline …`; rename, rewrite comment.
5. **Tests (TDD)** in `tests/scripts/test_telemetry_surface_check.py`:
   - `_meta` at template-root is detected (no missing-meta) — new
   - baseline suppresses allowlisted → rc 0; new drift not in baseline → rc 1
   - `--write-baseline` round-trips → rc 0; stale entry reported, not gated → rc 0
   - real-file: committed floor == the 5 expected allowlist keys (locks completeness)
   - real-file: `--gate` + committed baseline → rc 0 (proves the flip is safe)
   - update `test_real_committed_keyword_refs_are_flagged` (panels now fixed) +
     `test_real_joinability_template_is_meta_only_clean` (now fully clean)
6. **Follow-up ticket** (Needs Approval, deploy-gated): `latency_ms`/`probe_duration_ms` → float (FRE-599) +
   `agent-logs` reindex/rollover; drop those 2 allowlist entries on completion.

## Verify

- `make test-file FILE=tests/scripts/test_telemetry_surface_check.py` → green
- `uv run python -m scripts.audit.telemetry_surface_check --gate --baseline scripts/audit/telemetry_surface_baseline.json` → exit 0
- `uv run python -m scripts.audit.telemetry_surface_check --gate` → exit 1 (20 findings, unchanged)
- `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`

## Out of scope (per ticket)

Field-registry upgrade of the emit→mapping report check; pre-commit copy of the static floor.
