# FRE-865: Backfill — re-classify the ~7,992 existing class=None entities

**Ticket:** FRE-865 (Approved, Tier-2:Sonnet, stream:build2)
**Backing ADR:** ADR-0115 §"Implementation Notes" step 5 + Risks table row "Existing ~7,992
`class=None` entities stay unclassified"
**Depends on (merged):** FRE-863 (two-axis emission contract), FRE-864 (Entity class persistence
write — `create_entity`, `entity.knowledge_class`, `ensure_entity_class_index`)
**Explicitly NOT in scope:** running this against prod Neo4j (post-deploy ops action, batched with
the ADR-0115 seam deploy). **FRE-728 merged during this build** (D3 write-time dispatch —
`consolidator.py` routes each NEW extraction's entities/relationships by `output_kind` before any
Core write: `ephemeral` → no write, `finding` → a `sysgraph.stat` row, never Core). Confirmed by
reading its diff: FRE-728 is entirely a write-time gate on new consolidation — it has no sweep over
already-existing Core nodes. So it does **not** consume this backfill's
`class_backfill_output_kind` marker on pre-existing entities; a genuinely separate, not-yet-filed
follow-up ticket owns actually moving/deleting the entities this backfill marks.

## Scope (from ticket + ADR)

- Build a script that re-runs classification over the existing corpus of `:Entity` nodes carrying
  `class IS NULL`.
- Sets `class` to `World` or `Personal` for knowledge-natured entities; fails open to `World` on
  classifier uncertainty (never drops a candidate — mirrors FRE-637/D4).
- System-natured entities are **routed out via `output_kind`, not classed** — i.e. never given a
  `World`/`Personal` value; marked instead so a genuinely separate follow-up ticket (FRE-728 turned
  out to be write-time-only, per the note above — it doesn't own this) can find and act on them.
  `output_kind` is not persisted anywhere on `:Entity` today (grepped — zero hits), so this ticket
  introduces the marker property that records it.
- Reports before/after counts: how many moved no-class → World, no-class → Personal, and how many
  were routed out as System-natured (ephemeral/finding).
- Proven on the **test substrate only** (Neo4j :7688) with a fixture corpus — no prod runs, no real
  LLM cost beyond what a developer chooses to spend testing against the local test stack.

## Design decisions (flagging for review — not spelled out verbatim in the ADR)

**Revised after a codex plan-review pass (2026-07-12) — see "Codex findings addressed" below for
what changed and why.**

1. **No full-turn re-extraction.** The extractor's classifier (`entity_extraction.py`) operates on
   full conversation turns, but backfill targets are already-materialized `:Entity` nodes with no
   turn re-fetch guaranteed cheap/available at scale. Instead: a **standalone, purpose-built batch
   classifier** takes `(name, entity_type, description)` per entity — the same shape FRE-772's
   Concept-*type* classifier uses, but this ticket's classification is a materially harder decision
   (ownership/routing, not just a type label) and the ADR's own recovery story for a bad call leans
   on the **raw turn already being in ES** — a channel this standalone classifier does not consult.
   **This backfill is explicitly lower-fidelity than emission-time classification** and is expected
   to skew `World` more than a full-context classification would. That tradeoff is accepted here
   (test-substrate proof only; the prod run is a separate, later, master-gated ops action that can
   revisit fidelity) but must be stated plainly in the report and the PR/ticket comment, not
   glossed over.
2. **Script pattern: mirror `scripts/migrate_fre772_entity_type_v2.py`** almost exactly — it is the
   closest existing analog (batched LLM reclassification of existing Entity nodes, `GraphProtocol`
   seam, `CostGate` registration, prod-write guard, dry-run, structured report). New file:
   `scripts/migrate_fre865_entity_class_backfill.py`. Unlike FRE-772, the report also carries
   `prompt_version` and `classifier_model` (FRE-772 has these; the original FRE-865 draft omitted
   them — added back, since a subjective-classification backfill needs this provenance at least as
   much as a type migration does).
3. **Idempotency / no infinite re-billing:** candidates are `WHERE e.class IS NULL AND
   e.class_backfill_output_kind IS NULL` — a routed-out node carries the marker so it is never
   re-fetched (and never re-billed) on a later run. A classified node's `class` is no longer null so
   it's naturally excluded too. **Known, accepted gap:** a node that is classified (billed) but whose
   terminal write is lost to a mid-run crash still matches the candidate predicate and will be
   re-classified (re-billed) on the next run. No resumable-billing ledger is built for this — the
   volume/cost at test-substrate scale does not justify it, and it is documented here + in the
   script docstring as a known limitation rather than engineered around.
4. **Fail-open, not fail-closed** (the opposite of FRE-772's Concept classifier): every candidate
   this run resolves to *some* outcome — real classification, or a fail-open default
   (`output_kind=knowledge, class=World`) on any parse/format/exception failure, matching D4. **Safety
   valve added post-review:** `run_backfill` tracks `fail_open_count / total_candidates_this_run`
   and the whole-batch-exception count; if either ratio exceeds `--fail-open-threshold` (default
   0.5) across a run with at least `--fail-open-min-sample` (default 20) candidates, the run
   completes (never silently drops a candidate — D4 still holds) but `report.success = False` and
   `main()` exits non-zero with a printed warning — surfacing "the classifier path looks broken,
   investigate before running this again / before trusting this run's World labels" rather than
   quietly mass-labeling the corpus World on an outage. This is a **report-and-flag** gate, not an
   abort-mid-run gate — D4's "never drop a candidate" wins over "stop early," but the operator is
   loudly told the run's classification quality is suspect.
5. **New provenance properties on `:Entity`** (none existing today):
   - `class_backfill_run_id`, `class_backfill_at` — stamped on every node this backfill touches
     (classified OR marked), for audit trail distinguishing backfill writes from live-extraction
     writes.
   - `class_backfill_output_kind` — set only on marked (System-natured) nodes to `"ephemeral"` or
     `"finding"`; `class` is left untouched (still `NULL`).
   - `class_backfill_fail_open` — `true` on nodes where the classifier response was unusable and the
     D4 default was applied (subset of classified nodes).
6. **"Marked for later dispatch," not "routed out"** (renamed after review — the ADR's D3 invariant
   is *physical absence from Core*, which this ticket cannot deliver). FRE-728 (D3, merged during
   this build) turned out to be write-time-only — it gates NEW extractions before they reach Core,
   with no sweep over already-existing nodes — so it does not consume this backfill's marker either.
   This backfill only **identifies** System-natured existing entities via
   `class_backfill_output_kind` so a genuinely separate, not-yet-filed follow-up ticket can act on
   them. **This ticket does NOT satisfy ADR-0115 AC-5 on its own** — AC-5 is the assembled-seam
   criterion (persistence + dispatch + consolidation together) that master asserts at the
   integration gate across FRE-863/864/728/865, not a per-ticket claim. The PR/ticket comment must
   state this explicitly: marked nodes still have `class IS NULL` and still physically reside in
   Core after this ticket ships, and no existing consumer reads `class_backfill_output_kind` yet.
7. **Rollback is run-id-based, not snapshot-file-based** (simpler than FRE-772's file snapshot):
   since every touched node started from `class IS NULL`, rollback = `REMOVE
   e.class, e.class_backfill_*` `WHERE e.class_backfill_run_id = $run_id`. **Concurrency limitation,
   made explicit post-review:** this is safe only when no other writer touched the node between the
   backfill run and the rollback. The real Cypher rollback guards this with `WHERE
   e.class_backfill_run_id = $run_id AND e.last_seen <= $backfilled_at` (skip — don't clobber — any
   node whose `last_seen` moved past the backfill's own write, i.e. live traffic touched it since);
   skipped nodes are reported by name, not silently dropped. This is intentionally a lighter-weight
   guard than FRE-772's full snapshot-file rollback, acceptable because **this ticket never runs
   against prod** — a prod rollback story, if ever needed, is designed when the prod run is planned,
   not here.

## Files

- **New:** `scripts/migrate_fre865_entity_class_backfill.py` — the script (GraphProtocol seam +
  `_Neo4jGraph` real impl + batch classifier + `run_backfill`/`run_rollback` orchestration + CLI).
- **New:** `tests/scripts/test_migrate_fre865_entity_class_backfill.py` — unit tests, in-memory
  `FakeGraph` + deterministic fake batch classifier, no Neo4j/LLM (CI-gating).
- **New:** `tests/scripts/test_migrate_fre865_entity_class_backfill_integration.py` — integration
  test against real test-substrate Neo4j (`pytest.mark.integration`, `make test-infra-up`), seeded
  fixture corpus (Personal / World / System-natured / deliberately-ambiguous-for-fail-open),
  proving the real Cypher end to end + idempotent re-run + rollback.

## Codex findings addressed (plan-review, 2026-07-12)

A codex plan-review pass on the first draft of this plan (before any code was written) confirmed 5
issues + 3 additional risks. Disposition:

1. **Fail-open could silently mislabel everything World on a classifier outage (high).** →
   design decision 4: added `--fail-open-threshold`/`--fail-open-min-sample`, `report.success=False`
   + non-zero exit + printed warning when exceeded. D4's "never drop a candidate" is preserved (the
   run still completes and every candidate gets an outcome); what's added is *loud* observability
   that the run's quality is suspect, not a fail-closed abort.
2. **"Routed out" implies ADR-0115 D3's physical-absence invariant, which this ticket can't deliver
   without FRE-728 (medium).** → renamed to "marked for later dispatch" throughout (decision 6);
   PR/ticket comment must state this ticket does not close ADR-0115 AC-5 alone.
3. **Run-id rollback has no guard against concurrent live-traffic mutation between backfill and
   rollback (high).** → decision 7: rollback Cypher now guards on `e.last_seen <= $backfilled_at`
   and reports (not silently skips) any node it declines to restore. Explicitly scoped: this
   guard is sufficient because the ticket never touches prod; a prod rollback design is deferred to
   when a prod run is actually planned.
4. **A node classified-but-not-yet-written when the process crashes will be re-billed on retry
   (low).** → decision 3: documented as a known, accepted limitation (no resumable-billing ledger)
   given test-substrate-only scale; each node's terminal write is still a single atomic Cypher
   statement so no *partial* node state is possible, only "not yet written."
5. **A standalone (name, entity_type, description) classifier is meaningfully weaker than the
   emission-time (full-turn-context) classifier, and the FRE-772 analogy undersells that gap
   (high).** → decision 1 rewritten to say this plainly: lower-fidelity, skews World, accepted only
   because this is a test-substrate proof and the prod run is a separate future decision.
   Additional risk: report now carries `prompt_version` + `classifier_model` (decision 2), matching
   FRE-772's provenance fields, which the first draft had dropped.
6. **Parser test coverage gap vs. FRE-772's dedicated tests for missing/duplicate/ambiguous/
   off-vocabulary/whole-batch-malformed output (medium).** → added explicitly to the unit-test list
   in Atomic step 1 below (was previously only implied by "batching" tests).

## Atomic steps

1. Write failing unit tests in `tests/scripts/test_migrate_fre865_entity_class_backfill.py` against
   the not-yet-existing module:
   - classify happy paths: World, Personal, System-natured → marked (not classed), fail-open on an
     unparseable response;
   - parser anomaly tests mirroring FRE-772's: missing index, duplicate index (ambiguous both
     lines), off-vocabulary output_kind/class, whole-batch unnumbered output — each must resolve to
     the D4 fail-open outcome (`knowledge`/`World`), never left unresolved;
   - idempotent re-run excludes already-classified AND already-marked nodes (zero writes, zero model
     calls on the second pass);
   - dry-run writes nothing but still previews counts;
   - batching reduces call count (batch_count < total candidates);
   - report aggregates cost/tokens/`prompt_version`/`classifier_model` across batches;
   - fail-open safety valve: a run whose fail-open ratio exceeds `--fail-open-threshold` (above
     `--fail-open-min-sample`) sets `report.success=False` even though every candidate got an
     outcome; below the sample floor, a 100% fail-open run still succeeds (small samples don't
     trigger the valve);
   - rollback by run_id restores `class`+markers to null, and *skips* (reporting by name) a node
     whose `last_seen` moved past the backfill's own `class_backfill_at` (simulated concurrent
     mutation).
   → verify: `uv run pytest tests/scripts/test_migrate_fre865_entity_class_backfill.py -x` fails
   with `ModuleNotFoundError`.
2. Implement `scripts/migrate_fre865_entity_class_backfill.py`:
   - `EntityCandidate` (element_id, name, entity_type, description) dataclass.
   - `ClassifyResult` (output_kind, knowledge_class, fail_open, reason), `BatchClassifyResult`
     (results, cost_usd, input_tokens, output_tokens, cached_tokens) dataclasses.
   - `GraphProtocol` (count_by_class, count_unclassified, fetch_candidates, set_class, mark_for_dispatch,
     restore_by_run_id) + real `_Neo4jGraph` Cypher implementation. `restore_by_run_id` guards on
     `e.last_seen <= $backfilled_at` and returns both a restored-count and a skipped-name list.
   - Cache-stable `_CLASSIFIER_PREFIX` (output_kind + P/W class definitions, condensed from
     `entity_extraction.py`'s prompt) + `_render_batch`/`_build_batch_prompt`.
   - `_parse_batch_classification`: strict numbered `"<n>. <output_kind>[|<class>]"` parsing;
     any anomaly (missing/duplicate/ambiguous/off-vocabulary/whole-batch-unparseable) →
     `ClassifyResult(output_kind="knowledge", knowledge_class="World", fail_open=True,
     reason=<tag>)` — D4 fail-open, never left unresolved.
   - `_build_llm_batch_classifier()`: resolves `entity_extraction` role, `SystemTraceContext.new
     ("entity_class_backfill")`; any call exception → whole-batch fail-open result (never raises).
   - `run_backfill(...)`: pages `fetch_candidates` by cursor, chunks into `classify_batch_size`
     groups, concurrent classify (semaphore), writes `set_class`/`mark_for_dispatch` per result
     (skipped entirely when `dry_run`), aggregates a `BackfillReport` (before/after class
     histograms, counts of classified-World/classified-Personal/marked-by-kind/fail-open, model
     calls, batches, cost, tokens, `prompt_version`, `classifier_model`, remaining-unclassified
     count for multi-run progress). After the loop, computes the fail-open ratio and sets
     `report.success = False` (with a printed warning) if it exceeds `--fail-open-threshold` on a
     sample of at least `--fail-open-min-sample`.
   - `run_rollback(graph, run_id, batch_size)` → `(restored_count, skipped_names)`.
   - CLI: `--confirm-prod`, `--dry-run`, `--rollback --run-id <id>`, `--batch-size`,
     `--classify-batch-size`, `--fail-open-threshold` (default 0.5), `--fail-open-min-sample`
     (default 20), `--report-path`; reuse the `_setup_cost_gate` pattern (register `CostGate`
     before building the classifier — FRE-800 regression class); prod-write guard identical to
     FRE-772 (`AGENT_ENVIRONMENT != test` requires `--confirm-prod`).
   → verify: unit tests from step 1 pass: `uv run pytest
   tests/scripts/test_migrate_fre865_entity_class_backfill.py -v`.
3. Write the integration test (`make test-infra-up` first), seeding a fixture corpus of
   uniquely-prefixed `:Entity` nodes with `class IS NULL`: a Personal fixture, a World fixture, a
   System-natured fixture (e.g. an infra/healthcheck-flavored name+description), and a fixture whose
   classifier response is deliberately made unparseable (to exercise fail-open). Assert: Personal →
   `class=Personal`; World → `class=World`; System-natured → `class` still `NULL`,
   `class_backfill_output_kind` set (marked, not classed); fail-open fixture → `class=World`,
   `class_backfill_fail_open=true`; a second run makes zero additional writes/model calls (all
   excluded by the candidate predicate); rollback by `run_id` clears `class` + all
   `class_backfill_*` markers back to the pre-run state; a variant where one restored node's
   `last_seen` is bumped past `class_backfill_at` (simulating concurrent live traffic) is *skipped*
   by rollback and reported by name, not silently reverted. Clean up seeded nodes in a `finally`
   block.
   → verify: `AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 uv run pytest -m integration
   tests/scripts/test_migrate_fre865_entity_class_backfill_integration.py -v` (test substrate
   already redirects to :7688 per `tests/conftest.py`; the env var is *not* needed for the isolated
   test stack — confirm at implementation time whether `pytest.mark.integration` alone suffices, as
   it does for `test_migrate_fre772_integration.py`).
4. Quality gates: `make test` (module: `make test-file
   FILE=tests/scripts/test_migrate_fre865_entity_class_backfill.py`, then full `make test`) ·
   `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
5. Self-review: `code-review` skill at `low`-to-`medium` effort (new script + tests, no existing
   `src/` behavior changed, but touches memory/cost — err toward `medium`); `security-review` since
   the script takes Cypher params and makes outbound LLM calls (check for injection/param safety,
   already parameterized via Cypher `$params` — should be clean, but verify).
6. PR + Linear comment per skill Step 9.

## Report shape (for the ticket's "report counts before/after" requirement)

`BackfillReport`: `run_id`, `dry_run`, `prompt_version`, `classifier_model`, `started_at`,
`finished_at`, `before_class_histogram` (`{"World": n, "Personal": n, "(unset)": n}`),
`after_class_histogram` (same shape post-run), `classified_world`, `classified_personal`,
`marked_for_dispatch` (`{"ephemeral": n, "finding": n}`), `fail_open_count`,
`total_candidates_this_run`, `remaining_unclassified` (post-run count, for tracking progress across
the multiple runs a full 7,992-entity prod pass will need), `model_calls`, `batch_count`,
`cost_usd`, `input_tokens`, `output_tokens`, `cached_tokens`, `success` (False when the fail-open
safety valve trips — see design decision 4).

## Post-implementation code review (workflow-backed, high effort) — 4 confirmed findings, all fixed

A background code-review workflow (`code-review` skill, high effort) found 4 confirmed correctness
defects after implementation, all fixed on this branch before PR:

1. **Rollback silently no-op'd for entities whose `last_seen` is a plain ISO string** (the
   Turn-DISCUSSES-Entity mention path, `memory/service.py:1060`) rather than a native Neo4j
   `datetime()` (the create/access path, `service.py:1341`) — comparing the two directly evaluated
   to Cypher `null`, satisfying neither the restore nor the skipped branch, so such nodes were
   silently left un-rolled-back with no error and no skip-report entry. **Fixed** by wrapping both
   sides in `toString(...)`, mirroring the codebase's own established coercion
   (`service.py:285/300/308`). Locked in with a new integration test that sets `last_seen` as a
   literal string and asserts the node IS restored (this test was verified to fail without the fix).
2. **Classification spend billed to `main_inference` instead of `entity_extraction`** —
   `resolve_role_model_key("entity_extraction")` returns a resolved MODEL KEY (`"gpt-5.4-mini"` per
   `config/model_roles.yaml`), not the role name; passing that key as `get_llm_client(role_name=...)`
   makes the factory's `budget_role_for()` lookup miss and default to `main_inference`. **This is a
   pre-existing, currently-live bug shared by `entity_extraction.py`'s own production call
   (`entity_extraction.py:750`), `consolidator.py`, and `migrate_fre772_entity_type_v2.py`** — not
   introduced by this ticket, and out of this ticket's scope to fix repo-wide (would touch 3
   unrelated call sites and deserves its own ticket). **Fixed locally** in this script only, by
   bypassing the factory and constructing `LiteLLMClient` directly with an explicit
   `budget_role="entity_extraction"`, mirroring `entity_extraction.py`'s own eval-override branch
   which already does exactly this for the same reason. **Flagged as a separate follow-up** — see
   below.
3. **`report.success` was unconditionally `False` for every `--dry-run`**, defeating the fail-open
   safety valve's whole purpose (letting an operator preview classifier health before spending
   money) — a healthy and an unhealthy dry-run preview printed identically. **Fixed**: success now
   reflects only the fail-open valve, independent of `dry_run`; `_print_summary`'s WARNING and
   `_amain`'s exit code follow suit.
4. **A pluggable `BatchClassifier` returning an off-enum `output_kind`/`knowledge_class`** (nothing
   currently enforces `_parse_one_line`'s validation outside the LLM-backed classifier) would have
   been written to `e.class` verbatim, potentially violating the `{World, Personal}` invariant.
   **Fixed**: `run_backfill` now validates both fields itself before branching/writing, falling open
   to D4's default (never double-counting `fail_open_count` when multiple issues stack on one
   candidate).

**Follow-up to flag at PR/ticket comment**: finding 2's root cause (`get_llm_client(role_name=
<resolved model key>)` mis-billing to `main_inference`) is real and currently live in
`entity_extraction.py`'s production entity-extraction path — the `entity_extraction` daily budget
cap ($2.50/day per ADR-0065) is effectively never being charged against, while `main_inference`
silently absorbs that cost. This is a distinct, more consequential defect than anything in this
ticket's own scope; recommend a new Needs-Approval ticket for the cost_gate/config owner to
investigate and fix `budget_role_for`/`get_llm_client` role-vs-model-key semantics across all
affected call sites.

## Explicitly out of scope for this ticket

- Running against prod Neo4j (cloud-sim-*) — post-deploy ops action, gated behind the ADR-0115 seam
  deploy, master-authorized.
- Building a consumer that actually *moves*/deletes the marked (System-natured) entities out of
  Core — this script only *marks* them via `class_backfill_output_kind`. FRE-728 (merged during
  this build) does not own this either, since it only gates new writes, not existing nodes; a
  separate, not-yet-filed follow-up ticket owns physically relocating/deleting them.
- Any change to `entity_extraction.py`, `consolidator.py`, or `create_entity` — those are FRE-863/
  864/728's completed seam; this ticket only reads existing Entity nodes and writes the
  backfill-specific properties.
