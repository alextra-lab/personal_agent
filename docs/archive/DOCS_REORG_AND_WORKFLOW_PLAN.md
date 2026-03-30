# Docs Reorganization & Agent Workflow Plan

**Date**: 2026-03-09
**Status**: COMPLETE (executed 2026-03-09)
**Scope**: Documentation restructure, cursor hooks, cursor rules, Linear workflow

---

## Objective

Reorganize documentation into clear categories, establish a living Master Plan,
add verification hooks, and create cursor rules for the implementer/reviewer/planner
agent workflow. Make the result reusable across all Cursor projects.

---

## Step 1: Create new docs directories and move files

Create `docs/reference/`, `docs/guides/`, `docs/specs/`.
Move every file from `docs/` root into its proper home.

| File | Destination | Notes |
|------|-------------|-------|
| `CODING_STANDARDS.md` | `reference/` | |
| `CODING_CONVENTIONS.md` | `reference/` | Audit overlap with above later |
| `DATA_LIFECYCLE.md` | `reference/` | |
| `DEPENDENCY_SECURITY.md` | `reference/` | |
| `ENTITY_EXTRACTION_MODELS.md` | `reference/` | |
| `PROJECT_DIRECTORY_STRUCTURE.md` | `reference/` | |
| `ROOT_LEVEL_POLICY.md` | `reference/` | |
| `AGENT_MD_STRATEGY.md` | `reference/` | |
| `PR_REVIEW_RUBRIC.md` | `reference/` | |
| `VALIDATION_CHECKLIST.md` | `reference/` | |
| `VISION_DOC.md` | stays at `docs/` | Foundational, top-level |
| `USAGE_GUIDE.md` | `guides/` | |
| `CONFIGURATION.md` | `guides/` | |
| `SLM_SERVER_INTEGRATION.md` | `guides/` | |
| `GPU_METRICS_SETUP.md` | `guides/` | |
| `GPU_METRICS_SECURITY.md` | `guides/` | |
| `MACMON_GPU_METRICS.md` | `guides/` | |
| `KIBANA_DASHBOARDS.md` | `guides/` | |
| `METRICS_STORAGE_GUIDE.md` | `guides/` | |
| `TELEMETRY_ELASTICSEARCH_INTEGRATION.md` | `guides/` | |
| `NOTES.md` | **delete** | Empty file |
| `CHANGELOG_CAPTAIN_LOG_NAMING.md` | `plans/completed/` | Stale changelog note |
| `README.md` | stays, **rewrite** | Update index for new structure |

**Acceptance**: Every `docs/*.md` is either `VISION_DOC.md`, `README.md`, or moved.
`docs/` root has only those two files plus subdirectories.

---

## Step 2: Clean up `docs/plans/` — separate specs, archive completed

| File | Destination | Notes |
|------|-------------|-------|
| `CAPTAINS_LOG_ES_BACKFILL_SPEC.md` | `docs/specs/` | Spec, not a plan |
| `CLI_SERVICE_CLIENT_SPEC.md` | `docs/specs/` | Spec |
| `CONVERSATION_CONTINUITY_SPEC.md` | `docs/specs/` | Spec |
| `MCP_GOVERNANCE_DISCOVERY_SPEC.md` | `docs/specs/` | Spec |
| `TRACEABILITY_AND_PERFORMANCE_SPEC.md` | `docs/specs/` | Spec |
| `SYSTEM_HEALTH_MONITORING_IMPLEMENTATION_PLAN.md` | `docs/specs/` | Rename to `*_SPEC.md` |
| `IMPLEMENTATION_QUICK_REFERENCE.md` | `docs/guides/` | Quick-ref guide |
| `MCP_INTEGRATION_QUICK_START.md` | `docs/guides/` | Quick-start guide |
| `DAY_11.5_ROUTING_IMPLEMENTATION_SUMMARY.md` | `plans/completed/` | Historical |
| `MCP_GATEWAY_COMPLETION_SUMMARY.md` | `plans/completed/` | Historical |
| `MCP_GATEWAY_IMPLEMENTATION_PLAN_v2.md` | `plans/completed/` | Completed plan |
| `PHASE_2.2_FINAL_SUMMARY.md` | `plans/completed/` | Historical |
| `PHASE_2.2_STATUS.md` | `plans/completed/` | Historical |
| `PRESCRIPTIVE_SPECS_SUMMARY.md` | `plans/completed/` | Historical meta-summary |
| `router_refactor_analysis_and_plan.md` | `plans/completed/` | Completed plan |
| `router_routing_logic_implementation_plan.md` | `plans/completed/` | Completed plan |
| `PROJECT_PLAN_v0.1.md` | `plans/completed/` | Superseded by Master Plan |
| `IMPLEMENTATION_ROADMAP.md` | `plans/completed/` | Superseded by Master Plan |

**Remains in `plans/`**: `MASTER_PLAN.md` (new), `DEV_TRACKER.md`, `PHASE_2.3_PLAN.md`,
`VELOCITY_TRACKING.md`, `AGENTS.md`, `README.md` (update), `sessions/`, `completed/`.

**Acceptance**: `plans/` contains only active plans, tracking docs, and archived history.
No specs, no guides, no completed summaries loose in the directory.

---

## Step 3: Create `MASTER_PLAN.md`

A short (<100 lines) living document replacing `IMPLEMENTATION_ROADMAP.md` and
`PROJECT_PLAN_v0.1.md`. Structure:

- Current Focus (links to Linear issues + spec files)
- Upcoming (approved, not started)
- Backlog (needs approval)
- Completed (FIFO, details link to `completed/`)

Content pulled from the current `IMPLEMENTATION_ROADMAP.md` and `DEV_TRACKER.md`.

**Acceptance**: `MASTER_PLAN.md` exists, is <100 lines, links to Linear and specs.

---

## Step 4: Update `docs/README.md`

Rewrite the documentation index to reflect the new structure:
`reference/`, `guides/`, `specs/`, `plans/`, `architecture/`, `architecture_decisions/`, `research/`.

**Acceptance**: Every subdirectory and key file is linked from the index.

---

## Step 5: Update `docs/plans/README.md`

Simplify to reflect that plans/ now contains only active plans, tracking, and sessions.
Remove references to specs and guides that have moved.

**Acceptance**: README matches actual directory contents.

---

## Step 6: Create `.cursor/hooks.json` and hook scripts

Create:
- `.cursor/hooks.json` with `afterFileEdit` (Python lint check) and `stop` (test verification)
- `.cursor/hooks/check-python.sh` — runs `python3 -m py_compile` on edited `.py` files
- `.cursor/hooks/verify-on-stop.sh` — runs pytest on stop, returns followup if tests fail

**Acceptance**: Hooks load in Cursor (check via Settings > Hooks tab). Lint hook catches
a bad `.py` file. Stop hook runs tests.

---

## Step 7: Update cursor rules

| Rule | Change |
|------|--------|
| `file-organization.mdc` | Add `reference/`, `guides/`, `specs/` to placement guide |
| `session-orientation.mdc` | Point to `MASTER_PLAN.md` instead of `IMPLEMENTATION_ROADMAP.md` |
| `linear-implement-gate.mdc` | Add: label new issues with project name ("Personal Agent") |
| New: `agent-review.mdc` | Review agent workflow: check Done issues, validate, label Review OK/Needs Work |
| New: `agent-planning.mdc` | Planning agent workflow: maintain MASTER_PLAN.md, create issues from specs |

**Acceptance**: All rules pass a manual read-through for consistency.

---

## Step 8: Update `docs/plans/AGENTS.md`

Update the plans AGENTS.md to reflect the new directory structure and workflow.

**Acceptance**: AGENTS.md matches actual directory contents and references correct paths.

---

## Execution Order

```
Step 1  ──►  Step 2  ──►  Step 3  ──►  Step 4 + Step 5 (parallel)
                                              │
                                              ▼
                                        Step 6  ──►  Step 7  ──►  Step 8
```

Steps 1-5 are file reorganization (low risk, easily reversible with git).
Steps 6-7 are new infrastructure (higher value, needs testing).
Step 8 is cleanup.

---

## Out of Scope (future work)

- Linear label/status creation (needs Linear admin, separate task)
- Extracting global skills for reuse across projects
- Activating continual-learning skill
- Merging `CODING_CONVENTIONS.md` + `CODING_STANDARDS.md`
- Writing catch-up session log for Jan 23 - Mar 9 gap
