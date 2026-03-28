# Delegation Outcome Log

**Purpose:** Track structured outcomes from delegating tasks to external agents (primarily Claude Code)
via Stage B delegation packages. Feeds into EVAL-07 findings synthesis.

**Format reference:** `src/personal_agent/request_gateway/delegation_types.py` — `DelegationOutcome`

---

## Log Format

Each entry uses this structure:

```
### DEL-NNN — <short task title>

| Field               | Value |
|---------------------|-------|
| task_id             | DEL-NNN |
| target_agent        | claude-code / codex / other |
| date                | YYYY-MM-DD |
| context_sufficient  | yes / partial / no |
| rounds_needed       | N |
| success             | yes / no |
| duration_minutes    | N |
| user_satisfaction   | 1–5 |

**What worked:**
...

**What was missing:**
...

**Artifacts produced:**
- file/output list

**Notes:**
...
```

---

## Entries

---

### DEL-001 — Wire episodic→semantic promotion pipeline (FRE-148 / EVAL-03)

| Field               | Value |
|---------------------|-------|
| task_id             | DEL-001 |
| target_agent        | claude-code |
| date                | 2026-03-26 |
| context_sufficient  | partial |
| rounds_needed       | 2 |
| success             | yes |
| duration_minutes    | 45 |
| user_satisfaction   | 4 |

**What worked:**
The delegation package's `relevant_files` list was accurate — `memory/promote.py`, `brainstem/consolidation.py`, and `tests/personal_agent/memory/` were exactly the right entry points. The `conventions` entries (use `structlog`, no bare `except`, Google docstrings) were followed without prompting. The acceptance criteria checklist drove the implementation cleanly.

**What was missing:**
The package didn't include the `captains_log` module path, which was needed to understand the event structure for promotion telemetry. Claude Code had to discover it independently, adding a round of exploration before implementing the `promotion_completed` log event. Also missing: the specific Elasticsearch index name (`agent-logs-*`) where promotion events needed to be verified.

**Artifacts produced:**
- `src/personal_agent/memory/promote.py` (implemented)
- `tests/personal_agent/memory/test_promote.py`
- `docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md`

**Notes:**
The `known_pitfalls` field in the package should have included: "promotion pipeline requires Neo4j to be running; mock Neo4j in unit tests via the MemoryProtocol interface, not the driver directly." This caused one test failure round.

---

### DEL-002 — Context budget behavior review + harness evaluation (FRE-149 / EVAL-04)

| Field               | Value |
|---------------------|-------|
| task_id             | DEL-002 |
| target_agent        | claude-code |
| date                | 2026-03-28 |
| context_sufficient  | yes |
| rounds_needed       | 1 |
| success             | yes |
| duration_minutes    | 35 |
| user_satisfaction   | 5 |

**What worked:**
This was the strongest delegation yet. The package's task description was specific: "Run the 12-turn stress test, extract `context_budget_applied` events from ES, and compare against CP-19/CP-20/CP-28 assertions." The `memory_excerpt` included the EVAL-03 findings, which gave context on the ES field naming convention (`event_type` vs `event`). The acceptance criteria exactly matched the deliverables needed for EVAL-07.

**What was missing:**
Nothing critical. The package could have included the Kibana index name explicitly rather than relying on Claude Code to find it in the codebase. Minor: the ES query for `context_budget_applied` events wasn't included — Claude Code had to construct it from reading `budget.py`.

**Artifacts produced:**
- `scripts/eval_04_context_budget.py`
- `telemetry/evaluation/eval-04-context-budget/results.json`
- `telemetry/evaluation/eval-04-context-budget/harness_results.md`
- `docs/research/EVAL_04_CONTEXT_BUDGET_REPORT.md`

**Notes:**
The pattern of "give Claude Code a script to run + expected output format" works extremely well for evaluation tasks. The delegation was self-contained: run script, read ES, write report. No back-and-forth needed. This is the template to follow for remaining EVAL tasks.

---

### DEL-003 — CP-05 delegation intent path (ad-hoc evaluation, not an EVAL ticket)

| Field               | Value |
|---------------------|-------|
| task_id             | DEL-003 |
| target_agent        | claude-code |
| date                | 2026-03-25 |
| context_sufficient  | partial |
| rounds_needed       | 3 |
| success             | yes |
| duration_minutes    | 25 |
| user_satisfaction   | 3 |

**What worked:**
The task description (write a JSON config parser with schema validation) was unambiguous. Claude Code produced a working implementation with correct error message structure and edge case handling (circular references, missing keys, deep nesting). The delegation correctly identified `target_agent = "claude-code"` and complexity = COMPLEX.

**What was missing:**
The context package had `service_path = "src/personal_agent/"` but no guidance on where to place the new module. Claude Code created it at the top level of the src tree rather than as a utility in the right submodule. Required a correction round to move it. Also: no `test_patterns` was included, so the test file didn't follow the project's pytest + `conftest.py` fixture pattern — tests were self-contained but didn't use shared fixtures.

**Artifacts produced:**
- New parser module (moved to correct location in second round)
- Unit tests covering the three edge cases from CP-05

**Notes:**
File placement is consistently missing from delegation packages. Adding `"place new modules under src/personal_agent/<relevant_submodule>/"` to the `conventions` field in every package would eliminate this class of correction. Consider adding this to the `compose_delegation_package()` defaults in `delegation.py`.

---

## Patterns and Findings

*(Updated as log grows — feeds into EVAL-07 synthesis)*

| Pattern | Frequency | Implication |
|---------|-----------|-------------|
| Missing module placement guidance | 2/3 delegations | Add default convention: file placement in `src/personal_agent/` |
| Missing ES index/field names for verification | 2/3 delegations | Include `agent-logs-*` index and key event names in all evaluation delegations |
| Evaluation delegations (run script → report) work in 1 round | 1/1 | This delegation pattern is highly efficient; template it |
| `known_pitfalls` field underused | 2/3 delegations | Pitfalls from past delegations should auto-populate from this log |
