# FRE-453 — Canonical eval set: 7 representative turn types

> **Ticket:** FRE-453 (Approved, Observability Foundation, Tier-2:Sonnet)
> **Spec:** `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` (FRE-451) — §5 assignment conventions, §7.2 eval-set coverage, §7.3 label-lie, §7.4 thinking measurement
> **ADR:** ADR-0084 §Open decisions §3 (the canonical ~7-turn eval set); ADR-0088 (route-trace ledger = the instrument under test)
> **Pattern precedent:** `scripts/eval/fre481_decomposition_ab/` (dataset.yaml + harness.py + README; running live = master post-deploy action)

## Goal

Ship the M2 canonical eval set + a runner so the route-trace ledger can run all 7 turn
types and produce a structured report against the taxonomy. The eval set is the gate
criterion for the M2 instrument and the validation bed for the spec's §5 *[proposed —
M2 validates]* conventions (single-terminal-event, zero-outcomes-allowed).

## Design decisions

1. **Location & shape:** `scripts/eval/fre453_canonical_evalset/{dataset.yaml,harness.py,README.md}` — mirrors fre481.
2. **Drive path:** live service `/chat` (`POST /chat?message=…&session_id=…&channel=EVAL&profile=…`). `channel=EVAL` suppresses side-effecting tools. Multi-turn context cases send `setup_messages` into the same session first (unscored), then the scored stimulus.
3. **Read path:** direct `RouteTraceLedger` read (`get_by_trace_id`), settings-driven asyncpg — same trust level as fre481's direct ES read. Poll with timeout until the row lands (written at turn end). (The FRE-514 REST surface exists but needs a scoped token; direct read keeps the harness dependency-free. Eval writes to substrate are the *gateway's own* writes — fre481 precedent; running live is a master action.)
4. **Expected values are hypotheses — uniformly non-gating** *(codex finding 1)*: every programmatic comparison is reported as **MATCH/MISMATCH**, never PASS/FAIL, and never affects the exit code. Exit code reflects only instrument health: non-zero iff any case failed to produce a route-trace row or `/chat` errored. (A mismatch can be the finding the set exists to expose — e.g. case 5's expected route disagreement.)
5. **Pedagogical-outcome layer is explicitly scoped to rubric capture** *(codex finding 2)*: the assembler always writes `pedagogical_outcomes=None` (M3 emit sites don't exist — spec §6), so this harness cannot compare actual outcomes programmatically. FRE-453 ships: programmatic orchestration-layer comparison + a **fillable rubric section** per case (expected outcome set with `[ ] confirmed / [ ] not observed` boxes per outcome + the rubric criteria) next to the captured response text. The human-labeled pass over a live run is the M2 gate's second half and is recorded in the run's md report (master post-deploy action). This scoping is stated in the README.
6. **Two mismatch concepts, kept separate** *(codex finding 3)*: the programmatic check is the **structural route-mismatch candidate** (declared `decomposition_strategy` vs actual `orchestration_event` — same semantics as the ledger's `_LABEL_LIE_SQL` heuristic). The taxonomy §7.3 **label-lie** verdict (gateway label hiding pedagogical work like synthesis) is *rubric-derived*: it falls out of the human comparing confirmed outcomes against `gateway_label`, and the report names it that way.
7. **Orchestration-event expectations restricted to classifier-emittable values** *(codex finding 4)*: expected `orchestration_event` ∈ {`primary_handled`, `delegate_called`, `fallback_triggered`} only — the classifier never emits used/discarded (hybrid, FRE-515). Model path is evaluated as two independent row comparisons (`decomposition_strategy`, `model_role`), not a composite assertion; the dataset keeps the ticket's 4-value `model_path` vocabulary as the declared hypothesis and the harness derives the two field comparisons from it.

## Dataset schema (per case)

```yaml
cases:
  - id: trivial_conversational          # snake_case unique id
    title: Trivial conversational
    note: why this case exists / what it probes
    setup_messages: []                   # unscored context turns, same session
    stimulus: "..."                      # the scored turn
    expected:
      model_path: single_primary         # single_primary|single_sub_agent|hybrid|delegate (ticket vocab)
      orchestration_event: primary_handled  # classifier-emittable subset only (see decision 7)
      pedagogical_outcomes: []           # taxonomy §4 vocab (rubric layer); [] tests §5.3 zero-allowed
      task_type: conversational          # optional gateway-label hint (reported, not compared)
      tools_used_nonempty: true          # optional structural check (case 7 only)
    rubric:                              # human-judged criteria
      - "..."
    regression: "what a regression looks like"
```

## The 7 cases (the intellectual core)

| # | id | setup | stimulus (gist) | expected path / event | expected outcomes | key probe |
|---|---|---|---|---|---|---|
| 1 | `trivial_conversational` | — | "Hello! How are you doing today?" | single_primary / primary_handled | `[]` | thinking suppression on cheap turns (§7.4); validates §5.3 zero-allowed |
| 2 | `memory_recall` | 1 turn establishing a fact | "What did I say earlier about …?" | single_primary / primary_handled | `[]` (rubric: continuity) | retrieval preserves continuity; known ADR-0087 failure shape |
| 3 | `opening_ritual` | 1 turn ending with an explicit open thread | "Good morning — let's pick up where we left off." | single_primary / primary_handled | `[open_thread_preserved]` (+ rubric: retrieval-prompt = `recall_practiced` *candidate* — full assignment needs the learner's attempt, spec §4.1; single scored turn can't show it) | active-recall trigger + Socratic framing, not a summary dump |
| 4 | `closing_ritual` | 2 substantive turns (concept + surprise) | "Good place to stop — let's wrap up." | single_primary / primary_handled | `[concept_extracted, open_thread_preserved]` | concept extraction / field-note shape at session end |
| 5 | `cross_thread_synthesis` | 2 turns in different domains, structurally similar | "This reminds me of what we discussed about X — real connection?" | single_primary / primary_handled | `[cross_connection_made, synthesis_performed]` | **the §7.3 label-lie probe**: gateway likely says MEMORY_RECALL/SINGLE while the turn does synthesis |
| 6 | `emotionally_loaded` | — | "I'm confused — I was sure A, but B contradicts it and it's bugging me…" | single_primary / primary_handled | `[misalignment_detected, counterintuitive_finding_marked]` | emotional calibration + misconception catch |
| 7 | `tool_heavy_research` | — | "Investigate how the memory subsystem promotes episodic→semantic; trace the code path, cite files." | hybrid / delegate_called | `[]` (rubric: delegation boundary) | what gets offloaded vs kept; `tools_used` non-empty; flag-state sensitivity documented |

Stimulus topics are self-contained (no reliance on pre-existing prod memory state) so the
set is reproducible on any deployment; case 2/3/4/5 build their own context via
`setup_messages` in-session.

## Harness behaviour

Per case:
1. POST each `setup_message` to `/chat` (first call creates the session; subsequent reuse `session_id`). Setup turns are not scored but their trace_ids are recorded.
2. POST `stimulus` with the same `session_id` → scored `trace_id`; capture response text.
3. Poll `RouteTraceLedger.get_by_trace_id(trace_id)` (1s interval, default 60s timeout).
4. Evaluate programmatic comparisons against the row (all **MATCH/MISMATCH** findings, non-gating):
   - `orchestration_event == expected` (expected ∈ classifier-emittable subset)
   - model path, derived as two independent field comparisons: `single_primary` → `decomposition_strategy=='single'` + `model_role=='primary'`; `single_sub_agent` → `'single'`+`'sub_agent'`; `hybrid` → `decomposition_strategy=='hybrid'`; `delegate` → `'delegate'`
   - structural route-mismatch candidate: declared strategy vs actual event disagree (mirrors ledger `_LABEL_LIE_SQL` semantics) → reported flag (the §7.3 *label-lie* verdict itself is rubric-derived — see decision 6)
   - case-specific structural check where declared: `tools_used_nonempty` (case 7) → MATCH/MISMATCH
5. Collect measurement fields (no pass/fail): `gateway_label`, `task_type`, `complexity`, `thinking_enabled`, `input_tokens`, `output_tokens`, `cost_authoritative_usd`, `latency_total_ms`, `tool_iteration_count`, `tools_used`, `sub_agent_count`, `delegate_result_passed_to_synthesis`.
6. Render:
   - `<run_id>_<profile>.json` — full structured report incl. response texts (rubric material)
   - `<run_id>_<profile>.md` — per-case expected-vs-actual table (MATCH/MISMATCH), thinking/token measurement table (§7.4), fillable rubric section (`[ ]` per expected outcome + per rubric criterion), regression notes, structural route-mismatch flags
   - Output dir: `telemetry/evaluation/fre453-canonical-evalset/` (gitignored; never commit raw runs).

Validity note *(codex finding 7)*: `get_by_trace_id` selects by `trace_id` alone; the ledger
key is `(trace_id, task_id)` with per-topology rows planned (ADR-0088 seam). The harness reads
the **turn-level row** and the README documents that FRE-453 is built against single-row-per-trace;
when per-topology rows land, the read must filter `task_id IS NULL`.

CLI: `--run-id` (required), `--profile {local,cloud}`, `--chat-url` (default `http://localhost:9001/chat`), `--auth-email`, `--case <id>` (run a subset), `--out`, `--row-timeout-s`.

## Implementation steps (TDD)

1. **Failing tests first** — `tests/evaluation/test_fre453_canonical_evalset.py` (no LLM/DB; mirrors `test_skill_routing_analysis.py` import style):
   - dataset loads; exactly 7 cases; ids unique and == the 7 required types (§7.2)
   - every `expected.orchestration_event` ∈ taxonomy §3 vocab (import `OrchestrationEvent` literal values from `personal_agent.observability.route_trace.types`)
   - every `expected.pedagogical_outcomes` ⊆ the 10-outcome §4 vocab (constant in harness)
   - every `expected.model_path` ∈ 4-value enum; every case has non-empty `rubric` + `regression`
   - expectation evaluator unit tests on synthetic `RouteTraceRow`s: event match/mismatch, all 4 model-path mappings (each as its two field comparisons), structural route-mismatch flag both directions, `tools_used_nonempty` check
   - expected `orchestration_event` values restricted to the classifier-emittable subset (dataset validation test)
   - markdown renderer smoke test (contains case id, PASS/FAIL marks, rubric boxes)
   - Run: `make test-file FILE=tests/evaluation/test_fre453_canonical_evalset.py` → fails (module absent)
2. **`dataset.yaml`** — the 7 cases as specified above, fully written out (stimulus + setup turns + rubric + regression text).
3. **`harness.py`** — loader (frozen dataclasses), evaluator (pure functions over `RouteTraceRow`), ledger poll, `/chat` driver, JSON+md renderers, CLI. Google docstrings, structlog with trace_id, `from personal_agent.config import settings`, async I/O, no `Any`.
4. Tests green: same `make test-file …` command.
5. **`README.md`** — what the set is, how to run each profile, that running is a master post-deploy action, how the report feeds the §5 convention validation and FRE-515 (used/discarded rubric).
6. **Quality gates:** `make test` (full) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
7. **PR** from `worktree-build2`, pre-merge checklist only. STOP at PR.

## Acceptance criteria

| When | Item | How verified |
|---|---|---|
| Pre-merge | 7 cases load, vocab-validated against frozen taxonomy | unit tests in CI (`make test`) |
| Pre-merge | Evaluator correct on all 4 model paths + structural route-mismatch both directions | unit tests |
| Pre-merge | Quality gates green | `make test` / `make mypy` / ruff / pre-commit |
| Post-deploy (master) | Harness runs all 7 cases against live stack and emits JSON+md report | `uv run python scripts/eval/fre453_canonical_evalset/harness.py --run-id fre453-smoke --profile local` (then cloud) — Linear comment with report |
| Post-deploy (master) | Every case produced a route-trace row (gate) | harness exit 0 |
| Post-deploy (owner/master) | Human rubric pass over the md report — fills outcome boxes per case (the M2 gate's "both layers" half) | completed rubric section attached to Linear comment |
| Future gate | §5.2/§5.3 conventions confirmed or revised from a labeled run | FRE-515 / M2 review against first labeled report |

## Addendum — owner co-design session (2026-06-07)

Scope evolved with the owner before implementation; the dataset below supersedes the
7-case table above. Final shape — **19 cases, two tiers**:

1. **Tier `canonical` (7)** — the ADR-0084 pedagogical turn types, topics drawn from the
   owner's real learning threads (agent-harness development, cooking, music studies,
   security/CSIRT operations): trivial_conversational, memory_recall (beurre blanc),
   opening_ritual (parallel fifths open thread), closing_ritual (CSIRT dwell-time
   surprise), cross_thread_synthesis (mise en place × IR playbooks — the §7.3 probe),
   emotionally_loaded (borrowed chords misconception), tool_heavy_research (own memory
   subsystem code path).
2. **Tier `coverage` (12)** — realistic multi-tool queries, pedagogically framed, whose
   **union** of expected skills/tools spans the toolbox: self_observability_teaching,
   infra_triage_pedagogy, web_research_teach, data_prediction_reveal, diagram_quiz,
   open_threads_coach, knowledge_graph_gaps, browser_lesson, delegation_handoff (the
   only DELEGATE-path hypothesis), linear_capture, artifact_study_guide (HYBRID
   artifact-decomposition + FRE-506 gate-decision observation), plus the canonical 7's
   incidental coverage.
3. **Per-case expected fields extended:** `skills` (subset of `skills_loaded`),
   `tools_any_of` (tool names or family names), both MATCH/MISMATCH findings.
4. **Union-coverage enforcement test** (self-describing, no hand-maintained mapping):
   all `docs/skills/*.md` skills (minus explicit allowlist) and all native (non-`mcp_`)
   tools in `config/governance/tools.yaml` must be claimed by some case; MCP tools are
   enforced at **family** granularity (browser, linear) via declared families.
   v1 allowlist: `get_location`, `get_library_docs` — flagged in the dataset, not silent.
5. **Backend static-prompt surfaces are OBSERVATIONAL ONLY** (owner: v0.1, never tuned —
   expectations are the wrong instrument): every case records which surfaces fired
   (captain's-log capture, reflection, entity extraction, within-session compression,
   tool-result digest) with output excerpts, best-effort within a bounded ES wait;
   scheduled surfaces (consolidation, insights) go in a post-run sweep section. No
   verdicts; first run = their first exploration baseline.
6. **Posture restated:** every expectation is a hypothesis; the first run is
   baseline-learning, not pass/fail ("we will learn").

## Out of scope

- Auto-assigning pedagogical outcomes (M3).
- used/discarded hybrid refinement (FRE-515).
- Any change to taxonomy membership (ADR-0084 revision only).
- ES per-round token curves (fre481 owns that shape; the row's token sums suffice here).
