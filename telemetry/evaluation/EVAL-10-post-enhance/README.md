# EVAL-10: Post-Enhancement Verification

**Phase:** 4 (ENHANCE) — Context Intelligence
**Baseline:** EVAL-09 (34/35 paths, 176/177 assertions, 99.4%)
**Date:** Pending live rerun

## Purpose

Verify Phase 4 enhancements produce no regression from EVAL-09 baseline and
that new capabilities (rolling summarization, cross-session recall) function
correctly.

## Run Commands

```bash
# Category evals first (fast feedback)
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
    --categories context_management memory_quality cross_session \
    --output-dir telemetry/evaluation/EVAL-10-post-enhance/

# Full 37-path harness
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
    --output-dir telemetry/evaluation/EVAL-10-post-enhance/
```

## Success Criteria

- [ ] No regression from EVAL-09 baseline on existing 35 paths
- [ ] CP-30 (Cross-Session Entity Recall) passes
- [ ] CP-31 (Cross-Session Decision Recall) passes
- [ ] `context_compression_completed` events visible in telemetry
- [ ] `context_compression_used` events visible for long conversations
- [ ] `context_prefix_stable` hashes consistent within sessions

## Resolved Regression Blockers

- [x] FRE-210: CP-30/CP-31 recall intent cues now classify as `memory_recall`.
- [x] FRE-211: Harness post-path assertion coroutine is awaited in both single and multi-session flows.
- [x] FRE-212: Responsiveness probe no longer sends invalid `session_id`; `--skip-responsiveness-probe` is no longer required as a workaround.

## New Paths (Phase 4)

| Path | Name | Category |
|------|------|----------|
| CP-30 | Cross-Session Entity Recall | Cross-Session Recall |
| CP-31 | Cross-Session Decision Recall | Cross-Session Recall |

## Comparison Points vs EVAL-09

| Metric | EVAL-09 | EVAL-10 Target |
|--------|---------|----------------|
| Total Paths | 35 | 37 |
| Assertion Pass Rate | 99.4% | >= 99.4% |
| New Category (Cross-Session) | N/A | 2/2 pass |

## Phase 4 Telemetry Events to Verify

- `context_compression_triggered` — threshold crossed, task created
- `context_compression_completed` — summary generated
- `context_compression_used` — summary injected into context window
- `context_compression_failed` — fallback to static marker (should be rare)
- `context_prefix_stable` — KV cache prefix hash logged
- `state_document_built` — structured state doc generated
