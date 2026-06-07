# FRE-515 — Refine `delegate_called` → used/discarded (hybrid rubric on the eval set)

> **Ticket:** [FRE-515](https://linear.app/frenchforest/issue/FRE-515) (Approved, Observability Foundation)
> **Spec:** `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` §3.3 / §3.4 / §5.2 / §6
> **Depends on:** FRE-452 (classifier + structural signals, shipped) · FRE-453 (eval set, shipped; baseline `fre453-baseline-02` landed 2026-06-07)
> **Date:** 2026-06-07 · build session (`worktree-build2`)

## Design decisions (the ticket's open questions, answered)

### D1 — Write-time vs post-hoc: **post-hoc rubric annotation**

The orchestrator has **no incorporate/reject decision point**: `executor.py:1759` injects *all*
sub-agent summaries into a single synthesis message unconditionally
(`expansion_controller._build_synthesis_context` includes failures too), and the primary's
"incorporation" is a property of the generated text, not of control flow. Any write-time promotion
would therefore be guessing — exactly what the ticket forbids ("add a harness incorporate/reject
decision rather than guessing"). Adding such a decision point is an agent-behavior change, out of
scope for an observability ticket.

**Consequence:** `classify_orchestration_event` keeps returning the `delegate_called` floor.
`delegate_result_used` / `delegate_result_discarded` are applied **post-hoc by rubric** during the
eval-set human pass, recorded in the run report + Linear — not written back to the ledger row
(write-back is a follow-up if SQL queryability of rubric verdicts ever matters; see Follow-ups).

### D2 — Cheap structural signals: two, both candidate-grade

1. **`reply_overlap`** (write-time, per sub, stored in the `sub_agents` JSONB — no schema change):
   containment ratio of the sub-agent summary's distinct content tokens (lowercase alnum, len ≥ 4)
   in the final reply: `|tokens(summary) ∩ tokens(reply)| / |tokens(summary)|`, `None` when the
   summary has no such tokens. This is the ticket's "does the final reply depend on the sub-agent
   summary" signal. **It is a weak, noisy observation signal, not proof of dependence** (codex
   review: markdown/boilerplate inflate it; paraphrased incorporation deflates it; instruction-style
   summaries misread) — it informs the rubric, never decides it. It is added *now* despite its
   weakness because it is computable only at assembly time (the row stores no text — PII posture):
   deferred, it is unrecoverable for every row written in the meantime (observable-first posture).
2. **`delegate_disposition_candidate(row)`** (read-time, over row scalars — works on *historical*
   rows including the baseline):
   - `None` unless `orchestration_event == "delegate_called"` — disposition refinement applies
     **only to the floor event**; `fallback_triggered` rows also carry subs but are their own
     terminal event (classifier.py §3.5) and must not get a disposition block (codex blocker);
   - `"discarded_candidate"` when `not delegate_result_passed_to_synthesis`, or
     `error_type is not None`, or `final_reply_chars == 0`;
   - `"used_candidate"` otherwise.
   Deliberately threshold-free (no invented `final_reply_chars` floor — measurement-first; the
   rubric is the authority, the candidate is triage). `error_type` as a discard lean is
   candidate-grade only: an errored turn could still synthesize from successful subs — the rubric
   (Q1–Q3) overrides. Mirrors the `_LABEL_LIE_SQL` "candidate heuristic, never verdict" posture
   (ledger.py:86).

### D3 — The rubric (the hybrid instrument, spec §3.3/§3.4)

Rendered as a fillable block in the harness md report for every row whose
`orchestration_event == "delegate_called"` (never for `fallback_triggered` — its own terminal
event):

- **Q1 — dependence:** Does the final reply contain content traceable to a sub-agent summary
  (facts, structure, file paths, citations) that is not present in the primary's own tool results
  or prior context? → `delegate_result_used`.
- **Q2 — explicit rejection:** Does the reply explicitly reject, contradict, or supersede the
  sub-agent output on review? → `delegate_result_discarded` (explicit).
- **Q3 — implicit non-use:** Is the reply an error/apology, or written without any dependence on
  the summaries (e.g. answered from the primary's own work alone)? → `delegate_result_discarded`
  (implicit — spec §3.4's hybrid case).
- **Tie-break:** partial incorporation counts as `used` (the event records that delegation
  contributed, not that it dominated).

### D4 — §5.2 single-vs-layered convention: **provisionally supported, not "validated"**

The layered alternative (`delegate_called` + `delegate_result_used`) adds no information: the
`delegate_called` fact is structurally preserved on every row by `sub_agent_count > 0` (and the
`sub_agents` JSONB), so refining the event label loses nothing. Both baseline delegate rows remain
fully interpretable under a single refined label — but two rows are too small a sample to harden a
convention (codex review). Spec §5.2 wording moves from bare `[proposed — M2 validates]` to
**"provisionally supported by `fre453-baseline-02` (2026-06-07) for the delegate-called cases
observed; continue validating as fallback / explicit-discard cases appear."** Assignment
conventions are confirmed against the eval set over time, not made canon by document edit (spec §5
preamble).

### Validation against the eval set (ticket requirement)

`fre453-baseline-02` contains exactly two `delegate_called` rows — a clean contrast pair:

| case | subs | passed_to_synthesis | error_type | final_reply | rubric verdict | candidate |
|---|---|---|---|---|---|---|
| `tool_heavy_research` | 4/4 ok | True | None | 7,403 chars, synthesizes sub discoveries | **used** (Q1) | `used_candidate` ✓ |
| `artifact_study_guide` | 3/4 ok, 1 timeout | True | `LLMServerError` (524) | 501-char error apology | **discarded** (Q3, implicit) | `discarded_candidate` ✓ |

The candidate heuristic separates both correctly. Both rows are encoded as frozen unit-test
fixtures (curated extracts — scalars only, no raw log dump) so the validation is CI-durable.
The owner's rubric pass over the baseline report confirms the verdicts (the human half of the
hybrid instrument).

---

## Steps (TDD, atomic)

1. **`reply_overlap` in the assembler** — failing tests first.
   - `tests/observability/route_trace/test_assembler.py`: sub-agent records carry `reply_overlap`;
     high containment when summary tokens appear in the reply; `0.0` for an unrelated reply;
     `None` for an empty/token-free summary.
   - `src/personal_agent/observability/route_trace/assembler.py`: `_sub_agent_records(subs,
     final_reply)` gains the field (pure; rounded to 3 decimals); call site passes
     `ctx.final_reply`.
2. **`delegate_disposition_candidate` in the route-trace module** — failing tests first.
   - `tests/observability/route_trace/test_classifier.py`: the branches in D2.2 — including
     `None` for `fallback_triggered` and `primary_handled` rows — + the two baseline-row fixtures
     (frozen `RouteTraceRow`s) asserting `used_candidate` / `discarded_candidate`.
   - `src/personal_agent/observability/route_trace/classifier.py`: add the function (documented as
     candidate-grade, never a verdict; returns `Literal["used_candidate", "discarded_candidate"]
     | None`).
3. **Harness disposition block** — failing tests first.
   - `tests/evaluation/test_fre453_canonical_evalset.py`: report for a `delegate_called` row
     contains the disposition signals table, the candidate line, and the fillable
     `- [ ] delegate_result_used` / `- [ ] delegate_result_discarded` checkboxes + rubric
     questions; absent for `primary_handled` **and** `fallback_triggered` rows.
   - `scripts/eval/fre453_canonical_evalset/harness.py`: import the candidate fn; render the block
     in `render_markdown` (non-gating; dataset expectations stay restricted to the
     classifier-emittable subset — unchanged).
4. **Docs.**
   - `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md`: §3.3/§3.4 — add the refinement mechanism (post-hoc
     rubric; candidate signals; where the threshold sits per §3.4's "M2 must decide"); §5.2 —
     "provisionally supported" wording per D4 (not "validated").
   - `scripts/eval/fre453_canonical_evalset/README.md`: rubric section (D3 questions); move
     FRE-515 from "Follow-ups" to shipped behavior; note the disposition block in the report
     description.
5. **Quality gates.**
   - `make test-file FILE=tests/observability/route_trace/test_assembler.py`
   - `make test-file FILE=tests/observability/route_trace/test_classifier.py`
   - `make test-file FILE=tests/evaluation/test_fre453_canonical_evalset.py`
   - `make test` (full) · `make mypy` · `make ruff-check` · `make ruff-format` ·
     `pre-commit run --all-files`
6. **PR** from `worktree-build2` — stop at PR (master merges/deploys/closes).

## Acceptance criteria

| Phase | Criterion | Check |
|---|---|---|
| Pre-merge | `reply_overlap` present per sub record; pure; no schema change | unit tests step 1 |
| Pre-merge | `delegate_disposition_candidate` separates the two baseline rows correctly | fixture tests step 2 |
| Pre-merge | Report renders disposition rubric block for `delegate_called` rows only (not fallback); non-gating | tests step 3 |
| Pre-merge | Spec §5.2 provisionally-supported wording + §3.3/§3.4 refinement documented; README rubric section | review |
| Pre-merge | `make test` / `mypy` / `ruff` / `pre-commit` all green | step 5 |
| Post-deploy (master) | Next eval run's report shows the disposition block with live `reply_overlap` values for delegate cases | next FRE-453 run |
| Future gate | Owner rubric pass annotates used/discarded on delegate cases; verdicts recorded on Linear (FRE-453/FRE-515) | owner pass |
| Future gate | If rubric verdicts need SQL queryability, file the write-back follow-up (annotation column + provenance) | only if needed |

## Follow-ups (file as Needs Approval if/when warranted)

- **Durable annotation write-back:** `orchestration_event_refined` + annotator provenance columns,
  fed by the completed rubric pass — only if querying refined events in SQL/ES becomes a need.
- **Harness incorporate/reject decision point:** if a future ADR makes the primary explicitly
  review sub-agent output, the explicit decision becomes the programmatic signal and write-time
  refinement becomes defensible (spec §3.4 "programmatic where the harness records an explicit
  reject/skip decision").
