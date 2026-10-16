# FRE-1612 — Grafana panels for the self-improvement funnel

Backing design: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` (Dashboards section), ADR-0105 D6
(funnel states), ADR-0129 D6 (Grafana is the only dashboard UI). Tier: Standard (touches `src/` logic and
the ES templates). Diff class: self-serve (no production write path change beyond additive telemetry).

## Telemetry (code, TDD)

| # | Step | Files | Test |
|---|------|-------|------|
| 1 | Reflection entry carries `had_errors` and `telemetry_available` (`bool \| None`, `None` = unknown). Set in all three return paths of `generate_reflection_entry`. `had_errors` is `None` when `failure_path_reflection_enabled` is false. | `captains_log/models.py`, `captains_log/reflection.py` | `tests/test_captains_log/test_fre_1612_reflection_health.py` |
| 2 | `PromotionPipeline.run()` emits one `promotion_run` funnel event per non-dry run: `eligible`, `filed`, `linked`, `open_pipeline_count`, `cap`. Counters live in a per-run object that the exit paths fill. | `captains_log/promotion.py` | `tests/test_captains_log/test_fre_1612_promotion_run_event.py` |
| 3 | `_stamp_reflection_linkage` also stamps `filed_at`, for a fresh create only. A link to an existing ticket stamps none, because the filing time is not the linking time. | `captains_log/promotion.py` | same file as step 2 |
| 4 | `_stamp_ticket_outcome` stamps `ticket_outcome_at` and, when `filed_at` exists, `hours_to_outcome`. | `brainstem/jobs/outcome_ingestion.py` | `tests/test_brainstem/test_fre_1612_outcome_latency.py` (script run against the test ES stack on :9201) |
| 5 | Index templates declare the new fields. | `docker/elasticsearch/captains-funnel-events-index-template.json`, `captains-reflections-index-template.json`, `tests/scripts/test_es_templates.py` | `make test-file FILE=tests/scripts/test_es_templates.py` |
| 6 | Two datasources: `agent-captains-funnel-events-*`, and the reflections index on `ticket_outcome_at` (an outcome that lands today on an old proposal sits outside today's range on the `timestamp` axis). | `config/grafana/provisioning/datasources/datasources.yaml` | provisioning loads on the authoring instance |

Known limit, stated in the panel text: a repeated proposal merges into the existing entry
(`CaptainLogManager._merge_into_existing`). It creates no new reflection document, so the health share
counts documents, not turns.

## Codex plan review (2026-10-16), what changed

Fixed: outcome panels use their own datasource on `ticket_outcome_at`. `filed_at` is stamped for fresh creates only. A missed stamp (zero documents updated) logs a warning. The funnel and health panels state their limits in their descriptions: `eligible` counts attempts, a merged proposal adds no health document, a quiet run reads no open count. The deploy mapping step is part of `scripts/setup-elasticsearch.sh` (`apply_month_index_mapping`). Not built: a retry for a failed ES stamp. The stamps stay best-effort, as before.

## Dashboard (UI-built, `create-visualization` skill)

Five panels on `config/grafana/dashboards/self_improvement_funnel.json`, built on an ephemeral Editor
instance from this worktree's provisioning. The missing-skill panel stays. The old reflection-count panel
and the throttle table are replaced by the funnel panel and the cap panel. Gate 0 must print nothing for
this file.

## Docs

`docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` Dashboards section lists the panels that exist.
`docker/elasticsearch` template `_meta` text names `promotion_run`.

## Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Then the Gate 0 `jq` script from the skill. Then self-review on `git diff origin/main...HEAD`.

## Deploy notes for master (handoff)

* A template PUT does not map the existing monthly index (no write alias). `dynamic: false` on the funnel
  index means a new field is stored but not searchable. `scripts/setup-elasticsearch.sh` now PUTs the
  mapping onto the current month index. Run it before the first `promotion_run` event: an event written
  earlier stays unsearchable.
* A new datasource loads at Grafana start. Restart Grafana.
* AC-1 and AC-2 wait for the owner's filing decision on FRE-1611 (no promotion run while the pipeline
  flag is false).
