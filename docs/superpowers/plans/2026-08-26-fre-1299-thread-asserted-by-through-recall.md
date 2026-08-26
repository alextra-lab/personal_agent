# FRE-1299 — Thread asserted_by through recall for Stance items

## Scope decision (read first — this is the load-bearing call this ticket requires)

The ticket's language ("thread `asserted_by`... from Neo4j through recall into the
memory-context item") is written in terms of `Claim` nodes, but `Claim` nodes are pull-only
(ADR-0126 D4: "Claims are never injected into assembled context") — they reach the agent only
via the `search_memory` tool, never via `memory_context`/`register_memory_item`, which is the
surface `SourceRegistry.register_memory_item` and this ticket's ACs are explicitly scoped to
(AC-2's own worked example, the `Event`/"Wednesday, July 1, 2026" node, is a push-recall
`memory_context` item, confirmed by the existing pinned test
`test_agent_derived_memory_cannot_ground_a_claim`). A literal "thread the Claim's field" is
therefore not buildable without violating ADR-0126 D4 by injecting Claims into push recall —
out of scope and not requested.

The buildable target: **Stance** nodes are extracted in the *same* `_finalize_extraction` pass
as Claims (same LLM call, same turn, same `user_message`/`assistant_response`), but only Claims
get co-authorship stamped (FRE-1020). Stances get a `provenance` dict with no authorship axis.
Stances *do* reach push recall — `_stance_context_items` (T1, per-entity) and
`_behavioural_stance_context_items` (T2, curated set) in `request_gateway/context.py` both
build `{"type": "stance"/"behavioural_stance", ...}` items that flow straight into
`register_memory_item`. Extending FRE-1020's existing co-authorship attribution
(`_attribute_claim_authorship`) to Stances, and threading the result through the Stance
write/read Cypher into these two item builders, is the literal shared machinery the ticket
points at ("FRE-1022's corroboration gate... may share machinery; check before building a
second one" — both rest on the same co-authorship-from-captured-text primitive).

**Entities are deliberately left untouched.** They have no equivalent grounded-text
relationship to a single turn's `user_message`, and the ticket's own regression bar (AC-2, "a
fix that makes everything citable again has removed the protection") is exactly the risk of
guessing at entity authorship. Leaving them alone means the `Event`/July-1 fixture keeps
resolving `AGENT_DERIVED` for free — proven by an explicit new regression test, not just
absence of change.

`_entitlement_of` in `grounding/source_registry.py` already implements the consuming half
correctly (`item.get("asserted_by") == "user"` → `USER_STATED`, else `AGENT_DERIVED`) — **no
change needed there**. This plan is entirely about getting real Stance items to actually carry
that key. This is the sense in which "the fix is to stop the absence, not to make absence
permissive" (ticket, quoting itself): every edit below either stamps a real value or defaults
to the existing deny-by-absence, never adds a new permissive branch.

## Acceptance criteria mapping — and an honest gap on AC-1's literal wording

**Codex plan-review (2026-08-26) confirmed the scope call but flagged that AC-1 as literally
written ("a recall item whose backing Claim carries `asserted_by`") is not satisfied by this
plan, and that the plan must say so rather than imply equivalence.** Recorded here and in the
PR/handoff, not smoothed over: a **Personal Claim** (the node type AC-1 names) still cannot
become citable by this ticket — it cannot enter push recall without violating ADR-0126 D4, and
no ADR revision is in scope here. What this ticket delivers is the same *shape* of fix
(owner-co-authorship threaded from extraction through recall into entitlement) applied to the
one push-recall node type that has a per-turn grounded-text relationship to thread it from:
**Stance**. If master's read of AC-1 requires literal Claim-citability, this ticket does not
close it and that should come back as an explicit finding, not a bounce on unstated criteria.

- **AC-1** (owner-stated fact citable) — **narrowed**: a Stance whose `affect` text is grounded
  in the user's own words gets `asserted_by="user"` end to end → `register_memory_item` →
  `USER_STATED` → `verify_turn` passes. Personal Claims remain uncitable (see above).
- **AC-2** (agent-derived still refused): unchanged — Entity items never carry `asserted_by`;
  new regression test pins this explicitly rather than relying on absence of a Stance-specific
  change.
- **AC-3** (absence still denies): a Stance written before this ticket (no `asserted_by`
  property in Neo4j) reads back `None`/absent, which the read layer canonicalizes to `"agent"`
  by exact-match (`"user" if raw == "user" else "agent"`), never a permissive value;
  `_entitlement_of` already denies on anything but `"user"`.
- **AC-4** (observable under `observe`): add `source_not_entitled_count` to `GroundingRecord`
  so the entitlement-failure share is a direct field, not something requiring per-span
  hand-aggregation over `spans[].outcome`. `SOURCE_NOT_ENTITLED` is already folded into
  `no_source_count` (it's a member of `_TRUE_NO_SOURCE`) — the new field is a **more specific
  subset**, not a new failure family; a test asserts
  `source_not_entitled_count <= no_source_count` to pin that relationship.

**Corroboration** — the ticket title says "asserted_by *and corroboration*", but "What to
build" and all four ACs are silent on it, and it names FRE-1022's corroboration/promotion gate
(ADR-0098 D6 AC-9(a)/(b): an agent-derived claim must not self-corroborate by repetition) only
as "the adjacent instrument... check before building a second one." Checked: `promote.py` and
`KnowledgeWeight.corroboration_count` exist but corroboration is genuinely unimplemented
machinery (nothing increments the counter) — a distinct, sequenceable piece of work, not
something this ticket's ACs ask for. Not building it here; noting the check was done so it
isn't silently skipped.

## Steps

1. **`memory/weight.py`** — no change (reuse existing `AssertedBy = Literal["user", "agent"]`).

2. **`memory/models.py`** — add `asserted_by: str = "agent"` to `Stance` (mirrors `Claim.asserted_by`
   exactly — plain `str`, default `"agent"`, same docstring shape). Import stays as-is
   (`weight` module already imported for `KnowledgeWeight`; no new import needed since the
   field is typed `str`, matching `Claim`'s existing convention rather than `AssertedBy`).
   → verify: `Stance(target="x", affect="y", observed_at=<dt>).asserted_by == "agent"`.

3. **`second_brain/entity_extraction.py`, `_attribute_claim_authorship`** (L657-720) — codex
   plan-review flagged that FRE-1020's thresholds (`_USER_GROUNDING_FLOOR=0.5`,
   `_USER_GROUNDING_MARGIN=0.15`) were calibrated against 94 **Claim** sentences
   (self-contained declaratives) and reusing them unguarded for **Stance** `affect` text (often
   1-3 words, e.g. "loves it") is a real false-positive risk: a single coincidental word match
   against a short phrase can clear both floor and margin by chance, laundering an
   agent-inferred stance as user-asserted — the opposite direction of "do not weaken the
   default." No production Stance corpus is available in a build session to recalibrate
   against, so the mitigation is a structural guard, not a retuned threshold: add a
   `min_terms: int = 1` parameter (default is a no-op for the existing Claim call site — any
   non-empty `terms` set already implies `len >= 1`) and require `len(terms) >= min_terms`
   before the floor/margin check runs, else fall to `"agent"` immediately. Rename the function
   to `_attribute_authorship` (it's now a two-subject classifier; update its docstring
   accordingly) and add `subject_kind: Literal["claim", "stance"] = "claim"`, used only to name
   the borderline-telemetry event (`f"{subject_kind}_authorship_borderline"` — was the
   claim-only `"claim_authorship_borderline"`, now correct for both). Update the one docstring
   reference to this function's full path in `memory/weight.py`
   (`from_claim_provenance`'s docstring) and the import in
   `tests/personal_agent/memory/test_claim_authorship.py`.

   In `_finalize_extraction` (~L866-875), after `stance.setdefault("affect", "")`, add:
   ```python
   stance["asserted_by"] = _attribute_authorship(
       str(stance.get("affect", "")),
       user_message,
       assistant_response,
       subject_kind="stance",
       min_terms=_MIN_STANCE_GROUNDING_TERMS,
       trace_id=trace_id,
       session_id=session_id,
   )
   ```
   with a new module constant `_MIN_STANCE_GROUNDING_TERMS = 2` beside the existing threshold
   constants, documenting why stances need the extra guard the claim call site doesn't. Update
   `_finalize_extraction`'s docstring (currently says "stamp provenance on stances/claims" and
   singles out "each claim's `asserted_by` co-authorship") to note both stances and claims now
   get it.
   → verify: unit tests — a stance grounded in `user_message` (≥2 grounding terms) gets
   `"user"`; one grounded only in `assistant_response` gets `"agent"`; a 1-term stance that
   would pass floor/margin on the claim path (`min_terms=1`) still resolves `"agent"` under
   `min_terms=2` (the guard actually firing, not just present); the existing claim tests in
   `test_claim_authorship.py` pass unchanged with the renamed function (proving `min_terms=1`
   is a true no-op for claims).

4. **`second_brain/consolidator.py`, `_build_stance`** (L67-94) — add
   `asserted_by="user" if data.get("asserted_by") == "user" else "agent"` computed the same way
   `_build_claim` does it (L123), passed into `Stance(...)`.
   → verify: unit test mirroring the existing `_build_claim` coverage.

5. **`memory/service.py`, `assert_stance`** (L2491-2583) — add `asserted_by: $asserted_by` to
   the `CREATE (o)-[s:HAS_STANCE {...}]` property map, and `"asserted_by": stance.asserted_by`
   to `params`.
   → verify: mocked-driver test asserting the Cypher string and params carry it (mirror
   `test_assert_claim_persists_and_reads_back_authorship` in
   `tests/personal_agent/memory/test_claims_stance_cypher.py`).

6. **`memory/service.py`, `query_current_stances`** (L2965-3039) — add
   `s.asserted_by AS asserted_by` to the `RETURN` clause, and
   `"asserted_by": "user" if row["asserted_by"] == "user" else "agent"` to the returned dict —
   exact-match canonicalization (mirrors `_build_claim`'s existing
   `"user" if data.get("asserted_by") == "user" else "agent"` pattern in `consolidator.py`
   L123), not a truthiness fallback: this is AC-3 — a pre-existing Stance with no property on
   the edge reads back `None` from Neo4j, and `None`/`""`/any off-vocabulary value must all
   canonicalize to the deny-by-default tier the same way, not merely the falsy subset an `or`
   would catch. Update the docstring's stated return shape (`target`, `affect`, `mastery`) to
   include `asserted_by`, and update `MemoryProtocol.get_current_stances`'s abstract docstring
   (`memory/protocol.py` L303+) and the adapter's docstring (`memory/protocol_adapter.py`
   L362+) the same way — codex flagged these public-contract docstrings as the ones an
   implementer actually reads, not just this method's.
   → verify: mocked-driver test asserting the Cypher RETURN and the output dict shape, covering
   all three canonicalization cases: `"user"` → `"user"`, `"agent"` → `"agent"`, absent/`None`
   → `"agent"`.

7. **`request_gateway/context.py`** — `_stance_context_items` (L194-217) and
   `_behavioural_stance_context_items` (L238-263): add
   `"asserted_by": "user" if by_target[<key>].get("asserted_by") == "user" else "agent"` to each
   constructed item dict (same exact-match canonicalization as step 6, applied again here as
   defense-in-depth rather than trusting the upstream shape). Update both docstrings' stated
   item shape.
   → verify: unit tests for both functions asserting the key is carried through, including the
   off-vocabulary/absent → `"agent"` case.

8. **`captains_log/turn_evidence.py`, `GroundingRecord`** (L920-973) — add
   `source_not_entitled_count: int = 0` field + one docstring line (pattern: "How many
   non-exempt spans failed specifically on entitlement (ADR-0138 D2, FRE-1299) — the number
   that says whether `enforce` is safe to turn on for memory citations").

9. **`grounding/verification.py`, `build_grounding_record`** (L594-638) — add
   `source_not_entitled_count=sum(1 for span in verification.spans if span.outcome is
   CheckOutcome.SOURCE_NOT_ENTITLED)`.
   → verify: extend `tests/personal_agent/orchestrator/test_executor_grounding.py`'s existing
   `no_source_count == 1` assertion (L148) with a paired `source_not_entitled_count` check on a
   case that fires D2, plus a unit test in the grounding test suite isolating just the new
   field's arithmetic (0 when nothing fires it; N when N spans hit `SOURCE_NOT_ENTITLED` mixed
   with other no-source outcomes, proving it doesn't double-count `UNCITED`/`UNRESOLVED`).

10. **Grounding end-to-end tests** (`tests/personal_agent/grounding/test_verification.py`) —
    add, alongside the existing Entity-shaped AC-2 pin:
    - AC-1: a stance-shaped item (`{"type": "stance", "target": ..., "affect": ...,
      "asserted_by": "user"}`) registers `USER_STATED` and passes `verify_turn`.
    - AC-2 regression guard: assert an entity payload built by
      `memory.proactive._split_row_payloads` never carries an `asserted_by` key (protects
      against silent scope creep onto entities later).
    - AC-3: a stance-shaped item with no `asserted_by` key (simulating a legacy edge) still
      resolves `AGENT_DERIVED`.

11. **Composed end-to-end test** (codex plan-review MUST-FIX #2 — the per-layer unit tests in
    steps 3-7 each start from an already-correct synthetic input at that layer, so nothing
    proves the layers actually compose). New test, e.g.
    `tests/personal_agent/grounding/test_stance_entitlement_e2e.py`, chaining every pure-Python
    layer with only the Neo4j driver mocked (mirrors `test_claims_stance_cypher.py`'s mocking
    style — no live Neo4j, respects test substrate isolation):
    1. Call the real `_finalize_extraction` on a raw extractor-shaped result dict with a
       `stances` array, a `user_message` that grounds the stance's `affect`, and an
       `assistant_response` that doesn't — assert `result["stances"][0]["asserted_by"] ==
       "user"`.
    2. Feed that dict through the real `_build_stance` — assert `Stance.asserted_by == "user"`.
    3. Simulate the Cypher round-trip: build the row dict `query_current_stances` would return
       for that Stance (as the mocked-driver tests in step 6 do) — assert `"user"`.
    4. Feed that row through the real `_stance_context_items` — assert the item dict carries
       `"asserted_by": "user"`.
    5. Feed that item into a real `SourceRegistry.register_memory_item` — assert
       `Entitlement.USER_STATED`.
    6. Run `verify_turn` on a turn citing it — assert `CheckOutcome.PASSED`.
    Repeat the chain once for the agent-derived case (stance grounded in `assistant_response`
    only) ending in `SOURCE_NOT_ENTITLED`, and once for the absent-property legacy case ending
    in `AGENT_DERIVED` — three full chains, not just the happy path.

## Explicitly out of scope (note in PR handoff, no new ticket unless master wants one)

- Episodes (`type: "episode"` items, carrying `user_message`/`summary`) are not touched. An
  episode's cited content can be an LLM-authored `summary`, not the literal `user_message` —
  marking episodes broadly `user_stated` would risk exactly the laundering the entitlement gate
  exists to prevent. Worth a future ticket if episode-level authorship is wanted, scoped
  narrowly to the case where the *cited* content field actually is the raw `user_message`.
- The `search_memory` tool's Claim reads (`query_claims` → `register_tool_result`, kind=TOOL)
  hardcode `Entitlement.EXTERNAL` regardless of `asserted_by`, and `query_claims`'s own
  `RETURN` doesn't even select `cl.asserted_by`. This is a real, separate gap (Claims pulled
  this turn get *more* trust than an unlabelled Stance, not less) but is a different registry
  code path (`register_tool_result`, not `register_memory_item`) than what this ticket's ACs
  and "What to build" name, and it's schema-adjacent (query changes) and policy-adjacent
  (should `search_memory` even be `SourceKind.TOOL` rather than `SourceKind.MEMORY`?), which
  reads as its own decision rather than a fold-in. Codex plan-review flagged the earlier
  "no new ticket unless master wants one" framing as too soft for an entitlement bypass — file
  a Linear issue (`Needs Approval`, `PersonalAgent` label, referencing FRE-1299) at the end of
  this build rather than leaving it as a PR-body mention only.

## Quality gates

`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.
Diff class: touches production write path (Neo4j Cypher writes/reads in the memory substrate)
→ escalate per Step 6 (self-review + note "flagged for owner `/code-review ultra` before
merge" in the PR).
