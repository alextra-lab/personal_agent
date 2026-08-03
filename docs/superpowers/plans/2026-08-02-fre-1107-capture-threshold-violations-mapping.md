# FRE-1107 — capture template maps threshold_violations as integer but the producer writes list[str]

Ticket: https://linear.app/frenchforest/issue/FRE-1107

## Root cause (traced)

`docker/elasticsearch/captains-captures-index-template.json` maps
`metrics_summary.threshold_violations` to `integer`. The producer
(`RequestMonitor._compute_summary`, `src/personal_agent/brainstem/sensors/request_monitor.py:174/187`)
writes `list(set(self._threshold_violations))` — a list of human-readable strings like
`"CPU critically high: 100.0 percent (DEGRADED threshold)"`. `TaskCapture.metrics_summary`
(`src/personal_agent/captains_log/capture.py:72`) is an untyped `dict[str, Any]` passed straight
through to the ES capture document — the raw list, not the derived count computed separately in
`metrics_extraction.py` (`Metric(name="threshold_violations", value=count)`, a *different* field
entirely: `metrics_structured`). ES cannot coerce a list of strings into `integer` and rejects the
whole document.

The prior FRE-555 review baselined this as "ACCEPTED — reviewed-correct... integer is intentional,
this is a COUNT" (`scripts/audit/telemetry_surface_baseline.json`). That review conflated the raw
`metrics_summary.threshold_violations` field with the derived structured-metric count and was wrong.
`FLOAT_HINT` false-fires on the word "threshold" in the leaf name, which is how the static lint
caught the *shape* of the problem but not its actual direction.

Sibling audit (`_compute_summary`): `cpu_avg/max/min`, `memory_avg/max/min`, `duration_seconds` are
all `round()` floats — float mapping is correct. `samples_collected` is `len(samples)` — integer
mapping is correct. `threshold_violations` is the only mismatch in this template.

## Fix

1. **`docker/elasticsearch/captains-captures-index-template.json`** — map
   `metrics_summary.threshold_violations` to `{"type": "keyword", "ignore_above": 1024}`. This is
   what legacy (pre-template) daily indices already carry via dynamic mapping — the same
   `default_string_keyword` catch-all every other unmatched string field falls through to — so no
   transform is needed on reindex, per the ticket. Append a note to `_meta.description` correcting
   the prior (wrong) rationale, following the file's existing convention of documenting per-field
   reasoning.

2. **`scripts/audit/telemetry_surface_baseline.json`** — remove the `metrics_summary.threshold_violations`
   entry. After the fix, `ftype` is `keyword` (not in `_INT_TYPES`), so `check_trap_lint`'s
   `numeric-as-long` finding no longer fires for this field — the entry becomes stale in exactly the
   sense the checker's own `STALE allowlist entries (finding fixed — prune from baseline)` output
   flags, except the note it's replacing was actively wrong, not just outdated.

3. **`tests/scripts/test_telemetry_surface_check.py`** — `test_real_committed_floor_is_exactly_the_allowlisted_exceptions`
   currently asserts `metrics_summary.threshold_violations` is one of **six** allowlisted floor
   findings. Remove it from `expected_fields`; **five** remain: `latency_ms`, `probe_duration_ms`,
   `reason`, `decomposition_reason`, `threshold`. **Revised after codex plan-review** — strengthen the
   assertion from field-names-only to full finding-key equality, so a wrong `check`/`klass`/`family`/
   `source` on a surviving entry can't slip through unnoticed: build the expected set as full 5-tuples
   from the *trimmed* baseline file's remaining rows (via `load_baseline`) and assert
   `keys == expected_keys` (not just `{k[3] for k in keys} == expected_fields`).

4. **New guard — `scripts/audit/telemetry_surface_check.py`** (the ticket's third ask: "add a guard
   that compares each explicitly mapped field against a real sampled document from the live family,
   so a template asserting a type the producer never writes fails a check rather than waiting for a
   deploy to activate it"). A new **check 5**, environment-gated + report-only like the existing
   `check_live_mapping` (never affects `--gate` exit code — it needs a live ES connection and cannot
   run hermetically).

   **Revised after codex plan-review** (numeric-only scope was flawed — a bare list value is the
   *normal, correct* shape for a `keyword`/`text` field, e.g. `tools_used`/`supporting_metrics`; a
   guard that flags every list would false-fire on those). Type-family rules instead of one
   numeric-only rule, each applied **element-wise** so a list is only flagged when an element's shape
   doesn't fit — not merely for being a list:
   - `_NUMERIC_TYPES = {integer, long, short, byte, float, double, half_float, scaled_float}` — every
     element must be `int`/`float` or a numeric-parsable string; a `bool` element does not count
     (JSON `true`/`false` is not a number here).
   - `_STRING_TYPES = {keyword, text}` — every element must be a scalar (`str`/`int`/`float`/`bool`);
     a `dict` element is the mismatch (a nested object landing where a flat string was expected).
   - `_BOOLEAN_TYPES = {boolean}` — every element must be `bool`.
   - Anything else (`date`, `geo_point`, container/object/nested fields already excluded via
     `attrs.get("container")`, …) is **not validated** — date/geo shape-checking is a different,
     fragile problem not implicated in this defect class, and skipping it avoids false positives.
     This scoping is stated explicitly in the function's docstring rather than silently implied, so
     "compares each explicitly mapped field" in the ticket is read as "each field whose mapped type
     falls into a family this guard can validate without guessing," not literally every ES type.
   - An empty list is inconclusive (no element to check) — skip, not a mismatch.
   - `_flatten_doc_values(doc: dict) -> dict[str, Any]`: dotted-path leaf values from a real
     document — recurses into plain dicts, leaves lists as opaque leaf values at their path (arrays
     of dicts / `nested` fields are out of scope for the same reason as above).
   - `_field_type_mismatch(mapped_type: str, value: Any) -> str | None`: dispatches to the right
     type-family rule above; returns a klass string or `None`.
   - `sample_documents(es_url, pattern, size) -> list[dict]`: thin `_search` (`match_all`, `size`),
     best-effort — returns `[]` on any error, matching `check_live_mapping`'s liveness-probe style.
   - `check_sample_document_types(templates, es_url, size=20) -> list[Finding]`: per template, fetch
     samples from its first `index_patterns` entry, flatten each, run `_field_type_mismatch` against
     every explicit non-container field present in a sample. One `Finding` per distinct
     `(family, field)` mismatch (`check="sample-mapping"`, `klass="producer-type-mismatch"`).
   - Wire into `run_checks`: append to `report.report_only` when `es_url` is given, alongside
     `check_live_mapping`. Update the module docstring's numbered check list (5. sample↔mapping,
     with the type-family scoping noted).

5. **Tests** (`tests/scripts/test_telemetry_surface_check.py`) — hermetic, no live ES. **Revised
   after codex plan-review**: the ticket's own quoted example text ("100.0 percent") is a paraphrase,
   not the literal producer output — `_check_thresholds` (`request_monitor.py:150`) formats
   `f"CPU critically high: {cpu_percent:.1f}% (DEGRADED threshold)"`, i.e. a `%` sign, not the word
   "percent". Tests use the **literal** producer string: `"CPU critically high: 100.0% (DEGRADED
   threshold)"`, not the ticket's prose paraphrase.
   - Unit-test the pure functions directly (matching the file's existing pattern of testing
     `_resolve_field` directly rather than the network wrapper): `_field_type_mismatch` boundary
     cases per type family (numeric: int ok, numeric-string ok, list-of-numeric-strings ok, bare list
     with a non-numeric element not ok, bool element not ok; string: list-of-str ok, dict element not
     ok; boolean: non-bool element not ok) and `_flatten_doc_values` (nested dict flattens, list stays
     opaque at its path).
   - **End-to-end guard test** (closes the codex-flagged gap — the pure-function tests alone don't
     exercise template traversal, explicit-field lookup, or dedup): call
     `check_sample_document_types` with a real loaded `captains-captures-index-template.json` and an
     **injected** sample-fetch (monkeypatch `sample_documents`, not real network) returning one doc
     built from the literal producer string above.
     - Against the OLD mapping (`threshold_violations: integer`) → the call returns exactly one
       `Finding` for `metrics_summary.threshold_violations` with `check="sample-mapping"`,
       `klass="producer-type-mismatch"` (proves the guard would have caught this before the deploy
       that activated it).
     - Against the FIXED mapping (`keyword`) → empty result.
   - **Live-sample verification is explicitly NOT part of this PR's test suite** — it needs a live ES
     with the rebuilt captures indices, which don't exist until master runs the post-deploy runbook
     below. Verifying the guard against real (not synthetic) sampled documents is runbook step 5,
     not a build-session test.

## Out of scope for this PR (build session never deploys/touches live infra)

The two partial destination indices (`agent-captains-captures-2026-04`,
`agent-captains-captures-2026-05`) carry the bad mapping baked in at creation and must be deleted +
rebuilt from the still-intact legacy source indices after the corrected template is deployed. This
is a live-infra, ask-first deploy action (ES type-change + reindex) — master's job, not build's.
Full runbook goes in the Linear ticket close-out comment (Step 9), not the PR:

1. Deploy the corrected template (`scripts/setup-elasticsearch.sh`, or `put_and_apply_template` for
   `agent-captains-captures-template` specifically) and confirm via
   `GET agent-captains-captures-template` that the field now reads `keyword`.
2. `GET _cat/indices/agent-captains-captures-2026-04,agent-captains-captures-2026-05?v` to confirm
   these are the two partial destinations (small counts) before touching anything.
3. `DELETE` both — source legacy daily indices are untouched, so nothing is lost.
4. `uv run python scripts/migrate_fre1036_monthly_indices.py reindex --family agent-captains-captures --confirm-prod`
   to rebuild both destinations fresh under the corrected mapping.
5. Verify the reindex response carries zero failures and that a real threshold-violation document
   round-trips (query `metrics_summary.threshold_violations` existence on the rebuilt indices).
6. **Live-sample guard verification** (the thing this PR's tests use synthetic data for):
   `python scripts/audit/telemetry_surface_check.py --es-url <es>` and confirm the new
   `sample-mapping` check reports zero `producer-type-mismatch` findings for
   `metrics_summary.threshold_violations` against the rebuilt indices — real documents, not the
   hermetic test's injected one.
7. Resume the halted FRE-1036 captains-captures migration (`plan` → `delta` → eventually `cleanup`)
   from where it left off — the script is idempotent/re-runnable by design.

## Test plan

- `make test-file FILE=tests/scripts/test_telemetry_surface_check.py`
- `make mypy` / `make ruff-check` / `make ruff-format`
- `pre-commit run --all-files`

## Risk tier

Standard — touches an ES index template (schema) and adds new audit tooling. Codex plan-review
required before implementation.

## Codex plan-review (2026-08-02)

Verdict: **approve with changes**. Findings and how each was resolved in this revision:
1. Mapping choice (`keyword` + `ignore_above:1024`) — sound as originally planned, no change.
2. Guard scope — flawed (numeric-only + blanket list-is-a-mismatch would false-fire on legitimate
   list-valued keyword fields like `tools_used`). **Fixed**: type-family rules (numeric/string/
   boolean), applied element-wise, explicit-not-implicit about what's out of scope (date/geo/
   container).
3. Deferring index delete/rebuild to master's post-deploy runbook — sound, matches the repo's
   enforced deploy-approval-gate hook (build worktrees are hard-denied from deploying). No change.
4. Test gaps — pure-function tests don't exercise the guard's wiring, and the ticket's quoted example
   string isn't the producer's literal output. **Fixed**: added an end-to-end mocked-`sample_documents`
   test of `check_sample_document_types` itself; switched the example to the literal `_check_thresholds`
   format string; live-sample verification moved to an explicit runbook step (build sessions can't
   reach live ES).
5. Baseline/test — field count was wrong (six entries, not five) and the regression test only checked
   field names. **Fixed**: corrected count; test now asserts full finding-key equality against the
   trimmed baseline via `load_baseline`, not just field-name membership.
