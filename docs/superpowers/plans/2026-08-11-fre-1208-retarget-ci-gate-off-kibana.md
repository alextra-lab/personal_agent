# FRE-1208 — Retarget the ADR-0090 CI reconciliation gate off Kibana NDJSON

Plan ref: `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § T8.1.
Ticket: FRE-1208. Tier: 2 (Sonnet). Blocks FRE-1214 (T9 — Kibana retirement).

## Scope (from Step 2)

- `scripts/audit/telemetry_surface_check.py` is CI-wired (`.github/workflows/ci.yml:256`, hard gate)
  and currently parses `config/kibana/dashboards/*.ndjson` for the **dashboard corner** of ADR-0090's
  reconciliation. Its Lens-specific keys (`visState`, `kibanaSavedObjectMeta`, index-pattern saved
  objects) don't exist in Grafana panel JSON.
- **Take option (a)**: repoint at `config/grafana/dashboards/*.json`, rewrite field extraction for
  Grafana's shape. Confirmed empirically: every `"field"` key in every committed Grafana dashboard
  JSON lives inside `panels[].targets[].{metrics[],bucketAggs[]}` — no other shape needs walking.
- Archive `scripts/audit/fre533_reconcile.py` and `scripts/audit/verify_fre535_panels.py` — one-shot,
  no CI wiring, same Kibana-schema dependency.
- **Not in scope**: deleting `config/kibana/` itself (that's FRE-1214/T9 — this PR only stops the gate
  depending on it). AC-1 verifies this *locally*, not by actually deleting the directory in this PR.

## Design decisions

**Codex plan-review (2026-08-11) findings incorporated below**, marked `[codex]`: fail-open unknown-uid
handling (revised to fail-loud), missing `query`-string field extraction (added), archive path breaking
`parents[2]` repo-root resolution (avoided by archiving flat, no new subdirectory), the AC-3 shared-count
being 15 not 16 (corrected), and a real AC-3 superset gap in `cost_budget` (documented as a justified
drop, not silently asserted away). Verdict was "needs revision" on the pre-review draft; this is the
revised design.

1. **Family resolution (the index-pattern-saved-object equivalent).** Kibana resolved a panel's family
   via an `index-pattern` saved object's `title`. Grafana has no per-panel equivalent — the ES index
   glob lives on the **datasource**, in `config/grafana/provisioning/datasources/datasources.yaml`
   (`jsonData.index`, e.g. `agent-logs*` for uid `es-agent-logs`). New loader
   `load_datasource_index_patterns()` parses that YAML (`type: elasticsearch` entries only) into
   `{uid: index_pattern}`; each target's `datasource.uid` resolves through it to get the family glob,
   which then flows into the existing (unchanged) `resolve_template()`.
   **`[codex]` fail-loud on unknown uid:** a target whose `datasource.type == "elasticsearch"` but whose
   `uid` is *not* in the loaded map raises `ValueError` (dashboard/panel/uid named in the message) rather
   than silently resolving `index_pattern_title=None` and having `check_mapping_dashboard` skip it. A
   stale/typo'd uid must fail CI, not silently stop checking a family — "a gate that cannot fail
   verifies nothing" applies to config drift too, not just field drift.
   **`[codex]` fail-loud on missing dashboards dir:** `parse_panels` raises `FileNotFoundError` if
   `dashboards_dir` doesn't exist (same posture as `load_baseline`'s missing-path check) — a
   deleted/mistyped directory must not silently produce zero panels and a passing gate.
2. **Field extraction — three sources per target, not two.** Verified via one-off inspection (`python3`
   walk over every committed `config/grafana/dashboards/*.json`) that structural `"field"` keys appear
   *only* under `targets[].metrics[].field` and `targets[].bucketAggs[].field`. **`[codex]` correction:**
   ADR-0090's own stated target shape is `{query, metrics[].field, bucketAggs[].field}` — three parts —
   and several real committed panels (`health_check.json` "Fixture Health Check",
   `turn_session_artifact.json` "Turn classification detail" / "Artifact envelope detail (join on
   artifact_id)") reference fields **only** inside their Lucene-style `query` string (e.g.
   `event_type: "fre1072_health_check"`), with empty/field-less `metrics`/`bucketAggs`. Dropping `query`
   would make those panels invisible to the checker post-port — not a literal regression vs. the old
   Kibana walker (which also never parsed embedded query-string field refs), but a real gap against the
   ADR's *design intent*, which the ticket says to implement "as designed." Every committed ES-target
   `query` string was inspected (55 occurrences across all dashboards) and all follow the simple
   `field: value` / `field: (v1 or v2)` / `field:value` shape with no `_exists_:`-style meta-syntax — so
   extraction is a regex over `field_name` tokens immediately followed by `:`
   (`r'(?<![\w.])([A-Za-z_][A-Za-z0-9_.]*)\s*:'`), unioned into the same field set as the
   metrics/bucketAggs refs for that target. Scoped to targets whose own `datasource.type ==
   "elasticsearch"` — Tempo/other targets carry no `metrics`/`bucketAggs`/ES-shaped `query` and are
   skipped by construction.
3. **Reused FRE-533 primitives** (`FLOAT_HINT`, `MS_HINT`, `JOIN_KEY`, `TEXT_TRAP_HINT`, `DynamicRule`,
   `Template`, `flatten_properties`) are format-agnostic (they operate on the ES *mapping* corner, not
   the dashboard corner) and stay live. Since they currently live in `fre533_reconcile.py`, which is
   being archived, **inline a copy directly into `telemetry_surface_check.py`** rather than leaving an
   import pointing into the archive (an archived module must not be a live dependency) or inventing a
   new shared module (unwarranted abstraction for a one-time move). The archived copy keeps its own
   definitions verbatim — duplication in a frozen, never-imported artifact is fine. Codex confirmed this
   is sound and flagged the module docstring's now-false "Reuses the validated FRE-533 primitives"
   claim (`telemetry_surface_check.py:49`) — updated in step 5 below.
4. **Baseline impact.** Confirmed empirically: `scripts/audit/telemetry_surface_baseline.json`'s 6
   entries are *all* `trap-lint` (mapping corner) — zero `mapping-dashboard` (dashboard corner)
   entries today (codex independently re-verified this at file:line for each entry). So switching
   dashboard corners requires no baseline edits *if* the Grafana port is clean; this is verified live
   in Step 4 (AC-1/AC-3), not assumed — and per point 5 below, it is *not* fully clean for one dashboard.
5. **`[codex]` AC-3 correction — 15 shared dashboards, not 16, and `cost_budget` has a genuine,
   justified field drop.** `data_views.ndjson` (Kibana index-pattern definitions, not a dashboard) and
   `health_check.json` (new in Grafana, no Kibana counterpart) are both excluded from the shared set,
   leaving 15. Of those, `cost_budget` fails a naive superset check: the Kibana version references
   `cap_usd`, `running_total`, `utilization_ratio`; the currently-committed `config/grafana/dashboards/
   cost_budget.json` is a pre-existing, simpler ES-based recreation (event counts/roles) that predates
   FRE-1209's in-flight Postgres-backed rebuild of that same dashboard (parallel `build2` stream, not
   yet merged). This is a **pre-existing drop this ticket did not create and does not own fixing** —
   FRE-1209 supersedes `cost_budget.json`'s content entirely. AC-3's own wording ("any field the port
   drops is listed and justified") anticipates exactly this: document it in the PR/handoff, don't paper
   over it, don't block this PR on FRE-1209's separate landing.
6. **`[codex]` Archive placement — flat, no subdirectory.** The `graphiti_experiment` archive precedent
   nests one directory deeper (`scripts/archive/graphiti_experiment/`), but both `fre533_reconcile.py`
   and `verify_fre535_panels.py` compute `REPO = Path(__file__).resolve().parents[2]` assuming exactly
   two parent levels to repo root (matching their current `scripts/audit/` depth). Nesting them under a
   new `scripts/archive/fre533_fre535_reconciliation/` subdirectory would silently break that (and
   `DASH_DIRS`, `EMIT_DIRS`, template-loading paths, and the module docstrings' `Usage::` examples) for
   anyone who ever runs the archived copy manually. **Revised: archive flat** —
   `scripts/archive/fre533_reconcile.py`, `scripts/archive/verify_fre535_panels.py` — same nesting
   depth as today, zero internal path breakage, no script-internal edits needed beyond the header note.
7. **`[codex]` ADR-0090 is explicitly NOT touched by this ticket.** ADR-0090 already carries a
   2026-08-08 (FRE-1213) forward-binding amendment that explicitly anticipates this exact moment: *"Until
   T8.1 lands, a reader checking the repo will find the Kibana path still wired, and that is expected
   rather than drift."* Updating that "state of play" paragraph once T8.1 (this ticket) actually lands is
   ADR-amendment work — Tier 1/Opus territory per the plan doc's own T8 tiering, not this Tier-2 ticket's
   job (build-skill Step 2: "the ADR's own criteria are not yours to carry"). Left for a future
   ADR-session pass; noted in the PR/handoff so it isn't mistaken for an oversight.

## Atomic steps

1. **Inline the FRE-533 primitives** — in `scripts/audit/telemetry_surface_check.py`, replace
   `from scripts.audit.fre533_reconcile import (...)` with the primitives copied in verbatim
   (regexes, `_glob_match`, `DynamicRule`, `Template`, `flatten_properties`). ~2 min.
2. **Datasource loader** — add `DEFAULT_DATASOURCES_PATH` and `load_datasource_index_patterns(path)`
   (parses YAML via `yaml.safe_load`, filters `type == "elasticsearch"`, extracts `jsonData.index`;
   raises `FileNotFoundError` on a missing path, same fail-loud posture as `load_baseline`).
3. **Rewrite dashboard parsing** — replace `DEFAULT_DASHBOARDS_DIR` (→ `config/grafana/dashboards`),
   drop `_walk_field_refs`/`_index_pattern_titles`/`_panel_index_pattern` (Kibana-only), replace
   `parse_panels(dashboards_dir)` → `parse_panels(dashboards_dir, datasource_index)`:
   - raise `FileNotFoundError` if `dashboards_dir` doesn't exist;
   - glob `*.json`; for each panel, for each target where `target["datasource"]["type"] ==
     "elasticsearch"`: look up `uid = target["datasource"]["uid"]` in `datasource_index` — **raise
     `ValueError`** (naming dashboard file, panel title, uid) if the uid is unknown, else
     `index_pattern_title = datasource_index[uid]`;
   - collect fields from three sources into one set: `{m["field"] for m in target.get("metrics", [])
     + target.get("bucketAggs", []) if isinstance(m, dict) and isinstance(m.get("field"), str)}`
     **plus** a new `_query_field_refs(query: str) -> set[str]` applied to `target.get("query")`
     (regex `r'(?<![\w.])([A-Za-z_][A-Za-z0-9_.]*)\s*:'`, one match group per hit);
   - emit one `PanelRef` per non-empty target (`dashboard=filename`, `title=panel["title"]`); unchanged
     `Finding`/`check_mapping_dashboard` downstream — no changes needed there.
4. **Wire `run_checks()` / `main()`** — thread `datasources_path` through (`run_checks(templates_dir,
   dashboards_dir, datasources_path=DEFAULT_DATASOURCES_PATH, es_url=None)`), add `--datasources` CLI
   flag defaulting to `DEFAULT_DATASOURCES_PATH`.
5. **Update stale docstrings + CI comment** — `telemetry_surface_check.py`'s module docstring (the
   "Reuses the validated FRE-533 primitives" claim, now describing an inlined copy, not an import),
   the `PanelRef`/`run_checks`/`parse_panels` docstrings ("NDJSON filename" → "dashboard JSON
   filename", "Kibana NDJSON saved objects" → "Grafana dashboard JSON"), and
   `.github/workflows/ci.yml:229-234`'s comment block. **ADR-0090 itself is left untouched** — see
   design decision 7.
6. **Archive the siblings, flat** — `git mv scripts/audit/fre533_reconcile.py
   scripts/archive/fre533_reconcile.py`, `git mv scripts/audit/verify_fre535_panels.py
   scripts/archive/verify_fre535_panels.py` (same nesting depth as today — `parents[2]` still resolves
   to repo root, no internal path edits needed), and prepend a short header note to each moved file:
   one-shot FRE-533/FRE-535 artifact, Kibana-schema-dependent, superseded by FRE-1208's port — kept for
   provenance, not imported anywhere.
7. **Rewrite `tests/scripts/test_telemetry_surface_check.py`'s dashboard-shaped tests** — TDD, see
   below; the template/trap-lint/baseline tests (mapping corner) are untouched since that corner
   didn't change.

## TDD — tests to write first (failing), then make pass

All in `tests/scripts/test_telemetry_surface_check.py` unless noted.

- Replace `_viz`/`_index_pattern`/`_write_dashboard` (NDJSON/saved-object shaped) with Grafana-shaped
  builders: `_write_datasources_yaml(path, {uid: index_pattern})`, `_target(uid, metrics_fields=(),
  bucket_fields=(), query="", ds_type="elasticsearch")`, `_panel(title, targets)`,
  `_write_dashboard_json(path, panels)`.
- `test_parse_panels_extracts_fields_and_index_pattern` — rewritten for the new shape: one ES target
  with `metrics=[{"field": "cost_usd"}]`, `bucketAggs=[{"field": "@timestamp"}]` → `PanelRef` with
  `index_pattern_title` resolved through the datasource map and `fields == {"cost_usd", "@timestamp"}`.
- **New**: `test_parse_panels_extracts_query_field_refs` — a target with `query='event_type: "x" and
  outcome: "success"'` and empty `metrics`/`bucketAggs` → `fields == {"event_type", "outcome"}`.
  Regression-guards the codex-review fix (query-only panels like the real committed "Fixture Health
  Check" and "Turn classification detail" would otherwise be silently invisible to the checker).
- **New**: `test_parse_panels_skips_non_elasticsearch_targets` — a Tempo-typed target contributes no
  `PanelRef`/no fields, even with a `query` set.
- **New**: `test_parse_panels_raises_on_unknown_datasource_uid` — an ES-typed target whose `uid` isn't
  in the datasources map raises `ValueError` naming the dashboard/panel/uid (codex-review fix: was
  previously going to silently resolve to `None` and be skipped).
- **New**: `test_parse_panels_raises_on_missing_dashboards_dir` — a nonexistent `dashboards_dir` raises
  `FileNotFoundError` rather than silently yielding zero panels (codex-review fix).
- `_resolve_one`-style rewrite for **AC-2** (`check_mapping_dashboard` still fires): a panel whose
  `bucketAggs` field is a bare-keyword `.keyword` ref against a synthetic template → floor finding.
  This is the direct regression-guard that "a gate that cannot fail verifies nothing."
- `test_gate_exits_nonzero_on_introduced_drift` / `test_report_mode_exits_zero_despite_drift` /
  `test_gate_passes_on_clean_surface` — same intent, rewritten fixtures.
- Real-file smoke tests, rewritten against `config/grafana/dashboards` +
  `config/grafana/provisioning/datasources/datasources.yaml`:
  - `test_real_files_run_hermetically_report_mode` (unchanged assertion, new defaults)
  - `test_real_committed_dashboard_keyword_refs_are_clean` (unchanged assertion — this is where the
    port's correctness against the *actual* repo dashboards gets proven)
  - `test_real_committed_floor_is_exactly_the_allowlisted_exceptions` (unchanged assertion — proves
    no baseline edit was needed, confirming design decision 4)
  - `test_real_committed_baseline_makes_gate_pass` (unchanged assertion — this **is** AC-1's proof:
    the gate passes hermetically with the current repo state, and per design decision 4 that state
    already has zero Kibana-dependency in what the gate reads)
- **AC-1 literal proof** (also recorded in the PR/handoff, not just the smoke test above): run
  `mv config/kibana /tmp/kibana-removed-fre1208 && uv run python -m scripts.audit.telemetry_surface_check --gate --baseline scripts/audit/telemetry_surface_baseline.json; echo "exit=$?"; mv /tmp/kibana-removed-fre1208 config/kibana`
  locally — exit 0 with `config/kibana/` absent is the acceptance criterion verbatim.
- **AC-3 equivalence proof** — a one-off comparison (not a permanent regression test, since
  `config/kibana/` is deleted outright in FRE-1214/T9 and a test asserting against it would need
  deleting again there): a small script that, for each of the **15** dashboard basenames present in
  both `config/kibana/dashboards/*.ndjson` and `config/grafana/dashboards/*.json` (excludes
  `data_views.ndjson`, which is index-pattern definitions, not a dashboard, and `health_check.json`,
  which is new in Grafana with no Kibana counterpart), runs the **old** (pre-edit, captured via
  `git show HEAD:scripts/audit/telemetry_surface_check.py` before this branch's changes) Kibana
  `parse_panels` against the Kibana file and the **new** Grafana `parse_panels` against the Grafana
  file, and reports per-dashboard whether the Grafana field set is a superset-or-equal of the Kibana
  one. **Known finding, not a blocker**: `cost_budget` will show a real drop (`cap_usd`,
  `running_total`, `utilization_ratio` — see design decision 5) — document it with justification
  rather than silently pass or block the PR on it. Run once, capture output, paste into the Linear
  handoff comment as evidence.

## Exact test commands

```bash
uv run pytest tests/scripts/test_telemetry_surface_check.py -v
uv run python -m scripts.audit.telemetry_surface_check --gate --baseline scripts/audit/telemetry_surface_baseline.json
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Risk tier

**Standard** — touches `src`-adjacent `scripts/` logic feeding a hard CI gate (`ci.yml`), rewrites a
schema-parsing corner of an ADR-0090 floor check. **codex:rescue plan review required** before coding.

## Self-review escalation class (Step 8, for later)

Self-serve — this is a CI-gate/audit-script change, not a production write path (no Neo4j/Postgres/ES/R2
write in the running service), not destructive, not a schema change to a *live* system, not cost/governance
code. Both reviewers run, findings fixed on-branch, no owner-flagged escalation needed.
