# FRE-540 — A3 Three-way reconciliation checker (CI floor), report-only

> **Ticket:** FRE-540 (Approved, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit (L0)
> **Realizes:** ADR-0090 D5 (decided floor) · **Validated against:** FRE-533 reconciliation table
> **Refs:** `scripts/audit/fre533_reconcile.py` (reusable primitives) ·
> `docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md` (gold classifications)

## Scope (what this ships)

A **hermetic** (no Elasticsearch, no Kibana) static checker that closes the
mapping ↔ dashboard corner pair and lints the mapping corner for trap classes, plus a
**report-only** CI job. Two non-floor checks (emit→mapping grep, live-mapping) ship as
clearly-separated, environment-gated/report-only sections.

Out of scope (ADR-0090 open decisions, explicitly NOT done here): pre-commit placement,
field-registry upgrade of the emit corner, flipping the gate to hard-fail (a follow-up does
that once FRE-534/535 are green).

## Design decisions (grounded in the repo)

1. **Self-describing family map.** Each `docker/elasticsearch/*index-template.json` declares its own
   `index_patterns` + `priority`. The checker builds the pattern→template map *from the templates*
   (no hardcoded registry — the fre533 registry is already stale vs the split captains templates).
2. **Panel→family resolution (hermetic).** A viz/lens object references an index-pattern via
   `references[].type == "index-pattern"`; the index-pattern saved object carries `attributes.title`
   (e.g. `agent-logs-*`, `agent-logs*`). Resolve title→template by longest-literal-prefix match
   against template `index_patterns` (handles `agent-logs*`↔`agent-logs-*` and
   captures-vs-subagents overlap deterministically).
3. **"Mapped" resolution reuses FRE-533 primitives** (`flatten_properties`, `load_template`,
   `Template.expected_type`, `DynamicRule`) so classifications stay consistent with the gold table.
   - Plain field `F`: mapped if explicit, or matched by a `dynamic_templates` rule. If template is
     `dynamic:false` and `F` is not explicit → **finding** (silently dropped). If guarded-dynamic and
     `F` falls to numeric es-default inference → **finding** (`referenced-but-unmapped`).
   - `F.keyword`: valid iff base is explicit `text` *with* a `fields.keyword` subfield, **or** the
     default string dynamic rule maps it to `text+keyword`. A **bare `keyword`** base (no subfield) or a
     dynamic rule mapping to bare `keyword` → **finding** (`.keyword`-on-bare-keyword) — this is the
     dominant gold breakage (`model.keyword`, `role.keyword`, `phase.keyword`, `from_state.keyword`).
4. **Trap-class lint over explicit properties** (the only fields a template hermetically knows):
   - numeric trap: name matches `FLOAT_HINT`/`MS_HINT` but explicit type is `long`/`integer` → finding.
   - join-key trap: name in `{trace_id,session_id,task_id,span_id,run_id,entry_id}` or `*_id` (non-container)
     but explicit type ≠ `keyword` → finding.
   - long-text trap: name matches `TEXT_TRAP_HINT`, explicit type `keyword` with `ignore_above ∈ {256,1024}`
     → finding (silent >ignore_above drop).
   - `_meta` block absent → finding (ADR-0090 D2; report-only — none carry it yet, FRE-534 backfills).
5. **Reuse the FLOAT_HINT/MS_HINT/TEXT_TRAP_HINT/JOIN_KEY hints from fre533** (extend JOIN_KEY with
   `task_id`). Importing keeps the two tools' taxonomy identical (acceptance: "same classifications").

## Modes

- Default **report mode**: print findings grouped by check + family; **exit 0**.
- `--gate`: exit **1** if any *floor* finding (checks 1–2) exists. Used by the follow-up flip, not CI yet.
- Non-floor sections (emit→mapping grep over `telemetry/`/`captains_log/`/`observability/`;
  live-mapping via `GET _mapping` when `--es-url` reachable) are **always report-only**, never affect exit.

## Files

| File | Action |
|------|--------|
| `scripts/audit/__init__.py` | **new** — empty package marker (enables `from scripts.audit.fre533_reconcile import …`) |
| `scripts/audit/telemetry_surface_check.py` | **new** — the checker (argparse: `--gate`, `--es-url`, `--templates-dir`, `--dashboards-dir`) |
| `tests/scripts/test_telemetry_surface_check.py` | **new** — synthetic-fixture unit tests + real-file smoke |
| `.github/workflows/ci.yml` | **edit** — add `telemetry-surface` job (report mode, non-blocking) |

## Build steps (TDD; verify after each)

1. **Package marker + skeleton.** Add `scripts/audit/__init__.py`. Create the module with the dataclasses
   (`LoadedTemplate`, `PanelRef`, `Finding`) and import the fre533 primitives.
   → verify: `uv run python -c "import scripts.audit.telemetry_surface_check"` exits 0.
2. **Template loader test → impl.** Test: loads all real templates, builds pattern→template map,
   resolves `agent-logs*` and `agent-captains-captures-subagents-*` to the right files.
   → `uv run python -m pytest tests/scripts/test_telemetry_surface_check.py -k loader -q` red→green.
3. **Dashboard parser test → impl.** Test (synthetic NDJSON in `tmp_path`): extracts (panel-title,
   index-pattern-title, field-refs) incl. `.keyword` suffix and Lens `state`.
   → `pytest -k dashboard` red→green.
4. **Field-resolution test → impl.** Synthetic template covering: explicit text+keyword (OK),
   bare keyword + `.keyword` (broken), missing field + `.keyword` via default rule (broken),
   `dynamic:false` missing field (broken), numeric es-default (referenced-but-unmapped).
   → `pytest -k resolve` red→green.
5. **Trap-lint test → impl.** Synthetic template with a `long` cost field, a `text` `trace_id`,
   a `keyword ignore_above:1024` `error_message`, and no `_meta` → 4 findings of the right classes.
   → `pytest -k trap` red→green.
6. **Driver + modes test → impl.** Synthetic drift fixtures: report mode exit 0; `--gate` exit 1 on
   (a) panel→unmapped field, (b) numeric-as-long, (c) missing `_meta`. Clean fixtures → `--gate` exit 0.
   → `pytest -k gate` red→green.
7. **Real-file smoke + gold validation.** Over the committed files: assert the checker runs hermetically,
   exit 0 in report mode, and that the `monitors-joinability` template raises **zero trap findings**
   (the gold "model" family). Assert a known bare-keyword `.keyword` panel ref is reported. (No exact
   counts — repo surface evolves as FRE-534/535 land.)
   → `pytest tests/scripts/test_telemetry_surface_check.py -q` all green.
8. **CI wiring.** Add a `telemetry-surface` job to `ci.yml`: checkout → uv sync →
   `uv run python scripts/audit/telemetry_surface_check.py` (report mode, no `--gate`). Non-blocking
   because it does not pass `--gate`; note in the PR that a follow-up flips it once FRE-534/535 are green.
   → verify: `uv run python scripts/audit/telemetry_surface_check.py` exits 0 and prints a report.

## Quality gates (all before PR)

`make test-file FILE=tests/scripts/test_telemetry_surface_check.py` → then `make test` ·
`make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Follow-up tickets to file (Needs Approval, Telemetry Surface Audit)

- **Flip the floor to a hard gate** (`--gate` in CI) once FRE-534/535 triage the baseline green.
- (Open ADR decisions — only if owner wants them tracked now) field-registry emit corner; pre-commit copy.

## Acceptance mapping (FRE-540)

- Hermetic run over templates+dashboards, nonzero on introduced static drift → steps 6–7 (`--gate`).
- Same classifications as FRE-533 → reuse of fre533 hints/resolver + step 7.
- Wired into CI report-only; follow-up flips to gate → step 8 + follow-up ticket.
- Emit→mapping + live-mapping present as report-only/env-gated, separated from floor → module sections.
- Placement is the CI job; pre-commit + field-registry left as ADR open decisions → not done, noted.
