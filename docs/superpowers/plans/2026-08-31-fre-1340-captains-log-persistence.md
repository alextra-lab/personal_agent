# FRE-1340 — Captain's Log billing vs. persistence

## Diagnosis (AC-1)

Investigated live production (VPS containers + Postgres + Elasticsearch), not just code reading:

- `docker/postgres/init.sql` provisions `captains_log_captures` / `captains_log_reflections`
  tables. Exhaustive grep across `src/` shows **no INSERT ever targets them** — the only
  references are retention/archival housekeeping (`telemetry/lifecycle.py`,
  `telemetry/lifecycle_manager.py`, `brainstem/scheduler.py`'s daily archive/purge jobs), which
  manage lifecycle for a store nothing has ever written to. These are dead schema from an
  earlier design phase, superseded by a disk+ES architecture (`captains_log/capture.py`,
  `captains_log/manager.py`, Phase 2.2/2.3 comments, FRE-1036 ES consolidation) that was never
  cleaned up.
- The real, working substrate: `CaptainLogManager.save_entry()` writes JSON to disk
  (`telemetry/captains_log/*.json`, Docker volume `seshat_captains_log_cloud` in prod) and
  schedules an Elasticsearch index write (`agent-captains-reflections-YYYY-MM`); `write_capture()`
  does the same for `agent-captains-captures-YYYY-MM`.
- **Confirmed live**: all three `api_costs` rows the ticket cites (`purpose=captains_log`,
  trace_ids `d8ebb49d…`, `bfd3b47f…`, `811aedc5…`) have matching, populated reflection JSON files
  in the running `cloud-sim-seshat-gateway` container, timestamped seconds after the billed call.
  `agent-captains-reflections-2026-08` holds 645 docs; `agent-captains-captures-2026-08` holds
  15,292. The call fires, the write succeeds, the data is durable — master's diagnostic query
  just checked a store the code has never targeted.

**Conclusion: the call and the write both succeed. Nothing is broken in the sense the ticket
assumed.** AC-2 and AC-3 are therefore already true today, evidenced by the production rows
above — no code change closes them, because there is nothing to fix on that path.

## The one real defect (AC-4)

`reflection.py::generate_reflection_entry`'s DSPy path receives `(entry, missing_skill_names)`
from `generate_reflection_dspy`. `missing_skill_names` is used only to fire a
fire-and-forget `log.warning("missing_skill_requested", ...)` — it is never attached to `entry`.
Since `entry` is what `CaptainLogManager.write_entry()` persists to disk/ES, the missing-skill
signal never survives into the durable, reachable reflection record; it exists only as a
transient log line, dependent on `InsightsEngine.detect_missing_skill_patterns`'s clustering job
having already run before the log rotates out of retention. Verified live: `docker logs` on the
eval-stack gateway containers show a steady stream of real `missing_skill_requested` warnings
(`logs-checker`, `web-summarizer`, `legal-source-validator`, …) that never appear in any
persisted `CaptainLogEntry` on disk, because the model has nowhere to put them.

## Fix

1. `src/personal_agent/captains_log/models.py` — add one field to `CaptainLogEntry`, after
   `eval_mode`:
   ```python
   missing_skill_names: list[str] = Field(
       default_factory=list,
       description=(
           "Skills requested by name during this turn that don't exist in the skill "
           "library (FRE-328/FRE-1321 gap-recognition signal). Persisted here so it "
           "survives even when downstream clustering/aggregation hasn't run yet."
       ),
   )
   ```
2. `src/personal_agent/captains_log/reflection.py` — in `generate_reflection_entry`'s DSPy
   success branch, after the existing `emit_missing_skill_warnings(...)` call and before
   `return entry`, add:
   ```python
   entry.missing_skill_names = missing_skill_names
   ```
   (placed alongside the existing `entry.eval_mode = eval_mode` line). No change needed in
   `manager.py` — `save_entry()`/`_normalize_reflection_doc_for_es()` both serialize via
   `entry.model_dump(...)`, so the new field is written to disk and indexed to ES automatically.
3. No schema change, no Postgres migration. Dropping the dead `captains_log_captures`/
   `captains_log_reflections` tables (and the archival code that manages them) is a genuinely
   separate, riskier piece of cleanup — filed as a follow-up ticket rather than folded in here,
   since it needs a live-DB migration and touches scheduler/lifecycle code unrelated to this
   ticket's ACs.

## Tests (TDD)

- `tests/test_captains_log/test_reflection_missing_skills.py` — add a test that patches
  `generate_reflection_dspy` (same pattern as `test_reflection_dspy_gated.py`'s `_reflection_env`)
  to return `(entry, ["citation-validator", "compliance-checker"])`, calls
  `generate_reflection_entry`, and asserts the **returned entry's**
  `missing_skill_names == ["citation-validator", "compliance-checker"]` — the outcome the AC
  cares about, not the wiring.
- A default-empty-list regression test: DSPy returns `(entry, [])` → returned entry's
  `missing_skill_names == []`.

## AC disposition

- **AC-1**: satisfied by the diagnosis above (call succeeds, write succeeds, wrong store was
  queried) — documented in the PR/handoff with the evidence (file paths, ES doc counts, matched
  trace_ids).
- **AC-2**: satisfied by existing production evidence (the three matched trace_id → JSON file
  pairs) — no code change required.
- **AC-3**: already reconciled — rows already appear for the billed calls, just not in Postgres.
- **AC-4**: closed by the `missing_skill_names` field + the one-line assignment above, with a
  test proving the outcome.

## Follow-up ticket (not folded in)

File a Backlog ticket for dropping the dead `captains_log_captures`/`captains_log_reflections`
Postgres tables and their now-pointless archive/purge branches in
`brainstem/scheduler.py`/`telemetry/lifecycle_manager.py` — a live-DB migration, out of this
ticket's scope.
