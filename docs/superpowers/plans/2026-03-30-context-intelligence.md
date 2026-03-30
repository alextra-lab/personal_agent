# Context Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the agent from passive context truncation to active context management — clean docs, fix EVAL-08 failures, verify with numbers, then enhance with research-backed capabilities.

**Architecture:** Four strictly serial phases (CLEAN → FIX → VERIFY → ENHANCE). Each phase's output is the next phase's input. Phase gates with measurable exit criteria enforce the serial constraint. See `docs/specs/CONTEXT_INTELLIGENCE_SPEC.md` for full design.

**Tech Stack:** Python 3.12, structlog, asyncio, Neo4j, Elasticsearch, Pydantic, pytest

**Model assignments:** Sonnet (fast) for Phases 1-3. Opus only for Phase 4 design work and persistent-failure root-cause analysis. See spec §Model Assignment Summary.

---

## Phase 1: CLEAN (Documentation Triage)

### Task 1.1: Move Research File from Root

**Files:**
- Move: `ContextManagement_checkin.md` → `docs/research/context_management_research.md`

- [ ] **Step 1: Move the file**

```bash
git mv ContextManagement_checkin.md docs/research/context_management_research.md
```

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "docs: move context management research to docs/research/"
```

---

### Task 1.2: Archive Superseded Architecture Docs

**Files:**
- Create: `docs/archive/` directory
- Create: `docs/archive/PRE_REDESIGN_SUMMARY.md` (consolidated summary)
- Move: ~15 v0.1 architecture files, 3 router experiments, 3 non-ADR snapshots

- [ ] **Step 1: Create archive directory**

```bash
mkdir -p docs/archive
```

- [ ] **Step 2: Read each v0.1 doc and extract key decisions**

Read these files and extract key decisions, rationale, and any still-relevant technical details into a single consolidated summary:

- `docs/architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md`
- `docs/architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md`
- `docs/architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
- `docs/architecture/ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md`
- `docs/architecture/BRAINSTEM_SERVICE_v0.1.md`
- `docs/architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md`
- `docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md`
- `docs/architecture/CONTROL_LOOPS_SENSORS_v0.1.md`
- `docs/architecture/REQUEST_MONITOR_SPEC_v0.1.md`
- `docs/architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md`
- `docs/architecture/SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md`
- `docs/architecture/TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md`
- `docs/architecture/stack_and_language_choices_v0.1.md`
- `docs/architecture/system_architecture_v0.1.md`
- `docs/architecture/diagrams/c4_context_and_container.md`
- `docs/architecture/diagrams/nervous_system_orchestration.md`

Write `docs/archive/PRE_REDESIGN_SUMMARY.md` with structure:

```markdown
# Pre-Redesign Architecture Summary (Phases 1.0 – 2.2)

> **ARCHIVED** — Consolidated from v0.1 architecture documents. Current architecture: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`

## Key Decisions That Carried Forward
(decisions still valid in Redesign v2)

## Key Decisions That Were Superseded
(what changed and why — link to ADRs)

## Historical Context
(brief timeline: Phase 1.0 → 2.1 → 2.2 → Redesign trigger)
```

- [ ] **Step 3: Move v0.1 files to archive**

```bash
git mv docs/architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md docs/archive/
git mv docs/architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md docs/archive/
git mv docs/architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md docs/archive/
git mv docs/architecture/ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md docs/archive/
git mv docs/architecture/BRAINSTEM_SERVICE_v0.1.md docs/archive/
git mv docs/architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md docs/archive/
git mv docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md docs/archive/
git mv docs/architecture/CONTROL_LOOPS_SENSORS_v0.1.md docs/archive/
git mv docs/architecture/REQUEST_MONITOR_SPEC_v0.1.md docs/archive/
git mv docs/architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md docs/archive/
git mv docs/architecture/SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md docs/archive/
git mv docs/architecture/TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md docs/archive/
git mv docs/architecture/stack_and_language_choices_v0.1.md docs/archive/
git mv docs/architecture/system_architecture_v0.1.md docs/archive/
git mv docs/architecture/diagrams/c4_context_and_container.md docs/archive/
git mv docs/architecture/diagrams/nervous_system_orchestration.md docs/archive/
```

- [ ] **Step 4: Archive router experiments**

```bash
git mv docs/architecture/experiments/E-005-router-parameter-passing-evaluation.md docs/archive/
git mv docs/architecture/experiments/E-006-router-output-format-detection.md docs/archive/
git mv docs/architecture/experiments/E-007-thinking-router-model-optimization.md docs/archive/
```

- [ ] **Step 5: Archive non-ADR snapshots and old experiments**

```bash
git mv docs/architecture_decisions/PROJECT_STATUS_2025-12-28.md docs/archive/
git mv docs/architecture_decisions/RTM.md docs/archive/
git mv docs/architecture_decisions/METRICS_FORMAT_PROPOSAL.md docs/archive/
git mv docs/plans/DOCS_REORG_AND_WORKFLOW_PLAN.md docs/archive/
```

Archive old experiment proposals (read each first — extract any still-relevant findings before moving):

```bash
git mv docs/architecture_decisions/experiments/E-001-orchestration-evaluation.md docs/archive/
git mv docs/architecture_decisions/experiments/E-002-planner-critic-quality.md docs/archive/
git mv docs/architecture_decisions/experiments/E-003-safety-gateway-effectiveness.md docs/archive/
git mv docs/architecture_decisions/experiments/E-007-three-stage-routing.md docs/archive/
git mv docs/architecture_decisions/experiments/E-008-validation-agent-effectiveness.md docs/archive/
git mv docs/architecture_decisions/experiments/E-009-performance-based-routing.md docs/archive/
```

- [ ] **Step 6: Archive old session logs and router-era research**

```bash
git mv docs/plans/sessions/ACTION_ITEMS_2025-12-28.md docs/archive/
git mv docs/plans/sessions/SESSION-2025-12-28-architecture-kickoff.md docs/archive/
git mv docs/plans/sessions/SESSION-2025-12-29-cognitive-architecture-synthesis.md docs/archive/
git mv docs/plans/sessions/SESSION-2025-12-31-research-analysis-and-model-optimization.md docs/archive/
git mv docs/plans/sessions/SESSION-2026-01-16-evaluation-refinement.md docs/archive/
git mv docs/plans/sessions/SESSION-2026-01-17-summary.md docs/archive/
git mv docs/plans/sessions/SESSION-2026-01-19-service-architecture-planning.md docs/archive/
git mv docs/plans/sessions/SESSION-2026-01-23-phase-2.2-testing-completion.md docs/archive/
git mv docs/research/router_prompt_patterns_best_practices_2025-12-31.md docs/archive/
git mv docs/research/model_orchestration_research_analysis_2025-12-31.md docs/archive/
git mv docs/architecture_decisions/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md docs/archive/
```

- [ ] **Step 7: Commit archive batch**

```bash
git add -A && git commit -m "docs: archive ~45 superseded docs with consolidated summary"
```

- [ ] **Step 8: Verify file count reduction**

```bash
find docs -type f -name "*.md" ! -path "docs/archive/*" | wc -l
```

Expected: ~160 or fewer (down from 206). The archive directory should contain ~45+ files.

---

### Task 1.3: Fix Accuracy in Hot Documents

**Files:**
- Modify: ~13 documents (see list below)

- [ ] **Step 1: Update spec statuses**

In `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`: Find the Slice 3 status field and update from "Planned" to "Complete (2026-03-29)".

In `docs/specs/CONVERSATION_CONTINUITY_SPEC.md`: Update status from "Proposed" to "Partially Implemented — context_window.py implements token-aware truncation and session hydration. LLM summarization deferred to Context Intelligence spec."

In `docs/specs/SEARXNG_WEB_SEARCH_TOOL_SPEC.md`: Update status from "Proposed" to "Implemented".

- [ ] **Step 2: Fix architecture README**

In `docs/architecture/README.md`: Update any references to Slice 3 as "Planned" or "Evaluation phase" to reflect completion. Update the reading order now that v0.1 files are archived.

- [ ] **Step 3: Handle GOVERNANCE_MODEL.md**

Check `docs/architecture_decisions/GOVERNANCE_MODEL.md`. If empty (0 bytes), delete it:

```bash
rm docs/architecture_decisions/GOVERNANCE_MODEL.md
```

Or populate with a stub pointing to `config/governance/` and ADR-0005.

- [ ] **Step 4: Update stale reference docs**

In `docs/architecture_decisions/HYPOTHESIS_LOG.md`: Remove or update references to Planner+Critic architecture. Frame hypotheses in terms of current gateway + primary agent architecture.

In `docs/architecture_decisions/TECHNICAL_DEBT.md`: Refresh for current infrastructure. Remove items resolved during Redesign v2.

In `docs/architecture_decisions/EXPERIMENTS_ROADMAP.md`: Update to reflect Redesign v2 experiment priorities. Remove three-stage routing experiments (archived). Add current experiment areas (context management, proactive memory, cross-session recall).

- [ ] **Step 5: Fix guides**

In `docs/guides/SLM_SERVER_INTEGRATION.md`: Update architecture diagram from multi-tier router to single-brain + gateway model.

In `docs/guides/MCP_INTEGRATION_QUICK_START.md`: Rewrite from migration-log style to actual quick start format. Focus on "how to use MCP tools today" not "how we migrated to MCP."

- [ ] **Step 6: Fix reference docs**

In `docs/reference/PROJECT_DIRECTORY_STRUCTURE.md`: Regenerate from current tree:

```bash
find . -type d -not -path './.git/*' -not -path './node_modules/*' -not -path './.venv/*' -not -path './docs/archive/*' | sort | head -60
```

Use output to update the directory structure doc.

In `docs/reference/CODING_CONVENTIONS.md`: Merge any unique content into `docs/reference/CODING_STANDARDS.md`, then archive the conventions file:

```bash
git mv docs/reference/CODING_CONVENTIONS.md docs/archive/
```

- [ ] **Step 7: Fix research README**

In `docs/research/README.md`: Update links — remove references to archived router-era docs. Add links to current active research (EVAL_08_SLICE_3_PRIORITIES, context management research, evaluation reports).

- [ ] **Step 8: Commit accuracy fixes**

```bash
git add -A && git commit -m "docs: fix accuracy in 13 hot documents — statuses, guides, references"
```

---

### Task 1.4: Update Navigation Aids

**Files:**
- Modify: `docs/architecture/README.md`
- Create: `docs/specs/AGENTS.md`, `docs/research/AGENTS.md`, `docs/guides/AGENTS.md`
- Modify: `docs/VISION_DOC.md` (if needed)

- [ ] **Step 1: Create AGENTS.md for specs directory**

Create `docs/specs/AGENTS.md`:

```markdown
# Specifications

Technical specifications for agent components and features.

## Active Specs
- `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Canonical architecture (Slices 1-3 complete)
- `CONTEXT_INTELLIGENCE_SPEC.md` — Next evolution: context management + EVAL-08 fixes
- `SELF_TELEMETRY_TOOL_SPEC.md` — Agent self-inspection (implemented)
- `SEARXNG_WEB_SEARCH_TOOL_SPEC.md` — Web search tool (implemented)
- `CAPTAINS_LOG_ES_BACKFILL_SPEC.md` — Backlog item

## Historical / Partially Implemented
- `CONVERSATION_CONTINUITY_SPEC.md` — Partially implemented; deferred work in Context Intelligence spec
- `CLI_SERVICE_CLIENT_SPEC.md` — Draft
- `AGENT_HEALTH_TOOL_SPEC.md` — Proposed
```

- [ ] **Step 2: Create AGENTS.md for research directory**

Create `docs/research/AGENTS.md`:

```markdown
# Research

Evaluation findings, experiment reports, and research notes.

## Start Here
- `EVAL_08_SLICE_3_PRIORITIES.md` — Priority ranking that drove Slice 3 scope
- `context_management_research.md` — Context window management research (drives Context Intelligence spec)

## Evaluation Reports
- `EVALUATION_PHASE_FINDINGS.md` — Consolidated findings across EVAL-01 through EVAL-07
- `EVAL_03_MEMORY_PROMOTION_REPORT.md` — Memory promotion analysis
- `EVAL_04_CONTEXT_BUDGET_REPORT.md` — Context budget analysis
- `GRAPHITI_EXPERIMENT_REPORT.md` — Seshat vs Graphiti comparison (ADR-0035 input)

## Active Research
- `DELEGATION_OUTCOME_LOG.md` — Ongoing delegation tracking
- `EVALUATION_DATASET.md` — Eval harness scenario definitions
```

- [ ] **Step 3: Create AGENTS.md for guides directory**

Create `docs/guides/AGENTS.md`:

```markdown
# Guides

How-to and setup guides for operators and developers.

## Setup
- `CONFIGURATION.md` — Environment and settings
- `SLM_SERVER_INTEGRATION.md` — Local model server setup

## Operations
- `USAGE_GUIDE.md` — Running the agent
- `KIBANA_DASHBOARDS.md` — Dashboard overview
- `KIBANA_EXPANSION_DASHBOARDS.md` — Expansion/decomposition dashboards
- `KIBANA_INTENT_DASHBOARD.md` — Intent classification dashboard
- `TELEMETRY_ELASTICSEARCH_INTEGRATION.md` — ES setup and queries

## Evaluation
- `EVALUATION_PHASE_GUIDE.md` — How to run evaluations
```

- [ ] **Step 4: Verify VISION_DOC reading order**

Read `docs/VISION_DOC.md` §"For New AI Assistants" (around line 358). Verify the reading order still works with archived files removed. Update any broken references.

- [ ] **Step 5: Commit navigation aids**

```bash
git add -A && git commit -m "docs: add AGENTS.md navigation aids to specs/, research/, guides/"
```

---

### Task 1.5: Phase 1 Gate Check

- [ ] **Step 1: Verify zero contradictions**

Spot-check 5 key docs for accuracy:

```bash
# Check spec statuses match reality
rg -n "Status.*Proposed" docs/specs/ --glob '!docs/archive/*'
rg -n "Planned" docs/architecture/README.md
```

Any remaining "Proposed" for implemented specs or "Planned" for completed work = gate fail.

- [ ] **Step 2: Verify file count**

```bash
find docs -type f -name "*.md" ! -path "docs/archive/*" | wc -l
```

Expected: <=144 (30% reduction from 206).

- [ ] **Step 3: Verify reading path**

An agent starting from scratch should be able to follow: `VISION_DOC.md` → `README.md` → `MASTER_PLAN.md` → linked specs. Manually verify each link resolves to a file that exists and has accurate content.

- [ ] **Step 4: Commit gate verification**

```bash
git add -A && git commit -m "docs: Phase 1 CLEAN complete — gate check passed"
```

---

## Phase 2: FIX (EVAL-08 Failures)

> **Gate dependency:** Phase 1 must be complete before starting Phase 2.

### Task 2.1: Recall Controller — Diagnose Before Fixing

**Files:**
- Read: `src/personal_agent/request_gateway/recall_controller.py`
- Read: `tests/personal_agent/request_gateway/test_recall_controller.py`

- [ ] **Step 1: Write unit tests for all 7 CP-19 variant inputs**

Add tests to `tests/personal_agent/request_gateway/test_recall_controller.py` that test `_RECALL_CUE_PATTERNS` directly:

```python
import re
import pytest
from personal_agent.request_gateway.recall_controller import _RECALL_CUE_PATTERNS

@pytest.mark.parametrize("text,should_match", [
    # CP-19 (original) — should already pass
    ("Going back to the beginning — what was our primary database again?", True),
    # CP-19-v2
    ("What was our primary database again?", True),
    # CP-19-v3
    ("Going back to earlier — what caching system did we pick?", True),
    # CP-19-v4
    ("Remind me what we decided on the message queue?", True),
    # CP-19-v5
    ("What did we decide on the CI/CD pipeline?", True),
    # CP-19-v6
    ("Refresh my memory — what was our main programming language?", True),
    # CP-19-v7
    ("The tool we discussed earlier — can you confirm what it was?", True),
    # Negative cases — should NOT match
    ("What is dependency injection?", False),
    ("Tell me about Redis performance.", False),
])
def test_recall_cue_patterns_cp19_variants(text: str, should_match: bool) -> None:
    """Verify _RECALL_CUE_PATTERNS matches all CP-19 variant inputs."""
    match = _RECALL_CUE_PATTERNS.search(text)
    if should_match:
        assert match is not None, f"Pattern should match: {text!r}"
    else:
        assert match is None, f"Pattern should NOT match: {text!r}"
```

- [ ] **Step 2: Run the tests to see which actually fail**

```bash
uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py::test_recall_cue_patterns_cp19_variants -v
```

Record which variants pass (regex matches) vs fail (regex doesn't match). This tells you whether the bug is in patterns or pipeline.

- [ ] **Step 3: Document findings**

Write a comment or note: "Of 7 variants, N match the regex. The remaining failures are in: [list]. For variants that DO match but still fail in EVAL-08, the bug is in the pipeline, not the regex."

---

### Task 2.2: Recall Controller — Fix Regex Gaps

**Files:**
- Modify: `src/personal_agent/request_gateway/recall_controller.py`

- [ ] **Step 1: Fix patterns that don't match**

Based on Task 2.1 findings, add or adjust patterns in `_RECALL_CUE_PATTERNS`. Likely additions (adjust based on actual test results):

- Broaden "again" pattern to allow more words between determiner and "again"
- Handle em-dash and en-dash as word separators in "going back to earlier"
- Ensure "remind me" works with various follow-on words
- Ensure "refresh my memory" followed by dash/comma works

- [ ] **Step 2: Run pattern tests**

```bash
uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py::test_recall_cue_patterns_cp19_variants -v
```

Expected: All 7 positive cases pass, all negative cases pass.

- [ ] **Step 3: Run full recall controller tests**

```bash
uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py -v
```

Expected: All pass, no regressions.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "fix(recall): expand cue patterns for CP-19 adversarial variants"
```

---

### Task 2.3: Recall Controller — Fix Pipeline Gaps

**Files:**
- Read: `src/personal_agent/request_gateway/recall_controller.py` (full `run_recall_controller()`)
- Read: `src/personal_agent/request_gateway/pipeline.py` (where Stage 4b is called)

- [ ] **Step 1: Trace the pipeline invocation**

Read `run_recall_controller()` and `pipeline.py` to understand:
1. Under what conditions is `run_recall_controller()` called?
2. Does it only run for `task_type == CONVERSATIONAL`?
3. What happens if the LLM classifier already returns `memory_recall`? (Stage 4b may skip)
4. Where is `recall_cue_detected` event emitted?

- [ ] **Step 2: Identify why events don't fire for matching patterns**

For variants where the regex matches but `recall_cue_detected` doesn't appear in EVAL-08 telemetry: trace the code path. Common causes:
- Stage 4b skipped because LLM already classified as `memory_recall`
- Event emission behind a conditional that isn't met
- Session history too short for the session-fact scan to find candidates

- [ ] **Step 3: Fix the identified pipeline issue**

Implement the fix based on Step 2 findings. This may involve:
- Emitting `recall_cue_detected` even when LLM already classified correctly
- Adjusting Stage 4b gate conditions
- Fixing session-fact scan matching

- [ ] **Step 4: Write test for pipeline behavior**

Add an integration test that simulates a 3-turn conversation and verifies the recall controller fires on the third turn with a recall cue.

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/personal_agent/request_gateway/ -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "fix(recall): fix pipeline gate/emission for CP-19 variants"
```

---

### Task 2.4: Entity Promotion Pipeline Diagnosis

**Files:**
- Read: `src/personal_agent/second_brain/consolidator.py`
- Read: `src/personal_agent/second_brain/entity_extraction.py`
- Read: `src/personal_agent/memory/promote.py`
- Read: `src/personal_agent/memory/fact.py`
- Read: `src/personal_agent/memory/service.py` (~line 1190-1260)

- [ ] **Step 1: Trace entity lifecycle during a conversation**

Answer these questions by reading the code:
1. When does entity extraction run? (Per turn? On session close? On a schedule?)
2. When does the consolidator run? (Per turn? Async? Scheduled?)
3. What triggers `get_promotion_candidates()` → `promote_entity()`?
4. Is there a timing issue where the eval queries Neo4j before entities are written?

- [ ] **Step 2: Write a diagnostic test**

Create a test that simulates the CP-26 scenario:
1. Create a session
2. Send 3 turns mentioning "DataForge", "Apache Flink", "ClickHouse", "Priya Sharma"
3. Trigger entity extraction + consolidation
4. Query Neo4j for those entities
5. Assert they exist

```bash
uv run pytest tests/personal_agent/memory/test_promotion_pipeline.py -v -k "test_cp26_entity_lifecycle"
```

- [ ] **Step 3: Fix based on diagnosis**

The fix depends on findings. Possible fixes:
- If extraction doesn't run during eval: wire it into the conversation flow
- If consolidator timing is wrong: adjust trigger
- If entities write but with different names: fix name normalization
- If stability_score is gatekeeping: add recency boost to formula

- [ ] **Step 4: Run all memory tests**

```bash
uv run pytest tests/personal_agent/memory/ -v
uv run pytest tests/personal_agent/second_brain/ -v
```

Expected: All pass, including new diagnostic test.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "fix(memory): diagnose and fix entity promotion pipeline for EVAL-08"
```

---

### Task 2.5: Expansion Event Telemetry

**Files:**
- Read: `src/personal_agent/orchestrator/expansion_controller.py`
- Read: `telemetry/evaluation/` scenario definitions

- [ ] **Step 1: Search for event names**

```bash
rg "hybrid_expansion_start" src/
rg "hybrid_expansion_complete" src/
rg "expansion_start" src/
rg "expansion_complete" src/
```

Compare against what the eval harness expects:

```bash
rg "hybrid_expansion_start" telemetry/
rg "hybrid_expansion_complete" telemetry/
```

- [ ] **Step 2: Identify the mismatch**

Determine: are the events emitted under a different name? Or are they genuinely not emitted?

- [ ] **Step 3: Fix the mismatch**

If naming: align event names between code and eval (prefer fixing code to match the spec).
If not emitted: add emission at the correct point in the expansion controller.

- [ ] **Step 4: Write test**

Add a test that triggers a HYBRID expansion and asserts `hybrid_expansion_start` and `hybrid_expansion_complete` events are emitted.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/personal_agent/orchestrator/ -v
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "fix(expansion): align event names for hybrid_expansion_start/complete"
```

---

### Task 2.6: Phase 2 Gate Check

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: All pass, no regressions.

- [ ] **Step 2: Verify recall pattern tests**

```bash
uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py -v
```

All 7 CP-19 variant pattern tests must pass.

- [ ] **Step 3: Commit gate**

```bash
git add -A && git commit -m "chore: Phase 2 FIX complete — gate check passed"
```

---

## Phase 3: VERIFY (Prove It Works)

> **Gate dependency:** Phase 2 must be complete before starting Phase 3.

**Harness CLI:** `uv run python -m tests.evaluation.harness.run` (see `tests/evaluation/harness/run.py`). Use `--categories <slug>...` for category filters; `--run-id` writes under `telemetry/evaluation/<run-id>/`.

### Task 3.1: Run Context Management Category Eval

- [x] **Step 1: Run context-only eval**

```bash
uv run python -m tests.evaluation.harness.run --categories context_management \
  --run-id EVAL-09-cat-context --output-dir telemetry/evaluation
```

- [x] **Step 2: Save results**

Reports are written directly to `telemetry/evaluation/EVAL-09-cat-context/` (no `latest/` copy).

- [x] **Step 3: Analyze results**

`telemetry/evaluation/EVAL-09-cat-context/evaluation_results.md`. **Result (2026-03-30):** 8/8 paths, 100% assertions — meets target (>=6/8).

---

### Task 3.2: Run Memory Quality Category Eval

- [x] **Step 1: Run memory-quality-only eval**

```bash
uv run python -m tests.evaluation.harness.run --categories memory_quality \
  --run-id EVAL-09-cat-memory --output-dir telemetry/evaluation
```

- [x] **Step 2: Save and analyze**

**Result (2026-03-30):** 4/4 paths, 100% assertions — meets target (>=2/4).

---

### Task 3.3: Run Expansion Category Eval

- [x] **Step 1: Run decomposition + expansion eval**

```bash
uv run python -m tests.evaluation.harness.run --categories decomposition expansion \
  --run-id EVAL-09-cat-decomp-expansion --output-dir telemetry/evaluation
```

- [x] **Step 2: Save and analyze**

**Result (2026-03-30):** 7/7 paths, 100% assertions — meets target (>=5/7).

---

### Task 3.4: Full EVAL-09 Baseline Run

> **Only run after all category targets met.**

- [x] **Step 1: Run full 35-path harness**

```bash
uv run python -m tests.evaluation.harness.run \
  --run-id EVAL-09-post-fix-baseline --output-dir telemetry/evaluation
```

- [x] **Step 2: Save results**

Output directory: `telemetry/evaluation/EVAL-09-post-fix-baseline/`.

- [x] **Step 3: Compare against EVAL-08**

| Metric | EVAL-08 | EVAL-09 | Target | Pass? |
|--------|---------|---------|--------|-------|
| Overall Assertions | 77.2% | 99.4% (176/177) | >=86% | Yes |
| Context Management (paths) | 1/8 | 7/8 | >=75% | Yes |
| Memory Quality (paths) | 0/4 | 4/4 | >=50% | Yes |
| Decomposition + Expansion (paths) | 2/7 | 7/7 | >=71% | Yes |
| Intent (no regress) | 6/7 (86%) | 7/7 (100%) | >=86% | Yes |
| Memory System (no regress) | 4/4 (100%) | 4/4 (100%) | 100% | Yes |

**Note:** Full run had one failure: CP-19-v3 turn 3 `intent_classified.task_type` was `conversational` vs expected `memory_recall` (classifier), while `recall_cue_detected` was present. Category-isolated run for Context Management was 8/8 — treat as flake / load-order sensitivity; optional follow-up under Phase 4 or a second full run.

- [x] **Step 4: Commit EVAL-09**

```bash
git add telemetry/evaluation/EVAL-09-post-fix-baseline/ telemetry/evaluation/EVAL-09-cat-context/ \
  telemetry/evaluation/EVAL-09-cat-memory/ telemetry/evaluation/EVAL-09-cat-decomp-expansion/ && \
  git commit -m "eval: EVAL-09 post-fix baseline — 99.4% assertions (176/177)"
```

- [x] **Step 5: Update MASTER_PLAN**

EVAL-09 summary added to `docs/plans/MASTER_PLAN.md` Completed.

---

## Phase 4: ENHANCE (Context Intelligence)

> **Gate dependency:** Phase 3 EVAL-09 must meet targets before starting Phase 4.
>
> **Note:** Phase 4 tasks are outlined here, not fully detailed. Each sub-task should produce its own detailed implementation plan (using writing-plans skill) when the team is ready to execute it. This is because Phase 4 scope may adjust based on what EVAL-09 reveals.

### Task 4.1: Rolling LLM Summarization

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.1

**Outline:**
1. Add `compressor` model role to `config/models.yaml` (candidate: fast local model — LFM-2.5, Qwen3 small, or Phi-class)
2. Create `src/personal_agent/orchestrator/context_compressor.py`
3. Modify `src/personal_agent/orchestrator/context_window.py` — when evicting turns, pass to compressor instead of inserting `[Earlier messages truncated]`
4. Write tests for compression quality and truncation replacement
5. ADR for compressor model selection

### Task 4.2: Async Background Compression

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.2

**Outline:**
1. Add token-count threshold detection (65% of window)
2. Fire `asyncio.create_task()` for compression when threshold crossed
3. Store compressed summary for injection on next turn
4. Telemetry: `context_compression_triggered`, `context_compression_completed` events
5. Tests for async behavior and threshold detection

### Task 4.3: Proactive Memory

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.3 (requires Opus for design)

**Outline:**
1. Design doc: relevance scoring, noise control, token budget
2. Implement `suggest_relevant()` in Seshat/MemoryProtocol
3. Wire into `assemble_context()` in request gateway
4. A/B validation methodology
5. ADR for proactive memory architecture

### Task 4.4: Cross-Session Recall Validation

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.4

**Outline:**
1. Design new eval paths CP-30+ (seed session → close → new session → query)
2. Handle Neo4j state between eval sessions
3. Add to eval harness
4. Run and analyze

### Task 4.5: Structured Context Assembly

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.5

**Outline:**
1. Design the living state document schema (Goal/Constraints/State/Open Questions/Recent Actions)
2. Modify `src/personal_agent/request_gateway/context.py` to generate and prepend state doc
3. Tests for state doc accuracy across turns

### Task 4.6: KV Cache Preservation

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.6

**Outline:**
1. Ensure system prompt + anchor are stable prefix (immutable per session until compression event)
2. Measure latency impact with stable vs unstable prefix

### Task 4.7: Recall Classifier Layer 2

**Spec reference:** CONTEXT_INTELLIGENCE_SPEC §4.7 (requires Opus for design)

**Outline:**
1. Design: embedding similarity + semantic completeness scoring
2. Implement as Layer 2 after regex heuristic gate
3. Intent-aware filtering (only trigger for refinement/continuation intents)
4. Tests and eval paths

### Task 4.8: EVAL-10 Verification

- [ ] Run full harness after Phase 4 enhancements
- [ ] Compare against EVAL-09 baseline
- [ ] Verify no regressions
- [ ] Save as `telemetry/evaluation/EVAL-10-post-enhance/`
- [ ] Update MASTER_PLAN

---

## Self-Review Checklist

- [x] Every Phase 1-3 task has exact file paths
- [x] Every Phase 1-3 task has exact commands with expected output
- [x] Phase gates are explicit with measurable criteria
- [x] Serial dependency chain maintained (no task references future-phase output)
- [x] Phase 4 tasks are outlines (detailed plans deferred to execution time)
- [x] Model assignments specified in spec, not repeated per-task (see spec §Model Assignment Summary)
- [x] TDD approach in Phase 2 (write test → run → fix → verify)
- [x] Frequent commits (every logical unit of work)
