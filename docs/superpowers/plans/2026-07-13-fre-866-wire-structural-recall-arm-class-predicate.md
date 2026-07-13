# FRE-866 — Wire the ADR-0104 structural recall arm into fusion + class predicate

**Ticket:** FRE-866 (Approved, Tier-2:Sonnet, `stream:build2`)
**Backing ADR:** ADR-0115 §D6 / Implementation Notes step 4 (this ticket IS the filed
follow-up); consumer: ADR-0104 (Proposed) — the multi-path fusion architecture.
**Gate:** unblocked — FRE-864 (Entity class persistence) and FRE-865 (backfill) are both
Done; the class axis is live on the Entity node (`e.class ∈ {World, Personal}`, indexed).

## Scope (two pieces, per the ticket)

1. **Wire the structural recall arm into the fusion loop.** `structural_recall_arm`
   exists (`memory/service.py:3073`, FRE-707) but is never called by
   `_multipath_fused_recall` (`memory/service.py:3356`) — only `multi_query`/`dense` and
   `lexical` are assembled there today. Its master flag (`structural_arm_enabled`)
   already defaults off, so wiring it in is a **pure addition**: with the flag off
   (today's default), behavior is unchanged.
2. **Decide whether/how to add a class predicate to that arm.** ADR-0115 D6 explicitly
   left this unowned ("genuinely unowned future work... this ADR *enables* it... and
   files a follow-up ticket"). Decision (this plan, for owner sign-off): add a **safe,
   flag-gated class predicate**, structurally identical to the existing safe
   `entity_type` predicate (AC-4b: narrows to requested values but never drops a row
   whose `class` is `NULL`) — extending an already-accepted pattern to a newly-available
   closed axis, not inventing new ranking policy. **Not in scope:** any caller-side
   policy that decides *which* class to bias toward (e.g. "study thread → bias World") —
   there is no session/thread-topic signal to source that from yet, and ADR-0115 D6
   explicitly says this ADR does not build class-aware *ranking*. This ticket builds the
   predicate mechanism only, off by default, unconnected to any auto-invocation.

## Why the structural arm needs new plumbing, not just a flag flip

The other two optional arms (`lexical_recall_arm`, `multi_query_recall_arm`) share the
fusion core's contract: `(query_text, **arm_kwargs) -> list[RankedResult]`, with
`item_id = elementId(node)` for entities (so `_resolve_item_texts` /
`_multipath_broad_entities` / `_resolve_fused_turns` — all of which `MATCH (e:Entity)
WHERE elementId(e) = eid` — resolve them). `structural_recall_arm` instead returns
`list[EntityNode]` with `entity_id = e.name` (used only by its existing integration
tests) and takes structural params (`entity_types`, `recency_days`, `anchor_names`), not
`query_text`. Reusing it as-is inside the fusion loop would silently break identity
resolution (name ≠ elementId) for every downstream consumer. So this plan adds a
**second, ranked-result-returning method** rather than changing the public
`structural_recall_arm` contract the existing FRE-707 tests depend on — but (per codex
review) the two methods share one **private execution helper** (gate check, visibility
fragments, query-build, session-run, exception handling → raw records), not
copy-pasted plumbing; `structural_recall_arm` and `structural_recall_arm_ranked` become
thin wrappers that map those raw records to `EntityNode` and `RankedResult` respectively.

## Files touched

- `src/personal_agent/config/settings.py` — one new flag.
- `src/personal_agent/memory/service.py` — `_build_structural_arm_query`,
  `structural_recall_arm`, new `structural_recall_arm_ranked`, `_multipath_fused_recall`.
- `tests/test_memory/test_structural_arm_query.py` — class-predicate unit tests + item_id
  assertion (pure function, no substrate).
- `tests/test_memory/test_structural_arm.py` — one integration test for the class
  predicate against the test substrate.
- `tests/personal_agent/memory/test_multipath_core.py` — arm-assembly test proving the
  structural arm is included/excluded by its flag (parity with the existing
  dense/lexical coverage).

## Steps

1. **`config/settings.py`** — add, next to `structural_type_predicate_enabled`
   (~line 630):
   ```python
   structural_class_predicate_enabled: bool = Field(
       default=False,
       description=(
           "ADR-0115 D6 / FRE-866: gates the entity-class (World/Personal) "
           "sub-predicate of the structural arm, now that Entity.class is "
           "persisted (FRE-864) and backfilled (FRE-865). Off by default. When "
           "on, the class predicate is SAFE by construction — it narrows to the "
           "requested class(es) but never drops rows whose class is NULL, so an "
           "unclassified entity is never silently lost."
       ),
   )
   ```

2. **`memory/service.py::_build_structural_arm_query`** — add params
   `entity_classes: Sequence[str] | None = None` and
   `class_predicate_enabled: bool = False`; mirror the type-predicate block, but
   preserve only `NULL` (not `''`/`'Unknown'`): unlike `entity_type` — which predates
   FRE-637 and has historical `''`/`'Unknown'` placeholder values actually written to
   the graph — `class` is a new field (ADR-0115 D2/FRE-864) whose only two writers are
   the classifier (`World`/`Personal`, fail-open per D4 — never `''`/`'Unknown'`) and
   "never set" (`NULL`, for pre-ADR-0115 entities and non-extraction callers like
   `store_fact`). There is no third placeholder value to preserve, so the asymmetry
   with the type predicate is deliberate, not an oversight:
   ```python
   if class_predicate_enabled and entity_classes:
       e_where.append("(e.class IN $entity_classes OR e.class IS NULL)")
       params["entity_classes"] = list(entity_classes)
   ```
   Add `elementId(e) AS item_id` alongside `e AS e` in **both** RETURN branches (plain
   scan and anchor-traversal) so callers can build `RankedResult` without re-querying.
   Update the docstring's Args/Returns.

3. **Shared private execution helper** — extract the current body of
   `structural_recall_arm` (gate check, visibility fragments, `_build_structural_arm_query`
   call, `session.run`/exception handling) into a private helper, e.g.
   `_run_structural_arm_query(...) -> list[dict[str, Any]] | None` (returns the raw
   records, or `None` when gated off/disconnected — distinguishing "arm didn't run"
   from "arm ran and found nothing" for the two callers below to interpret as they
   need). Takes the same params as today's `structural_recall_arm` plus the new
   `entity_classes`.

4. **`structural_recall_arm`** — becomes a thin wrapper: calls the shared helper, maps
   `None`/empty to `[]`, maps records via `_entity_node_from_record(r["e"])`. Add
   `entity_classes: Sequence[str] | None = None` param, threaded through. Default
   `None` preserves every existing test's behavior unchanged (return type, log event,
   and error handling all unchanged).

5. **New `structural_recall_arm_ranked`** — the second thin wrapper: calls the same
   shared helper, maps records to
   `RankedResult(item_id=r["item_id"], rank=i+1, kind="entity")`. Logs
   `structural_recall_arm_completed` with `arm="structural"` (parity with the other
   arms' completion logs; the existing `structural_recall_arm` log call moves into
   the shared helper so both wrappers emit once, not twice).

6. **`_multipath_fused_recall`** — after the existing lexical block, add:
   ```python
   if current_settings.structural_arm_enabled:
       arm_names.append("structural")
       arm_coros.append(self.structural_recall_arm_ranked(**arm_kwargs))
   ```
   No `entity_types`/`recency_days`/`anchor_names`/`entity_classes` passed — the shared
   core has no source for these from a raw `query_text`, so this is the plain
   closed-axis scan (recency-ordered, visibility-scoped) the arm already supports
   without predicates. This is the full extent of "wiring in": the arm becomes a live,
   flag-gated fusion participant; it does not yet receive a caller-supplied predicate
   (future work, if a caller wants to pass one — no code path does today).

7. **Docstrings** — `structural_recall_arm`'s "flag-dark until the multi-path fusion
   core (FRE-722/724) wires it in" line becomes stale once wired; update to state it is
   wired into `_multipath_fused_recall`, still gated by `structural_arm_enabled`
   (default off, so no live behavior changes until explicitly enabled).

8. **Tests — `test_structural_arm_query.py`** (pure, no substrate):
   - `test_class_predicate_keeps_unclassified_rows` — enabled + `entity_classes=["World"]`
     → `"e.class IN $entity_classes"` and `"e.class IS NULL"` both in the Cypher.
   - `test_class_predicate_absent_when_disabled` — enabled flag False → no `e.class` clause.
   - `test_class_predicate_absent_without_classes` — enabled but `entity_classes=None` →
     no clause.
   - `test_class_predicate_absent_with_empty_list` — enabled but `entity_classes=[]` → no
     clause (codex finding: the `and entity_classes` guard treats `None` and `[]`
     identically; both must be proven, not just `None`).
   - `test_item_id_present_in_both_branches` — `elementId(e) AS item_id` in the plain-scan
     and the anchor-traversal Cypher.

9. **Tests — `test_structural_arm.py`** (integration, test Neo4j):
   - `test_class_scoped_recall_keeps_unclassified_entities` — seed one `World`, one
     `Personal`, one unclassified (`class` unset) entity; with
     `structural_class_predicate_enabled=True` and `entity_classes=["World"]`, assert the
     World and unclassified entities both return, Personal does not (mirrors
     `test_type_scoped_recall_keeps_unenforced_entities`).

10. **Tests — `test_multipath_core.py`** (codex-expanded coverage):
    - Extend `_enable()` with a `structural: bool = False` kwarg
      (`monkeypatch.setattr(s, "structural_arm_enabled", structural, raising=False)`).
    - `test_structural_arm_included_when_enabled` — flag on, mock
      `service.structural_recall_arm_ranked`, assert `"structural" in
      result.arms_executed` and its candidates appear in the fused items, with
      correct `item_id`/`kind="entity"` propagated through unchanged (codex finding:
      prove ranked-wrapper identity, not just presence).
    - `test_structural_arm_excluded_when_disabled` — flag off (today's default), assert
      `"structural" not in result.arms_executed` — the no-behavior-change proof for the
      default state.
    - `test_structural_plus_dense_meets_ac1_floor` — structural on, multiquery/lexical
      off: assert `len(result.arms_executed) >= 2` (dense + structural) — codex finding:
      prove ADR-0104 AC-1's "≥2 independent arms" floor holds with structural as one of
      only two arms, not just that structural is present alongside others.
    - `test_structural_lexical_agreement_ranks_first` — an item surfaced by both
      structural and lexical outranks one surfaced by only one arm (mirrors the
      existing `test_two_arms_run_and_fuse_by_rank` pattern at
      `test_multipath_core.py:43`, substituting structural for multi-query) — codex
      finding: prove RRF agreement specifically involving the new arm, not just
      dense+lexical.
    - `test_structural_arm_exception_recorded_not_raised` — mocked
      `structural_recall_arm_ranked` raises → `"structural"` lands in `arms_failed`,
      `per_arm_counts["structural"] == 0`, other arms' results still returned (mirrors
      `test_arm_exception_recorded_not_raised` at `test_multipath_core.py:83`) — codex
      finding: prove failure isolation for this arm specifically.

11. **Quality gates** — `make test-file FILE=tests/test_memory/test_structural_arm_query.py`,
    `make test-file FILE=tests/test_memory/test_structural_arm.py` (needs
    `make test-infra-up`), `make test-file FILE=tests/personal_agent/memory/test_multipath_core.py`,
    then `make test` (full), `make mypy`, `make ruff-check`, `make ruff-format`,
    `pre-commit run --all-files`.

## Acceptance criteria (ADR-0115 D6 follow-up — the testable outcomes)

- **AC-A — the arm is no longer flag-dark relative to fusion.** With
  `structural_arm_enabled=True`, `_multipath_fused_recall` executes the structural arm
  and its candidates enter RRF fusion (parity with the lexical/multiquery wiring
  proof pattern already in `test_multipath_core.py`). *Evidence:* new
  `test_structural_arm_included_when_enabled`.
- **AC-B — default behavior is unchanged.** With `structural_arm_enabled=False` (the
  shipped default, unchanged by this PR), the arm contributes nothing — same as before
  this ticket. *Evidence:* new `test_structural_arm_excluded_when_disabled`; all
  pre-existing structural-arm tests keep passing unmodified.
- **AC-C — a class predicate exists on the arm, safe by construction.** Enabling
  `structural_class_predicate_enabled` with `entity_classes=["World"]` narrows results to
  World + unclassified entities, never dropping a `class IS NULL` row (parity with
  AC-4b's type-predicate discipline; ADR-0115 D4 fail-open posture). *Evidence:* new
  `test_class_scoped_recall_keeps_unclassified_entities` (integration) +
  `test_class_predicate_keeps_unclassified_rows` (unit).
- **AC-D — identity parity with the other arms.** The structural arm's fusion
  contribution uses `elementId(e)` as `item_id` (not `e.name`), so
  `_resolve_item_texts`/`_multipath_broad_entities`/`_resolve_fused_turns` resolve it
  without special-casing. *Evidence:* `test_item_id_present_in_both_branches` +
  `test_structural_arm_included_when_enabled` asserting resolvable fused items.
- **AC-E — ADR-0104 AC-1's arm-count floor and RRF agreement hold with structural as a
  participant, and its failures isolate like every other arm.** *Evidence:*
  `test_structural_plus_dense_meets_ac1_floor`,
  `test_structural_lexical_agreement_ranks_first`,
  `test_structural_arm_exception_recorded_not_raised` (all codex-review additions).

## Explicitly out of scope (flagged, not silently dropped)

- No caller passes `entity_classes`/`entity_types`/`recency_days`/`anchor_names` into
  the structural arm's fusion-core invocation — it runs as a plain closed-axis scan.
  Wiring a real predicate value from a caller (e.g. a "study thread" class bias) is a
  separate, unowned design decision ADR-0115 D6 deliberately did not make and this
  ticket does not make either — it only builds the mechanism.
- `structural_arm_enabled` and `structural_class_predicate_enabled` both ship default
  off — enabling either in production is a follow-up rollout decision (FRE-433
  flag→verified→rollout discipline), not part of this ticket.
- ADR-0104 remains Proposed; this ticket does not change its status (that ADR's own
  seam — AC-1…AC-6 live — is a separate, larger, master-gated claim).
