# FRE-453 — Canonical eval set (18 turn types)

The M2 canonical eval set for the route-trace instrument (ADR-0084 §Open decisions §3,
ADR-0088, `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md`): 18 realistic turns, run through the
live stack, scored against the route-trace ledger, and reported against the result-type
taxonomy.

## What it is

- **Tier `canonical` (7)** — the required pedagogical turn types: trivial conversational,
  memory recall, opening ritual, closing ritual, cross-thread synthesis (the §7.3
  label-lie probe), emotionally loaded learning, tool-heavy research. Topics are the
  owner's real learning threads (agent-harness development, cooking, music studies,
  CSIRT operations).
- **Tier `coverage` (11)** — realistic multi-tool queries, pedagogically framed, whose
  **union** of expected skills/tools spans the toolbox. Includes the set's only
  DELEGATE-path hypothesis (`delegation_handoff`) and the artifact-decomposition probe
  (`artifact_study_guide`).

**Every expectation is a hypothesis** (spec §5 "[proposed — M2 validates]"). The harness
reports MATCH/MISMATCH as findings; nothing gates on them. Exit code is instrument
health only: non-zero iff a case produced no route-trace row or `/chat` errored. The
first run is the behavioural baseline — we run it to *learn*, not to pass.

## Scope notes

- **Pedagogical outcomes are rubric capture.** The assembler writes
  `pedagogical_outcomes=None` until the M3 layer emits substrate writes (spec §6), so
  actual outcomes cannot be compared programmatically. The report renders each case's
  expected outcome set + rubric criteria as a fillable checklist next to the captured
  response text; the **human pass over a live run is the M2 gate's second half**.
- **Two mismatch concepts.** The programmatic flag is the *structural route-mismatch
  candidate* (declared strategy vs actual orchestration event — the ledger's
  `_LABEL_LIE_SQL` heuristic). The taxonomy **§7.3 label-lie** verdict (gateway label
  hiding pedagogical work) is rubric-derived during the human pass.
- **Backend static-prompt surfaces are observational only.** Captain's-log capture,
  reflection, entity extraction, within-session compression, and tool-result digests are
  v0.1 prompts that have never been tuned — the harness records fired / not-fired
  (bounded wait) with excerpts, plus a post-run sweep for scheduled surfaces
  (consolidation, insights). No expectations until a tuning pass defines "good".
- **Validity window.** The ledger key is `(trace_id, task_id)`; this harness reads the
  turn-level row by `trace_id` alone and is valid while route traces are
  single-row-per-trace. When ADR-0088 per-topology rows land, the read must filter
  `task_id IS NULL`.

## Union coverage enforcement (self-describing)

`tests/evaluation/test_fre453_canonical_evalset.py` diffs the dataset's claimed
skills/tools against `docs/skills/*.md` and `config/governance/tools.yaml` at test time:

- every skill must be claimed by some case, or appear in the dataset's explicit
  `coverage.allowlist_skills`;
- every **native** (non-`mcp_`) tool likewise (`coverage.allowlist_tools`);
- MCP tools are enforced at **family** granularity (`coverage.tool_families`, e.g.
  `browser:` → `mcp_browser_`) — a real browser task exercises many micro-tools in one
  turn, so per-method cases are meaningless.

Adding a new skill or native tool without an eval case fails the suite — the coverage
gap surfaces itself. v1 exemptions: `get_location`, `get_library_docs`,
`expand_tool_result`.

## Running it (master post-deploy action)

Running drives the **live** stack (real LLM turns, real substrate writes by the gateway
itself) — fre481 precedent: the harness is the build deliverable; running it is a master
action.

```bash
# all 18 cases, local backend
uv run python scripts/eval/fre453_canonical_evalset/harness.py \
    --run-id fre453-baseline-01 --profile local \
    --auth-email <loopback-eval-email>

# one case, cloud backend
uv run python scripts/eval/fre453_canonical_evalset/harness.py \
    --run-id fre453-smoke --profile cloud --case trivial_conversational \
    --auth-email <loopback-eval-email>
```

Notes:

- `channel=EVAL` is always sent — side-effecting tools (e.g. `create_linear_issue`) are
  suppressed by the service; the `linear_capture` case tests *routing*, not the write.
- Multi-turn cases send their `setup_messages` into the same session first (unscored;
  their trace_ids are recorded in the JSON).
- Output lands in `telemetry/evaluation/fre453-canonical-evalset/<run>_<profile>.{json,md}`
  (gitignored — raw runs are never committed; curated summaries go in Linear/docs).
- The md report contains the fillable rubric checklist; the completed checklist is
  attached to the Linear ticket as the human-labeled half of the M2 gate.

## Unit tests (CI — no LLM, no DB)

```bash
make test-file FILE=tests/evaluation/test_fre453_canonical_evalset.py
```

## Follow-ups this feeds

- **FRE-515** — refine `delegate_called` → used/discarded with the hybrid rubric on
  this set.
- Spec §5.2/§5.3 proposed conventions (single-terminal-event; zero-outcomes-allowed)
  are confirmed or revised from the first labeled run.
